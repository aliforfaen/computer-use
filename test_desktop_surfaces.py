"""Synthetic tests for the local socket, thin CLI client and official MCP stdio surface."""

from __future__ import annotations

import base64
import json
import os
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from desktop_cli import MAX_DESKTOP_TEXT_CHARS, _build_parser, _read_text_file, ipc_call, main as cli_main
from desktop_daemon import OwnerLock, RequestHandler, DesktopSocketServer


ROOT = Path(__file__).resolve().parent
PNG_1X1 = base64.b64encode(
    bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000b49444154789c636000020000050001a5f645400000000049454e44ae426082")
).decode("ascii")


class FakeOwner:
    def __init__(self):
        self.calls = []
        self.closed = False
        self.lock = threading.Lock()

    def dispatch(self, method, params, context):
        with self.lock:
            self.calls.append((method, params, context))
        if method == "capabilities":
            return {"ok": True, "virtual_only": True, "allowed_apps": ["kate"]}
        if method == "status":
            return {"ok": True, "session": None}
        if method == "candidates":
            return {"ok": True, "app": params.get("app"), "candidates": [{"ref": "r1", "role": "text", "label": "Editor", "value": "secret editable text"}]}
        if method == "observe":
            return {"ok": True, "observation": {"capture": {"capture_id": "cap-1", "image_sha256": "abc"}, "image_base64": PNG_1X1}}
        if method == "act":
            return {"ok": False, "error": {"code": "approval_required", "message": "This action requires explicit approval."}}
        return {"ok": True, "stopped": True}

    def close(self):
        self.closed = True


class _FakeIpcHandler(socketserver.StreamRequestHandler):
    def handle(self):
        raw = self.rfile.readline()
        try:
            req = json.loads(raw)
        except Exception:
            return
        method = req.get("method")
        if method == "desktop_observe" or method == "observe":
            result = {"ok": True, "observation": {"capture": {"capture_id": "mcp-capture", "image_sha256": "fixture-hash"}, "image_base64": PNG_1X1}}
        elif method == "desktop_candidates" or method == "candidates":
            result = {"ok": True, "app": "kate", "candidates": [{"ref": "r1", "label": "Editor", "value": "must not escape"}]}
        elif method == "desktop_act" or method == "act":
            result = {"ok": False, "error": {"code": "approval_required", "message": "approval required"}}
        else:
            result = {"ok": True, "virtual_only": True, "allowed_apps": ["kate"]}
        self.wfile.write((json.dumps({"id": req["id"], "result": result}) + "\n").encode())


