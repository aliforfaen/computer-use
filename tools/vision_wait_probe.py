#!/usr/bin/env python3
"""Small paid DeepSeek heartbeat probe over the local M4 virtual fixture.

Four synthetic states are judged with the existing ReaderJudge/WaitWatcher.
Fixture event timestamps are independent ground truth. This probe has hard
attempt, image, prompt, output, spacing, and peak-price ceilings; it performs
no retry, OCR, Jev, provider fallback, or primary-agent accounting.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import shlex
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.request import Request, urlopen

from observation import ObservationAdapter
from vision_reader import ReaderConfig, ReaderResult, VisionReader, _question_prompt
from wait_watcher import AdapterFrameSource, ReaderJudge, WaitSpec, WaitWatcher

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "run" / "vision-wait-probe"
MODEL = "deepseek-flash"
BASE_URL = "https://api.deepseek.com"
MAX_CALLS = 12
MAX_CASE_CALLS = 3
MAX_TOKENS = 128
MIN_REQUEST_GAP_SECONDS = 1.0
PROMPT_ASCII_LIMIT = 2048
PROMPT_TOKEN_FACTOR = 2  # conservative reserve: at most two tokens per prompt byte
PROMPT_OVERHEAD_TOKENS = 256
IMAGE_TOKEN_LIMIT = 1024  # official DeepSeek Vision guide maximum per image
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_DIMENSION = 8192
MAX_COST_USD = 0.80
# Use peak, cache-miss rates for the reserve even when actual use may be off-peak/cached.
INPUT_USD_PER_MILLION = 0.30
OUTPUT_USD_PER_MILLION = 1.20
MAX_INPUT_TOKENS_PER_CALL = IMAGE_TOKEN_LIMIT + PROMPT_ASCII_LIMIT * PROMPT_TOKEN_FACTOR + PROMPT_OVERHEAD_TOKENS
MAX_COST_PER_CALL = (MAX_INPUT_TOKENS_PER_CALL * INPUT_USD_PER_MILLION
                     + MAX_TOKENS * OUTPUT_USD_PER_MILLION) / 1_000_000
MAX_COST_RESERVE = MAX_CALLS * MAX_COST_PER_CALL

# The first heartbeat run (2026-10-01) was interrupted by a comment-only SSE
# stream. Its exact attempt count and token usage were never recorded, so the
# frozen worst case for all 12 configured attempts is reserved as possibly
# charged. This is a budget reserve, not observed billing.
PRIOR_INTERRUPTED_RUN_ATTEMPTS = 12
PRIOR_INTERRUPTED_RUN_RESERVE_USD = PRIOR_INTERRUPTED_RUN_ATTEMPTS * MAX_COST_PER_CALL
# This follow-up run is authorized for exactly one provider attempt.
FOLLOWUP_MAX_CALLS = 1

TRIAL_CASES: tuple[dict[str, Any], ...] = (
    {"case": "ready", "state": "ready", "delay_ms": 0, "dialog_ms": 0, "noise": 0, "expected": "ready"},
    {"case": "error", "state": "error", "delay_ms": 650, "dialog_ms": 0, "noise": 0, "expected": "error"},
    {"case": "outside_dialog", "state": "ready", "delay_ms": 1800, "dialog_ms": 550, "noise": 0, "expected": "unexpected"},
    {"case": "no_change", "state": "no-change", "delay_ms": 0, "dialog_ms": 0, "noise": 0, "expected": "timeout"},
)


class ProbeBudgetError(RuntimeError):
    """Raised before a request that could violate the probe's hard budget."""


class ProbeOutputError(RuntimeError):
    """Raised when the output directory already contains run evidence."""


def estimate_cost_usd(prompt_tokens: int, completion_tokens: int) -> float:
    if type(prompt_tokens) is not int or prompt_tokens < 0 or type(completion_tokens) is not int or completion_tokens < 0:
        raise ValueError("token counts must be non-negative integers")
    return (prompt_tokens * INPUT_USD_PER_MILLION + completion_tokens * OUTPUT_USD_PER_MILLION) / 1_000_000


