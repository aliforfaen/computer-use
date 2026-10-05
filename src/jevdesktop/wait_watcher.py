"""Bounded screenshot wait watcher reusing ObservationAdapter and Reader.

This is a local primitive, not a task planner or service endpoint. Capture
callers should use ``ObservationAdapter.capture(..., scope='app')`` followed
by ``observe(..., mode='image')`` so region-specific waits still provide the
judge the entire app window for unexpected dialogs and errors.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from jevdesktop.observation import CaptureRef, ObservationAdapter, ObservationError
from jevdesktop.vision_reader import Reader, ReaderResult

Judgment = Literal["wait", "wake", "error", "unexpected"]
WaitStatus = Literal["ready", "unexpected", "timeout", "cancelled", "error"]
_JUDGMENTS = frozenset({"wait", "wake", "error", "unexpected"})


@dataclass(frozen=True)
class WaitSpec:
    """Caller-authored wait condition; region is descriptive and never crops."""

    expected: str
    region: dict[str, int] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.expected, str) or not self.expected.strip():
            raise ValueError("expected wait condition is required")
        if self.region is not None:
            rect = self.region
            if (not isinstance(rect, dict) or any(type(rect.get(k)) is not int for k in ("x", "y", "width", "height"))
                    or rect["x"] < 0 or rect["y"] < 0 or rect["width"] <= 0 or rect["height"] <= 0):
                raise ValueError("region must be a non-empty non-negative rectangle")


@dataclass(frozen=True)
class WaitFrame:
    """One fresh complete app image plus optional source capture identity."""

    image: bytes = field(repr=False)
    capture_id: str | None = None
    captured_at: float | None = None
    metadata: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.image, bytes) or not self.image:
            raise ValueError("frame image must be non-empty bytes")
        if self.captured_at is not None and not math.isfinite(self.captured_at):
            raise ValueError("captured_at must be finite")


@dataclass(frozen=True)
class JudgeDecision:
    """A code-validated semantic judgment and optional reader accounting."""

    judgment: Judgment
    reader_result: ReaderResult | None = field(default=None, repr=False)


@dataclass(frozen=True)
class WaitResult:
    status: WaitStatus
    evidence: WaitFrame | None
    elapsed_seconds: float
    captures: int
    capture_errors: int
    judgments: int
    coalesced_frames: int
    capture_seconds: float
    judge_seconds: float
    error_code: str | None = None
    reader_usage: dict[str, Any] = field(default_factory=dict, repr=False)


class AdapterFrameSource:
    """Capture the full app window through the existing mapped adapter."""

    def __init__(self, adapter: ObservationAdapter, app: str, *, title: str | None = None):
        if not callable(getattr(adapter, "capture", None)) or not callable(getattr(adapter, "observe", None)):
            raise TypeError("adapter must provide capture and observe methods")
        self.adapter = adapter
        self.app = app
        self.title = title

    def __call__(self) -> WaitFrame:
        ref = self.adapter.capture(self.app, title=self.title, scope="app")
        observation = self.adapter.observe(ref, mode="image")
        if observation.image is None:
            raise ObservationError("image_unavailable", "app image was unavailable")
        metadata = getattr(observation, "metadata", None)
        if not isinstance(metadata, dict):
            metadata = None
        return WaitFrame(observation.image, ref.capture_id, time.monotonic(), metadata)


class ReaderJudge:
    """Use the existing vision Reader seam for an enumerated wait judgment."""

    def __init__(self, reader: Reader):
        self.reader = reader

    def __call__(self, frame: WaitFrame, spec: WaitSpec) -> JudgeDecision:
        region_note = (f"Focused area in the whole image: {spec.region}. " if spec.region else "")
        question = [{
            "field": "judgment",
            "type": "string",
            "description": (
                f"{region_note}Wait condition: {spec.expected!r}. Inspect the entire app image, "
                "including outside the focused area for dialogs, errors, or other unexpected state. "
                "Return only one allowed judgment: wait while pending; wake when the condition is met; "
                "error when the app shows a task error; unexpected for an unrelated or obstructing state."
            ),
        }]
        result = self.reader.interpret(frame.image, question)
        if result.status not in {"ok", "uncertain"}:
            return JudgeDecision("error", result)
        judgment = result.data.get("judgment") if result.status in {"ok", "uncertain"} and result.data else None
        if judgment not in _JUDGMENTS:
            invalid = ReaderResult("invalid_response", error="invalid_judgment", provider=result.provider,
                                   model=result.model, served_model=result.served_model, usage=result.usage,
                                   latency_ms=result.latency_ms)
            return JudgeDecision("error", invalid)
        return JudgeDecision(judgment, result)


class WaitWatcher:
    """Poll captures on one producer and judge one latest frame at a time.

    Frames arriving during a judgment replace the pending frame. Positive
    wake/unexpected/app-error decisions are debounced; a backend or malformed
    judgment error is terminal. A valid ``error`` judgment means the app
    visibly reports an error, while reader failures remain ``error`` with a
    distinct reader error code.
    Deadlines and cancellation are checked between callbacks. Python cannot
    safely interrupt a blocked capture/reader call, so those callbacks must
    enforce their own finite operation timeout. Shutdown joins the capture
    producer before returning, avoiding orphan calls into a stopped owner.
    All clocks/timings are local wall measurements, with no inferred primary
    agent turns or token savings.
    """

    def __init__(self, capture: Callable[[], WaitFrame], judge: Callable[[WaitFrame, WaitSpec], Any], *,
                 interval_seconds: float = 0.2, debounce_seconds: float = 0.2,
                 clock: Callable[[], float] = time.monotonic):
        if not callable(capture) or not callable(judge):
            raise TypeError("capture and judge must be callable")
        if not math.isfinite(interval_seconds) or interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive and finite")
        if not math.isfinite(debounce_seconds) or debounce_seconds < 0:
            raise ValueError("debounce_seconds must be non-negative and finite")
        self.capture = capture
        self.judge = judge
        self.interval_seconds = float(interval_seconds)
        self.debounce_seconds = float(debounce_seconds)
        self.clock = clock

    @staticmethod
    def _decision(value: Any) -> tuple[Judgment | None, ReaderResult | None]:
        if isinstance(value, JudgeDecision):
            return (value.judgment if value.judgment in _JUDGMENTS else None, value.reader_result)
        if isinstance(value, str):
            return (value if value in _JUDGMENTS else None, None)
        return (None, None)

    def wait(self, spec: WaitSpec, *, deadline_seconds: float,
             cancel_event: threading.Event | None = None) -> WaitResult:
        if not isinstance(spec, WaitSpec):
            raise TypeError("spec must be a WaitSpec")
        if not math.isfinite(deadline_seconds) or deadline_seconds <= 0:
            raise ValueError("deadline_seconds must be positive and finite")
        cancel = cancel_event or threading.Event()
        started = self.clock()
        deadline = started + deadline_seconds
        changed = threading.Condition()
        stop = threading.Event()
        latest: tuple[int, WaitFrame] | None = None
        capture_count = capture_errors = coalesced = judged_seq = judgments = 0
        capture_time = judge_time = 0.0
        usage: dict[str, Any] = {}
        capture_error_code: str | None = None

        def capture_loop() -> None:
            nonlocal latest, capture_count, capture_errors, coalesced, capture_time, capture_error_code
            while not stop.is_set() and not cancel.is_set():
                before = self.clock()
                try:
                    frame = self.capture()
                    if not isinstance(frame, WaitFrame):
                        raise TypeError("capture must return WaitFrame")
                    duration = max(0.0, self.clock() - before)
                    with changed:
                        capture_count += 1
                        capture_time += duration
                        if latest is not None and latest[0] > judged_seq:
                            coalesced += 1
                        latest = (capture_count, frame)
                        changed.notify_all()
                except Exception as exc:
                    duration = max(0.0, self.clock() - before)
                    with changed:
                        capture_errors += 1
                        capture_time += duration
                        code = getattr(exc, "code", None)
                        capture_error_code = code if isinstance(code, str) else "capture_failed"
                        changed.notify_all()
                    return
                stop.wait(self.interval_seconds)

        worker = threading.Thread(target=capture_loop, name="wait-watcher-capture", daemon=True)
        worker.start()
        status: WaitStatus = "timeout"
        evidence: WaitFrame | None = None
        error_code: str | None = None
        candidate: Judgment | None = None
        candidate_since: float | None = None
        try:
            while True:
                now = self.clock()
                if cancel.is_set():
                    status = "cancelled"
                    break
                if capture_errors:
                    status, error_code = "error", capture_error_code or "capture_failed"
                    break
                if now >= deadline:
                    status = "timeout"
                    break
                with changed:
                    fresh = latest if latest is not None and latest[0] > judged_seq else None
                    if fresh is None:
                        changed.wait(min(self.interval_seconds, max(0.0, deadline - now)))
                        continue
                seq, frame = fresh
                # Captures may block or Condition.wait may wake late. Never
                # start a judgment after this wait's deadline/cancellation.
                before_judge = self.clock()
                if cancel.is_set():
                    status = "cancelled"
                    break
                if before_judge >= deadline:
                    status = "timeout"
                    break
                evidence = frame
                try:
                    raw = self.judge(frame, spec)
                    decision, reader_result = self._decision(raw)
                except Exception:
                    decision, reader_result = None, None
                duration = max(0.0, self.clock() - before_judge)
                with changed:
                    judgments += 1
                    judge_time += duration
                    judged_seq = seq
                if reader_result is not None:
                    usage = {k: v for k, v in reader_result.usage.items() if isinstance(v, (int, float, str, bool))}
                # A slow callback cannot be preempted safely, but its result
                # must not turn a canceled or expired wait into a wake.
                if cancel.is_set():
                    status = "cancelled"
                    break
                if self.clock() >= deadline:
                    status = "timeout"
                    break
                if decision is None:
                    status, error_code = "error", "invalid_judgment"
                    break
                if decision == "error":
                    if reader_result is not None and reader_result.status not in {"ok", "uncertain"}:
                        status = "error"
                        error_code = reader_result.error or "reader_error"
                        break
                elif decision == "wait":
                    candidate, candidate_since = None, None
                    continue
                if decision != candidate:
                    candidate, candidate_since = decision, self.clock()
                if self.debounce_seconds == 0 or (
                    candidate_since is not None and self.clock() - candidate_since >= self.debounce_seconds
                ):
                    status = {"wake": "ready", "unexpected": "unexpected", "error": "error"}[decision]
                    if decision == "error":
                        error_code = "app_error_detected"
                    break
        finally:
            stop.set()
            worker.join()
        elapsed = max(0.0, self.clock() - started)
        return WaitResult(status, evidence, elapsed, capture_count, capture_errors, judgments, coalesced,
                          capture_time, judge_time, error_code, usage)
