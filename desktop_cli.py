"""Thin command-line client for the local desktop owner."""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import socket
import sys
from pathlib import Path
from typing import Any

from desktop_daemon import MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, default_socket_path

MAX_DESKTOP_TEXT_CHARS = 4096


def ipc_call(method: str, params: dict[str, Any] | None = None, *, transport: str = "local-cli",
             socket_path: Path | None = None, timeout: float = 180.0) -> dict[str, Any]:
    request_id = secrets.token_hex(12)
    request = {"id": request_id, "method": method, "params": params or {},
               "context": {"caller_node": "local", "transport": transport}}
    payload = (json.dumps(request, separators=(",", ":"), ensure_ascii=True) + "\n").encode()
    if len(payload) > MAX_REQUEST_BYTES:
        raise RuntimeError("request_too_large")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(str(socket_path or default_socket_path()))
        client.sendall(payload)
        chunks = bytearray()
        while not chunks.endswith(b"\n"):
            part = client.recv(min(65536, MAX_RESPONSE_BYTES + 1 - len(chunks)))
            if not part:
                raise RuntimeError("owner_disconnected")
            chunks.extend(part)
            if len(chunks) > MAX_RESPONSE_BYTES:
                raise RuntimeError("response_too_large")
    try:
        response = json.loads(chunks)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("invalid_owner_response") from exc
    if not isinstance(response, dict) or response.get("id") != request_id:
        raise RuntimeError("owner_response_id_mismatch")
    if "error" in response:
        error = response.get("error")
        if isinstance(error, dict):
            raise RuntimeError(f"{error.get('code', 'owner_error')}: {error.get('message', 'desktop owner request failed')}")
        raise RuntimeError("owner_error")
    result = response.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("invalid_owner_response")
    return result


def _read_text_file(path: str | None) -> str:
    if path is None:
        raise ValueError("--text-file is required for type_text")
    value = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    if len(value) > MAX_DESKTOP_TEXT_CHARS:
        raise ValueError("text_argument_too_long")
    return value


