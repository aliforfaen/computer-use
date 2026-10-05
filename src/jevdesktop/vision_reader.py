"""Bounded OpenAI-compatible image reader transport.

The module deliberately returns only structured, safe error codes. It never
logs request bodies, credentials, screenshots, or extracted text.

Stream parsing consumes raw response chunks (``iter_bytes``), so the total
deadline and the input-size cap are checked on every incoming chunk,
including partial lines that have not yet seen a newline. For the body phase
the guaranteed bound is ``total_timeout_seconds + timeout_seconds``: the
absolute deadline checked between chunks, plus at most one blocked transport
read bounded by the HTTP read/idle timeout. The probe configures 30 s + 5 s
inside a 40 s watch limit.

Header reception is *not* covered by our deadline: ``httpx`` only returns from
``stream()`` once the status line and headers are complete, and the read
/idle timeout bounds each idle gap but not a peer that trickles header bytes
faster than that timeout. A host that needs a hard wall-clock bound across
the header phase must add its own outer deadline; this module does not claim
one.

The per-chunk checks run when the transport yields a chunk. A lower-level
framing layer that buffers an incomplete unit (for example an unterminated
HTTP transfer chunk) can delay delivery; in that case the idle read timeout,
not the per-chunk deadline, is what bounds the delay.
"""

from __future__ import annotations

import codecs
import json
import math
import os
import base64
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

try:
    import httpx
except ImportError:  # pragma: no cover - dependency is supplied by caller
    httpx = None  # type: ignore[assignment]


@dataclass(frozen=True)
class ReaderConfig:
    provider: str = "deepseek"
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    key_env: str = "DEEPSEEK_API_KEY"
    # timeout_seconds is the HTTP connect/read inactivity limit. A blocked
    # transport read (including while awaiting headers) is bounded by this
    # value.
    timeout_seconds: float = 60.0
    # Absolute wall deadline for the body phase, checked on every chunk. The
    # worst-case body runtime is total_timeout_seconds + timeout_seconds (one
    # blocked read that began just before the deadline). Header reception is
    # bounded only by timeout_seconds per idle gap; see the module docstring.
    total_timeout_seconds: float = 180.0
    max_tokens: int = 256
    # Cap on raw response bytes, enforced on every chunk so it also bounds a
    # partial line before its newline arrives.
    max_response_chars: int = 64 * 1024

    def __post_init__(self) -> None:
        if self.provider not in {"deepseek", "mimo", "generic"}:
            raise ValueError("unsupported_provider")
        if not self.base_url.startswith(("https://", "http://")) or not self.model or not self.key_env:
            raise ValueError("invalid_reader_configuration")
        if not math.isfinite(self.timeout_seconds) or not 0.1 <= self.timeout_seconds <= 180:
            raise ValueError("timeout_out_of_bounds")
        if not math.isfinite(self.total_timeout_seconds) or not 0.1 <= self.total_timeout_seconds <= 180:
            raise ValueError("total_timeout_out_of_bounds")
        if type(self.max_tokens) is not int or not 1 <= self.max_tokens <= 256:
            raise ValueError("max_tokens_out_of_bounds")
        if type(self.max_response_chars) is not int or not 1 <= self.max_response_chars <= 256 * 1024:
            raise ValueError("max_response_chars_out_of_bounds")


@dataclass(frozen=True)
class ReaderResult:
    status: str
    data: dict[str, Any] | None = field(default=None, repr=False)
    error: str | None = None
    provider: str = ""
    model: str = ""
    served_model: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    latency_ms: float | None = None
    uncertainty: str | None = None
    owner_elapsed_ms: float | None = None


class Reader(Protocol):
    def capabilities(self) -> dict[str, Any]: ...
    def interpret(self, image_bytes: bytes, questions: list[dict[str, Any]]) -> ReaderResult: ...


def _usage_totals(value: Any, prefix: str = "") -> dict[str, int]:
    """Flatten numeric usage counters without preserving provider text."""
    totals: dict[str, int] = {}
    if not isinstance(value, dict):
        return totals
    for key, item in value.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if type(item) is int and item >= 0:
            totals[name] = item
        elif isinstance(item, dict):
            totals.update(_usage_totals(item, name))
    return totals


