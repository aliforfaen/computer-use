#!/usr/bin/env python3
"""Run a small, app-only vision-model benchmark against a captured fixture suite."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    import httpx
except ImportError as exc:  # pragma: no cover - exercised by CLI users without dependency
    raise SystemExit("vision_benchmark.py requires httpx; install it with `python -m pip install httpx`") from exc


MAX_COMPLETION_TOKENS = 256
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_KEY_ENV = "DEEPSEEK_API_KEY"
SCOPES = {"app", "full", "crop"}


class BenchmarkError(Exception):
    """A safe-to-report benchmark or provider response error."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strict_type(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return False


def _expected_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    raise BenchmarkError("unsupported expected fact type")


def parse_answer(content: str, questions: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str | None]:
    """Parse JSON and validate the requested fields without conflating accuracy."""
    try:
        answer = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None, "invalid_json"
    if not isinstance(answer, dict):
        return None, "response_not_object"
    requested_fields = {q.get("field") for q in questions if isinstance(q, dict)}
    if set(answer) != requested_fields:
        return answer, "unexpected_or_missing_fields"
    for question in questions:
        field = question.get("field")
        typ = question.get("type")
        if not isinstance(field, str) or typ not in {"string", "number", "integer", "boolean", "array", "object"}:
            return None, "invalid_manifest_question"
        if field not in answer:
            return answer, "missing_field"
        if not _strict_type(answer[field], typ):
            return answer, "wrong_field_type"
    return answer, None


