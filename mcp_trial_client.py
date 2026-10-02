#!/usr/bin/env python3
"""Call one tool through a fresh local official MCP stdio client.

This bypasses a host's cached tool schema while still exercising desktop_mcp's
actual stdio server and the same owner socket. Text content blocks are neither
printed nor copied into the result file; image blocks are saved separately.
"""

from __future__ import annotations

import argparse
import anyio
import base64
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from mcp import types
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


ROOT = Path(__file__).resolve().parent
MAX_IMAGE_BYTES = 30 * 1024 * 1024


def _read_params(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("params_file_must_contain_object")
    return value


def _validate_args(schema: dict[str, Any], params: dict[str, Any]) -> None:
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise ValueError("tool_schema_properties_invalid")
    unknown = set(params) - set(properties)
    if unknown and schema.get("additionalProperties") is False:
        raise ValueError("unknown_tool_argument")
    missing = set(schema.get("required", [])) - set(params)
    if missing:
        raise ValueError("required_tool_argument_missing")
    for key, value in params.items():
        field = properties.get(key)
        if not isinstance(field, dict):
            continue
        enum = field.get("enum")
        if enum is not None and value not in enum:
            raise ValueError("tool_argument_outside_current_enum")
        max_length = field.get("maxLength")
        if type(max_length) is int and isinstance(value, str) and len(value) > max_length:
            raise ValueError("tool_argument_too_long")
        expected = field.get("type")
        if expected == "string" and not isinstance(value, str):
            raise ValueError("tool_argument_type_invalid")
        if expected == "boolean" and not isinstance(value, bool):
            raise ValueError("tool_argument_type_invalid")
        if expected == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
            raise ValueError("tool_argument_type_invalid")
        if expected == "number" and (not isinstance(value, int | float) or isinstance(value, bool)):
            raise ValueError("tool_argument_type_invalid")
        if expected == "object" and not isinstance(value, dict):
            raise ValueError("tool_argument_type_invalid")
        if expected == "array" and not isinstance(value, list):
            raise ValueError("tool_argument_type_invalid")


def _redact_images(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: ("[image saved separately]" if key == "image_base64" else _redact_images(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_images(item) for item in value]
    return value


def _save_images(result: types.CallToolResult, output_dir: Path) -> list[dict[str, Any]]:
    saved = []
    for index, block in enumerate(result.content, 1):
        if not isinstance(block, types.ImageContent):
            continue
        try:
            image = base64.b64decode(block.data, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("invalid_mcp_image_encoding") from exc
        if not image or len(image) > MAX_IMAGE_BYTES:
            raise ValueError("mcp_image_size_invalid")
        mime_type = block.mime_type
        if mime_type == "image/png" and image.startswith(b"\x89PNG\r\n\x1a\n"):
            suffix = ".png"
        elif mime_type == "image/jpeg" and image.startswith(b"\xff\xd8\xff"):
            suffix = ".jpg"
        else:
            raise ValueError("unsupported_mcp_image_format")
        path = output_dir / f"image-{index:02d}{suffix}"
        path.write_bytes(image)
        saved.append({"path": str(path.resolve()), "mime_type": mime_type,
                      "bytes": len(image), "sha256": hashlib.sha256(image).hexdigest()})
    return saved


async def _call(tool_name: str, params: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError("output_directory_not_empty")
    output_dir.mkdir(parents=True, exist_ok=True)
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "desktop_mcp"],
        cwd=str(ROOT),
    )
    async with stdio_client(server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as client:
            await client.initialize()
            listed = await client.list_tools()
            tool = next((candidate for candidate in listed.tools if candidate.name == tool_name), None)
            if tool is None:
                raise ValueError("tool_not_available_from_fresh_stdio_server")
            _validate_args(tool.input_schema, params)
            result = await client.call_tool(tool_name, params)
    images = _save_images(result, output_dir)
    payload = {
        "tool": tool_name,
        "is_error": bool(result.is_error),
        "structured_content": _redact_images(result.structured_content),
        "content_types": [getattr(block, "type", "unknown") for block in result.content],
        "images": images,
    }
    result_path = output_dir / "result.json"
    result_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    return {"tool": tool_name, "is_error": bool(result.is_error),
            "result_path": str(result_path.resolve()), "images": images}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tool", help="tool name exposed by desktop_mcp")
    parser.add_argument("--params-file", type=Path, default=None, help="JSON object of tool arguments")
    parser.add_argument("--output-dir", type=Path, required=True, help="directory for result JSON and images")
    args = parser.parse_args(argv)
    try:
        params = _read_params(args.params_file)
        summary = anyio.run(_call, args.tool, params, args.output_dir)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        # Avoid printing server text, request contents, or response bodies.
        print(f"mcp_trial_failed:{str(exc) if isinstance(exc, ValueError) else type(exc).__name__}", file=sys.stderr)
        return 1
    except Exception as exc:  # MCP/provider internals may include sensitive message bodies.
        print(f"mcp_trial_failed:{type(exc).__name__}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=True, separators=(",", ":")))
    return 2 if summary["is_error"] else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
