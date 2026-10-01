from __future__ import annotations

import io
import unittest

from PIL import Image

from vision_reader import ReaderResult
from wait_watcher import ReaderJudge, WaitFrame, WaitSpec
from vision_wait_probe import (
    BudgetedReader,
    MAX_CASE_CALLS,
    MAX_CALLS,
    MAX_COST_RESERVE,
    MAX_COST_USD,
    MAX_TOKENS,
    MIN_REQUEST_GAP_SECONDS,
    ProbeBudgetError,
    estimate_cost_usd,
    validate_probe_budget,
)


class FakeClock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, duration):
        self.sleeps.append(duration)
        self.now += duration


class FakeReader:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.calls = []

    def capabilities(self):
        return {"images": True}

    def interpret(self, image_bytes, questions):
        self.calls.append((image_bytes, questions))
        if self.results:
            return self.results.pop(0)
        return ReaderResult("ok", data={"judgment": "wait"}, provider="deepseek", model="deepseek-flash",
                            served_model="deepseek-flash", usage={"prompt_tokens": 50, "completion_tokens": 2})


def png_bytes(width=64, height=48):
    output = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(output, format="PNG")
    return output.getvalue()


class VisionWaitProbeTests(unittest.TestCase):
    def test_cost_ceiling_uses_peak_rates_and_stays_under_authorized_cap(self):
        budget = validate_probe_budget()
        self.assertEqual(MAX_CALLS, 12)
        self.assertEqual(MAX_CASE_CALLS, 3)
        self.assertEqual(MAX_TOKENS, 128)
        self.assertEqual(budget["minimum_request_start_gap_seconds"], MIN_REQUEST_GAP_SECONDS)
        self.assertLess(MAX_COST_RESERVE, MAX_COST_USD)
        self.assertLess(MAX_COST_RESERVE, 0.03)
        self.assertGreater(estimate_cost_usd(1024, 128), 0)
        with self.assertRaises(ValueError):
            estimate_cost_usd(-1, 2)

    def test_starts_are_spaced_and_usage_is_recorded(self):
        clock = FakeClock()
        inner = FakeReader()
        reader = BudgetedReader(inner, clock=clock, sleeper=clock.sleep)
        reader.begin_case("ready")
        questions = [{"field": "judgment", "type": "string", "description": "Return wait, wake, error, or unexpected."}]
        reader.interpret(png_bytes(), questions)
        reader.interpret(png_bytes(), questions)
        starts = [entry["request_start_monotonic"] for entry in reader.attempts]
        self.assertGreaterEqual(starts[1] - starts[0], 1.0)
        self.assertEqual(len(inner.calls), 2)
        self.assertEqual(reader.attempts[0]["served_model"], "deepseek-flash")
        self.assertIsInstance(reader.attempts[0]["cost_upper_usd"], float)

    def test_real_readerjudge_question_fits_the_paid_probe_bound(self):
        class CaptureQuestions:
            def __init__(self):
                self.questions = None

            def interpret(self, image_bytes, questions):
                self.questions = questions
                return ReaderResult("ok", data={"judgment": "wait"}, provider="deepseek", model="deepseek-flash")

            def capabilities(self):
                return {"images": True}

        inner = CaptureQuestions()
        bounded = BudgetedReader(inner)
        bounded.begin_case("ready")
        frame = WaitFrame(png_bytes(), "id")
        ReaderJudge(bounded)(frame, WaitSpec("green ready state and forecast text", {"x": 20, "y": 96, "width": 920, "height": 480}))
        self.assertLessEqual(bounded.attempts[0]["prompt_ascii_chars"], 2048)

    def test_attempt_caps_fail_before_an_extra_provider_call(self):
        clock = FakeClock()
        inner = FakeReader()
        reader = BudgetedReader(inner, clock=clock, sleeper=clock.sleep)
        questions = [{"field": "judgment", "type": "string", "description": "Return wait, wake, error, or unexpected."}]
        reader.begin_case("ready")
        for _ in range(MAX_CASE_CALLS):
            reader.interpret(png_bytes(), questions)
        with self.assertRaises(ProbeBudgetError):
            reader.interpret(png_bytes(), questions)
        self.assertEqual(len(inner.calls), MAX_CASE_CALLS)
        self.assertEqual(reader.stop_reason, "case_attempt_cap_reached")

    def test_rate_limit_stops_all_later_provider_attempts(self):
        clock = FakeClock()
        limited = FakeReader([ReaderResult("error", error="http_status_429", provider="deepseek", model="deepseek-flash")])
        reader = BudgetedReader(limited, clock=clock, sleeper=clock.sleep)
        questions = [{"field": "judgment", "type": "string", "description": "Return wait, wake, error, or unexpected."}]
        reader.begin_case("ready")
        result = reader.interpret(png_bytes(), questions)
        self.assertEqual(result.error, "http_status_429")
        self.assertTrue(reader.rate_limited)
        with self.assertRaises(ProbeBudgetError):
            reader.interpret(png_bytes(), questions)
        self.assertEqual(len(limited.calls), 1)

    def test_actual_usage_over_bound_prevents_any_next_request(self):
        clock = FakeClock()
        over = FakeReader([ReaderResult("ok", data={"judgment": "wait"},
                                        usage={"prompt_tokens": 999_999, "completion_tokens": 2})])
        reader = BudgetedReader(over, clock=clock, sleeper=clock.sleep)
        questions = [{"field": "judgment", "type": "string", "description": "Return wait, wake, error, or unexpected."}]
        reader.begin_case("ready")
        reader.interpret(png_bytes(), questions)
        self.assertEqual(reader.stop_reason, "usage_exceeded_estimate_bound")
        with self.assertRaises(ProbeBudgetError):
            reader.interpret(png_bytes(), questions)
        self.assertEqual(len(over.calls), 1)
        self.assertIn("usage_anomaly", reader.attempts[0])

    def test_minimum_gap_wait_cannot_start_request_after_wait_deadline(self):
        clock = FakeClock()
        inner = FakeReader()
        reader = BudgetedReader(inner, clock=clock, sleeper=clock.sleep)
        reader.begin_case("ready")
        reader.set_wait_deadline(clock.now + 0.5)
        questions = [{"field": "judgment", "type": "string", "description": "Return wait, wake, error, or unexpected."}]
        reader.interpret(png_bytes(), questions)
        with self.assertRaises(ProbeBudgetError) as raised:
            reader.interpret(png_bytes(), questions)
        self.assertEqual(str(raised.exception), "wait_deadline_reached")
        self.assertTrue(reader.deadline_blocked)
        self.assertEqual(len(inner.calls), 1)
        self.assertEqual(len(reader.attempts), 1)

    def test_oversized_prompt_fails_and_unicode_prompt_is_counted_as_json_escaped_ascii(self):
        inner = FakeReader()
        reader = BudgetedReader(inner)
        reader.begin_case("ready")
        questions = [{"field": "judgment", "type": "string", "description": "x" * 3000}]
        with self.assertRaises(ProbeBudgetError):
            reader.interpret(png_bytes(), questions)
        non_ascii = [{"field": "judgment", "type": "string", "description": "café"}]
        reader.interpret(png_bytes(), non_ascii)
        self.assertGreater(reader.attempts[0]["prompt_ascii_chars"], len("café"))
        self.assertEqual(len(inner.calls), 1)


if __name__ == "__main__":
    unittest.main()