def validate_probe_budget(max_calls: int = MAX_CALLS, max_case_calls: int = MAX_CASE_CALLS) -> dict[str, Any]:
    if type(max_calls) is not int or not 1 <= max_calls <= MAX_CALLS:
        raise ProbeBudgetError("max_calls_out_of_bounds")
    if type(max_case_calls) is not int or not 1 <= max_case_calls <= MAX_CASE_CALLS:
        raise ProbeBudgetError("max_case_calls_out_of_bounds")
    reserve = max_calls * MAX_COST_PER_CALL
    if MAX_CALLS != len(TRIAL_CASES) * MAX_CASE_CALLS or MAX_TOKENS > 128 or reserve >= MAX_COST_USD:
        raise ProbeBudgetError("configured_cost_ceiling_invalid")
    if PRIOR_INTERRUPTED_RUN_ATTEMPTS != MAX_CALLS:
        raise ProbeBudgetError("prior_run_reserve_out_of_sync")
    return {"max_provider_attempts": max_calls, "max_attempts_per_case": max_case_calls,
            "max_tokens": MAX_TOKENS, "minimum_request_start_gap_seconds": MIN_REQUEST_GAP_SECONDS,
            "max_input_tokens_per_call": MAX_INPUT_TOKENS_PER_CALL,
            "peak_input_usd_per_million": INPUT_USD_PER_MILLION,
            "peak_output_usd_per_million": OUTPUT_USD_PER_MILLION,
            "max_cost_per_attempt_usd": round(MAX_COST_PER_CALL, 9),
            "max_total_reserved_cost_usd": round(reserve, 9),
            "authorized_cap_usd": MAX_COST_USD,
            "remaining_margin_usd": round(MAX_COST_USD - reserve, 9)}


