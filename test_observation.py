from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from observation import CaptureStore, ObservationAdapter, ObservationError, Rect
from vision_reader import ReaderResult


class FakeEngine:
    def __init__(self, image_path: Path, *, mapping: str | None = None):
        self.image_path = image_path
        self.mapping = mapping or "Coordinate space: logical; origin (0, 0); size 40x30; scale 1; coverage full"
        self.queries = 0

    def _run_kwin_query(self, _):
        self.queries += 1
        return {"ok": True, "result": [{"id": "window-1", "app": "org.kde.kcalc", "caption": "Calculator private title",
                                            "frame": {"x": 10, "y": 5, "width": 20, "height": 15}}]}

    def screenshot(self, include_cursor=False):
        return f"Screenshot saved: {self.image_path} (0.1 KB)\n{self.mapping}"


class FakeReader:
    def __init__(self):
        self.calls: list[tuple[bytes, list[dict]]] = []

    def capabilities(self):
        return {"images": True}

    def interpret(self, image_bytes, questions):
        self.calls.append((image_bytes, questions))
        return ReaderResult("ok", data={"display": "1"}, provider="fake", model="test")


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "screen.png"
        im = Image.new("RGB", (40, 30), "white")
        for x in range(10, 30):
            for y in range(5, 20):
                im.putpixel((x, y), (255, 0, 0))
        im.save(self.path)
        self.reader = FakeReader()
        self.engine = FakeEngine(self.path)
        self.adapter = ObservationAdapter(self.engine, "session-a", allowed_apps={"kcalc"}, reader=self.reader)

    def tearDown(self):
        self.tmp.cleanup()

    def test_app_image_metadata_and_data_reuse_same_capture(self):
        ref = self.adapter.capture("kcalc")
        metadata = self.adapter.observe(ref, mode="metadata")
        self.assertIsNone(metadata.image)
        self.assertEqual(self.reader.calls, [])
        image = self.adapter.observe(ref.capture_id, mode="image")
        self.assertEqual(hashlib.sha256(image.image).hexdigest(), ref.image_sha256)
        questions = [{"field": "display", "type": "string", "description": "Read the display."}]
        both = self.adapter.observe(ref, mode="both", questions=questions)
        self.assertEqual(both.image, image.image)
        self.assertEqual(self.reader.calls[0][0], both.image)
        self.assertEqual(both.data, {"display": "1"})
        self.assertNotIn("caption", metadata.metadata["window"])
        self.assertEqual(ref.width, 20)
        self.assertEqual(ref.height, 15)

    def test_full_and_explicit_crop(self):
        full = self.adapter.capture("kcalc", scope="full")
        self.assertEqual((full.width, full.height), (40, 30))
        crop = self.adapter.capture("kcalc", scope="crop", crop=Rect(5, 4, 8, 7))
        self.assertEqual((crop.width, crop.height), (8, 7))
        with self.assertRaisesRegex(ObservationError, "explicit rectangle"):
            self.adapter.capture("kcalc", scope="crop")
        with self.assertRaisesRegex(ObservationError, "outside"):
            self.adapter.capture("kcalc", scope="crop", crop=(35, 25, 8, 7))

    def test_allowlist_mapping_and_scope_fail_closed(self):
        with self.assertRaises(ObservationError) as exc:
            ObservationAdapter(self.engine, "session-a").capture("kcalc")
        self.assertEqual(exc.exception.code, "app_not_allowed")
        unsupported = FakeEngine(self.path, mapping="Coordinate space: logical; origin (1, 0); size 40x30; scale 1; coverage full")
        with self.assertRaises(ObservationError) as exc:
            ObservationAdapter(unsupported, "session-a", allowed_apps={"kcalc"}).capture("kcalc")
        self.assertEqual(exc.exception.code, "unsupported_mapping")
        with self.assertRaises(ObservationError):
            self.adapter.capture("kcalc", scope="region")

    def test_store_expiry_capacity_session_and_integrity(self):
        now = [0.0]
        store = CaptureStore(ttl_seconds=2, max_items=1, clock=lambda: now[0])
        adapter = ObservationAdapter(self.engine, "session-a", allowed_apps={"kcalc"}, store=store)
        first = adapter.capture("kcalc")
        second = adapter.capture("kcalc", scope="full")
        with self.assertRaises(ObservationError) as exc:
            adapter.observe(first, mode="image")
        self.assertEqual(exc.exception.code, "capture_expired_or_unknown")
        wrong_session = ObservationAdapter(self.engine, "session-b", allowed_apps={"kcalc"}, store=store)
        with self.assertRaises(ObservationError) as exc:
            wrong_session.observe(second, mode="image")
        self.assertEqual(exc.exception.code, "capture_session_mismatch")
        now[0] = 3.0
        with self.assertRaises(ObservationError):
            adapter.observe(second)

    def test_geometry_race_and_mutable_metadata_cannot_change_capture(self):
        original = self.engine._run_kwin_query
        calls = [0]
        def moved(query):
            result = original(query)
            calls[0] += 1
            if calls[0] == 2:
                result["result"][0]["frame"]["x"] += 1
            return result
        self.engine._run_kwin_query = moved
        with self.assertRaises(ObservationError) as exc:
            self.adapter.capture("kcalc")
        self.assertEqual(exc.exception.code, "window_changed_during_capture")
        self.engine._run_kwin_query = original
        ref = self.adapter.capture("kcalc")
        with self.assertRaises(TypeError):
            ref.mapping["origin"]["x"] = 99
        first = self.adapter.observe(ref)
        first.metadata["mapping"]["origin"]["x"] = 99
        self.assertEqual(self.adapter.observe(ref).metadata["mapping"]["origin"]["x"], 0)

    def test_store_rejects_tampering_and_unbounded_configuration(self):
        with self.assertRaises(ValueError):
            CaptureStore(ttl_seconds=float("nan"))
        ref = self.adapter.capture("kcalc")
        stored = self.adapter.store.get(ref.capture_id)
        stored.image = b"tampered"
        with self.assertRaises(ObservationError) as exc:
            self.adapter.observe(ref)
        self.assertEqual(exc.exception.code, "capture_hash_mismatch")

    def test_reader_error_is_structured_and_safe(self):
        class BadReader(FakeReader):
            def interpret(self, image_bytes, questions):
                raise RuntimeError("secret response and private text")
        adapter = ObservationAdapter(self.engine, "session-a", allowed_apps={"kcalc"}, reader=BadReader())
        ref = adapter.capture("kcalc")
        result = adapter.observe(ref, mode="data", questions=[{"field": "x", "type": "string", "description": "x"}])
        self.assertEqual(result.errors[0]["code"], "reader_failed")
        self.assertNotIn("secret", repr(result))


if __name__ == "__main__":
    unittest.main()
