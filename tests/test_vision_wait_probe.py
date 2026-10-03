"""Safety-invariant tests for the paid heartbeat probe harness.

The probe tool is an experiment harness, not the product surface. This file
keeps only the invariants that protect real money and real cleanup: budget
ceilings fail before an extra provider call, the pre-call journal is durable,
interrupts do not retry, owned sessions/profiles are cleaned, and evidence is
never overwritten. The exhaustive per-failure-point matrix was collapsed.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

import wait_benchmark
from vision_reader import ReaderResult
from wait_watcher import ReaderJudge, WaitFrame, WaitSpec
from tools.vision_wait_probe import (
    BudgetedReader,
    MAX_CALLS,
    MAX_CASE_CALLS,
    MAX_COST_RESERVE,
    MAX_COST_USD,
    MAX_TOKENS,
    MIN_REQUEST_GAP_SECONDS,
    PRIOR_INTERRUPTED_RUN_RESERVE_USD,
    ProbeBudgetError,
    TRIAL_CASES,
    estimate_cost_usd,
    validate_probe_budget,
)


QUESTIONS = [{"field": "judgment", "type": "string",
              "description": "Return wait, wake, error, or unexpected."}]


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


class InterruptingReader(FakeReader):
    """Simulates SIGINT arriving during a blocking provider read."""

    def interpret(self, image_bytes, questions):
        self.calls.append((image_bytes, questions))
        raise KeyboardInterrupt


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
        ReaderJudge(bounded)(frame, WaitSpec("green ready state and forecast text",
                                             {"x": 20, "y": 96, "width": 920, "height": 480}))
        self.assertLessEqual(bounded.attempts[0]["prompt_ascii_chars"], 2048)

    def test_attempt_caps_fail_before_an_extra_provider_call(self):
        clock = FakeClock()
        inner = FakeReader()
        reader = BudgetedReader(inner, clock=clock, sleeper=clock.sleep)
        reader.begin_case("ready")
        for _ in range(MAX_CASE_CALLS):
            reader.interpret(png_bytes(), QUESTIONS)
        with self.assertRaises(ProbeBudgetError):
            reader.interpret(png_bytes(), QUESTIONS)
        self.assertEqual(len(inner.calls), MAX_CASE_CALLS)
        self.assertEqual(reader.stop_reason, "case_attempt_cap_reached")

    def test_rate_limit_and_usage_anomaly_stop_further_calls(self):
        clock = FakeClock()
        limited = FakeReader([ReaderResult("error", error="http_status_429",
                                           provider="deepseek", model="deepseek-flash")])
        reader = BudgetedReader(limited, clock=clock, sleeper=clock.sleep)
        reader.begin_case("ready")
        self.assertEqual(reader.interpret(png_bytes(), QUESTIONS).error, "http_status_429")
        self.assertTrue(reader.rate_limited)
        with self.assertRaises(ProbeBudgetError):
            reader.interpret(png_bytes(), QUESTIONS)
        self.assertEqual(len(limited.calls), 1)

        over = FakeReader([ReaderResult("ok", data={"judgment": "wait"},
                                        usage={"prompt_tokens": 999_999, "completion_tokens": 2})])
        reader = BudgetedReader(over, clock=clock, sleeper=clock.sleep)
        reader.begin_case("ready")
        reader.interpret(png_bytes(), QUESTIONS)
        self.assertEqual(reader.stop_reason, "usage_exceeded_estimate_bound")
        with self.assertRaises(ProbeBudgetError):
            reader.interpret(png_bytes(), QUESTIONS)
        self.assertEqual(len(over.calls), 1)
        self.assertIn("usage_anomaly", reader.attempts[0])

    def test_interrupt_propagates_without_an_extra_provider_request(self):
        inner = InterruptingReader()
        events = []
        reader = BudgetedReader(inner, on_attempt_event=lambda event, payload: events.append(event))
        reader.begin_case("ready")
        with self.assertRaises(KeyboardInterrupt):
            reader.interpret(png_bytes(), QUESTIONS)
        self.assertEqual(len(inner.calls), 1)
        self.assertEqual(events, ["attempt_started"])
        self.assertEqual(len(reader.attempts), 1)
        self.assertEqual(reader.attempts[0]["status"], "in_flight")

    def test_pre_call_journal_is_durable_before_the_provider_call(self):
        tmp = Path(tempfile.mkdtemp())
        journal = tmp / "progress.jsonl"
        observed = {}

        def on_event(event, payload):
            with journal.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"event": event, **payload}, sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())

        class WatchingReader:
            def capabilities(self):
                return {"images": True}

            def interpret(self, image_bytes, questions):
                observed["journal_at_call"] = journal.read_text(encoding="utf-8")
                return ReaderResult("ok", data={"judgment": "wait"}, provider="deepseek", model="deepseek-flash")

        try:
            clock = FakeClock()
            reader = BudgetedReader(WatchingReader(), clock=clock, sleeper=clock.sleep, on_attempt_event=on_event)
            reader.begin_case("ready")
            reader.interpret(png_bytes(), QUESTIONS)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertIn('"attempt_started"', observed["journal_at_call"])

    def test_interrupted_case_stops_owned_session_and_removes_profile(self):
        import kwin_mcp.core as kwin_core
        import tools.vision_wait_probe as probe

        engines = []
        profiles = []

        class FakeEngine:
            def __init__(self):
                self.stop_calls = 0
                engines.append(self)

            def session_start(self, **kwargs):
                return "Input backend: KWin EIS\nSession started"

            def session_stop(self):
                self.stop_calls += 1
                return "Session stopped"

        class FakeAdapter:
            def __init__(self, engine, trial, allowed_apps):
                self.trial = trial

            def capture(self, app, *, title, scope):
                return type("Ref", (), {"capture_id": "cap-1"})()

            def observe(self, ref, *, mode):
                return type("Obs", (), {"image": png_bytes()})()

        class FakeServer:
            server_port = 9

            def snapshot(self, trial):
                return []

        def fake_mkdtemp(prefix):
            path = Path(real_mkdtemp(prefix=prefix))
            profiles.append(path)
            return str(path)

        inner = InterruptingReader()
        events = []
        budget_reader = BudgetedReader(inner, on_attempt_event=lambda event, payload: events.append(event))
        scratch = Path(tempfile.mkdtemp())
        real_mkdtemp = tempfile.mkdtemp
        try:
            with mock.patch.object(kwin_core, "AutomationEngine", FakeEngine), \
                 mock.patch.object(probe, "ObservationAdapter", FakeAdapter), \
                 mock.patch.object(wait_benchmark, "_url", lambda server, case, trial: "http://127.0.0.1/x"), \
                 mock.patch.object(wait_benchmark, "_wait_loaded", lambda server, trial: 0.0), \
                 mock.patch.object(wait_benchmark, "_status_tone", lambda payload: "loading"), \
                 mock.patch.object(probe, "_post_arm", lambda port, trial: None), \
                 mock.patch.object(probe.tempfile, "mkdtemp", fake_mkdtemp):
                with self.assertRaises(KeyboardInterrupt):
                    probe._run_case(FakeServer(), dict(TRIAL_CASES[0]), scratch, budget_reader,
                                    deadline_seconds=1.0, debounce_seconds=0.0)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        self.assertEqual(engines[0].stop_calls, 1)
        self.assertTrue(profiles and not profiles[0].exists())
        self.assertEqual(events, ["attempt_started"])
        self.assertEqual(len(inner.calls), 1)
        self.assertEqual(budget_reader.attempts[0]["status"], "in_flight")

    class _CaseServer:
        server_port = 9

        def snapshot(self, trial):
            return []

    def test_run_case_removes_profile_when_engine_construction_fails(self):
        import kwin_mcp.core as kwin_core
        import tools.vision_wait_probe as probe

        real_mkdtemp = tempfile.mkdtemp
        profiles = []

        def fake_mkdtemp(prefix):
            path = Path(real_mkdtemp(prefix=prefix))
            profiles.append(path)
            return str(path)

        class BrokenEngine:
            def __init__(self):
                raise RuntimeError("engine_unavailable")

        budget_reader = BudgetedReader(FakeReader())
        scratch = Path(tempfile.mkdtemp())
        try:
            with mock.patch.object(probe.tempfile, "mkdtemp", fake_mkdtemp), \
                 mock.patch.object(kwin_core, "AutomationEngine", BrokenEngine):
                row = probe._run_case(self._CaseServer(), dict(TRIAL_CASES[0]), scratch, budget_reader,
                                      deadline_seconds=1.0, debounce_seconds=0.0)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error"], "engine_unavailable")
        self.assertEqual(row["cleanup"], "not_started")
        self.assertEqual(row["profile_cleanup"], "passed")
        self.assertTrue(profiles and not profiles[0].exists())

    def test_run_closes_reader_and_server_when_start_fails(self):
        import tools.vision_wait_probe as probe

        tmp = Path(tempfile.mkdtemp())
        closed = []

        class Reader:
            def capabilities(self):
                return {"images": True}

            def interpret(self, *args):
                raise AssertionError("reader must not be called")

            def close(self):
                closed.append(True)

        class Server:
            def __init__(self):
                self.closed = 0

            def start(self):
                raise RuntimeError("start_failed")

            def server_close(self):
                self.closed += 1

        server = Server()
        try:
            with mock.patch.object(wait_benchmark, "_versions", lambda: {}):
                with self.assertRaises(RuntimeError):
                    probe.run(tmp, reader_factory=Reader, server_factory=lambda: server)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(closed, [True])
        self.assertEqual(server.closed, 1)

    def test_run_refuses_to_overwrite_existing_evidence(self):
        import tools.vision_wait_probe as probe

        tmp = Path(tempfile.mkdtemp())
        prior = tmp / "progress.jsonl"
        prior.write_text('{"event":"attempt_started"}\n', encoding="utf-8")
        before = prior.read_text(encoding="utf-8")
        try:
            with self.assertRaises(probe.ProbeOutputError):
                probe.run(tmp, reader_factory=FakeReader, server_factory=lambda: None)
            after = prior.read_text(encoding="utf-8")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(after, before)

    def test_interrupted_run_persists_pre_call_journal_and_does_not_retry(self):
        import tools.vision_wait_probe as probe

        tmp = Path(tempfile.mkdtemp())
        observed = {}
        calls = []

        class RunInterruptingReader:
            def capabilities(self):
                return {"images": True}

            def interpret(self, image_bytes, questions):
                calls.append("call")
                observed["journal_at_call"] = (tmp / "progress.jsonl").read_text(encoding="utf-8")
                raise KeyboardInterrupt

            def close(self):
                calls.append("closed")

        class FakeServer:
            server_port = 7

            def start(self):
                pass

            def shutdown(self):
                pass

            def server_close(self):
                pass

            def snapshot(self, trial):
                return []

        def fake_run_case(server, case, output, budget_reader, *, deadline_seconds, debounce_seconds,
                          on_case_event=None):
            budget_reader.begin_case(case["case"])
            if on_case_event is not None:
                on_case_event("case_started", {"case": case["case"]})
            budget_reader.interpret(png_bytes(), QUESTIONS)
            return {}

        with mock.patch.object(probe, "_run_case", fake_run_case), \
             mock.patch.object(wait_benchmark, "_versions", lambda: {"kwin": "test"}):
            result = probe.run(tmp, reader_factory=lambda: RunInterruptingReader(), server_factory=FakeServer)
        journal_text = Path(result["progress_journal"]).read_text(encoding="utf-8")
        shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["paid_provider_attempts"], 1)
        self.assertEqual(calls.count("call"), 1)
        self.assertEqual(calls.count("closed"), 1)
        self.assertIn("attempt_started", observed["journal_at_call"])
        self.assertIn("run_interrupted", journal_text)
        self.assertEqual(result["attempts_with_unreported_usage"], 1)
        self.assertFalse(result["execution_completed"])
        self.assertAlmostEqual(result["budget"]["prior_interrupted_run_reserve_usd"],
                               PRIOR_INTERRUPTED_RUN_RESERVE_USD, places=9)


if __name__ == "__main__":
    unittest.main()