class BoundedReader:
    """Per-session call cap and truthful local usage ledger for any reader.

    The cap counts every attempted interpretation, including failures. A 429
    latches the wrapper closed for the remainder of the session so no later
    observation can silently retry a rate-limited provider.
    """

    def __init__(self, reader: Reader, max_calls: int, *,
                 on_attempt_event: Callable[[str, int, ReaderResult | None], None] | None = None,
                 cancel_event: threading.Event | None = None):
        if type(max_calls) is not int or max_calls <= 0:
            raise ValueError("max_reader_calls_must_be_positive")
        self.reader = reader
        reader_caps = reader.capabilities()
        self.provider = str(reader_caps.get("provider", ""))
        self.model = str(reader_caps.get("model", ""))
        self.max_calls = max_calls
        self.calls_used = 0
        self.successful_calls = 0
        self.failed_calls = 0
        self.latency_total_ms = 0.0
        self.owner_elapsed_total_ms = 0.0
        self.reported_usage: dict[str, int] = {}
        self.rate_limited = False
        self.audit_failed = False
        self.on_attempt_event = on_attempt_event
        self.cancel_event = cancel_event
        bind_cancel = getattr(reader, "set_cancel_event", None)
        if callable(bind_cancel):
            bind_cancel(cancel_event)
        self._lock = threading.Lock()
        self._request_lock = threading.Lock()

    def capabilities(self) -> dict[str, Any]:
        return {**self.reader.capabilities(), "max_calls_per_session": self.max_calls}

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return {"provider": self.provider, "model": self.model,
                    "calls_used": self.calls_used, "max_calls": self.max_calls,
                    "successful_calls": self.successful_calls, "failed_calls": self.failed_calls,
                    "rate_limited": self.rate_limited, "audit_failed": self.audit_failed,
                    "latency_total_ms": round(self.latency_total_ms, 2),
                    "provider_latency_total_ms": round(self.latency_total_ms, 2),
                    "owner_elapsed_total_ms": round(self.owner_elapsed_total_ms, 2),
                    "reported_usage": dict(self.reported_usage)}

    def interpret(self, image_bytes: bytes, questions: list[dict[str, Any]]) -> ReaderResult:
        if not isinstance(image_bytes, bytes) or not image_bytes:
            return ReaderResult("error", error="empty_image", provider=self.provider, model=self.model)
        try:
            _question_prompt(questions)
        except (ValueError, KeyError, TypeError):
            return ReaderResult("error", error="invalid_questions", provider=self.provider, model=self.model)
        with self._request_lock:
            with self._lock:
                if self.cancel_event is not None and self.cancel_event.is_set():
                    return ReaderResult("error", error="cancelled", provider=self.provider, model=self.model)
                if self.rate_limited:
                    return ReaderResult("error", error="provider_rate_limited", provider=self.provider, model=self.model)
                if self.audit_failed:
                    return ReaderResult("error", error="reader_audit_failed", provider=self.provider, model=self.model)
                if self.calls_used >= self.max_calls:
                    return ReaderResult("error", error="reader_call_budget_exceeded", provider=self.provider, model=self.model)
                self.calls_used += 1
                ordinal = self.calls_used
            if self.on_attempt_event is not None:
                try:
                    self.on_attempt_event("started", ordinal, None)
                except Exception:
                    result = ReaderResult("error", error="reader_audit_failed", provider=self.provider, model=self.model)
                    with self._lock:
                        self.failed_calls += 1
                        self.audit_failed = True
                    return result
            try:
                result = self.reader.interpret(image_bytes, questions)
            except Exception:
                result = ReaderResult("error", error="reader_failed", provider=self.provider, model=self.model)
            if self.on_attempt_event is not None:
                try:
                    self.on_attempt_event("completed", ordinal, result)
                except Exception:
                    result = ReaderResult("error", error="reader_audit_failed", provider=self.provider, model=self.model,
                                          usage=result.usage, latency_ms=result.latency_ms)
                    with self._lock:
                        self.audit_failed = True
            with self._lock:
                if result.status in {"ok", "uncertain"}:
                    self.successful_calls += 1
                else:
                    self.failed_calls += 1
                if result.error == "http_status_429":
                    self.rate_limited = True
                if result.latency_ms is not None and math.isfinite(result.latency_ms) and result.latency_ms >= 0:
                    self.latency_total_ms += result.latency_ms
                if (result.owner_elapsed_ms is not None and math.isfinite(result.owner_elapsed_ms)
                        and result.owner_elapsed_ms >= 0):
                    self.owner_elapsed_total_ms += result.owner_elapsed_ms
                for key, count in _usage_totals(result.usage).items():
                    self.reported_usage[key] = self.reported_usage.get(key, 0) + count
        return result


