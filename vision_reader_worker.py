"""One-request child process for the owner's isolated vision reader."""

from __future__ import annotations

import base64
import json
import sys
from typing import Any

MAX_INPUT_BYTES = 12 * 1024 * 1024


def _error(code: str) -> dict[str, Any]:
    return {"status": "error", "error": code, "provider": "", "model": "", "usage": {}}


def main() -> int:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        value = _error("reader_request_too_large")
    else:
        try:
            request = json.loads(raw)
            if not isinstance(request, dict) or not isinstance(request.get("config"), dict):
                raise ValueError
            config_value = request["config"]
            config = {key: config_value[key] for key in (
                "provider", "base_url", "model", "key_env", "timeout_seconds", "total_timeout_seconds",
                "max_tokens", "max_response_chars")}
            image = base64.b64decode(request["image_base64"], validate=True)
            questions = request["questions"]
            from vision_reader import ReaderConfig, VisionReader

            result = VisionReader(ReaderConfig(**config)).interpret(image, questions)
            value = {"status": result.status, "data": result.data, "error": result.error,
                     "served_model": result.served_model, "usage": result.usage,
                     "latency_ms": result.latency_ms, "uncertainty": result.uncertainty}
        except Exception:
            value = _error("reader_worker_protocol_error")
    sys.stdout.write(json.dumps(value, separators=(",", ":"), ensure_ascii=True) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover - child-process entry point
    raise SystemExit(main())