class BudgetedReader:
    """Wrap VisionReader with a global/case call cap and conservative cost bound."""

    def __init__(self, reader: Any, *, max_calls: int = MAX_CALLS,
                 max_case_calls: int = MAX_CASE_CALLS,
                 on_attempt_event: Callable[[str, dict[str, Any]], None] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleeper: Callable[[float], None] = time.sleep):
        if type(max_calls) is not int or not 1 <= max_calls <= MAX_CALLS:
            raise ValueError("max_calls_out_of_bounds")
        if type(max_case_calls) is not int or not 1 <= max_case_calls <= MAX_CASE_CALLS:
            raise ValueError("max_case_calls_out_of_bounds")
        self.reader = reader
        self.max_calls = max_calls
        self.max_case_calls = max_case_calls
        self.on_attempt_event = on_attempt_event
        self.clock = clock
        self.sleeper = sleeper
        self.attempts: list[dict[str, Any]] = []
        self.case_name: str | None = None
        self.case_attempts = 0
        self.wait_deadline: float | None = None
        self.deadline_blocked = False
        self.last_start: float | None = None
        self.rate_limited = False
        self.stop_reason: str | None = None
        self.actual_upper_cost_usd = 0.0

    def begin_case(self, name: str) -> None:
        self.case_name = name
        self.case_attempts = 0
        self.wait_deadline = None
        self.deadline_blocked = False

    def set_wait_deadline(self, deadline: float) -> None:
        if not math.isfinite(deadline):
            raise ValueError("wait deadline must be finite")
        self.wait_deadline = deadline

    def capabilities(self) -> dict[str, Any]:
        return self.reader.capabilities()

    def _validate_image_and_prompt(self, image_bytes: bytes, questions: list[dict[str, Any]]) -> tuple[int, int]:
        if not isinstance(image_bytes, bytes) or not image_bytes or len(image_bytes) > MAX_IMAGE_BYTES:
            raise ProbeBudgetError("image_bytes_out_of_bounds")
        try:
            prompt = _question_prompt(questions)
            # The provider receives this text within a JSON request; count its
            # fully escaped form as an intentionally conservative text bound.
            prompt_len = len(json.dumps(prompt, ensure_ascii=True, separators=(",", ":")).encode("ascii"))
        except (UnicodeEncodeError, ValueError, TypeError, KeyError) as exc:
            raise ProbeBudgetError("prompt_not_ascii_or_invalid") from exc
        if prompt_len > PROMPT_ASCII_LIMIT:
            raise ProbeBudgetError("prompt_too_long")
        try:
            from PIL import Image
            import io
            with Image.open(io.BytesIO(image_bytes)) as opened:
                if opened.format not in {"PNG", "JPEG", "GIF", "WEBP"}:
                    raise ProbeBudgetError("unsupported_image_format")
                width, height = opened.size
        except ProbeBudgetError:
            raise
        except Exception as exc:
            raise ProbeBudgetError("invalid_image") from exc
        if width < 1 or height < 1 or max(width, height) > MAX_IMAGE_DIMENSION:
            raise ProbeBudgetError("image_dimensions_out_of_bounds")
        estimated_input_tokens = IMAGE_TOKEN_LIMIT + prompt_len * PROMPT_TOKEN_FACTOR + PROMPT_OVERHEAD_TOKENS
        if estimated_input_tokens > MAX_INPUT_TOKENS_PER_CALL:
            raise ProbeBudgetError("estimated_input_token_bound_exceeded")
        return estimated_input_tokens, prompt_len

    def interpret(self, image_bytes: bytes, questions: list[dict[str, Any]]) -> ReaderResult:
        estimated_input_tokens, prompt_len = self._validate_image_and_prompt(image_bytes, questions)
        if self.stop_reason:
            raise ProbeBudgetError(self.stop_reason)
        if self.case_name is None:
            raise ProbeBudgetError("case_not_started")
        if len(self.attempts) >= self.max_calls:
            self.stop_reason = "global_attempt_cap_reached"
            raise ProbeBudgetError(self.stop_reason)
        if self.case_attempts >= self.max_case_calls:
            self.stop_reason = "case_attempt_cap_reached"
            raise ProbeBudgetError(self.stop_reason)
        if self.last_start is not None:
            remaining_gap = MIN_REQUEST_GAP_SECONDS - (self.clock() - self.last_start)
            if remaining_gap > 0:
                self.sleeper(remaining_gap)
        if self.wait_deadline is not None and self.clock() >= self.wait_deadline:
            # The spacing wait may outlive the watcher deadline. Do not count
            # or start a provider attempt after that deadline.
            self.deadline_blocked = True
            raise ProbeBudgetError("wait_deadline_reached")
        if MAX_COST_RESERVE > MAX_COST_USD:
            self.stop_reason = "reserved_cost_cap_exceeded"
            raise ProbeBudgetError(self.stop_reason)

        started = self.clock()
        if self.wait_deadline is not None and started >= self.wait_deadline:
            self.deadline_blocked = True
            raise ProbeBudgetError("wait_deadline_reached")
        self.last_start = started
        self.case_attempts += 1
        record: dict[str, Any] = {"attempt": len(self.attempts) + 1, "case": self.case_name,
                                  "request_start_monotonic": started, "prompt_ascii_chars": prompt_len,
                                  "estimated_input_token_ceiling": estimated_input_tokens,
                                  "max_output_tokens": MAX_TOKENS, "status": "in_flight"}
        self.attempts.append(record)
        if self.on_attempt_event is not None:
            self.on_attempt_event("attempt_started", dict(record))
        try:
            result = self.reader.interpret(image_bytes, questions)
        except Exception as exc:
            record.update({"status": "transport_exception", "error_code": type(exc).__name__,
                           "elapsed_seconds": max(0.0, self.clock() - started), "usage": None,
                           "cost_upper_usd": None})
            if self.on_attempt_event is not None:
                self.on_attempt_event("attempt_finished", dict(record))
            return ReaderResult("error", error="reader_transport_error", provider="deepseek", model=MODEL)

        usage = {key: value for key, value in result.usage.items()
                 if key in {"prompt_tokens", "completion_tokens", "total_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"}
                 and type(value) is int and value >= 0}
        prompt_tokens = usage.get("prompt_tokens")
        if prompt_tokens is None and ("prompt_cache_hit_tokens" in usage or "prompt_cache_miss_tokens" in usage):
            prompt_tokens = usage.get("prompt_cache_hit_tokens", 0) + usage.get("prompt_cache_miss_tokens", 0)
        completion_tokens = usage.get("completion_tokens")
        cost_upper = (estimate_cost_usd(prompt_tokens, completion_tokens)
                      if prompt_tokens is not None and completion_tokens is not None else None)
        record.update({"status": result.status, "error_code": result.error,
                       "provider": result.provider or "deepseek", "requested_model": result.model or MODEL,
                       "served_model": result.served_model, "usage": usage if usage else None,
                       "cost_basis": "peak_cache_miss_upper_bound" if cost_upper is not None else None,
                       "cost_upper_usd": round(cost_upper, 9) if cost_upper is not None else None,
                       "elapsed_seconds": max(0.0, self.clock() - started),
                       "latency_ms": result.latency_ms})
        if result.error == "http_status_429":
            self.rate_limited = True
            self.stop_reason = "rate_limited_429"
        if prompt_tokens is not None and prompt_tokens > estimated_input_tokens:
            record["usage_anomaly"] = "input_usage_exceeded_reserved_bound"
            self.stop_reason = "usage_exceeded_estimate_bound"
        if completion_tokens is not None and completion_tokens > MAX_TOKENS:
            record["usage_anomaly"] = "output_usage_exceeded_configured_max_tokens"
            self.stop_reason = "usage_exceeded_estimate_bound"
        if cost_upper is not None:
            self.actual_upper_cost_usd += cost_upper
            if self.actual_upper_cost_usd > MAX_COST_USD:
                record["usage_anomaly"] = "observed_upper_cost_exceeded_authorized_cap"
                self.stop_reason = "actual_usage_cost_exceeded_cap"
        if self.on_attempt_event is not None:
            self.on_attempt_event("attempt_finished", dict(record))
        return result


