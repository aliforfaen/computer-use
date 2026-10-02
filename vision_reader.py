"""Bounded OpenAI-compatible image reader transport.

The module deliberately returns only structured, safe error codes. It never
logs request bodies, credentials, screenshots, or extracted text.

Stream runtime is bounded by ``total_timeout_seconds`` checked between stream
lines. A transport read blocked at that deadline is bounded by
``timeout_seconds`` (the HTTP read/idle timeout, passed per request so an
injected client cannot extend it). Worst-case stream runtime is therefore
``total_timeout_seconds + timeout_seconds``; the probe configures 30 s + 5 s
inside a 40 s watch deadline.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

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
    # transport read is bounded by this value.
    timeout_seconds: float = 60.0
    # The parser checks this monotonic wall deadline between stream lines, so
    # worst-case bounded runtime is total_timeout_seconds + timeout_seconds
    # (one blocked read that began just before the deadline).
    total_timeout_seconds: float = 180.0
    max_tokens: int = 256
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


class Reader(Protocol):
    def capabilities(self) -> dict[str, Any]: ...
    def interpret(self, image_bytes: bytes, questions: list[dict[str, Any]]) -> ReaderResult: ...


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


def parse_stream(response: Any, on_first_content=None, *, max_response_chars: int = 256 * 1024,
                 deadline_monotonic: float | None = None, clock=time.monotonic) -> tuple[str, dict[str, Any], str, str | None]:
    """Parse OpenAI-compatible SSE once for both reader and benchmark callers."""
    fragments: list[str] = []
    usage: dict[str, Any] = {}
    finish_reason: str | None = None
    served_model: str | None = None
    saw_done = False
    response_chars = 0
    try:
        lines = response.iter_lines()
        for line in lines:
            if deadline_monotonic is not None and clock() >= deadline_monotonic:
                raise StreamParseError("stream_total_timeout")
            if not isinstance(line, str):
                raise StreamParseError("malformed_stream_event")
            response_chars += len(line)
            if response_chars > max_response_chars:
                raise StreamParseError("response_too_large")
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
                    if sum(map(len, fragments)) > max_response_chars:
                        raise StreamParseError("response_too_large")
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
