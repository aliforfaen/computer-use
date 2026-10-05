from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jevdesktop.desktop_worker import Worker, _ensure_bounded_atspi_worker


class FakeEngine:
    def __init__(self):
        self.keys = []
        self.typed = []
        self.unicode_typed = []

    def keyboard_key(self, key):
        self.keys.append(key)
        return "pressed"

    def keyboard_type(self, text):
        self.typed.append(text)
        return "typed-eis"

    def keyboard_type_unicode(self, text):
        self.unicode_typed.append(text)
        return "typed-unicode"

    def mouse_scroll(self, x, y, delta, *, discrete=False, steps=1):
        self.scrolls = (x, y, delta, discrete, steps)
        return "scrolled"

    def session_stop(self):
        return "Session stopped"


class FakeProcess:
    def poll(self):
        return None


class AtspiEngineStub:
    _atspi_proc = None
    _atspi_bus = ""
    _atspi_a11y = ""
    _atspi_buffer = b""

    def _session_env(self):
        return {"DBUS_SESSION_BUS_ADDRESS": "bus"}

    def _a11y_bus_address(self, env):
        return "a11y"

    def _teardown_atspi_worker(self):
        raise AssertionError("unexpected teardown")


class DesktopWorkerDocumentTests(unittest.TestCase):
    def test_window_readiness_waits_for_exact_mapped_app_window(self):
        class DelayedWindowEngine:
            def __init__(self):
                self.queries = 0

            def _run_kwin_query(self, _params):
                self.queries += 1
                if self.queries < 3:
                    return {"ok": True, "result": [{
                        "id": "unrelated", "app": "org.kde.kate", "caption": "Kate",
                        "frame": {"x": 0, "y": 0, "width": 800, "height": 600},
                    }]}
                return {"ok": True, "result": [{
                    "id": "kcalc-1", "app": "org.kde.kcalc", "caption": "KCalc",
                    "frame": {"x": 0, "y": 0, "width": 600, "height": 400},
                }]}

        now = [10.0]
        worker = Worker()
        engine = DelayedWindowEngine()
        worker.engine = engine

        def advance(seconds):
            now[0] += seconds

        with patch("jevdesktop.desktop_worker.time.monotonic", side_effect=lambda: now[0]), \
             patch("jevdesktop.desktop_worker.time.sleep", side_effect=advance):
            worker._wait_for_app_window("kcalc", timeout_seconds=1.0, poll_seconds=0.1)

        self.assertEqual(engine.queries, 3)

    def test_window_readiness_times_out_when_only_wrong_app_is_mapped(self):
        class WrongWindowEngine:
            def _run_kwin_query(self, _params):
                return {"ok": True, "result": [{
                    "id": "kate-1", "app": "org.kde.kate", "caption": "Kate",
                    "frame": {"x": 0, "y": 0, "width": 800, "height": 600},
                }]}

        now = [20.0]
        worker = Worker()
        worker.engine = WrongWindowEngine()

        def advance(seconds):
            now[0] += seconds

        with patch("jevdesktop.desktop_worker.time.monotonic", side_effect=lambda: now[0]), \
             patch("jevdesktop.desktop_worker.time.sleep", side_effect=advance):
            with self.assertRaisesRegex(ValueError, "app_window_open_unconfirmed"):
                worker._wait_for_app_window("kcalc", timeout_seconds=0.25, poll_seconds=0.1)

    def test_generated_paths_are_exclusive_and_0600(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            documents = root / "run" / "documents"
            with patch("jevdesktop.desktop_worker.ROOT", root), patch("jevdesktop.desktop_worker.DOCUMENTS", documents):
                first = Worker._new_document()
                second = Worker._new_document()
                self.assertNotEqual(first, second)
                self.assertEqual(first.read_bytes(), b"")
                self.assertEqual(first.stat().st_mode & 0o777, 0o600)
                self.assertEqual(second.stat().st_mode & 0o777, 0o600)

    def test_engine_reuses_driver_atspi_worker_with_project_cap_shim(self):
        process = FakeProcess()
        with patch("jevdesktop.desktop_worker.subprocess.Popen", return_value=process) as popen:
            engine = AtspiEngineStub()
            self.assertIs(_ensure_bounded_atspi_worker(engine), process)
        args, kwargs = popen.call_args
        self.assertEqual(args[0][1:4], ["-m", "jevdesktop.jev_accessibility_worker", "--serve"])
        self.assertEqual(kwargs["stdin"], __import__("subprocess").PIPE)
        self.assertEqual(engine._atspi_bus, "bus")
        self.assertEqual(engine._atspi_a11y, "a11y")

    def test_document_path_is_worker_owned_and_survives_temp_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            base = root / "run" / "documents"
            base.mkdir(mode=0o700, parents=True)
            path = base / "handover-0123456789abcdef01234567.txt"
            path.write_bytes(b"persist me")
            worker = Worker()
            worker.app = "kate"
            worker.document_path = path
            temporary = Path(folder) / "profile"
            temporary.mkdir()
            worker.paths.append(temporary)
            with patch("jevdesktop.desktop_worker.ROOT", root), patch("jevdesktop.desktop_worker.DOCUMENTS", base):
                self.assertEqual(worker._owned_document(), path)
                worker.cleanup()
                self.assertTrue(path.exists())
                self.assertFalse(temporary.exists())

    def test_confirmed_stop_clears_document_session_binding_but_preserves_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            base = root / "run" / "documents"
            base.mkdir(mode=0o700, parents=True)
            path = base / "handover-0123456789abcdef01234567.txt"
            path.write_bytes(b"persist after stop")
            worker = Worker()
            worker.app = "kate"
            worker.document_path = path
            worker.engine = FakeEngine()
            with patch("jevdesktop.desktop_worker.ROOT", root), patch("jevdesktop.desktop_worker.DOCUMENTS", base):
                result = worker.stop()
            self.assertTrue(result["stopped"])
            self.assertIsNone(worker.app)
            self.assertIsNone(worker.document_path)
            self.assertTrue(path.exists())

    def test_key_operations_are_closed_and_document_read_is_base64(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            base = root / "run" / "documents"
            base.mkdir(mode=0o700, parents=True)
            path = base / "handover-0123456789abcdef01234567.txt"
            content = "Handover ✓\n".encode("utf-8")
            path.write_bytes(content)
            worker = Worker()
            worker.app = "kate"
            worker.document_path = path
            engine = FakeEngine()
            worker.engine = engine
            with patch("jevdesktop.desktop_worker.ROOT", root), patch("jevdesktop.desktop_worker.DOCUMENTS", base):
                self.assertEqual(worker.call("document_key", {"operation": "save"}), "pressed")
                self.assertEqual(worker.call("document_bytes", {}), {
                    "utf8_base64": base64.b64encode(content).decode("ascii")})
                with self.assertRaisesRegex(ValueError, "document_operation_not_allowed"):
                    worker.call("document_key", {"operation": "ctrl+q"})
            self.assertEqual(engine.keys, ["ctrl+s"])

    def test_kate_uses_bounded_unicode_paste_for_ascii_document_text(self):
        worker = Worker()
        worker.app = "kate"
        engine = FakeEngine()
        worker.engine = engine
        text = "A" * 1200 + "\n"
        self.assertEqual(worker.call("keyboard_type", {"text": text}), "typed-unicode")
        self.assertEqual(engine.unicode_typed, [text])
        self.assertEqual(engine.typed, [])

    def test_non_kate_ascii_fixture_typing_keeps_existing_driver_path(self):
        worker = Worker()
        worker.app = "firefox"
        engine = FakeEngine()
        worker.engine = engine
        self.assertEqual(worker.call("keyboard_type", {"text": "Advance"}), "typed-eis")
        self.assertEqual(engine.typed, ["Advance"])
        self.assertEqual(engine.unicode_typed, [])

    def test_scroll_uses_bounded_discrete_detents_and_preserves_direction(self):
        worker = Worker()
        worker.app = "firefox"
        engine = FakeEngine()
        worker.engine = engine
        self.assertEqual(worker.call("mouse_scroll", {"x": 40, "y": 50, "delta": 2, "steps": 2}), "scrolled")
        self.assertEqual(engine.scrolls, (40, 50, 2, True, 2))
        self.assertEqual(worker.call("mouse_scroll", {"x": 40, "y": 50, "delta": -1, "steps": 1}), "scrolled")
        self.assertEqual(engine.scrolls, (40, 50, -1, True, 1))
        with self.assertRaisesRegex(ValueError, "scroll_argument_invalid"):
            worker.call("mouse_scroll", {"x": 40, "y": 50, "delta": 120, "steps": 1})


if __name__ == "__main__":
    unittest.main()