def _post_arm(port: int, trial: str) -> None:
    request = Request(f"http://127.0.0.1:{port}/arm?trial={trial}", data=b"", method="POST")
    with urlopen(request, timeout=3) as response:
        if response.status != 204:
            raise RuntimeError("fixture_arm_failed")


def _case_result_events(server: Any, trial: str) -> list[dict[str, Any]]:
    return [{"kind": e.get("kind"), "received_monotonic": e.get("received_monotonic"), "page_ms": e.get("page_ms")}
            for e in server.snapshot(trial)]


def _run_case(server: Any, case: dict[str, Any], output: Path, budget_reader: BudgetedReader, *,
              deadline_seconds: float = 2.5, debounce_seconds: float = 0.25,
              on_case_event: Callable[[str, dict[str, Any]], None] | None = None) -> dict[str, Any]:
    from wait_benchmark import _url, _wait_loaded, _status_tone
    from kwin_mcp.core import AutomationEngine

    trial = uuid.uuid4().hex
    profile = Path(tempfile.mkdtemp(prefix="jev-vision-wait-profile-"))
    engine = None
    session_attempted = False
    row: dict[str, Any] = {"case": case["case"], "trial": trial, "expected": case["expected"],
                           "status": "failed", "cleanup": "pending"}
    title = "Jev local wait fixture"
    try:
        engine = AutomationEngine()
        if on_case_event is not None:
            on_case_event("case_started", {"case": case["case"], "trial": trial})
        command = " ".join(("firefox", "--no-remote", "--new-instance", "--profile", shlex.quote(str(profile)),
                            shlex.quote(_url(server, case, trial))))
        # Mark attempted before the call: a partial or interrupted start may
        # have created owned resources that session_stop must clean up.
        session_attempted = True
        started = engine.session_start(app_command=command, screen_width=1280, screen_height=800,
                                       isolate_home=True, keep_home=False, keep_screenshots=False,
                                       env={"MOZ_ENABLE_ACCESSIBILITY": "1"})
        if "Input backend: KWin EIS" not in started:
            raise RuntimeError("kwin_eis_unavailable")
        adapter = ObservationAdapter(engine, trial, allowed_apps={"firefox"})
        capture = AdapterFrameSource(adapter, "firefox", title=title)
        loaded_at = _wait_loaded(server, trial)
        baseline = capture()
        if _status_tone(baseline.image) != "loading":
            raise RuntimeError("fixture_baseline_not_loading")
        _post_arm(server.server_port, trial)
        budget_reader.begin_case(case["case"])
        expected_text = ("The fixture shows a green ready state with forecast text." if case["expected"] == "ready" else
                         "The fixture shows an application error state." if case["expected"] == "error" else
                         "The fixture shows a ready state without an unexpected dialog." if case["expected"] == "unexpected" else
                         "The loading state remains pending with no ready result or dialog.")
        spec = WaitSpec(expected_text, {"x": 20, "y": 96, "width": 920, "height": 480})
        wait_start = time.monotonic()
        wait_deadline = wait_start + deadline_seconds
        budget_reader.set_wait_deadline(wait_deadline)
        wait_result = WaitWatcher(capture, ReaderJudge(budget_reader), interval_seconds=0.1,
                                  debounce_seconds=debounce_seconds).wait(spec, deadline_seconds=deadline_seconds)
        decision_at = time.monotonic()
        events = _case_result_events(server, trial)
        truth_kind = {"ready": "ready", "error": "error", "unexpected": "dialog", "timeout": None}[case["expected"]]
        truth = next((event for event in events if event["kind"] == truth_kind), None) if truth_kind else None
        normalized = wait_result.status
        if budget_reader.deadline_blocked:
            normalized = "timeout"
        if budget_reader.stop_reason in {"case_attempt_cap_reached", "global_attempt_cap_reached",
                                        "usage_exceeded_estimate_bound", "actual_usage_cost_exceeded_cap"}:
            normalized = "unknown"
        observed_at = decision_at if normalized in {"ready", "error", "unexpected"} else None
        row.update({"status": "completed", "observed": normalized,
                    "success": normalized == case["expected"] if normalized != "unknown" else None,
                    "loaded_monotonic": loaded_at, "wait_started_monotonic": wait_start,
                    "ground_truth_event": truth, "decision_monotonic": decision_at,
                    "detection_delay_ms": round((observed_at - truth["received_monotonic"]) * 1000, 2)
                    if observed_at is not None and truth is not None else None,
                    "false_wake": bool(observed_at is not None and truth is None),
                    "missed_event": bool(truth is not None and observed_at is None),
                    "wait": {"status": wait_result.status, "evidence_capture_id": wait_result.evidence.capture_id if wait_result.evidence else None,
                             "evidence_captured_monotonic": wait_result.evidence.captured_at if wait_result.evidence else None,
                             "elapsed_seconds": wait_result.elapsed_seconds, "captures": wait_result.captures,
                             "capture_errors": wait_result.capture_errors, "judgments": wait_result.judgments,
                             "coalesced_frames": wait_result.coalesced_frames,
                             "capture_seconds": wait_result.capture_seconds, "judge_seconds": wait_result.judge_seconds,
                             "error_code": wait_result.error_code},
                    "fixture_events": events,
                    "attempts": [entry for entry in budget_reader.attempts if entry.get("case") == case["case"]],
                    "cleanup": "pending"})
    except Exception as exc:
        # Record only safe exception classes and stable underscore codes.
        message = str(exc)
        row["error"] = message if message and message.replace("_", "").isalnum() else type(exc).__name__
        row["fixture_events"] = _case_result_events(server, trial)
        row["attempts"] = [entry for entry in budget_reader.attempts if entry.get("case") == case["case"]]
    finally:
        if session_attempted and engine is not None:
            try:
                stop = engine.session_stop()
                row["cleanup"] = "passed" if isinstance(stop, str) and "Session stopped" in stop else "unconfirmed"
            except Exception as exc:
                row["cleanup"] = f"failed:{type(exc).__name__}"
        else:
            row["cleanup"] = "not_started"
        try:
            shutil.rmtree(profile)
            row["profile_cleanup"] = "passed"
        except FileNotFoundError:
            row["profile_cleanup"] = "passed"
        except Exception as exc:
            row["profile_cleanup"] = f"failed:{type(exc).__name__}"
        if on_case_event is not None:
            on_case_event("case_finished", {"case": case["case"], "trial": trial,
                                             "status": row.get("status"), "cleanup": row.get("cleanup"),
                                             "profile_cleanup": row.get("profile_cleanup")})
    return row


