#!/usr/bin/env python3
"""Compare fixed screenshot polling with the local M4 watcher.

This is a bounded synthetic local-image benchmark, not a vision/provider or
primary-agent A/B test. Browser event POSTs are ground truth only and never
enter the wait judge. Run on cachy with ``uv run wait_benchmark.py``.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import random
import shlex
import shutil
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from benchmark_capture import _atspi_version, _version
from observation import ObservationAdapter

ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / "benchmark_fixtures" / "wait.html"
OUT = ROOT / "run" / "wait-benchmark"
SCREEN = (1280, 800)
INTERVAL = 0.20
DEADLINE = 5.0
POLL = 0.20
CASES: tuple[dict[str, Any], ...] = (
    {"case": "loading_ready", "state": "ready", "delay_ms": 1100, "noise": False, "dialog_ms": 0, "expected": "ready"},
    {"case": "loading_error", "state": "error", "delay_ms": 1100, "noise": False, "dialog_ms": 0, "expected": "error"},
    {"case": "no_change", "state": "no-change", "delay_ms": 0, "noise": False, "dialog_ms": 0, "expected": "timeout"},
    {"case": "animation_noise", "state": "ready", "delay_ms": 1400, "noise": True, "dialog_ms": 0, "expected": "ready"},
    {"case": "outside_dialog", "state": "ready", "delay_ms": 1800, "noise": False, "dialog_ms": 700, "expected": "unexpected"},
)


class FixtureServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        self.events: dict[str, list[dict[str, Any]]] = {}
        self.event_lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlsplit(self.path)
                if parsed.path == "/armed":
                    from urllib.parse import parse_qs
                    values = parse_qs(parsed.query).get("trial", [])
                    trial = values[0] if values else ""
                    with owner.event_lock:
                        armed = any(event["kind"] == "arm" for event in owner.events.get(trial, []))
                    self.send_response(204 if armed else 202)
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    return
                if parsed.path == "/wait.html":
                    body = FIXTURE.read_text(encoding="utf-8")
                    payload = body.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                elif parsed.path == "/favicon.ico":
                    self.send_response(204)
                    self.end_headers()
                else:
                    self.send_error(404)

            def do_POST(self) -> None:  # noqa: N802
                parsed = urlsplit(self.path)
                if parsed.path == "/arm":
                    from urllib.parse import parse_qs
                    trial_values = parse_qs(parsed.query).get("trial", [])
                    trial = trial_values[0] if trial_values else ""
                    if not trial or len(trial) > 80:
                        self.send_error(400)
                        return
                    with owner.event_lock:
                        owner.events.setdefault(trial, []).append({"kind": "arm", "received_monotonic": time.monotonic(), "page_ms": None})
                    self.send_response(204)
                    self.end_headers()
                    return
                if parsed.path != "/event":
                    self.send_error(404)
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(min(size, 2048)))
                    trial, kind = payload.get("trial"), payload.get("kind")
                    if not isinstance(trial, str) or not isinstance(kind, str) or len(trial) > 80 or len(kind) > 20:
                        raise ValueError
                except (ValueError, TypeError, json.JSONDecodeError):
                    self.send_error(400)
                    return
                record = {"kind": kind, "received_monotonic": time.monotonic(),
                          "page_ms": payload.get("page_ms") if isinstance(payload.get("page_ms"), (int, float)) else None}
                with owner.event_lock:
                    owner.events.setdefault(trial, []).append(record)
                self.send_response(204)
                self.end_headers()

        super().__init__(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.serve_forever, name="wait-fixture-http", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def snapshot(self, trial: str) -> list[dict[str, Any]]:
        with self.event_lock:
            return list(self.events.get(trial, []))


def _versions() -> dict[str, str]:
    return {"kwin_mcp": importlib.metadata.version("kwin-mcp"),
            "kwin": _version(["kwin_wayland", "--version"]),
            "firefox": _version(["firefox", "--version"]), "atspi": _atspi_version()}


def _url(server: FixtureServer, case: dict[str, Any], trial: str) -> str:
    from urllib.parse import urlencode
    values = {"trial": trial, "state": case["state"], "delay": case["delay_ms"],
              "dialog": case["dialog_ms"], "noise": int(case["noise"])}
    return f"http://127.0.0.1:{server.server_port}/wait.html?{urlencode(values)}"


def _changed_ratio(base: bytes, current: bytes, *, outside: bool) -> float:
    from PIL import Image, ImageChops
    import io
    with Image.open(io.BytesIO(base)) as a, Image.open(io.BytesIO(current)) as b:
        a, b = a.convert("RGB"), b.convert("RGB")
        if a.size != b.size:
            return 1.0
        width, height = a.size
        # Fixed fixture coordinates normalized to the full Firefox app capture.
        if outside:
            rect = (int(width * .76), int(height * .13), int(width * .99), int(height * .90))
        else:
            rect = (int(width * .015), int(height * .12), int(width * .74), int(height * .72))
        delta = ImageChops.difference(a.crop(rect), b.crop(rect)).convert("L")
        # Ignore text antialiasing and the fixture's small animated indicator.
        mask = delta.point(lambda value: 255 if value > 10 else 0)
        histogram = mask.histogram()
        return histogram[255] / max(1, mask.width * mask.height)


def _status_tone(payload: bytes) -> str:
    from PIL import Image
    import io
    with Image.open(io.BytesIO(payload)) as opened:
        image = opened.convert("RGB")
        width, height = image.size
        sample = image.crop((int(width * .17), int(height * .28),
                             int(width * .48), int(height * .48))).resize((1, 1))
        red, green, blue = sample.getpixel((0, 0))
    if blue < 90 and red > 160 and green > 100:
        return "loading"
    if green - red > 35:
        return "ready"
    if red - green > 35:
        return "error"
    return "unknown"


def _normalized_status(status: str) -> str:
    return status if status in {"ready", "error", "unexpected"} else "timeout"


def _make_judge(baseline: bytes, counters: dict[str, Any]) -> Callable[[Any, Any], str]:
    from PIL import Image
    import io

    def judge(frame: Any, _spec: Any) -> str:
        started = time.perf_counter()
        counters["local_judgments"] += 1
        try:
            payload = frame.image
            if _changed_ratio(baseline, payload, outside=True) > 0.015:
                return "unexpected"
            if _changed_ratio(baseline, payload, outside=False) > 0.012:
                return _status_tone(payload) if _status_tone(payload) == "error" else "wake"
            return "wait"
        finally:
            counters["judgment_ms"] += (time.perf_counter() - started) * 1000
    return judge


def _wait_loaded(server: FixtureServer, trial: str, timeout: float = 12) -> float:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events = server.snapshot(trial)
        loaded = next((e["received_monotonic"] for e in events if e["kind"] == "loaded"), None)
        if loaded is not None:
            return loaded
        time.sleep(.03)
    raise RuntimeError("fixture_load_timeout")


def _firefox_command(profile: Path, url: str) -> str:
    return " ".join(("firefox", "--no-remote", "--new-instance", "--profile", shlex.quote(str(profile)), shlex.quote(url)))


def _run_trial(server: FixtureServer, case: dict[str, Any], arm: str,
               trial_number: int, order_index: int) -> dict[str, Any]:
    from kwin_mcp.core import AutomationEngine
    from wait_watcher import WaitFrame, WaitSpec, WaitWatcher

    trial = uuid.uuid4().hex
    profile = Path(tempfile.mkdtemp(prefix="jev-wait-profile-"))
    engine = AutomationEngine()
    session_attempted = False
    trial_start = time.monotonic()
    counters: dict[str, Any] = {"capture_calls": 0, "local_judgments": 0, "judgment_ms": 0.0,
                                "paid_backend_requests": 0, "primary_turns": None, "primary_tokens": None}
    events: list[dict[str, Any]] = []
    result: dict[str, Any] = {"type": "trial", "trial": trial, "trial_number": trial_number,
        "order_index": order_index, "case": case["case"], "arm": arm,
        "reset_marker": {"private_profile": str(profile), "unique_trial_id": trial, "initial_state": "loading"},
        "status": "failed", "ground_truth_source": "loopback_browser_event_post_server_monotonic"}
    try:
        start_text = engine.session_start(app_command=_firefox_command(profile, _url(server, case, trial)),
            screen_width=SCREEN[0], screen_height=SCREEN[1], isolate_home=True, keep_home=False,
            keep_screenshots=False, env={"MOZ_ENABLE_ACCESSIBILITY": "1"})
        session_attempted = True
        if "Input backend: KWin EIS" not in start_text:
            raise RuntimeError("kwin_eis_unavailable")
        adapter = ObservationAdapter(engine, trial, allowed_apps={"firefox"})
        capture_ms = 0.0

        def capture() -> WaitFrame:
            nonlocal capture_ms
            started = time.perf_counter()
            ref = adapter.capture("firefox", title="Jev local wait fixture", scope="app")
            observation = adapter.observe(ref, mode="image")
            if observation.image is None:
                raise RuntimeError("image_capture_missing")
            capture_ms += (time.perf_counter() - started) * 1000
            counters["capture_calls"] += 1
            return WaitFrame(image=observation.image, capture_id=ref.capture_id, captured_at=time.monotonic())

        loaded_at = _wait_loaded(server, trial)
        baseline_frame = capture()
        if _status_tone(baseline_frame.image) != "loading":
            raise RuntimeError("fixture_initial_state_not_loading")
        from urllib.request import Request, urlopen
        arm_request = Request(f"http://127.0.0.1:{server.server_port}/arm?trial={trial}", data=b"", method="POST")
        with urlopen(arm_request, timeout=2) as response:
            if response.status != 204:
                raise RuntimeError("fixture_arm_failed")
        arm_at = next(e["received_monotonic"] for e in server.snapshot(trial) if e["kind"] == "arm")
        judge = _make_judge(baseline_frame.image, counters)
        wait_start = arm_at
        if arm == "polling":
            deadline = wait_start + DEADLINE
            observed = "timeout"
            wake_at = None
            evidence_at = None
            while time.monotonic() < deadline:
                time.sleep(min(POLL, max(0, deadline - time.monotonic())))
                frame = capture()
                verdict = judge(frame, None)
                if verdict == "unexpected":
                    observed = verdict
                    evidence_at = frame.captured_at
                    wake_at = time.monotonic()
                    break
                if verdict in {"wake", "error"}:
                    observed = "ready" if verdict == "wake" else "error"
                    evidence_at = frame.captured_at
                    wake_at = time.monotonic()
                    break
            wait_result: dict[str, Any] = {"status": observed,
                "elapsed_seconds": (wake_at or time.monotonic()) - wait_start,
                "capture_count": counters["capture_calls"] - 1, "judgment_count": counters["local_judgments"],
                "decision_monotonic": wake_at, "evidence_captured_monotonic": evidence_at,
                "watcher_timing": None}
        else:
            watcher = WaitWatcher(capture=capture, judge=judge, interval_seconds=INTERVAL,
                                  debounce_seconds=0.12)
            spec = WaitSpec(expected="local synthetic status transition",
                            region={"x": 20, "y": 96, "width": 920, "height": 480})
            wait_object = watcher.wait(spec, deadline_seconds=DEADLINE)
            evidence_meta = ({"capture_id": wait_object.evidence.capture_id,
                              "captured_at": wait_object.evidence.captured_at} if wait_object.evidence else None)
            wait_result = {"status": wait_object.status, "evidence": evidence_meta,
                "elapsed_seconds": wait_object.elapsed_seconds, "captures": wait_object.captures,
                "capture_errors": wait_object.capture_errors, "judgments": wait_object.judgments,
                "coalesced_frames": wait_object.coalesced_frames,
                "capture_seconds": wait_object.capture_seconds, "judge_seconds": wait_object.judge_seconds,
                "error_code": wait_object.error_code}
            status = wait_result.get("status", "unknown")
            evidence = wait_object.evidence
            evidence_at = evidence.captured_at if evidence is not None else None
            wake_at = time.monotonic() if status in {"ready", "unexpected", "error"} else None
            wait_result["decision_monotonic"] = wake_at
            wait_result["evidence_captured_monotonic"] = evidence_at
            counters["local_judgments"] = wait_result.get("judgments", 0)
            counters["judgment_ms"] = float(wait_result.get("judge_seconds", 0)) * 1000
        events = server.snapshot(trial)
        actual_transition = next((e for e in events if e["kind"] in {"ready", "error"}), None)
        actual_dialog = next((e for e in events if e["kind"] == "dialog"), None)
        observed_status = wait_result.get("status")
        normalized = _normalized_status(observed_status)
        transition = actual_dialog if case["expected"] == "unexpected" else actual_transition
        ground_truth_at = transition["received_monotonic"] if transition else None
        observed_at = wake_at if isinstance(wake_at, (int, float)) else None
        result.update({"status": "passed", "expected": case["expected"], "observed": normalized,
            "success": normalized == case["expected"], "false_wake": bool(normalized in {"ready", "error", "unexpected"} and ground_truth_at is None),
            "missed_deadline": bool(ground_truth_at is not None and (observed_at is None or observed_at >= wait_start + DEADLINE)),
            "loaded_monotonic": loaded_at, "arm_monotonic": arm_at, "wait_started_monotonic": wait_start,
            "decision_monotonic": observed_at, "evidence_captured_monotonic": evidence_at,
            "ground_truth_transition_monotonic": ground_truth_at,
            "detection_delay_ms": round((observed_at - ground_truth_at) * 1000, 2) if observed_at and ground_truth_at else None,
            "elapsed_ms": round(float(wait_result.get("elapsed_seconds", 0)) * 1000, 2),
            "capture_ms": round(capture_ms if arm == "polling" else float(wait_result.get("capture_seconds", 0)) * 1000, 2), "counts": counters,
            "wait_result": wait_result, "event_log": events,
            "versions": _versions(), "cleanup": "pending"})
    except BaseException as exc:
        result["error"] = type(exc).__name__ if not isinstance(exc, RuntimeError) or not str(exc).replace("_", "").isalnum() else str(exc)
        result["event_log"] = server.snapshot(trial)
    finally:
        if session_attempted:
            try:
                stop = engine.session_stop()
                result["cleanup"] = "passed" if isinstance(stop, str) and "Session stopped" in stop else "unconfirmed"
            except Exception as exc:
                result["cleanup"] = f"failed:{type(exc).__name__}"
        else:
            result["cleanup"] = "not_started"
        try:
            shutil.rmtree(profile)
            result["profile_cleanup"] = "passed"
        except FileNotFoundError:
            result["profile_cleanup"] = "passed"
        except Exception as exc:
            result["profile_cleanup"] = f"failed:{type(exc).__name__}"
    return result


def run(output: Path = OUT, *, seed: int = 17, repetitions: int = 1,
        max_trials: int = 10, cases: tuple[dict[str, Any], ...] = CASES) -> list[dict[str, Any]]:
    if not 1 <= repetitions <= 5 or not 1 <= max_trials <= 50:
        raise ValueError("repetitions must be 1..5 and max_trials 1..50")
    plans = make_plan(seed, repetitions, max_trials, cases)
    output.mkdir(parents=True, exist_ok=True)
    manifest = output / "manifest.jsonl"
    rows: list[dict[str, Any]] = []
    server = FixtureServer()
    server.start()
    try:
        with manifest.open("w", encoding="utf-8") as stream:
            metadata = {"type": "run", "seed": seed, "repetitions": repetitions,
                "planned_trials": len(plans), "randomized_order": [{"case": c["case"], "arm": a, "repetition": r} for r, c, a in plans],
                "created_monotonic": time.monotonic(), "versions": _versions(),
                "method": "local screenshot diff only; no OCR, Jev, vision provider, or primary-agent calls",
                "primary_turns": None, "primary_tokens": None, "paid_backend_requests": 0}
            stream.write(json.dumps(metadata, sort_keys=True) + "\n")
            stream.flush()
            for index, (rep, case, arm) in enumerate(plans, 1):
                row = _run_trial(server, case, arm, rep, index)
                row["repetition"] = rep
                rows.append(row)
                stream.write(json.dumps(row, sort_keys=True) + "\n")
                stream.flush()
    finally:
        server.shutdown()
        server.server_close()
        server.thread.join(timeout=2)
    return rows


def make_plan(seed: int, repetitions: int, max_trials: int,
              cases: tuple[dict[str, Any], ...] = CASES) -> list[tuple[int, dict[str, Any], str]]:
    if not 1 <= repetitions <= 5 or not 1 <= max_trials <= 50:
        raise ValueError("repetitions must be 1..5 and max_trials 1..50")
    plans = [(rep + 1, dict(case), arm) for rep in range(repetitions)
             for case in cases for arm in ("polling", "watcher")]
    random.Random(seed).shuffle(plans)
    return plans[:max_trials]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUT)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--max-trials", type=int, default=10)
    args = parser.parse_args(argv)
    rows = run(args.output, seed=args.seed, repetitions=args.repetitions, max_trials=args.max_trials)
    passed = sum(r.get("status") == "passed" and r.get("success") for r in rows)
    print(json.dumps({"manifest": str(args.output / "manifest.jsonl"), "trials": len(rows),
                      "successful": passed, "cleanup_failures": sum(r.get("cleanup") != "passed" or r.get("profile_cleanup") != "passed" for r in rows)}, sort_keys=True))
    return 0 if len(rows) == args.max_trials and all(r.get("status") == "passed" and r.get("cleanup") == "passed" and r.get("profile_cleanup") == "passed" for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
