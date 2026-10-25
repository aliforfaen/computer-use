"""Official MCP SDK stdio facade over the local desktop owner socket."""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from typing import Any

import anyio
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from jevdesktop.desktop_cli import ipc_call
from jevdesktop.desktop_daemon import default_socket_path
from jevdesktop.transactions import SHORTCUT_KEYS


def _obj(properties: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": False}


def _string(description: str | None = None, enum: list[str] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"type": "string"}
    if description:
        result["description"] = description
    if enum:
        result["enum"] = enum
    return result


def _tools() -> list[types.Tool]:
    app = _string("Exact application identity from the configured allowlist.")
    session = _string("Owner-issued session ID.")
    mode = _string(enum=["supervised", "guarded", "yolo"])
    desktop_mode = _string("Virtual is the default. Live operates one newly launched owned app.",
                           enum=["virtual", "live"])
    tools = [
        ("desktop_capabilities", "Report supported local desktop operations and limits.", _obj(), True, False),
        ("desktop_workspace", "Discover live windows and the active workspace without taking focus. Captions are limited to allowlisted apps. Use exact window IDs to attach.", _obj(), True, False),
        ("desktop_profiles", "List configured application profiles and allowlist availability.", _obj(), True, False),
        ("desktop_relays", "Read named MCP SSH relay state, timestamps and errors. Process/TCP status does not prove MCP readiness.", _obj(), True, False),
        ("desktop_status", "Report owner and active session status.", _obj(), True, False),
        ("desktop_session_start", "Start an allowlisted profile or attach an existing live window. Live tasks keep their app foreground; stop preserves attached apps and restores original focus.",
         _obj({"app": app, "mode": mode, "desktop_mode": desktop_mode,
               "owner_present_override": {"type": "boolean", "description": "Required for live mode because physical-input detection is unavailable."},
               "attach": {"type": "boolean", "description": "Attach to an existing exact live window. Its app is never closed during cleanup."},
               "window_id": _string("Exact live window ID from desktop_workspace; required with attach=true."),
               "semantic_candidates": {"type": "boolean", "description": "Opt into AT-SPI. Default false for live, true for virtual. Live opt-in also requires temporary_a11y."},
               "temporary_a11y": {"type": "boolean", "description": "Required only when enabling live semantic candidates; captured AT-SPI flags are restored during cleanup."}}, ["app"]), False, False),
        ("desktop_session_stop", "Stop the specified session, or the sole active session.", _obj({"session_id": session}), False, True),
        ("desktop_candidates", "Read fresh semantic refs, control names and screen-logical bounds. Editable values are omitted.", _obj({"app": app}, ["app"]), True, False),
        ("desktop_observe", "Observe one allowlisted app. Images are returned only for explicit image or both output.", _obj({
            "app": app,
            "output": _string(enum=["metadata", "image", "data", "both"]),
            "scope": _string(enum=["app", "full", "crop"]),
            "crop": _obj({"x": {"type": "integer"}, "y": {"type": "integer"}, "width": {"type": "integer", "minimum": 1}, "height": {"type": "integer", "minimum": 1}}, ["x", "y", "width", "height"]),
            "include_candidates": {"type": "boolean", "description": "Pair a NEW capture with fresh semantic refs and mapped image bounds. Sequential, not atomic. Costs two extra state reads; cannot reuse capture_id."},
            "capture_id": _string("Reuse an unexpired capture ID from the same session."),
            "questions": {"type": "array", "items": _obj({"field": {"type": "string", "maxLength": 80}, "type": _string(enum=["string", "number", "integer", "boolean", "array", "object"]), "description": {"type": "string", "maxLength": 500}, "nullable": {"type": "boolean"}}, ["field", "type", "description"]), "maxItems": 8},
        }, ["app"]), True, False),
        ("desktop_wait", "EXPERIMENTAL heartbeat: wait for a bounded screenshot condition using the configured reader. Prefer polling desktop_observe/desktop_candidates. Returns a fresh final app image; this never performs an action.", _obj({
            "app": app,
            "expected": {"type": "string", "minLength": 1, "maxLength": 500,
                "description": "Caller-authored visual condition. The reader can only return wait, wake, error or unexpected; it cannot act."},
            "timeout_seconds": {"type": "number", "minimum": 0.1, "maximum": 120},
        }, ["app", "expected"]), True, False),
        ("desktop_act", "Perform a fresh grounded action. Use verification=observed to deliver input and collect fresh state without claiming the effect; strong verifiers prove specific postconditions.", _obj({
            "app": app,
            "action": _string(enum=["click", "type_text", "replace_document", "save_document", "navigate_url", "scroll", "keyboard_shortcut"]),
            "target_ref": _string("Fresh candidate reference from desktop_candidates or desktop_observe with include_candidates."),
            "key": _string("Named shortcut for keyboard_shortcut; requires verification=observed and a focused target.", enum=sorted(SHORTCUT_KEYS)),
            "approved": {"type": "boolean", "description": "Explicit caller approval for this action under supervised/guarded policy."},
            "verification": _string(enum=["target_focused", "target_text", "fixture_state", "display_text", "document_saved", "navigation_url", "window_title", "visible_text", "scroll_changed", "observed"]),
            "expected": {"type": "string", "maxLength": 4096, "description": "Exact postcondition. navigation_url checks the submitted HTTP(S) host and path in Firefox's address bar (Firefox may hide the scheme); it does not prove page readiness. For window_title or visible_text, use the expected resulting text."},
            "text": {"type": "string", "maxLength": 4096, "description": "Text for a verified accessible editor. replace_document is Kate-only and edits only the current focused editor in its owner-generated task document. Text is never included in audit or reader context."},
            "direction": _string(enum=["up", "down"]),
            "steps": {"type": "integer", "minimum": 1, "maximum": 8},
        }, ["app", "action", "target_ref", "verification"]), False, True),
        ("desktop_input", "Send one input grounded in a fresh capture from this session. Image coordinates are relative to the returned crop. Input delivery is not proof of effect. Guarded/supervised input requires explicit approved=true. Re-observe after uncertainty; never auto-retry.", _obj({
            "app": app, "capture_id": _string("Current capture whose image was explicitly returned by desktop_observe. Older captures cannot rearm after input; post-input metadata must first be observed as image."),
            "action": _string(enum=["click", "double_click", "right_click", "keyboard_shortcut", "type_text", "scroll", "drag"]),
            "x": {"type": "integer"}, "y": {"type": "integer"},
            "end_x": {"type": "integer"}, "end_y": {"type": "integer"},
            "key": _string(enum=sorted(SHORTCUT_KEYS)), "text": {"type": "string", "maxLength": 4096},
            "direction": _string(enum=["up", "down", "left", "right"]),
            "steps": {"type": "integer", "minimum": 1, "maximum": 8},
            "approved": {"type": "boolean"},
        }, ["app", "capture_id", "action"]), False, True),
        ("desktop_reconcile", "Acknowledge uncertain prior input, inspect current state and resume without replaying any input. This never proves the earlier task effect.",
         _obj({"app": app, "acknowledge_uncertain": {"type": "boolean"}}, ["app", "acknowledge_uncertain"]), False, False),
        ("desktop_cancel", "Cancel current work for a session, or the current owner task.", _obj({"session_id": session}), False, False),
        ("desktop_stop_all", "Stop all active sessions and work.", _obj(), False, True),
    ]
    return [types.Tool(name=name, description=description, inputSchema=schema,
                       annotations=types.ToolAnnotations(readOnlyHint=readonly,
                                                         destructiveHint=destructive,
                                                         idempotentHint=readonly,
                                                         openWorldHint=False))
            for name, description, schema, readonly, destructive in tools]


METHODS = {
    "desktop_capabilities": "capabilities",
    "desktop_workspace": "workspace",
    "desktop_profiles": "profiles",
    "desktop_relays": "relays",
    "desktop_input": "input",
    "desktop_reconcile": "reconcile",
    "desktop_status": "status",
    "desktop_session_start": "session_start",
    "desktop_session_stop": "session_stop",
    "desktop_candidates": "candidates",
    "desktop_observe": "observe",
    "desktop_wait": "wait",
    "desktop_act": "act",
    "desktop_cancel": "cancel",
    "desktop_stop_all": "stop_all",
}


def _scrub_candidate_values(value: Any) -> Any:
    if isinstance(value, list):
        return [_scrub_candidate_values(item) for item in value]
    if isinstance(value, dict):
        return {key: _scrub_candidate_values(item) for key, item in value.items()
                if key not in {"value", "editable_value", "current_value"}}
    return value


def _extract_image(value: Any, blocks: list[types.ContentBlock]) -> Any:
    if isinstance(value, list):
        return [_extract_image(item, blocks) for item in value]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key == "image_base64" and isinstance(item, str):
                try:
                    base64.b64decode(item, validate=True)
                except (ValueError, TypeError):
                    result["image_error"] = "invalid_image_encoding"
                else:
                    image_bytes = base64.b64decode(item)
                    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
                        mime_type = "image/png"
                    elif image_bytes.startswith(b"\xff\xd8\xff"):
                        mime_type = "image/jpeg"
                    else:
                        result["image_error"] = "unsupported_image_format"
                        continue
                    blocks.append(types.ImageContent(type="image", data=item, mimeType=mime_type))
                continue
            result[key] = _extract_image(item, blocks)
        return result
    return value


def _build_server(socket_path: Path | None = None) -> Server:
    async def list_tools(_ctx, _params):
        return types.ListToolsResult(tools=_tools())

    async def call_tool(_ctx, request_params):
        name = request_params.name
        if name not in METHODS:
            return types.CallToolResult(content=[types.TextContent(type="text", text="unknown_tool")], isError=True)
        args = request_params.arguments or {}
        method_params = dict(args)
        if name == "desktop_act":
            action = method_params.get("action")
            if action in {"type_text", "replace_document", "navigate_url"} and not isinstance(method_params.get("text"), str):
                return types.CallToolResult(content=[types.TextContent(type="text", text="text_required_for_type_text")], isError=True)
            if action in {"click", "save_document", "scroll", "keyboard_shortcut"} and "text" in method_params:
                return types.CallToolResult(content=[types.TextContent(type="text", text="text_not_valid_for_click")], isError=True)
            if action == "scroll" and (method_params.get("direction") not in {"up", "down"}
                                       or type(method_params.get("steps")) is not int):
                return types.CallToolResult(content=[types.TextContent(type="text", text="scroll_direction_and_steps_required")], isError=True)
            if action != "scroll" and any(key in method_params for key in ("direction", "steps")):
                return types.CallToolResult(content=[types.TextContent(type="text", text="scroll_args_only_valid_for_scroll")], isError=True)
        try:
            result = await anyio.to_thread.run_sync(
                lambda: ipc_call(METHODS[name], method_params, transport="local-mcp", socket_path=socket_path),
                abandon_on_cancel=False,
            )
        except (OSError, RuntimeError) as exc:
            return types.CallToolResult(content=[types.TextContent(type="text", text=str(exc)[:400])], isError=True)
        if name == "desktop_candidates":
            result = _scrub_candidate_values(result)
        image_blocks: list[types.ContentBlock] = []
        output = _extract_image(result, image_blocks)
        text = json.dumps(output, ensure_ascii=True, separators=(",", ":"))
        if isinstance(output, dict) and output.get("ok") is False:
            return types.CallToolResult(content=[types.TextContent(type="text", text=text)],
                                        structuredContent=output, isError=True)
        return types.CallToolResult(content=[types.TextContent(type="text", text=text), *image_blocks],
                                    structuredContent=output, isError=False)

    return Server("jev-desktop", version="0.1.0",
                  instructions="Discover desktop_workspace and desktop_profiles when connecting. Use desktop_relays to inspect available app MCP routes. Live sessions may attach an existing window and keep the task app foreground; semantic candidates are opt-in. For screenshot-grounded work use desktop_observe then desktop_input; coordinates are relative to that fresh image and never come from Jev. An observed input result has uncertain effect. This server is a local client of one desktop owner. App access is explicitly allowlisted. Live mode requires an explicit owner-present override because physical-input detection is unavailable; temporary AT-SPI opt-in is required only for semantic candidates. It preserves attached apps, closes only task-launched apps, and restores the original window on stop. Read fresh candidates before acting; act accepts strong code-owned verifiers or observed (fresh post-state, effect uncertain). Grounding bounds use screen logical coordinates; paired image bounds are returned with include_candidates=true. An observation image is returned only when output=image or output=both is explicitly requested. desktop_wait is a bounded visual readiness observation; inspect its returned final image before deciding whether to act.",
                  on_list_tools=list_tools, on_call_tool=call_tool)


async def _run(socket_path: Path | None = None) -> None:
    server = _build_server(socket_path)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main(argv: list[str] | None = None) -> int:
    parser = __import__("argparse").ArgumentParser(prog="jev-desktop-mcp", description="Connect MCP stdio to the local Jev desktop owner.")
    parser.add_argument("--socket", type=Path, default=None, help="owner Unix socket path")
    args = parser.parse_args(argv)
    try:
        anyio.run(_run, args.socket)
    except (OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
