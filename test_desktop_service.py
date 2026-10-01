from __future__ import annotations

import json
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

from desktop_service import DesktopService, DesktopWorkerClient


class _Alive:
    def __init__(self): self.dead = False; self.pid = 501234
    def poll(self): return 1 if self.dead else None


class FakeWorker:
    def __init__(self):
        self.proc = _Alive()
        self.rows = [{"role": "text", "name": "", "states": ["editable", "enabled", "sensitive", "showing", "visible", "focused"],
                      "actions": ["SetFocus"], "x": 10, "y": 20, "width": 300, "height": 100,
                      "mapped": True, "text": ""}]
        self.block = False
        self.entered = threading.Event()
        self.release = threading.Event()
        self.stopped = 0
        self.fail_start = False

    def start(self, app):
        if self.fail_start: raise RuntimeError("start_failed")
        return {"started": True}
    def stop(self): self.stopped += 1; return {"stopped": True}
    def window_query(self):
        return {"ok": True, "result": [{"id": "window-1", "app": "org.kde.kate", "caption": "private caption",
                                         "frame": {"x": 0, "y": 0, "width": 640, "height": 400}}]}
    def atspi_find(self, app):
        if self.block:
            self.entered.set(); self.release.wait(2)
        return {"ok": True, "result": [dict(row) for row in self.rows]}
    def window_geometry(self, app=None, *, app_name=None):
        return ('Windows (1):\n- org.kde.kate "private caption"\n    id: window-1\n'
                '    frame: 0, 0, 640x400\n    client: (0, 0, 640x400)')
    def active_window(self): return 'Active window:\n- org.kde.kate "private caption" [active]\n    id: window-1'
    def mouse_click(self, x, y, button="left"): pass
    def keyboard_type(self, text): self.rows[0]["text"] += text
    def screenshot(self, include_cursor=False): raise AssertionError("not used")
    def _run_kwin_query(self, _): return self.window_query()
    def _run_atspi(self, command, **params): return self.atspi_find(params["app_name"])
    def _get_session(self):
        class Info: session_type = type("SessionType", (), {"value": "virtual"})()
        return type("Session", (), {"info": Info()})()


class DesktopServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.worker = FakeWorker()
        self.service = DesktopService(Path(self.tmp.name) / "audit.jsonl", allowed_apps={"kate"},
                                      worker_factory=lambda: self.worker, stop_timeout=0.05)
        self.ctx = {"caller_node": "local-test", "transport": "unit"}

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def start(self):
        response = self.service.dispatch("session_start", {"app": "kate"}, self.ctx)
        self.assertTrue(response["ok"], response)
        return response["session"]["session_id"]

    def test_allowlist_single_owner_and_status_does_not_hold_lock(self):
        self.assertEqual(self.service.dispatch("capabilities")["allowed_apps"], ["kate"])
        denied = self.service.dispatch("session_start", {"app": "kcalc"})
        self.assertEqual(denied["error"]["code"], "app_not_allowed")
        self.start()
        duplicate = self.service.dispatch("session_start", {"app": "kate"})
        self.assertEqual(duplicate["error"]["code"], "session_exists")
        self.assertEqual(self.service.dispatch("status")["session"]["state"], "running")
        candidates = self.service.dispatch("candidates", {"app": "kate"})
        self.assertTrue(candidates["ok"], candidates)

    def test_exact_text_action_and_audit_never_contains_typed_text(self):
        self.start()
        candidates = self.service.dispatch("candidates", {"app": "kate"})["candidates"]
        result = self.service.dispatch("act", {"app": "kate", "action": "type_text",
                                                  "target_ref": candidates[0]["ref"], "text": "private typed payload",
                                                  "verification": "target_text", "expected": "private typed payload"}, self.ctx)
        self.assertTrue(result["ok"], result)
        log = (Path(self.tmp.name) / "audit.jsonl").read_text()
        self.assertNotIn("private typed payload", log)
        self.assertNotIn("private caption", log)
        entries = [json.loads(line) for line in log.splitlines()]
        self.assertEqual(entries[-1]["caller_node"], "local-test")

    def test_competing_call_is_denied_and_cancel_is_available(self):
        self.start()
        self.worker.block = True
        outcome = []
        thread = threading.Thread(target=lambda: outcome.append(self.service.dispatch("candidates", {"app": "kate"})))
        thread.start()
        self.assertTrue(self.worker.entered.wait(1))
        denied = self.service.dispatch("candidates", {"app": "kate"})
        self.assertEqual(denied["error"]["code"], "session_busy")
        cancelled = self.service.dispatch("cancel", {})
        self.assertTrue(cancelled["cancelled"])
        self.worker.release.set()
        thread.join(1)
        self.assertFalse(thread.is_alive())

    def test_dead_worker_marks_session_broken_and_stop_all_is_truthful(self):
        self.start()
        self.worker.proc.dead = True
        status = self.service.dispatch("status")["session"]
        self.assertEqual(status["state"], "broken")
        self.assertIsNotNone(status["worker_pid"])
        stopped = self.service.dispatch("stop_all")
        self.assertTrue(stopped["ok"], stopped)
        self.assertIsNone(self.service.dispatch("status")["session"])

    def test_observation_budget_is_enforced_by_owner(self):
        self.start()
        self.service._session.observations = 16
        result = self.service.dispatch("observe", {"app": "kate"})
        self.assertEqual(result["error"]["code"], "task_observation_budget_exceeded")

    def test_typing_cannot_verify_an_unchanged_expected_value(self):
        self.start()
        candidate = self.service.dispatch("candidates", {"app": "kate"})["candidates"][0]
        result = self.service.dispatch("act", {"app": "kate", "action": "type_text",
            "target_ref": candidate["ref"], "text": "new text",
            "verification": "target_text", "expected": ""})
        self.assertEqual(result["error"]["code"], "action_precondition_failed")
        self.assertEqual(self.worker.rows[0]["text"], "")

    def test_restart_after_recovering_dead_worker_uses_new_child(self):
        self.start()
        self.worker.proc.dead = True
        self.worker.stop = lambda: (_ for _ in ()).throw(RuntimeError("worker_failed"))
        self.worker.recover_cleanup = lambda: True
        stopped = self.service.dispatch("stop_all")
        self.assertTrue(stopped["stopped"], stopped)
        replacement = FakeWorker()
        self.service.worker_factory = lambda: replacement
        restarted = self.service.dispatch("session_start", {"app": "kate"})
        self.assertTrue(restarted["ok"], restarted)
        self.assertIs(self.service._worker, replacement)

    def test_deny_default_allowlist_and_unconfirmed_start_keeps_broken_owner(self):
        empty = DesktopService(Path(self.tmp.name) / "empty.jsonl", worker_factory=lambda: FakeWorker())
        self.assertEqual(empty.dispatch("capabilities")["allowed_apps"], [])
        self.assertEqual(empty.dispatch("session_start", {"app": "kate"})["error"]["code"], "app_not_allowed")
        empty.close()

        self.worker.fail_start = True
        failed = self.service.dispatch("session_start", {"app": "kate"})
        self.assertEqual(failed["error"]["code"], "worker_failed")
        status = self.service.dispatch("status")["session"]
        self.assertEqual(status["state"], "broken")
        self.assertFalse(status["active"])
        self.assertEqual(self.service.dispatch("session_start", {"app": "kate"})["error"]["code"], "session_exists")

    def test_cancelled_candidate_read_does_not_return_success(self):
        self.start()
        self.worker.block = True
        outcome = []
        thread = threading.Thread(target=lambda: outcome.append(self.service.dispatch("candidates", {"app": "kate"})))
        thread.start()
        self.assertTrue(self.worker.entered.wait(1))
        self.service.dispatch("cancel", {})
        self.worker.release.set()
        thread.join(1)
        self.assertEqual(outcome[0]["error"]["code"], "cancelled")

    def test_zombie_process_is_not_counted_as_live(self):
        with patch.object(DesktopWorkerClient, "_proc_info", return_value={"state": "Z", "pgrp": 7, "start_ticks": 10}):
            self.assertFalse(DesktopWorkerClient._same_live_process(7, 10))


if __name__ == "__main__":
    unittest.main()