class IsolatedVisionReader:
    """Run each provider request in a killable process with a hard wall bound.

    ``VisionReader`` retains its stream-level body safeguards for benchmarks.
    The supported owner uses this process wrapper as an outer deadline across
    DNS, connection, response headers and body, and to stop a request on task
    cancellation. A reader process is always reaped before returning.
    """

    def __init__(self, config: "ReaderConfig", *, hard_timeout_seconds: float | None = None,
                 cancel_event: threading.Event | None = None, poll_seconds: float = 0.05):
        self.config = config
        derived_bound = config.timeout_seconds + config.total_timeout_seconds + 2.0
        self.hard_timeout_seconds = float(hard_timeout_seconds if hard_timeout_seconds is not None else derived_bound)
        if not math.isfinite(self.hard_timeout_seconds) or not 0.1 <= self.hard_timeout_seconds <= 362:
            raise ValueError("reader_hard_timeout_out_of_bounds")
        if not math.isfinite(poll_seconds) or not 0.01 <= poll_seconds <= 0.5:
            raise ValueError("reader_poll_interval_out_of_bounds")
        self.cancel_event = cancel_event
        self.poll_seconds = poll_seconds
        self.last_child_pid: int | None = None
        self.last_child_exitcode: int | None = None

    def capabilities(self) -> dict[str, Any]:
        return {**VisionReader(self.config).capabilities(),
                "request_execution": "isolated_process",
                "hard_timeout_seconds": round(self.hard_timeout_seconds, 3),
                "hard_timeout_covers": "process_start, connection, response_headers_and_body",
                "cancellation": "terminate_and_reap_reader_process",
                "latency_fields": {"latency_ms": "provider_transport_elapsed_ms",
                                   "owner_elapsed_ms": "request_wall_time_including_process_start"}}

    def set_cancel_event(self, cancel_event: threading.Event | None) -> None:
        self.cancel_event = cancel_event

    @staticmethod
    def _reap(proc: subprocess.Popen[bytes]) -> None:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.communicate(timeout=0.5)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.communicate(timeout=1.0)
                except subprocess.TimeoutExpired:
                    # The direct child is already SIGKILLed; wait remains a
                    # bounded kernel/process-state operation in normal Linux use.
                    proc.wait()
        else:
            proc.communicate()

    def interpret(self, image_bytes: bytes, questions: list[dict[str, Any]]) -> ReaderResult:
        if self.cancel_event is not None and self.cancel_event.is_set():
            return ReaderResult("error", error="cancelled", provider=self.config.provider, model=self.config.model)
        if not isinstance(image_bytes, bytes) or not image_bytes:
            return ReaderResult("error", error="empty_image", provider=self.config.provider, model=self.config.model)
        try:
            _question_prompt(questions)
        except (ValueError, KeyError, TypeError):
            return ReaderResult("error", error="invalid_questions", provider=self.config.provider, model=self.config.model)
        started = time.perf_counter()
        deadline = time.monotonic() + self.hard_timeout_seconds
        request = {"config": {key: getattr(self.config, key) for key in (
            "provider", "base_url", "model", "key_env", "timeout_seconds", "total_timeout_seconds",
            "max_tokens", "max_response_chars")},
            "image_base64": base64.b64encode(image_bytes).decode("ascii"), "questions": questions}
        payload = (json.dumps(request, separators=(",", ":"), ensure_ascii=True) + "\n").encode("utf-8")
        if len(payload) > 12 * 1024 * 1024:
            return ReaderResult("error", error="reader_request_too_large", provider=self.config.provider,
                                model=self.config.model, owner_elapsed_ms=round((time.perf_counter() - started) * 1000, 2))
        try:
            proc = subprocess.Popen([sys.executable, "-m", "jevdesktop.vision_reader_worker"],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            elapsed = round((time.perf_counter() - started) * 1000, 2)
            return ReaderResult("error", error="reader_worker_unavailable", provider=self.config.provider,
                                model=self.config.model, owner_elapsed_ms=elapsed)
        self.last_child_pid = proc.pid
        input_pending: bytes | None = payload
        try:
            while True:
                if self.cancel_event is not None and self.cancel_event.is_set():
                    self._reap(proc)
                    elapsed = round((time.perf_counter() - started) * 1000, 2)
                    return ReaderResult("error", error="cancelled", provider=self.config.provider, model=self.config.model,
                                        owner_elapsed_ms=elapsed)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._reap(proc)
                    elapsed = round((time.perf_counter() - started) * 1000, 2)
                    return ReaderResult("error", error="request_deadline_exceeded", provider=self.config.provider,
                                        model=self.config.model, owner_elapsed_ms=elapsed)
                try:
                    output, _ = proc.communicate(input=input_pending, timeout=min(self.poll_seconds, remaining))
                    input_pending = None
                    break
                except subprocess.TimeoutExpired:
                    input_pending = None
                    continue
            if proc.returncode != 0 or len(output) > 256 * 1024:
                elapsed = round((time.perf_counter() - started) * 1000, 2)
                return ReaderResult("error", error="reader_worker_failed", provider=self.config.provider,
                                    model=self.config.model, owner_elapsed_ms=elapsed)
            try:
                value = json.loads(output)
                if not isinstance(value, dict) or value.get("status") not in {"ok", "uncertain", "error", "invalid_response"}:
                    raise ValueError
                data = value.get("data")
                if data is not None and not isinstance(data, dict):
                    raise ValueError
                usage = value.get("usage") if isinstance(value.get("usage"), dict) else {}
                latency = value.get("latency_ms")
                if isinstance(latency, bool) or not isinstance(latency, (int, float)) or not math.isfinite(latency) or latency < 0:
                    latency = None
                return ReaderResult(value["status"], data=data, error=value.get("error") if isinstance(value.get("error"), str) else None,
                                    provider=self.config.provider, model=self.config.model,
                                    served_model=value.get("served_model") if isinstance(value.get("served_model"), str) else None,
                                    usage=usage, latency_ms=latency,
                                    uncertainty=value.get("uncertainty") if isinstance(value.get("uncertainty"), str) else None,
                                    owner_elapsed_ms=round((time.perf_counter() - started) * 1000, 2))
            except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
                elapsed = round((time.perf_counter() - started) * 1000, 2)
                return ReaderResult("error", error="reader_worker_protocol_error", provider=self.config.provider,
                                    model=self.config.model, owner_elapsed_ms=elapsed)
        finally:
            if proc.poll() is None:
                self._reap(proc)
            self.last_child_exitcode = proc.returncode

    def close(self) -> None:
        # No persistent child is retained between requests.
        return None


class StreamParseError(ValueError):
    """Safe stream failure with a stable machine-readable code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def build_request_body(image_bytes: bytes, prompt: str, *, provider: str, model: str, max_tokens: int) -> dict[str, Any]:
    """Build the shared image-chat payload used by the adapter and benchmark."""
    import base64

    if provider not in {"deepseek", "mimo", "generic"}:
        raise ValueError("unsupported_provider")
    if not isinstance(image_bytes, bytes) or not image_bytes or not isinstance(prompt, str) or not prompt:
        raise ValueError("invalid_request_content")
    if type(max_tokens) is not int or not 1 <= max_tokens <= 256:
        raise ValueError("max_tokens_out_of_bounds")
    content_type = "image/jpeg" if image_bytes.startswith(b"\xff\xd8\xff") else "image/png"
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:{content_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"}},
        ]}],
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
        "response_format": {"type": "json_object"},
    }
    if provider in {"deepseek", "mimo"}:
        body["thinking"] = {"type": "disabled"}
    if provider == "mimo":
        body["max_completion_tokens"] = body.pop("max_tokens")
    return body


def _safe_usage(value: Any, depth: int = 0) -> Any:
    """Retain numeric usage counters without copying arbitrary provider strings."""
    if type(value) is int and value >= 0:
        return value
    if depth < 2 and isinstance(value, dict):
        return {str(key): nested for key, item in value.items()
                if (nested := _safe_usage(item, depth + 1)) is not None}
    return None


def _iter_response_lines(response: Any, *, max_response_chars: int, deadline_monotonic: float | None,
                         clock) -> Any:
    """Yield decoded SSE lines while bounding time and size on raw chunks.

    ``iter_lines`` buffers bytes until a newline, so a peer that trickles bytes
    without newlines can reset the idle timeout forever while the parser never
    regains control. Consuming ``iter_bytes`` lets the deadline and byte cap
    apply to every received chunk, including a partial line. A read that blocks
    with no bytes is still bounded by the transport read timeout.
    """
    if not callable(getattr(response, "iter_bytes", None)):
        raise StreamParseError("unsupported_stream_transport")
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    pending = ""
    total_bytes = 0
    for chunk in response.iter_bytes():
        if deadline_monotonic is not None and clock() >= deadline_monotonic:
            raise StreamParseError("stream_total_timeout")
        if not isinstance(chunk, (bytes, bytearray)):
            raise StreamParseError("malformed_stream_event")
        total_bytes += len(chunk)
        if total_bytes > max_response_chars:
            raise StreamParseError("response_too_large")
        pending += decoder.decode(bytes(chunk))
        if "\n" in pending:
            lines = pending.split("\n")
            pending = lines.pop()
            for line in lines:
                yield line
    pending += decoder.decode(b"", final=True)
    if pending:
        yield pending


def parse_stream(response: Any, on_first_content=None, *, max_response_chars: int = 256 * 1024,
                 deadline_monotonic: float | None = None, clock=time.monotonic) -> tuple[str, dict[str, Any], str, str | None]:
    """Parse OpenAI-compatible SSE once for both reader and benchmark callers.

    The deadline and the size cap are enforced on every raw chunk, so a
    newline-free byte trickle cannot bypass them; the byte cap also bounds a
    partial line before it is terminated.
    """
    fragments: list[str] = []
    usage: dict[str, Any] = {}
    finish_reason: str | None = None
    served_model: str | None = None
    saw_done = False
    try:
        for line in _iter_response_lines(response, max_response_chars=max_response_chars,
                                         deadline_monotonic=deadline_monotonic, clock=clock):
            if not isinstance(line, str):
                raise StreamParseError("malformed_stream_event")
            if not line or line.startswith(":") or not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                saw_done = True
                break
            try:
                event = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise StreamParseError("malformed_stream_event") from exc
            if not isinstance(event, dict):
                raise StreamParseError("malformed_stream_event")
            if isinstance(event.get("model"), str):
                served_model = event["model"]
            if isinstance(event.get("usage"), dict):
                usage.update(_safe_usage(event["usage"]))
            choices = event.get("choices", [])
            if not isinstance(choices, list):
                raise StreamParseError("malformed_stream_event")
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                if choice.get("finish_reason") is not None:
                    finish_reason = choice["finish_reason"]
                delta = choice.get("delta") or {}
                if not isinstance(delta, dict):
                    raise StreamParseError("malformed_stream_event")
                content = delta.get("content")
                if isinstance(content, str) and content:
                    if not fragments and on_first_content is not None:
                        on_first_content()
                    fragments.append(content)
    except StreamParseError:
        raise
    if not saw_done:
        raise StreamParseError("stream_missing_done")
    if finish_reason is None:
        raise StreamParseError("stream_missing_finish_reason")
    if finish_reason != "stop":
        raise StreamParseError("stream_not_completed")
    if not fragments:
        raise StreamParseError("stream_empty_content")
    return "".join(fragments), usage, finish_reason, served_model


def _validate_questions(questions: list[dict[str, Any]]) -> None:
    if not isinstance(questions, list) or not questions:
        raise ValueError("questions_required")
    seen: set[str] = set()
    for question in questions:
        if not isinstance(question, dict):
            raise ValueError("invalid_question")
        field_name = question.get("field")
        if not isinstance(field_name, str) or not field_name or field_name in seen:
            raise ValueError("invalid_or_duplicate_field")
        seen.add(field_name)
        if question.get("type") not in {"string", "number", "integer", "boolean", "array", "object"}:
            raise ValueError("invalid_question")
        if not isinstance(question.get("description"), str) or not question["description"]:
            raise ValueError("invalid_question")
        if "nullable" in question and type(question["nullable"]) is not bool:
            raise ValueError("invalid_nullable_flag")


def _question_prompt(questions: list[dict[str, Any]]) -> str:
    _validate_questions(questions)
    lines = [
        "Observe only what is visible in the supplied application screenshot.",
        "Return one JSON object with exactly the requested fields and value types.",
        "Do not infer hidden state. Follow each question's uncertainty convention.",
        "Questions:",
    ]
    for question in questions:
        field_name = question.get("field")
        value_type = question.get("type")
        description = question.get("description")
        lines.append(f"- {field_name} ({value_type}): {description}")
    return "\n".join(lines)


def _valid_value(value: Any, kind: str) -> bool:
    if kind == "string":
        return isinstance(value, str)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "array":
        return isinstance(value, list)
    if kind == "object":
        return isinstance(value, dict)
    return False


def _parse_content(content: str, questions: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str | None, bool]:
    try:
        value = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None, "invalid_json", False
    if not isinstance(value, dict):
        return None, "response_not_object", False
    expected = {question["field"] for question in questions}
    if set(value) != expected:
        return value, "unexpected_or_missing_fields", False
    uncertain = False
    for question in questions:
        field_value = value[question["field"]]
        if field_value is None and question.get("nullable") is True:
            uncertain = True
        elif not _valid_value(field_value, question["type"]):
            return value, "wrong_field_type", False
    return value, None, uncertain


class VisionReader:
    """Synchronous streamed image reader for OpenAI-compatible endpoints."""

    def __init__(self, config: ReaderConfig | None = None, *, client: Any = None):
        self.config = config or ReaderConfig()
        self._client = client
        self._owns_client = client is None

    def capabilities(self) -> dict[str, Any]:
        return {
            "images": True,
            "structured_json": True,
            "providers": ["deepseek", "mimo", "generic"],
            "provider": self.config.provider,
            "model": self.config.model,
            "streaming": True,
        }

    def _timeout(self) -> Any:
        # Passed per request so an injected shared client cannot silently
        # extend the bounded read/idle wait past this reader's configuration.
        return httpx.Timeout(self.config.timeout_seconds,
                             connect=min(15.0, self.config.timeout_seconds))

    def _http_client(self) -> Any:
        if httpx is None:
            raise RuntimeError("httpx_unavailable")
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout())
        return self._client

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def interpret(self, image_bytes: bytes, questions: list[dict[str, Any]]) -> ReaderResult:
        cfg = self.config
        if cfg.provider not in {"deepseek", "mimo", "generic"}:
            return ReaderResult("error", error="unsupported_provider", provider=cfg.provider, model=cfg.model)
        if not isinstance(image_bytes, bytes) or not image_bytes:
            return ReaderResult("error", error="empty_image", provider=cfg.provider, model=cfg.model)
        try:
            prompt = _question_prompt(questions)
        except (ValueError, KeyError, TypeError):
            return ReaderResult("error", error="invalid_questions", provider=cfg.provider, model=cfg.model)
        api_key = os.environ.get(cfg.key_env)
        if not api_key:
            return ReaderResult("error", error="missing_api_key", provider=cfg.provider, model=cfg.model)
        body = build_request_body(image_bytes, prompt, provider=cfg.provider, model=cfg.model, max_tokens=cfg.max_tokens)
        url = cfg.base_url.rstrip("/") + "/chat/completions"
        started = time.perf_counter()
        started_monotonic = time.monotonic()
        usage: dict[str, Any] = {}
        served_model: str | None = None
        finish_reason: str | None = None
        try:
            with self._http_client().stream("POST", url, headers={"Authorization": f"Bearer {api_key}"}, json=body,
                                            timeout=self._timeout()) as response:
                if response.status_code < 200 or response.status_code >= 300:
                    return ReaderResult("error", error=f"http_status_{response.status_code}", provider=cfg.provider, model=cfg.model,
                                        latency_ms=round((time.perf_counter() - started) * 1000, 2))
                answer_text, usage, finish_reason, served_model = parse_stream(
                    response, max_response_chars=cfg.max_response_chars,
                    deadline_monotonic=started_monotonic + cfg.total_timeout_seconds)
        except StreamParseError as exc:
            return ReaderResult("error", error=exc.code, provider=cfg.provider, model=cfg.model,
                                served_model=served_model, usage=usage,
                                latency_ms=round((time.perf_counter() - started) * 1000, 2))
        except Exception as exc:
            if httpx is not None and isinstance(exc, httpx.TimeoutException):
                error = "request_timeout"
            elif httpx is not None and isinstance(exc, httpx.TransportError):
                error = "transport_error"
            else:
                error = "reader_transport_error"
            return ReaderResult("error", error=error, provider=cfg.provider, model=cfg.model,
                                latency_ms=round((time.perf_counter() - started) * 1000, 2))
        latency = round((time.perf_counter() - started) * 1000, 2)
        data, shape_error, uncertain = _parse_content(answer_text, questions)
        if shape_error:
            return ReaderResult("invalid_response", data=data, error=shape_error, provider=cfg.provider, model=cfg.model,
                                served_model=served_model, usage=usage, latency_ms=latency, uncertainty="response_shape_invalid")
        return ReaderResult("uncertain" if uncertain else "ok", data=data, provider=cfg.provider, model=cfg.model,
                            served_model=served_model, usage=usage, latency_ms=latency,
                            uncertainty="reader_returned_nullable_value" if uncertain else None)