def _add_socket(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--socket", type=Path, default=argparse.SUPPRESS, help="owner Unix socket path")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jev-desktop",
        description="Local client for the single-owner Jev desktop service. No command auto-starts a daemon.",
        epilog=("Observation is app-scoped by default. `observe --output image|both` explicitly returns image bytes. "
                "Candidate values are withheld by default. Type text from --text-file (or stdin with '-'); typed text is never sent to Jev."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_socket(parser)
    commands = parser.add_subparsers(dest="command", required=True)
    daemon = commands.add_parser("daemon", help="run the local owner in foreground")
    daemon.add_argument("--foreground", action="store_true", required=True, help="mandatory; daemon never background-spawns itself")
    daemon.add_argument("--socket", type=Path, default=argparse.SUPPRESS)
    daemon.add_argument("--run-dir", type=Path, default=None)
    daemon.add_argument("--audit", type=Path, default=None)
    daemon.add_argument("--allow-app", action="append", default=[], required=True, help="explicit allowlist app identity (repeatable)")
    daemon.add_argument("--reader-provider", choices=("deepseek", "mimo", "generic"), default=None)
    daemon.add_argument("--reader-base-url", default=None)
    daemon.add_argument("--reader-model", default=None)
    daemon.add_argument("--reader-key-env", default=None, help="environment variable name, never a key value")
    daemon.add_argument("--max-reader-calls", type=int, default=None)
    daemon.add_argument("--idle-timeout", type=float, default=180.0,
                        help="stop the session after this many inactive seconds")
    daemon.add_argument("--max-session-lifetime", type=float, default=1800.0,
                        help="maximum session age in seconds")
    daemon.add_argument("--max-actions", type=int, default=64,
                        help="maximum input actions per session")
    daemon.add_argument("--max-observations", type=int, default=256,
                        help="maximum observations and candidate reads per session")
    for name, help_text in (("capabilities", "show owner capabilities"), ("status", "show owner/session status")):
        sub = commands.add_parser(name, help=help_text)
        _add_socket(sub)
    start = commands.add_parser("session-start", aliases=["start"], help="start an allowlisted virtual session")
    start.add_argument("app")
    start.add_argument("--mode", choices=("supervised", "guarded", "yolo"), default="guarded")
    _add_socket(start)
    stop = commands.add_parser("session-stop", help="stop a session, or the sole active session")
    stop.add_argument("--session-id")
    _add_socket(stop)
    session = commands.add_parser("session", help="start or stop the current virtual session")
    session_commands = session.add_subparsers(dest="session_command", required=True)
    nested_start = session_commands.add_parser("start", help="start an allowlisted virtual session")
    nested_start.add_argument("app")
    nested_start.add_argument("--mode", choices=("supervised", "guarded", "yolo"), default="guarded")
    _add_socket(nested_start)
    nested_stop = session_commands.add_parser("stop", help="stop a session, or the sole active session")
    nested_stop.add_argument("--session-id")
    _add_socket(nested_stop)
    candidates = commands.add_parser("candidates", help="enumerate fresh semantic candidates")
    candidates.add_argument("app")
    _add_socket(candidates)
    observe = commands.add_parser("observe", help="capture and observe an allowlisted app")
    observe.add_argument("app")
    observe.add_argument("--output", choices=("metadata", "image", "data", "both"), default="metadata")
    observe.add_argument("--scope", choices=("app", "full", "crop"), default="app")
    observe.add_argument("--crop", help="JSON crop object: {\"x\":int,\"y\":int,\"width\":int,\"height\":int}")
    observe.add_argument("--capture-id", help="reuse an unexpired capture ID")
    observe.add_argument("--questions-json", help="reader questions as a JSON list of field/type/description objects")
    _add_socket(observe)
    act = commands.add_parser("act", help="execute and verify one grounded action")
    act.add_argument("app")
    act.add_argument("action", choices=("click", "type_text", "replace_document", "save_document"))
    act.add_argument("target_ref")
    act.add_argument("--approved", action="store_true", help="record approval for supervised or guarded actions")
    act.add_argument("--verification", choices=("target_focused", "target_text", "fixture_state", "display_text", "document_saved"), required=True)
    act.add_argument("--expected", help="exact expected visible text (kept as a string)")
    act.add_argument("--text-file", help="read typing payload from this file; '-' reads stdin")
    _add_socket(act)
    cancel = commands.add_parser("cancel", help="cancel current work for a session")
    cancel.add_argument("--session-id")
    _add_socket(cancel)
    stop_all = commands.add_parser("stop", help="stop a session; use --all to stop all sessions and work")
    stop_all.add_argument("--all", action="store_true", required=True)
    _add_socket(stop_all)
    stop_all_alias = commands.add_parser("stop-all", help="alias for stop --all")
    _add_socket(stop_all_alias)
    shutdown = commands.add_parser("shutdown", help="stop the owner and its child/session")
    _add_socket(shutdown)
    return parser


def _emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=True, separators=(",", ":")))


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if not hasattr(args, "socket"):
        args.socket = None
    if args.command == "daemon":
        from desktop_daemon import main as daemon_main
        daemon_args = ["--foreground", "--run-dir", str(args.run_dir or "")]
        if args.socket:
            daemon_args += ["--socket", str(args.socket)]
        if args.audit:
            daemon_args += ["--audit", str(args.audit)]
        for app in args.allow_app:
            daemon_args += ["--allow-app", app]
        if args.reader_provider:
            daemon_args += ["--reader-provider", args.reader_provider]
        if args.reader_base_url:
            daemon_args += ["--reader-base-url", args.reader_base_url]
        if args.reader_model:
            daemon_args += ["--reader-model", args.reader_model]
        if args.reader_key_env:
            daemon_args += ["--reader-key-env", args.reader_key_env]
        if args.max_reader_calls is not None:
            daemon_args += ["--max-reader-calls", str(args.max_reader_calls)]
        daemon_args += ["--idle-timeout", str(args.idle_timeout),
                        "--max-session-lifetime", str(args.max_session_lifetime),
                        "--max-actions", str(args.max_actions),
                        "--max-observations", str(args.max_observations)]
        if args.run_dir is None:
            daemon_args = ["--foreground"] + (["--socket", str(args.socket)] if args.socket else [])
            if args.audit:
                daemon_args += ["--audit", str(args.audit)]
            for app in args.allow_app:
                daemon_args += ["--allow-app", app]
            if args.reader_provider:
                daemon_args += ["--reader-provider", args.reader_provider]
            if args.reader_base_url:
                daemon_args += ["--reader-base-url", args.reader_base_url]
            if args.reader_model:
                daemon_args += ["--reader-model", args.reader_model]
            if args.reader_key_env:
                daemon_args += ["--reader-key-env", args.reader_key_env]
            if args.max_reader_calls is not None:
                daemon_args += ["--max-reader-calls", str(args.max_reader_calls)]
            daemon_args += ["--idle-timeout", str(args.idle_timeout),
                            "--max-session-lifetime", str(args.max_session_lifetime),
                            "--max-actions", str(args.max_actions),
                            "--max-observations", str(args.max_observations)]
        return daemon_main(daemon_args)
    method_params: dict[str, Any]
    method = args.command
    if method in {"capabilities", "status"}:
        method_params = {}
    elif method in {"session-start", "start"} or (method == "session" and args.session_command == "start"):
        method, method_params = "session_start", {"app": args.app, "mode": args.mode}
    elif method in {"session-stop", "session"}:
        if method == "session" and args.session_command != "stop":
            raise ValueError("unknown session command")
        method, method_params = "session_stop", {"session_id": args.session_id} if args.session_id else {}
    elif method == "candidates":
        method_params = {"app": args.app}
    elif method == "observe":
        method_params = {"app": args.app, "scope": args.scope, "output": args.output}
        if args.crop:
            crop = json.loads(args.crop)
            if not isinstance(crop, dict):
                raise ValueError("--crop must be a JSON object")
            method_params["crop"] = crop
        if args.capture_id:
            method_params["capture_id"] = args.capture_id
        if args.questions_json:
            questions = json.loads(args.questions_json)
            if not isinstance(questions, list) or not all(isinstance(item, dict) for item in questions):
                raise ValueError("--questions-json must be a list of question objects")
            method_params["questions"] = questions
    elif method == "act":
        if args.expected is not None and len(args.expected) > MAX_DESKTOP_TEXT_CHARS:
            raise ValueError("expected_text_too_long")
        method_params = {"app": args.app, "action": args.action, "target_ref": args.target_ref,
                         "approved": args.approved,
                         "verification": args.verification}
        if args.expected is not None:
            method_params["expected"] = args.expected
        if args.action == "type_text":
            method_params["text"] = _read_text_file(args.text_file)
        elif args.action == "replace_document":
            method_params["text"] = _read_text_file(args.text_file)
        elif args.text_file is not None:
            raise ValueError("--text-file applies only to type_text or replace_document")
    elif method == "cancel":
        method_params = {"session_id": args.session_id} if args.session_id else {}
    elif method in {"stop", "stop-all", "shutdown"}:
        method = "stop_all" if method in {"stop", "stop-all"} else "shutdown"
        method_params = {}
    else:  # pragma: no cover
        raise RuntimeError("unknown command")
    try:
        response = ipc_call(method, method_params, socket_path=args.socket)
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    _emit(response)
    return 0 if response.get("ok", True) else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
