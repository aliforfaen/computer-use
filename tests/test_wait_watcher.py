from __future__ import annotations

import threading
import time
import unittest

from jevdesktop.vision_reader import ReaderResult, _validate_questions
from jevdesktop.wait_watcher import AdapterFrameSource, ReaderJudge, WaitFrame, WaitSpec, WaitWatcher


class WaitWatcherTests(unittest.TestCase):
    def test_wake_requires_debounce_and_reports_real_counts(self):
        frames = iter([b"one", b"two", b"three", b"four"])
        calls = []

        def capture():
            try:
                image = next(frames)
            except StopIteration:
                image = b"four"
            calls.append(image)
            return WaitFrame(image, capture_id=f"capture-{len(calls)}")

        decisions = iter(["wake", "wait", "wake", "wake", "wake"])
        watcher = WaitWatcher(capture, lambda _frame, _spec: next(decisions),
                              interval_seconds=0.01, debounce_seconds=0.015)
        result = watcher.wait(WaitSpec("ready"), deadline_seconds=0.5)
        self.assertEqual(result.status, "ready")
        self.assertGreaterEqual(result.captures, 3)
        self.assertEqual(result.captures, len(calls))
        self.assertGreaterEqual(result.judgments, 4)
        self.assertGreaterEqual(result.elapsed_seconds, 0.015)
        self.assertGreaterEqual(result.capture_seconds, 0)
        self.assertGreaterEqual(result.judge_seconds, 0)
        self.assertTrue(result.evidence.capture_id.startswith("capture-"))

    def test_unexpected_judgment_receives_whole_app_frame_when_region_is_set(self):
        seen = []

        def judge(frame, spec):
            seen.append((frame.image, spec.region))
            return "unexpected"

        watcher = WaitWatcher(lambda: WaitFrame(b"whole-app"), judge,
                              interval_seconds=0.01, debounce_seconds=0)
        result = watcher.wait(WaitSpec("loading label disappears", {"x": 4, "y": 8, "width": 20, "height": 10}),
                              deadline_seconds=0.3)
        self.assertEqual(result.status, "unexpected")
        self.assertEqual(seen[0], (b"whole-app", {"x": 4, "y": 8, "width": 20, "height": 10}))

    def test_latest_frame_coalescing_and_only_one_judgment_at_a_time(self):
        captured = 0
        active = 0
        max_active = 0
        lock = threading.Lock()

        def capture():
            nonlocal captured
            with lock:
                captured += 1
                n = captured
            return WaitFrame(f"frame-{n}".encode(), capture_id=str(n))

        def judge(_frame, _spec):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.06)
                return "wake"
            finally:
                with lock:
                    active -= 1

        result = WaitWatcher(capture, judge, interval_seconds=0.005, debounce_seconds=0).wait(
            WaitSpec("done"), deadline_seconds=0.5)
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.judgments, 1)
        self.assertEqual(max_active, 1)
        self.assertGreater(result.captures, 1)
        self.assertGreater(result.coalesced_frames, 0)
        self.assertLessEqual(result.judgments, result.captures)

    def test_timeout_and_cancellation(self):
        waiting = WaitWatcher(lambda: WaitFrame(b"pending"), lambda *_: "wait",
                              interval_seconds=0.01, debounce_seconds=0)
        timeout = waiting.wait(WaitSpec("finished"), deadline_seconds=0.045)
        self.assertEqual(timeout.status, "timeout")
        self.assertGreaterEqual(timeout.captures, 1)
        self.assertGreaterEqual(timeout.judgments, 1)

        cancelled = threading.Event()
        threading.Timer(0.03, cancelled.set).start()
        result = waiting.wait(WaitSpec("finished"), deadline_seconds=0.5, cancel_event=cancelled)
        self.assertEqual(result.status, "cancelled")

    def test_bad_capture_and_bad_or_error_judgment_fail_explicitly(self):
        capture_error = WaitWatcher(lambda: (_ for _ in ()).throw(RuntimeError("private")), lambda *_: "wait",
                                    interval_seconds=0.01).wait(WaitSpec("ready"), deadline_seconds=0.2)
        self.assertEqual((capture_error.status, capture_error.error_code), ("error", "capture_failed"))
        self.assertEqual(capture_error.capture_errors, 1)

        class BudgetError(Exception):
            code = "task_observation_budget_exceeded"

        budget = WaitWatcher(lambda: (_ for _ in ()).throw(BudgetError()), lambda *_: "wait",
                             interval_seconds=0.01, debounce_seconds=0).wait(
                                 WaitSpec("ready"), deadline_seconds=0.2)
        self.assertEqual((budget.status, budget.error_code), ("error", "task_observation_budget_exceeded"))

        invalid = WaitWatcher(lambda: WaitFrame(b"frame"), lambda *_: "coordinates",
                              interval_seconds=0.01).wait(WaitSpec("ready"), deadline_seconds=0.2)
        self.assertEqual((invalid.status, invalid.error_code), ("error", "invalid_judgment"))
        app_error = WaitWatcher(lambda: WaitFrame(b"frame"), lambda *_: "error",
                                interval_seconds=0.01, debounce_seconds=0).wait(WaitSpec("ready"), deadline_seconds=0.2)
        self.assertEqual((app_error.status, app_error.error_code), ("error", "app_error_detected"))

    def test_reader_judge_schema_and_error_separation(self):
        class FakeReader:
            def __init__(self, result=None):
                self.result = result
                self.questions = None
                self.asserted_image = None

            def interpret(self, image, questions):
                self.asserted_image = image
                self.questions = questions
                _validate_questions(questions)
                return self.result or ReaderResult("ok", data={"judgment": "wait"})

            def capabilities(self):
                return {"images": True}

        reader = FakeReader()
        decision = ReaderJudge(reader)(WaitFrame(b"image"), WaitSpec("ready", {"x": 1, "y": 2, "width": 3, "height": 4}))
        self.assertEqual(decision.judgment, "wait")
        self.assertEqual(reader.asserted_image, b"image")
        self.assertEqual(reader.questions[0]["type"], "string")

        app_reader = FakeReader(ReaderResult("ok", data={"judgment": "error"}, provider="fake", model="test"))
        result = WaitWatcher(lambda: WaitFrame(b"frame"), ReaderJudge(app_reader), interval_seconds=0.01,
                             debounce_seconds=0).wait(WaitSpec("ready"), deadline_seconds=0.2)
        self.assertEqual((result.status, result.error_code), ("error", "app_error_detected"))

        broken_reader = FakeReader(ReaderResult("error", error="http_status_429", provider="fake", model="test"))
        failed = WaitWatcher(lambda: WaitFrame(b"frame"), ReaderJudge(broken_reader), interval_seconds=0.01,
                             debounce_seconds=0).wait(WaitSpec("ready"), deadline_seconds=0.2)
        self.assertEqual((failed.status, failed.error_code), ("error", "http_status_429"))

        malformed_reader = FakeReader(ReaderResult("ok", data={"judgment": "coordinates"}, provider="fake", model="test"))
        malformed = WaitWatcher(lambda: WaitFrame(b"frame"), ReaderJudge(malformed_reader), interval_seconds=0.01,
                                debounce_seconds=0).wait(WaitSpec("ready"), deadline_seconds=0.2)
        self.assertEqual((malformed.status, malformed.error_code), ("error", "invalid_judgment"))

    def test_cancel_and_deadline_during_judgment_override_wake(self):
        cancel = threading.Event()

        def slow_wake(_frame, _spec):
            time.sleep(0.04)
            return "wake"

        timer = threading.Timer(0.01, cancel.set)
        timer.start()
        cancelled = WaitWatcher(lambda: WaitFrame(b"frame"), slow_wake,
                                interval_seconds=0.005, debounce_seconds=0).wait(
                                    WaitSpec("ready"), deadline_seconds=0.3, cancel_event=cancel)
        timer.join()
        self.assertEqual(cancelled.status, "cancelled")

        started = time.monotonic()
        expired = WaitWatcher(lambda: WaitFrame(b"frame"), slow_wake,
                              interval_seconds=0.005, debounce_seconds=0).wait(
                                  WaitSpec("ready"), deadline_seconds=0.015)
        self.assertEqual(expired.status, "timeout")
        self.assertGreaterEqual(time.monotonic() - started, 0.04)

    def test_adapter_frame_source_uses_app_scope_and_preserves_capture_identity(self):
        class Adapter:
            def __init__(self):
                self.request = None

            def capture(self, app, *, title, scope):
                self.request = (app, title, scope)
                return type("Ref", (), {"capture_id": "source-1"})()

            def observe(self, ref, *, mode):
                self.asserted = mode
                return type("Observation", (), {"image": b"full-app-image"})()

        adapter = Adapter()
        frame = AdapterFrameSource(adapter, "firefox", title="test")()
        self.assertEqual(adapter.request, ("firefox", "test", "app"))
        self.assertEqual(adapter.asserted, "image")
        self.assertEqual((frame.image, frame.capture_id), (b"full-app-image", "source-1"))

    def test_input_validation(self):
        with self.assertRaises(ValueError):
            WaitSpec("")
        with self.assertRaises(ValueError):
            WaitSpec("ready", {"x": -1, "y": 0, "width": 2, "height": 2})
        with self.assertRaises(ValueError):
            WaitWatcher(lambda: WaitFrame(b"x"), lambda *_: "wait", interval_seconds=0)


if __name__ == "__main__":
    unittest.main()
