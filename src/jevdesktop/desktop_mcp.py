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
    app = _string("Required to start selected_application scope; otherwise an optional current-target assertion. Omit when starting full scopes. An app name never switches windows.")
    session = _string("Owner-issued session ID.")
    mode = _string(enum=["supervised", "guarded", "yolo"])
    desktop_mode = _string("For selected_application scope only.",
                           enum=["virtual", "live"])
    access_scope = _string("Authorization boundary: selected app, isolated virtual desktop, or whole live desktop.",
                           enum=["selected_application", "full_virtual_desktop", "full_live_desktop"])
    tools = [
        ("desktop_capabilities", "Report supported local desktop operations and limits.", _obj(), True, False),
        ("desktop_workspace", "Discover windows in the current live workspace or isolated virtual session. Full live scope may reveal the whole workspace.", _obj(), True, False),
        ("desktop_profiles", "List configured launch profiles and current-scope access. app_allowlisted describes selected-application policy only; full scope does not require app entries.", _obj(), True, False),
        ("desktop_relays", "Read named MCP SSH relay state, timestamps and errors. Process/TCP status does not prove MCP readiness.", _obj(), True, False),
        ("desktop_status", "Report owner and active session status.", _obj(), True, False),
        ("desktop_session_start", "Start an owner-authorized scope. Full live scope may select any exact current window and preserves all existing apps at stop.",
         _obj({"app": app, "mode": mode, "desktop_mode": desktop_mode, "access_scope": access_scope,
               "owner_present_override": {"type": "boolean", "description": "Required for live mode because physical-input detection is unavailable."},
               "attach": {"type": "boolean", "description": "Selected-application scope only: attach an existing exact live window. Omit this flag for full scopes, which preserve preexisting live apps."},
               "window_id": _string("Exact current window ID. For full_live_desktop this selects the initial task window."),
               "semantic_candidates": {"type": "boolean", "description": "Default true only for selected_application virtual sessions; default false for live and full scopes. full_virtual_desktop currently supports screenshots only. Live AT-SPI opt-in also requires temporary_a11y."},
               "temporary_a11y": {"type": "boolean", "description": "Required only when enabling live semantic candidates; captured AT-SPI flags are restored during cleanup."}}, []), False, False),
        ("desktop_select_window", "In a full_live_desktop or full_virtual_desktop session, select and focus one currently enumerated exact window. This invalidates all older captures and semantic references.",
         _obj({"window_id": _string("Exact current window ID from desktop_workspace.")}, ["window_id"]), False, False),
        ("desktop_launch_app", "In a full_virtual_desktop session, launch a configured application profile by argv. No command string is accepted.",
         _obj({"profile_id": _string("Configured application profile ID.")}, ["profile_id"]), False, False),
        ("desktop_session_stop", "Stop the specified session, or the sole active session.", _obj({"session_id": session}), False, True),
        ("desktop_candidates", "Read fresh semantic refs, control names and screen-logical bounds when semantics are enabled. Full scopes currently use screenshots only. Editable values are omitted.", _obj({"app": app}), True, False),
        ("desktop_observe", "Observe the selected app, or request scope=desktop for a mapped full-screen image inside a full desktop scope. Images are returned only for explicit image or both output.", _obj({
            "app": app,
            "output": _string(enum=["metadata", "image", "data", "both"]),
            "scope": _string(enum=["app", "full", "crop", "desktop"]),
            "crop": _obj({"x": {"type": "integer"}, "y": {"type": "integer"}, "width": {"type": "integer", "minimum": 1}, "height": {"type": "integer", "minimum": 1}}, ["x", "y", "width", "height"]),
            "include_candidates": {"type": "boolean", "description": "Pair a NEW capture with fresh semantic refs and mapped image bounds. Sequential, not atomic. Costs two extra state reads; cannot reuse capture_id."},
            "capture_id": _string("Reuse an unexpired capture ID from the same session."),
            "questions": {"type": "array", "items": _obj({"field": {"type": "string", "maxLength": 80}, "type": _string(enum=["string", "number", "integer", "boolean", "array", "object"]), "description": {"type": "string", "maxLength": 500}, "nullable": {"type": "boolean"}}, ["field", "type", "description"]), "maxItems": 8},
        }, []), True, False),
        ("desktop_wait", "EXPERIMENTAL heartbeat: wait for a bounded screenshot condition using the configured reader. Prefer polling desktop_observe/desktop_candidates. Returns a fresh final app image; this never performs an action.", _obj({
            "app": app,
            "expected": {"type": "string", "minLength": 1, "maxLength": 500,
                "description": "Caller-authored visual condition. The reader can only return wait, wake, error or unexpected; it cannot act."},
            "timeout_seconds": {"type": "number", "minimum": 0.1, "maximum": 120},
        }, ["expected"]), True, False),
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
        }, ["action", "target_ref", "verification"]), False, True),
        ("desktop_input", "Send one input grounded in a fresh capture from this session. Image coordinates are relative to the returned crop. Input delivery is not proof of effect. Guarded/supervised input requires explicit approved=true. Re-observe after uncertainty; never auto-retry.", _obj({
            "app": app, "capture_id": _string("Current capture whose image was explicitly returned by desktop_observe. Older captures cannot rearm after input; post-input metadata must first be observed as image."),
            "action": _string(enum=["click", "double_click", "right_click", "keyboard_shortcut", "type_text", "scroll", "drag"]),
            "x": {"type": "integer"}, "y": {"type": "integer"},
            "end_x": {"type": "integer"}, "end_y": {"type": "integer"},
            "key": _string(enum=sorted(SHORTCUT_KEYS)), "text": {"type": "string", "maxLength": 4096},
            "direction": _string(enum=["up", "down", "left", "right"]),
            "steps": {"type": "integer", "minimum": 1, "maximum": 8},
            "approved": {"type": "boolean"},
        }, ["capture_id", "action"]), False, True),
        ("desktop_reconcile", "Acknowledge uncertain prior input, inspect current state and resume without replaying any input. This never proves the earlier task effect.",
         _obj({"app": app, "acknowledge_uncertain": {"type": "boolean"}}, ["acknowledge_uncertain"]), False, False),
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
    "desktop_select_window": "select_window",
    "desktop_launch_app": "launch_app",
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
                  instructions="Discover desktop_capabilities and desktop_workspace when connecting. Select access_scope explicitly: selected_application uses the configured app allowlist; full_virtual_desktop stays inside its isolated task desktop; full_live_desktop can select exact current workspace windows and preserves preexisting apps. Full live requires owner_present_override because physical-input detection is unavailable. Temporary AT-SPI opt-in is required only for semantic candidates. Use desktop_launch_app only for configured argv profiles in full_virtual_desktop; it accepts no command string. Use desktop_select_window with a fresh exact window ID before window-grounded work; switching invalidates old captures and semantic refs. For workspace-only full desktop viewing, desktop_observe(scope=desktop, output=image) returns a mapped screen capture and cannot authorize input. For screenshot-grounded input select a real window, call desktop_observe then desktop_input; coordinates are relative to that fresh image and never come from Jev. Input results have uncertain effect. Read fresh candidates before semantic actions; action calls use strong verifiers or observed (fresh post-state, effect uncertain). An observation image is returned only when output=image or output=both is explicitly requested. desktop_wait is a bounded visual readiness observation; inspect its returned final image before deciding whether to act.",
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