def run(output: Path = OUT, *, seed: int = 41,
        reader_factory: Callable[[], Any] | None = None,
        server_factory: Callable[[], Any] | None = None) -> dict[str, Any]:
    # The first interrupted invocation reserves all 12 attempts. This follow-up
    # is limited to one provider call, keeping the session ceiling at 13 calls.
    max_calls = max_case_calls = FOLLOWUP_MAX_CALLS
    selected_cases = (TRIAL_CASES[0],)
    deadline_seconds = 40.0
    debounce_seconds = 0.0
    budget = validate_probe_budget(max_calls, max_case_calls)
    output.mkdir(parents=True, exist_ok=True)
    # Never truncate or overwrite existing evidence (including the paid
    # follow-up result). Each run needs a fresh output directory.
    if any(output.iterdir()):
        raise ProbeOutputError("output_directory_not_empty")
    journal = output / "progress.jsonl"
    journal.write_text("", encoding="utf-8")

    def journal_event(event: str, payload: dict[str, Any]) -> None:
        line = {"event": event, "recorded_monotonic": time.monotonic(), **payload}
        with journal.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(line, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    journal_event("run_started", {"max_provider_attempts": max_calls, "reserved_cost_usd": budget["max_total_reserved_cost_usd"],
                                   "selected_cases": [case["case"] for case in selected_cases]})
    if not os.environ.get("DEEPSEEK_API_KEY") and reader_factory is None:
        result = {"status": "blocked", "error": "missing_api_key", "budget": budget, "paid_provider_attempts": 0}
        (output / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return result
    if reader_factory is None:
        config = ReaderConfig(provider="deepseek", base_url=BASE_URL, model=MODEL,
                              key_env="DEEPSEEK_API_KEY", timeout_seconds=5, total_timeout_seconds=30,
                              max_tokens=MAX_TOKENS, max_response_chars=8192)
        reader_factory = lambda: VisionReader(config)
    reader = None
    server = None
    server_started = False
    rows: list[dict[str, Any]] = []
    interrupted = False
    try:
        reader = reader_factory()
        budget_reader = BudgetedReader(reader, max_calls=max_calls, max_case_calls=max_case_calls,
                                       on_attempt_event=journal_event)
        from wait_benchmark import _versions
        versions = _versions()
        if server_factory is None:
            from wait_benchmark import FixtureServer
            server_factory = FixtureServer
        server = server_factory()
        server.start()
        server_started = True
        shuffled = list(selected_cases)
        random.Random(seed).shuffle(shuffled)
        try:
            for case in shuffled:
                if budget_reader.stop_reason or len(budget_reader.attempts) >= max_calls:
                    break
                row = _run_case(server, case, output, budget_reader, deadline_seconds=deadline_seconds,
                                debounce_seconds=debounce_seconds, on_case_event=journal_event)
                rows.append(row)
                if budget_reader.rate_limited or budget_reader.stop_reason:
                    break
        except KeyboardInterrupt:
            interrupted = True
            journal_event("run_interrupted", {"reason": "keyboard_interrupt"})
    finally:
        try:
            if server is not None and server_started:
                try:
                    server.shutdown()
                finally:
                    try:
                        server.server_close()
                    finally:
                        thread = getattr(server, "thread", None)
                        if thread is not None:
                            thread.join(timeout=3)
            elif server is not None:
                # start() never completed; close what we can without a
                # shutdown() that would wait forever on a non-running server.
                close_server = getattr(server, "server_close", None)
                if callable(close_server):
                    try:
                        close_server()
                    except Exception:
                        pass
        finally:
            if reader is not None:
                close = getattr(reader, "close", None)
                if callable(close):
                    close()

    attempts = budget_reader.attempts
    complete_costs = [entry["cost_upper_usd"] for entry in attempts
                      if isinstance(entry.get("cost_upper_usd"), (int, float))]
    usage_missing = len(attempts) - len(complete_costs)
    execution_completed = len(rows) == len(shuffled) and all(row.get("status") == "completed" for row in rows)
    result = {"type": "vision_wait_probe", "status": "interrupted" if interrupted else ("completed" if execution_completed else ("stopped" if rows else "blocked")),
              "seed": seed, "randomized_case_order": [case["case"] for case in shuffled],
              "requested_model": MODEL, "provider": "deepseek", "base_url": BASE_URL,
              "platform_versions": versions, "progress_journal": str(journal),
              "paid_provider_attempts": len(attempts), "rate_limited": budget_reader.rate_limited,
              "stop_reason": budget_reader.stop_reason,
              "budget": {**budget, "prior_interrupted_run_reserve_usd": PRIOR_INTERRUPTED_RUN_RESERVE_USD,
                         "whole_session_conservative_reserve_usd": round(PRIOR_INTERRUPTED_RUN_RESERVE_USD + budget["max_total_reserved_cost_usd"], 9)},
              "observed_upper_cost_usd": round(sum(complete_costs), 9) if usage_missing == 0 else None,
              "attempt_cost_upper_reserve_usd": round(len(attempts) * MAX_COST_PER_CALL, 9),
              "attempts_with_unreported_usage": usage_missing, "actual_usage_cost_complete": usage_missing == 0,
              "primary_agent_turns": None, "primary_agent_tokens": None,
              "method": "local Firefox virtual session, full app screenshot, DeepSeek ReaderJudge, independent loopback fixture events",
              "cases": rows}
    result["execution_completed"] = execution_completed and not interrupted
    result["all_case_results_match"] = bool(rows) and all(row.get("success") is True for row in rows)
    result["cleanup_passed"] = (len(rows) == len(shuffled)
                                 and all(row.get("cleanup") == "passed" and row.get("profile_cleanup") == "passed" for row in rows))
    result["acceptance_passed"] = (result["execution_completed"] and result["all_case_results_match"]
                                   and result["cleanup_passed"] and not budget_reader.stop_reason
                                   and not budget_reader.rate_limited)
    (output / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUT)
    parser.add_argument("--seed", type=int, default=41)
    args = parser.parse_args(argv)
    try:
        result = run(args.output, seed=args.seed)
    except ProbeOutputError as exc:
        print(json.dumps({"status": "refused", "error": str(exc), "output": str(args.output)}, sort_keys=True))
        return 2
    print(json.dumps({"status": result.get("status"), "paid_provider_attempts": result.get("paid_provider_attempts", 0),
                      "rate_limited": result.get("rate_limited", False), "stop_reason": result.get("stop_reason"),
                      "execution_completed": result.get("execution_completed", False),
                      "all_case_results_match": result.get("all_case_results_match", False),
                      "cleanup_passed": result.get("cleanup_passed", False),
                      "acceptance_passed": result.get("acceptance_passed", False),
                      "observed_upper_cost_usd": result.get("observed_upper_cost_usd"),
                      "output": str(args.output / "result.json")}, sort_keys=True))
    return 0 if result.get("acceptance_passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
