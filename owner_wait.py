"""Owner/session integration for the bounded screenshot readiness watcher."""

from __future__ import annotations

import hashlib
import math
from typing import Any

from observation import ObservationError
from wait_watcher import AdapterFrameSource, ReaderJudge, WaitSpec, WaitWatcher


MAX_WAIT_SECONDS = 120.0
CAPTURE_INTERVAL_SECONDS = 0.5
WAKE_DEBOUNCE_SECONDS = 0.25


def run_owner_wait(session: Any, expected: str, timeout_seconds: float) -> dict[str, Any]:
    """Capture/judge within one owner's existing reader and observation budgets.

    The returned image, capture metadata, ID and hash all refer to the exact
    final frame used by the watcher. Each internal frame consumes one owner
    observation. Reader interpretation uses the session's already bounded
    reader object, so calls and reported usage are shared with `observe`.
    """
    if not isinstance(expected, str) or not expected.strip() or len(expected) > 500:
        raise ValueError("invalid_params")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
        raise ValueError("invalid_params")
    timeout = float(timeout_seconds)
    if not math.isfinite(timeout) or timeout <= 0 or timeout > MAX_WAIT_SECONDS:
        raise ValueError("invalid_params")
    if session.adapter.reader is None:
        raise ValueError("reader_unavailable")
    if session.reader_calls >= session.max_reader_calls:
        raise ValueError("reader_call_budget_exceeded")
    if session.observations >= session.max_observations:
        raise ValueError("task_observation_budget_exceeded")

    reader_before = session.adapter.reader.summary()
    now = session.monotonic()
    remaining_lifetime = session.max_session_lifetime - (now - session.started_clock)
    effective_timeout = min(timeout, remaining_lifetime)
    if effective_timeout <= 0:
        raise ValueError("task_time_budget_exceeded")

    def capture_frame():
        if session.cancel.is_set():
            raise ObservationError("cancelled", "wait was cancelled")
        if session.expiring:
            raise ObservationError("session_expiring", "session is expiring")
        if session.stopping:
            raise ObservationError("session_busy", "session is stopping")
        if session.monotonic() - session.started_clock >= session.max_session_lifetime:
            raise ObservationError("task_time_budget_exceeded", "session lifetime is exhausted")
        if session.observations >= session.max_observations:
            raise ObservationError("task_observation_budget_exceeded", "session read budget is exhausted")
        frame = AdapterFrameSource(session.adapter, session.app)()
        # A completed screenshot is real in-session work. Refresh idle time as
        # the watcher proceeds so a bounded active wait is not mistaken for
        # an abandoned session.
        session.observations += 1
        if not session.cancel.is_set() and not session.expiring and not session.stopping:
            session.last_activity_clock = session.monotonic()
            from datetime import datetime, timezone
            session.last_activity_at = datetime.now(timezone.utc).isoformat()
        return frame

    watcher = WaitWatcher(capture_frame, ReaderJudge(session.adapter.reader),
                          interval_seconds=CAPTURE_INTERVAL_SECONDS,
                          debounce_seconds=WAKE_DEBOUNCE_SECONDS)
    result = watcher.wait(WaitSpec(expected), deadline_seconds=effective_timeout, cancel_event=session.cancel)
    evidence = result.evidence
    evidence_payload = None
    if evidence is not None:
        metadata = evidence.metadata
        if not isinstance(metadata, dict) or not isinstance(evidence.capture_id, str):
            raise ObservationError("capture_metadata_unavailable", "wait evidence has no same-capture metadata")
        digest = hashlib.sha256(evidence.image).hexdigest()
        if metadata.get("capture_id") != evidence.capture_id or metadata.get("image_sha256") != digest:
            raise ObservationError("capture_hash_mismatch", "wait image does not match its capture metadata")
        import base64
        evidence_payload = {"capture": metadata, "image_base64": base64.b64encode(evidence.image).decode("ascii")}
    reader_summary = session.adapter.reader.summary()
    reader_delta = {
        key: reader_summary[key] - reader_before[key]
        for key in ("calls_used", "successful_calls", "failed_calls", "latency_total_ms",
                    "provider_latency_total_ms", "owner_elapsed_total_ms")
        if key in reader_summary and key in reader_before
    }
    reader_delta["reported_usage"] = {
        key: value - reader_before["reported_usage"].get(key, 0)
        for key, value in reader_summary["reported_usage"].items()
        if isinstance(value, (int, float))
    }
    reader_delta["rate_limited"] = reader_summary["rate_limited"] and not reader_before["rate_limited"]
    return {
        "wait": {
            "status": result.status,
            "elapsed_seconds": round(result.elapsed_seconds, 3),
            "requested_timeout_seconds": timeout,
            "effective_timeout_seconds": round(effective_timeout, 3),
            "captures": result.captures,
            "capture_errors": result.capture_errors,
            "judgments": result.judgments,
            "coalesced_frames": result.coalesced_frames,
            "capture_seconds": round(result.capture_seconds, 3),
            "judge_seconds": round(result.judge_seconds, 3),
            "error_code": result.error_code,
        },
        "evidence": evidence_payload,
        "reader": {"this_wait": reader_delta, "session_total": reader_summary},
    }
