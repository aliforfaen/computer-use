from __future__ import annotations

import io
import json
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.request import Request, urlopen

from PIL import Image, ImageDraw

from wait_benchmark import CASES, FixtureServer, _make_judge, _normalized_status, _status_tone, make_plan


class WaitBenchmarkTests(unittest.TestCase):
    def _png(self, draw) -> bytes:
        image = Image.new("RGB", (1280, 800), "white")
        draw(ImageDraw.Draw(image))
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()

    def _judge(self, baseline: bytes, current: bytes) -> str:
        counters = {"local_judgments": 0, "judgment_ms": 0.0}
        return _make_judge(baseline, counters)(SimpleNamespace(image=current), None)

    def test_seeded_arm_order_is_reproducible_and_bounded(self) -> None:
        first = make_plan(31, 2, 10)
        self.assertEqual(first, make_plan(31, 2, 10))
        self.assertEqual(len(first), 10)
        self.assertEqual({arm for _, _, arm in first}, {"polling", "watcher"})
        with self.assertRaises(ValueError):
            make_plan(1, 1, 51)

    def test_fixture_matrix_has_all_requested_conditions(self) -> None:
        self.assertEqual({case["case"] for case in CASES},
                         {"loading_ready", "loading_error", "no_change", "animation_noise", "outside_dialog"})
        self.assertEqual(CASES[2]["expected"], "timeout")
        self.assertGreater(CASES[-1]["delay_ms"], CASES[-1]["dialog_ms"])

    def test_error_and_unexpected_judgments_survive_result_normalization(self) -> None:
        self.assertEqual(_normalized_status("error"), "error")
        self.assertEqual(_normalized_status("unexpected"), "unexpected")
        self.assertEqual(_normalized_status("timeout"), "timeout")

    def test_local_judge_detects_ready_error_and_outside_dialog(self) -> None:
        baseline = self._png(lambda _draw: None)
        ready = self._png(lambda draw: draw.rectangle((100, 200, 650, 340), fill="#86efac"))
        error = self._png(lambda draw: draw.rectangle((100, 200, 650, 340), fill="#fca5a5"))
        dialog = self._png(lambda draw: draw.rectangle((980, 180, 1270, 390), fill="#7f1d1d"))
        small_noise = self._png(lambda draw: draw.ellipse((450, 270, 471, 291), fill="#2563eb"))
        self.assertEqual(self._judge(baseline, ready), "wake")
        self.assertEqual(self._judge(baseline, error), "error")
        self.assertEqual(self._judge(baseline, dialog), "unexpected")
        self.assertEqual(self._judge(baseline, small_noise), "wait")

    def test_fixture_status_palette_has_distinct_loading_ready_and_error_colors(self) -> None:
        loading = self._png(lambda draw: draw.rectangle((100, 200, 650, 340), fill="#fbbf24"))
        ready = self._png(lambda draw: draw.rectangle((100, 200, 650, 340), fill="#86efac"))
        error = self._png(lambda draw: draw.rectangle((100, 200, 650, 340), fill="#fca5a5"))
        self.assertEqual(_status_tone(loading), "loading")
        self.assertEqual(_status_tone(ready), "ready")
        self.assertEqual(_status_tone(error), "error")

    def test_loading_to_error_color_change_exceeds_visual_diff_threshold(self) -> None:
        loading = self._png(lambda draw: draw.rectangle((54, 239, 654, 389), fill="#fbbf24"))
        error = self._png(lambda draw: draw.rectangle((54, 239, 654, 389), fill="#fca5a5"))
        self.assertEqual(self._judge(loading, error), "error")

    def test_event_endpoint_records_independent_server_monotonic_time(self) -> None:
        server = FixtureServer()
        server.start()
        try:
            trial = "test-trial"
            with urlopen(f"http://127.0.0.1:{server.server_port}/armed?trial={trial}", timeout=2) as response:
                self.assertEqual(response.status, 202)
            body = json.dumps({"trial": trial, "kind": "ready", "page_ms": 123.0}).encode()
            request = Request(f"http://127.0.0.1:{server.server_port}/event", data=body,
                              headers={"Content-Type": "application/json"}, method="POST")
            with urlopen(request, timeout=2) as response:
                self.assertEqual(response.status, 204)
            event, = server.snapshot(trial)
            self.assertEqual(event["kind"], "ready")
            self.assertEqual(event["page_ms"], 123.0)
            self.assertIsInstance(event["received_monotonic"], float)
            self.assertLessEqual(event["received_monotonic"], time.monotonic())
            arm = Request(f"http://127.0.0.1:{server.server_port}/arm?trial={trial}", data=b"", method="POST")
            with urlopen(arm, timeout=2) as response:
                self.assertEqual(response.status, 204)
            self.assertTrue(any(item["kind"] == "arm" for item in server.snapshot(trial)))
            with urlopen(f"http://127.0.0.1:{server.server_port}/armed?trial={trial}", timeout=2) as response:
                self.assertEqual(response.status, 204)
        finally:
            server.shutdown()
            server.server_close()
            server.thread.join(timeout=2)

    def test_fixture_records_events_but_judge_does_not_accept_truth_data(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "benchmark_fixtures" / "wait.html").read_text()
        self.assertIn("send(state, performance.now())", source)
        self.assertIn("waitForArm();", source)
        self.assertIn("/armed?trial=", source)
        self.assertIn("send('dialog', performance.now())", source)
        self.assertNotIn("event_log", source)


if __name__ == "__main__":
    unittest.main()
