from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from desktop_service import DesktopService
from local_service_probe import ProbeError, _mcp_observation_ok, _socket_request


class _FakeWorker:
    def __init__(self):
        self.click_entered = threading.Event()
        self.release_click = threading.Event()
        self.display = ""
        self.stop_called = False

    def start(self, app):
        if app != "kcalc":
            raise ValueError("unexpected_app")

    def stop(self):
        self.stop_called = True
        return {"stopped": True}

    def terminate(self):
        self.stop_called = True

    def _get_session(self):
        return SimpleNamespace(info=SimpleNamespace(session_type=SimpleNamespace(value="virtual")))

    def window_geometry(self, app=None, app_name=None):
        return (
            'Windows (1):\n- org.kde.kcalc "Calculator"\n    id: 7\n'
            '    frame: 0, 0, 1280x800\n    client: (0, 0, 1280x800)'
        )

    def active_window(self):
        return 'Active window:\n- org.kde.kcalc "Calculator" [active]\n    id: 7'

    def atspi_find(self, app):
        rows = [
            {
                "role": "button", "name": "One",
                "states": ["enabled", "sensitive", "showing", "visible"],
                "actions": ["Press"], "x": 20, "y": 20, "width": 40, "height": 40,
                "mapped": True, "text": "",
            }
        ]
        rows.append({
            "role": "text", "name": "Display", "states": ["editable", "enabled", "showing", "visible"],
            "actions": [], "x": 10, "y": 10, "width": 100, "height": 20,
            "mapped": True, "text": self.display,
        })
        return {"ok": True, "result": rows}

    def mouse_click(self, x, y, button="left"):
        self.click_entered.set()
        if not self.release_click.wait(3):
            raise TimeoutError("test_click_barrier_timeout")
        self.display = "1"

    def keyboard_type(self, text):
        raise AssertionError("typing is outside this test")

    keyboard_type_unicode = keyboard_type

    def _run_atspi(self, command, **params):
        return self.atspi_find(params["app_name"])

    def _run_kwin_query(self, params):
        return {
            "ok": True,
            "result": [{
                "app": "org.kde.kcalc", "caption": "Calculator", "id": "7",
                "frame": {"x": 0, "y": 0, "width": 1280, "height": 800},
            }],
        }


class ProbeProtocolTests(unittest.TestCase):
    def test_unix_client_checks_request_id_and_returns_result(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "probe.sock"
            server = __import__("socket").socket(__import__("socket").AF_UNIX, __import__("socket").SOCK_STREAM)
            server.bind(str(path))
            server.listen(1)

            def serve_once():
                connection, _ = server.accept()
                with connection:
                    request = json.loads(connection.makefile("rb").readline())
                    self.assertEqual(request["method"], "status")
                    self.assertEqual(request["context"]["transport"], "unix")
                    response = {"id": request["id"], "result": {"ok": True, "session": None}}
                    connection.sendall((json.dumps(response) + "\n").encode())

            thread = threading.Thread(target=serve_once)
            thread.start()
            result = _socket_request(path, "status")
            thread.join(timeout=2)
            server.close()
            self.assertEqual(result, {"ok": True, "session": None})

    def test_unix_client_rejects_mismatched_response_id(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "probe.sock"
            sock = __import__("socket").socket(__import__("socket").AF_UNIX, __import__("socket").SOCK_STREAM)
            sock.bind(str(path))
            sock.listen(1)

            def serve_once():
                connection, _ = sock.accept()
                with connection:
                    connection.makefile("rb").readline()
                    connection.sendall(b'{"id":"wrong","result":{"ok":true}}\n')

            thread = threading.Thread(target=serve_once)
            thread.start()
            try:
                with self.assertRaisesRegex(ProbeError, "unix_response_id_mismatch"):
                    _socket_request(path, "status")
            finally:
                thread.join(timeout=2)
                sock.close()

    def test_mcp_observation_requires_success_app_and_owner_session(self):
        call = SimpleNamespace(
            isError=False,
            structuredContent={
                "ok": True,
                "observation": {
                    "capture": {"session_id": "s-expected", "window": {"app": "org.kde.kcalc"}}
                },
            },
            content=[],
        )
        self.assertTrue(_mcp_observation_ok(call, "s-expected"))
        self.assertFalse(_mcp_observation_ok(call, "another-session"))
        call.isError = True
        self.assertFalse(_mcp_observation_ok(call, "s-expected"))

    def test_two_clients_cannot_interleave_kcalc_action(self):
        with tempfile.TemporaryDirectory() as temp:
            worker = _FakeWorker()
            service = DesktopService(
                audit_path=Path(temp) / "audit.jsonl",
                worker_factory=lambda: worker,
                allowed_apps={"kcalc"},
            )
            context_a = {"transport": "unix", "caller_node": "client-a"}
            context_b = {"transport": "local-mcp", "caller_node": "client-b"}
            started = service.dispatch("session_start", {"app": "kcalc", "mode": "guarded"}, context_a)
            session_id = started["session"]["session_id"]
            candidates = service.dispatch("candidates", {"app": "kcalc", "session_id": session_id}, context_a)
            one = [c for c in candidates["candidates"] if c["label"] == "One"]
            self.assertEqual(len(one), 1)
            action_params = {
                "app": "kcalc", "session_id": session_id, "action": "click",
                "target_ref": one[0]["ref"], "verification": "display_text", "expected": "1",
                "mode": "guarded",
            }
            first_result: dict = {}

            def first_client():
                first_result.update(service.dispatch("act", action_params, context_a))

            thread = threading.Thread(target=first_client)
            thread.start()
            self.assertTrue(worker.click_entered.wait(2), "first client never entered the action")
            second = service.dispatch("act", action_params, context_b)
            self.assertFalse(second.get("ok"))
            self.assertEqual(second.get("error", {}).get("code"), "session_busy")
            worker.release_click.set()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive(), "first action did not finish")
            self.assertTrue(first_result.get("ok"))
            self.assertEqual(first_result.get("verification"), "passed")
            service.close()
            self.assertTrue(worker.stop_called)


if __name__ == "__main__":
    unittest.main()