def _load_manifest(manifest_path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkError("manifest_unreadable_or_invalid") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        raise BenchmarkError("manifest_missing_cases")
    return payload


def _select_requests(manifest: dict[str, Any], manifest_path: Path, scope: str) -> tuple[list[dict[str, Any]], list[str]]:
    root = manifest_path.parent
    requests: list[dict[str, Any]] = []
    skipped: list[str] = []
    for case in manifest["cases"]:
        if not isinstance(case, dict):
            raise BenchmarkError("invalid_manifest_case")
        case_id = case.get("case_id")
        questions = case.get("questions")
        expected = case.get("expected_facts")
        images = case.get("images")
        if not isinstance(case_id, str) or not isinstance(questions, list) or not isinstance(expected, dict) or not isinstance(images, list):
            raise BenchmarkError("invalid_manifest_case_shape")
        if not questions or not images:
            raise BenchmarkError("case_has_no_questions_or_images")
        for question in questions:
            if not isinstance(question, dict) or not isinstance(question.get("field"), str) or not isinstance(question.get("description"), str):
                raise BenchmarkError("invalid_manifest_question")
            if question.get("type") not in {"string", "number", "integer", "boolean", "array", "object"}:
                raise BenchmarkError("invalid_manifest_question")
        if {q.get("field") for q in questions if isinstance(q, dict)} != set(expected):
            raise BenchmarkError("question_and_expected_fact_fields_differ")
        if any(question["type"] != _expected_type(expected[question["field"]]) and not (question["type"] == "number" and _expected_type(expected[question["field"]]) == "integer") for question in questions):
            raise BenchmarkError("unsupported_expected_fact_type")
        chosen = [item for item in images if isinstance(item, dict) and item.get("scope") == scope]
        if not chosen:
            skipped.append(case_id)
            continue
        for image in chosen:
            rel = image.get("path")
            if not isinstance(rel, str):
                raise BenchmarkError("invalid_manifest_image_path")
            path = Path(rel)
            image_path = path if path.is_absolute() else root / path
            try:
                data = image_path.read_bytes()
            except OSError as exc:
                raise BenchmarkError("fixture_image_unreadable") from exc
            digest = hashlib.sha256(data).hexdigest()
            stated_hash = image.get("sha256")
            if stated_hash and stated_hash != digest:
                raise BenchmarkError("fixture_image_hash_mismatch")
            requests.append({
                "case_id": case_id,
                "questions": questions,
                "expected_facts": expected,
                "image": image,
                "image_path": str(image_path),
                "image_data": data,
                "image_sha256": digest,
                "source_provenance": case.get("source_provenance"),
            })
    if not requests:
        raise BenchmarkError(f"no_cases_for_scope_{scope}")
    return requests, skipped


def _prompt(questions: list[dict[str, Any]]) -> str:
    lines = [
        "Observe only what is visible in the supplied application screenshot.",
        "Return one JSON object with exactly the requested fields and the requested value types.",
        "Use an empty string for a visibly blank text field; use null only if the schema allows it.",
        "Do not infer hidden state. Use the requested uncertainty convention in each question.",
        "Questions:",
    ]
    for question in questions:
        lines.append(f"- {question['field']} ({question['type']}): {question['description']}")
    return "\n".join(lines)


def _sse_content(response: httpx.Response, on_first_content) -> tuple[str, dict[str, Any], str, str | None]:
    fragments: list[str] = []
    usage: dict[str, Any] = {}
    finish_reason: str | None = None
    served_model: str | None = None
    saw_done = False
    for line in response.iter_lines():
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            saw_done = True
            break
        try:
            event = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise BenchmarkError("malformed_stream_event") from exc
        if not isinstance(event, dict):
            raise BenchmarkError("malformed_stream_event")
        if isinstance(event.get("usage"), dict):
            def safe_usage(value: Any, depth: int = 0) -> Any:
                if value is None or isinstance(value, (str, int, float, bool)):
                    return value
                if depth < 2 and isinstance(value, dict):
                    return {str(k): safe_usage(v, depth + 1) for k, v in value.items() if isinstance(v, (dict, str, int, float, bool)) or v is None}
                return None
            usage.update({str(k): safe_usage(v) for k, v in event["usage"].items()})
        if isinstance(event.get("model"), str):
            served_model = event["model"]
        choices = event.get("choices", [])
        if not isinstance(choices, list):
            raise BenchmarkError("malformed_stream_event")
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            if choice.get("finish_reason") is not None:
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                raise BenchmarkError("malformed_stream_event")
            content = delta.get("content")
            if isinstance(content, str) and content:
                if not fragments:
                    on_first_content()
                fragments.append(content)
    if not saw_done:
        raise BenchmarkError("stream_missing_done")
    if finish_reason is None:
        raise BenchmarkError("stream_missing_finish_reason")
    if finish_reason != "stop":
        raise BenchmarkError("stream_not_completed")
    if not fragments:
        raise BenchmarkError("stream_empty_content")
    return "".join(fragments), usage, finish_reason, served_model


def _request_one(client: httpx.Client, request: dict[str, Any], *, base_url: str, model: str, provider: str,
                 api_key: str, max_tokens: int) -> dict[str, Any]:
    prepare_started = time.perf_counter()
    image_bytes = request["image_data"]
    encoded = base64.b64encode(image_bytes).decode("ascii")
    content_type = "image/jpeg" if image_bytes[:3] == b"\xff\xd8\xff" else "image/png"
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": _prompt(request["questions"])},
            {"type": "image_url", "image_url": {"url": f"data:{content_type};base64,{encoded}"}},
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
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    url = base_url.rstrip("/") + "/chat/completions"
    body_preparation_ms = round((time.perf_counter() - prepare_started) * 1000, 2)
    started = time.perf_counter()
    ttft: float | None = None
    def mark_ttft() -> None:
        nonlocal ttft
        ttft = time.perf_counter() - started
    try:
        with client.stream("POST", url, headers=headers, json=body) as response:
            if response.status_code < 200 or response.status_code >= 300:
                # Do not expose provider response text: it may contain request data.
                raise BenchmarkError(f"http_status_{response.status_code}")
            answer_text, usage, finish_reason, served_model = _sse_content(response, mark_ttft)
    except httpx.TimeoutException as exc:
        raise BenchmarkError("request_timeout") from exc
    except httpx.TransportError as exc:
        raise BenchmarkError("transport_error") from exc
    completed = time.perf_counter() - started
    answer, shape_error = parse_answer(answer_text, request["questions"])
    expected = request["expected_facts"]
    comparisons = {
        key: answer is not None and key in answer and _strict_type(answer[key], _expected_type(value)) and answer[key] == value
        for key, value in expected.items()
    }
    try:
        json.loads(answer_text)
        valid_json = True
    except json.JSONDecodeError:
        valid_json = False
    valid = shape_error is None
    return {
        "status": "ok" if valid else "invalid_response",
        "error": shape_error,
        "valid_json": valid_json,
        "valid_shape": valid,
        "answer": answer if answer is not None else None,
        "accuracy": {"correct": sum(comparisons.values()), "total": len(comparisons), "per_field": comparisons},
        "ttft_ms": round(ttft * 1000, 2) if ttft is not None else None,
        "completion_ms": round(completed * 1000, 2),
        "body_preparation_ms": body_preparation_ms,
        "finish_reason": finish_reason,
        "served_model": served_model,
        "usage": usage,
    }


def _record_error(exc: BenchmarkError) -> dict[str, Any]:
    return {"status": "error", "error": str(exc)}


def _summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    successful = [record for record in records if record.get("status") == "ok"]
    valid = [record for record in records if record.get("valid_shape")]
    all_scores = [record["accuracy"]["correct"] / record["accuracy"]["total"] for record in records if record.get("accuracy", {}).get("total")]
    field_correct = sum(record.get("accuracy", {}).get("correct", 0) for record in records)
    field_total = sum(record.get("accuracy", {}).get("total", 0) for record in records)
    ttfts = sorted(record["ttft_ms"] for record in successful if record.get("ttft_ms") is not None)
    completions = sorted(record["attempt_ms"] for record in records if record.get("attempt_ms") is not None)
    def percentile(values: list[float], p: float) -> float | None:
        if not values:
            return None
        return round(values[min(len(values) - 1, max(0, math.ceil(len(values) * p) - 1))], 2)
    def median(values: list[float]) -> float | None:
        n = len(values)
        if not n:
            return None
        return round((values[(n - 1) // 2] + values[n // 2]) / 2, 2)
    return {
        "request_count": len(records),
        "success_count": len(successful),
        "valid_json_shape_count": len(valid),
        "valid_json_count": sum(record.get("valid_json", False) for record in records),
        "error_count": sum(record.get("status") == "error" for record in records),
        "malformed_count": sum(record.get("status") == "invalid_response" for record in records),
        "incorrect_count": sum(record.get("status") == "ok" and record["accuracy"]["correct"] < record["accuracy"]["total"] for record in records),
        "all_facts_correct_count": sum(bool(record.get("valid_shape") and record.get("accuracy", {}).get("correct") == record.get("accuracy", {}).get("total")) for record in records),
        "macro_mean_field_accuracy": round(sum(all_scores) / len(all_scores), 4) if all_scores else None,
        "overall_field_accuracy": {"correct": field_correct, "total": field_total,
                                   "ratio": round(field_correct / field_total, 4) if field_total else None},
        "usage_totals": {key: sum(record.get("usage", {}).get(key, 0) or 0 for record in records if isinstance(record.get("usage", {}).get(key, 0), (int, float)))
                         for key in ("prompt_tokens", "completion_tokens", "total_tokens")},
        "ttft_valid_replies_ms": {"median": median(ttfts), "p95": percentile(ttfts, .95), "max": max(ttfts) if ttfts else None},
        "attempt_completion_ms": {"median": median(completions), "p95": percentile(completions, .95), "max": max(completions) if completions else None},
    }


def run_benchmark(*, manifest_path: Path, output_dir: Path, base_url: str, model: str, key_env: str,
                  provider: str, repetitions: int, scope: str, max_calls: int, seed: int,
                  max_tokens: int = MAX_COMPLETION_TOKENS) -> dict[str, Any]:
    if repetitions < 1 or max_calls < 1 or max_tokens < 1 or max_tokens > MAX_COMPLETION_TOKENS:
        raise BenchmarkError("invalid_benchmark_limits")
    manifest = _load_manifest(manifest_path)
    fixtures, skipped_cases = _select_requests(manifest, manifest_path, scope)
    planned = [dict(item, repetition=rep) for rep in range(1, repetitions + 1) for item in fixtures]
    if len(planned) > max_calls:
        raise BenchmarkError(f"planned_calls_{len(planned)}_exceed_max_calls_{max_calls}")
    api_key = os.environ.get(key_env)
    if not api_key:
        raise BenchmarkError(f"missing_key_env_{key_env}")
    order = list(range(len(planned)))
    random.Random(seed).shuffle(order)
    output_dir.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    jsonl_path = output_dir / f"vision-results-{run_id}.jsonl"
    records: list[dict[str, Any]] = []
    headers = {"Authorization": f"Bearer {api_key}"}
    # One client per run keeps the connection pool alive across randomized requests.
    with httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0), headers=headers) as client:
        for index in order:
            item = planned[index]
            started_at = _now()
            attempt_started = time.perf_counter()
            try:
                outcome = _request_one(client, item, base_url=base_url, model=model, provider=provider,
                                       api_key=api_key, max_tokens=max_tokens)
            except BenchmarkError as exc:
                outcome = _record_error(exc)
                outcome["valid_json"] = False
                outcome["valid_shape"] = False
                outcome["accuracy"] = {"correct": 0, "total": len(item["expected_facts"]),
                                        "per_field": {key: False for key in item["expected_facts"]}}
            outcome["attempt_ms"] = round((time.perf_counter() - attempt_started) * 1000, 2)
            record = {
                "started_at": started_at,
                "caller_node": "local",
                "transport": "cli",
                "tool": "benchmark.interpret_fixture",
                "autonomy_mode": "guarded",
                "case_id": item["case_id"],
                "repetition": item["repetition"],
                "scope": scope,
                "provider": provider,
                "model": model,
                "served_model": outcome.get("served_model"),
                "base_url": base_url,
                "image": {key: value for key, value in item["image"].items() if key != "path"},
                "image_sha256": item["image_sha256"],
                "source_provenance": item["source_provenance"],
                **outcome,
            }
            records.append(record)
            with jsonl_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    summary = {
        "run_id": run_id,
        "completed_at": _now(),
        "manifest": str(manifest_path),
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "scope": scope,
        "repetitions": repetitions,
        "seed": seed,
        "max_tokens": max_tokens,
        "persistent_http_client": True,
        "capture": [{"case_id": case.get("case_id"), "setup_ms": case.get("setup_ms"), "capture_ms": case.get("capture_ms"),
                     "versions": case.get("versions")}
                    for case in manifest["cases"] if isinstance(case, dict) and any(isinstance(image, dict) and image.get("scope") == scope for image in case.get("images", []))],
        "skipped_cases_without_scope_image": skipped_cases,
        "results_jsonl": jsonl_path.name,
        "results": _summary(records),
    }
    summary_path = output_dir / f"vision-summary-{run_id}.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return summary


def _load_dotenv(dotenv_path: Path) -> None:
    """Load simple KEY=value entries without printing or overriding process env."""
    try:
        lines = dotenv_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name, value = name.strip(), value.strip().strip("\"'")
        if name and name not in os.environ:
            os.environ[name] = value


def _capture(output_dir: Path, scope: str) -> Path:
    try:
        from benchmark_capture import capture_suite
    except ImportError as exc:
        raise BenchmarkError("benchmark_capture_module_unavailable") from exc
    try:
        manifest = capture_suite(output_dir)
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        # Provider / desktop errors may include sensitive window or request details.
        raise BenchmarkError(f"capture_failed_{type(exc).__name__}") from exc
    if not isinstance(manifest, dict):
        raise BenchmarkError("capture_suite_returned_invalid_manifest")
    path = output_dir / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("run"), help="fixture and result directory")
    parser.add_argument("--manifest", type=Path, help="reuse an existing fixture manifest instead of capturing")
    parser.add_argument("--capture-only", action="store_true", help="capture the public fixture suite and exit")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--key-env", default=DEFAULT_KEY_ENV, help="environment variable holding provider key")
    parser.add_argument("--provider", choices=("deepseek", "mimo", "generic"), default="deepseek")
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--scope", choices=sorted(SCOPES), default="app")
    parser.add_argument("--max-calls", type=int, default=24)
    parser.add_argument("--seed", type=int, default=20261001)
    parser.add_argument("--max-tokens", type=int, default=MAX_COMPLETION_TOKENS)
    parser.add_argument("--dotenv", type=Path, default=Path(".env"), help="optional ignored dotenv file")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _load_dotenv(args.dotenv)
    try:
        manifest_path = args.manifest
        if manifest_path is None:
            manifest_path = _capture(args.output, args.scope)
        if args.capture_only:
            print(json.dumps({"manifest": str(manifest_path), "capture_only": True}))
            return 0
        summary = run_benchmark(manifest_path=manifest_path, output_dir=args.output, base_url=args.base_url,
                                model=args.model, key_env=args.key_env, provider=args.provider,
                                repetitions=args.repetitions, scope=args.scope, max_calls=args.max_calls,
                                seed=args.seed, max_tokens=args.max_tokens)
        print(json.dumps(summary, ensure_ascii=False))
        results = summary["results"]
        return 0 if results["success_count"] == results["request_count"] and results["incorrect_count"] == 0 else 1
    except BenchmarkError as exc:
        print(f"benchmark error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("benchmark interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