class _FakeIpcServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class DesktopSurfaceTests(unittest.TestCase):
    def test_cli_uses_configured_default_socket_without_explicit_argument(self):
        with patch("desktop_cli.ipc_call", return_value={"ok": True, "session": None}) as call:
            with redirect_stdout(StringIO()):
                self.assertEqual(cli_main(["status"]), 0)
            call.assert_called_once_with("status", {}, socket_path=None)

    def test_cli_wait_forwards_a_bounded_visual_condition(self):
        with patch("desktop_cli.ipc_call", return_value={"ok": True, "wait": {"status": "ready"}}) as call:
            with redirect_stdout(StringIO()):
                self.assertEqual(cli_main(["wait", "firefox", "--expected", "heading visible", "--timeout", "12"]), 0)
            call.assert_called_once_with("wait", {"app": "firefox", "expected": "heading visible",
                                                   "timeout_seconds": 12.0}, socket_path=None)

    def test_cli_socket_argument_survives_both_supported_positions(self):
        for argv in (["--socket", "/tmp/a.sock", "status"], ["status", "--socket", "/tmp/b.sock"]):
            args = _build_parser().parse_args(argv)
            self.assertEqual(str(args.socket), "/tmp/a.sock" if argv[0] == "--socket" else "/tmp/b.sock")

    def test_cli_exposes_lifecycle_caps_and_kate_document_actions(self):
        daemon = _build_parser().parse_args(["daemon", "--foreground", "--allow-app", "kate"])
        self.assertEqual(daemon.idle_timeout, 180.0)
        self.assertEqual(daemon.max_session_lifetime, 1800.0)
        self.assertEqual(daemon.max_actions, 64)
        self.assertEqual(daemon.max_observations, 256)
        replacement = _build_parser().parse_args([
            "act", "kate", "replace_document", "ref-1", "--verification", "target_text", "--text-file", "-"
        ])
        self.assertEqual(replacement.action, "replace_document")
        saved = _build_parser().parse_args([
            "act", "kate", "save_document", "ref-1", "--verification", "document_saved"
        ])
        self.assertEqual(saved.verification, "document_saved")
        navigation = _build_parser().parse_args([
            "act", "firefox", "navigate_url", "ref-1", "--verification", "navigation_url", "--text-file", "-"
        ])
        self.assertEqual(navigation.action, "navigate_url")
        scroll = _build_parser().parse_args([
            "act", "firefox", "scroll", "ref-2", "--verification", "scroll_changed", "--direction", "down", "--steps", "2"
        ])
        self.assertEqual((scroll.direction, scroll.steps), ("down", 2))
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as text_file:
            text_file.write("x" * (MAX_DESKTOP_TEXT_CHARS + 1))
            text_file.flush()
            with self.assertRaisesRegex(ValueError, "text_argument_too_long"):
                _read_text_file(text_file.name)

    def test_mcp_schema_advertises_document_actions_and_saved_verifier(self):
        from desktop_mcp import _tools

        by_name = {tool.name: tool for tool in _tools()}
        action = by_name["desktop_act"].input_schema["properties"]
        self.assertIn("replace_document", action["action"]["enum"])
        self.assertIn("save_document", action["action"]["enum"])
        self.assertIn("document_saved", action["verification"]["enum"])
        self.assertIn("navigate_url", action["action"]["enum"])
        self.assertIn("scroll", action["action"]["enum"])
        self.assertIn("scroll_changed", action["verification"]["enum"])
        self.assertEqual(action["text"]["maxLength"], 4096)
        self.assertEqual(action["expected"]["maxLength"], 4096)

    def test_ipc_dispatch_carries_local_provenance_and_expected_shape(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "owner.sock"
            owner = FakeOwner()
            server = DesktopSocketServer(path, owner)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                result = ipc_call("act", {"action": "click", "expected": "1"}, socket_path=path)
                self.assertFalse(result["ok"])
                method, params, context = owner.calls[0]
                self.assertEqual(method, "act")
                self.assertEqual(params["expected"], "1")
                self.assertEqual(context, {"caller_node": "local", "transport": "local-cli"})
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_lock_excludes_a_second_owner_without_stale_pid_logic(self):
        with tempfile.TemporaryDirectory() as folder:
            lock_path = Path(folder) / "owner.sock.lock"
            first = OwnerLock(lock_path)
            first.acquire()
            try:
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    OwnerLock(lock_path).acquire()
            finally:
                first.close()

    def test_official_mcp_stdio_handshake_observe_image_and_error(self):
        from mcp.client.session import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
        import anyio

        with tempfile.TemporaryDirectory() as folder:
            socket_path = Path(folder) / "fake.sock"
            fake_server = _FakeIpcServer(str(socket_path), _FakeIpcHandler)
            threading.Thread(target=fake_server.serve_forever, daemon=True).start()

            async def exercise():
                params = StdioServerParameters(command=sys.executable,
                    args=["-m", "desktop_mcp", "--socket", str(socket_path)],
                    env={"PYTHONPATH": str(ROOT)}, cwd=ROOT)
                async with stdio_client(params) as (reader, writer):
                    async with ClientSession(reader, writer) as client:
                        await client.initialize()
                        listed = await client.list_tools()
                        names = {tool.name for tool in listed.tools}
                        self.assertIn("desktop_observe", names)
                        observed = await client.call_tool("desktop_observe", {"app": "kate", "output": "image"})
                        self.assertFalse(observed.is_error)
                        self.assertEqual(observed.structured_content["observation"]["capture"]["capture_id"], "mcp-capture")
                        images = [block for block in observed.content if getattr(block, "type", None) == "image"]
                        self.assertEqual(len(images), 1)
                        self.assertEqual(images[0].mime_type, "image/png")
                        candidate = await client.call_tool("desktop_candidates", {"app": "kate"})
                        self.assertNotIn("must not escape", str(candidate.structured_content))
                        denied = await client.call_tool("desktop_act", {"app": "kate", "action": "click", "target_ref": "r1", "verification": "target_focused"})
                        self.assertTrue(denied.is_error)
            try:
                anyio.run(exercise)
            finally:
                fake_server.shutdown()
                fake_server.server_close()


if __name__ == "__main__":
    unittest.main()
