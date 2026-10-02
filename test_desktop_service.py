from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from desktop_service import DesktopService, DesktopWorkerClient
from observation import ObservationError


class _Alive:
    def __init__(self): self.dead = False; self.pid = 501234
    def poll(self): return 1 if self.dead else None


class FakeClock:
    def __init__(self, value=1000.0): self.value = value
    def __call__(self): return self.value
    def advance(self, seconds): self.value += seconds


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
        self.saved_bytes = b""

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
    def document_key(self, operation):
        if operation == "select_all": self.rows[0]["text"] = ""
        elif operation == "save": self.saved_bytes = self.rows[0]["text"].encode("utf-8")
        else: raise ValueError("invalid_document_operation")
    def document_bytes(self): return self.saved_bytes
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
        capabilities = self.service.dispatch("capabilities")
        self.assertEqual(capabilities["allowed_apps"], ["kate"])
        self.assertEqual(capabilities["reader"]["project_provider_budget_usd"], 1.0)
        self.assertEqual(capabilities["reader"]["paid_reader"], "disabled_until_cost_reservation_available")
        denied = self.service.dispatch("session_start", {"app": "kcalc"})
        self.assertEqual(denied["error"]["code"], "app_not_allowed")
        self.start()
        duplicate = self.service.dispatch("session_start", {"app": "kate"})
        self.assertEqual(duplicate["error"]["code"], "session_exists")
        self.assertEqual(self.service.dispatch("status")["session"]["state"], "running")
        self.assertEqual(self.service.dispatch("status")["session"]["project_provider_reserved_usd"], 0.0)
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

    def test_replace_and_save_document_verify_editor_text_and_disk_bytes(self):
        self.start()
        content = "A task-owned handover with exact saved bytes. " + ("This bounded paragraph stays complete. " * 7)
        candidate = self.service.dispatch("candidates", {"app": "kate"})["candidates"][0]
        revised = self.service.dispatch("act", {"app": "kate", "action": "replace_document",
            "target_ref": candidate["ref"], "text": content,
            "verification": "target_text", "expected": content}, self.ctx)
        self.assertTrue(revised["ok"], revised)
        fresh = self.service.dispatch("candidates", {"app": "kate"})["candidates"][0]
        saved = self.service.dispatch("act", {"app": "kate", "action": "save_document",
            "target_ref": fresh["ref"], "verification": "document_saved"}, self.ctx)
        self.assertTrue(saved["ok"], saved)
        self.assertEqual(saved["evidence"], {"exact_bytes_saved": True})
        audit = (Path(self.tmp.name) / "audit.jsonl").read_text()
        self.assertNotIn(content, audit)

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
        self.service._session.observations = self.service._session.max_observations
        result = self.service.dispatch("observe", {"app": "kate"})
        self.assertEqual(result["error"]["code"], "task_observation_budget_exceeded")

    def test_observation_errors_keep_only_their_safe_code(self):
        self.start()
        session = self.service._session
        session.adapter.capture = lambda *args, **kwargs: (_ for _ in ()).throw(
            ObservationError("mapping_unavailable", "private capture path or caption"))
        result = self.service.dispatch("observe", {"app": "kate"})
        self.assertEqual(result["error"]["code"], "mapping_unavailable")
        self.assertNotIn("private", json.dumps(result))

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

    def test_start_reports_only_whitelisted_worker_failure_code(self):
        self.worker.start = lambda app: (_ for _ in ()).throw(RuntimeError("document_open_unconfirmed"))
        self.worker.recover_cleanup = lambda: True
        failed = self.service.dispatch("session_start", {"app": "kate"})
        self.assertEqual(failed["error"]["code"], "document_open_unconfirmed")
        self.assertNotIn("path", failed["error"]["message"].casefold())
        self.worker.start = lambda app: (_ for _ in ()).throw(RuntimeError("/tmp/private-window-title"))
        failed = self.service.dispatch("session_start", {"app": "kate"})
        self.assertEqual(failed["error"]["code"], "worker_failed")
        self.assertNotIn("private-window-title", json.dumps(failed))

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

    def test_idle_watchdog_cleans_session_without_another_client_call(self):
        clock = FakeClock()
        service = DesktopService(Path(self.tmp.name) / "watchdog.jsonl", allowed_apps={"kate"},
                                 worker_factory=lambda: self.worker, idle_timeout=10,
                                 max_session_lifetime=100, monotonic=clock, watchdog_interval=0.01)
        self.assertTrue(service.dispatch("session_start", {"app": "kate"})["ok"])
        clock.advance(10.1)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and service.dispatch("status")["session"] is not None:
            time.sleep(0.01)
        status = service.dispatch("status")
        self.assertIsNone(status["session"])
        self.assertEqual(status["lifecycle"]["last_stop_reason"], "idle_timeout")
        self.assertEqual(self.worker.stopped, 1)
        service.close()

    def test_status_polling_does_not_extend_idle_window(self):
        clock = FakeClock()
        service = DesktopService(Path(self.tmp.name) / "polling.jsonl", allowed_apps={"kate"},
                                 worker_factory=lambda: self.worker, idle_timeout=10,
                                 max_session_lifetime=100, monotonic=clock, watchdog_interval=3600)
        self.assertTrue(service.dispatch("session_start", {"app": "kate"})["ok"])
        activity = service._session.last_activity_clock
        for _ in range(5):
            clock.advance(1)
            status = service.dispatch("status")
            self.assertIsNotNone(status["session"])
        self.assertEqual(service._session.last_activity_clock, activity)
        clock.advance(5.1)
        service._expire_if_needed()
        self.assertIsNone(service.dispatch("status")["session"])
        service.close()

    def test_rejected_action_does_not_extend_idle_window(self):
        clock = FakeClock()
        service = DesktopService(Path(self.tmp.name) / "rejected.jsonl", allowed_apps={"kate"},
                                 worker_factory=lambda: self.worker, idle_timeout=10,
                                 max_session_lifetime=100, monotonic=clock, watchdog_interval=3600)
        self.assertTrue(service.dispatch("session_start", {"app": "kate"})["ok"])
        clock.advance(8)
        rejected = service.dispatch("act", {"app": "kate", "action": "not-an-action"})
        self.assertEqual(rejected["error"]["code"], "invalid_params")
        self.assertEqual(service._session.last_activity_clock, 1000.0)
        clock.advance(2.1)
        self.assertTrue(service._expire_if_needed())
        service.close()

    def test_watchdog_expiry_is_pinned_to_the_session_it_observed(self):
        clock = FakeClock()
        replacement = FakeWorker()
        workers = iter((self.worker, replacement))
        service = DesktopService(Path(self.tmp.name) / "pinned-expiry.jsonl", allowed_apps={"kate"},
                                 worker_factory=lambda: next(workers), idle_timeout=10,
                                 max_session_lifetime=100, monotonic=clock, watchdog_interval=3600)
        old_id = service.dispatch("session_start", {"app": "kate"})["session"]["session_id"]
        clock.advance(11)
        original_dispatch = service.dispatch

        def replace_before_stale_stop(method, params=None, context=None):
            if method == "session_stop" and context and context.get("transport") == "watchdog":
                self.assertTrue(original_dispatch("session_stop", {"session_id": old_id})["stopped"])
                self.assertTrue(original_dispatch("session_start", {"app": "kate"})["ok"])
            return original_dispatch(method, params, context)

        service.dispatch = replace_before_stale_stop
        self.assertTrue(service._expire_if_needed())
        status = service.dispatch("status")
        self.assertNotEqual(status["session"]["session_id"], old_id)
        self.assertIsNone(status["lifecycle"]["last_stop_reason"])
        self.assertEqual(replacement.stopped, 0)
        service.close()

    def test_broken_idle_session_uses_recovery_cleanup(self):
        clock = FakeClock()
        service = DesktopService(Path(self.tmp.name) / "broken-watchdog.jsonl", allowed_apps={"kate"},
                                 worker_factory=lambda: self.worker, idle_timeout=10,
                                 max_session_lifetime=100, monotonic=clock, watchdog_interval=3600)
        started = service.dispatch("session_start", {"app": "kate"})
        self.assertTrue(started["ok"], started)
        self.worker.stop = lambda: (_ for _ in ()).throw(RuntimeError("worker_failed"))
        recovered = []
        self.worker.recover_cleanup = lambda: recovered.append(True) or True
        service._session.state = "broken"
        clock.advance(10.1)
        self.assertTrue(service._expire_if_needed())
        self.assertIsNone(service.dispatch("status")["session"])
        self.assertEqual(recovered, [True])
        self.assertEqual(service.dispatch("status")["lifecycle"]["last_stop_reason"], "idle_timeout")
        service.close()

    def test_competing_stops_cannot_stop_a_replacement_session(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def blocked_stop():
            calls.append(True)
            entered.set()
            release.wait(2)
            self.worker.stopped += 1
            return {"stopped": True}

        self.worker.stop = blocked_stop
        old_id = self.start()
        outcomes = []
        first = threading.Thread(target=lambda: outcomes.append(
            self.service.dispatch("session_stop", {"session_id": old_id})))
        first.start()
        self.assertTrue(entered.wait(1))
        second = self.service.dispatch("session_stop", {"session_id": old_id})
        self.assertEqual(second["error"]["code"], "session_busy")
        self.assertEqual(self.service.dispatch("session_start", {"app": "kate"})["error"]["code"], "session_exists")
        release.set()
        first.join(1)
        self.assertFalse(first.is_alive())
        self.assertTrue(outcomes[0]["stopped"])
        replacement = self.service.dispatch("session_start", {"app": "kate"})
        self.assertTrue(replacement["ok"], replacement)
        self.assertEqual(self.service.dispatch("session_stop", {"session_id": old_id})["error"]["code"], "session_not_found")
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.service.dispatch("status")["session"]["state"], "running")

    def test_absolute_lifetime_expires_despite_recent_activity(self):
        clock = FakeClock()
        service = DesktopService(Path(self.tmp.name) / "lifetime.jsonl", allowed_apps={"kate"},
                                 worker_factory=lambda: self.worker, idle_timeout=100,
                                 max_session_lifetime=10, monotonic=clock, watchdog_interval=3600)
        self.assertTrue(service.dispatch("session_start", {"app": "kate"})["ok"])
        clock.advance(9)
        service._session.last_activity_clock = clock()
        self.assertTrue(service.dispatch("candidates", {"app": "kate"})["ok"])
        clock.advance(1.1)
        self.assertTrue(service._expire_if_needed())
        self.assertEqual(service.dispatch("status")["lifecycle"]["last_stop_reason"], "max_lifetime")
        service.close()

    def test_audit_has_request_correlation_and_dispatch_duration(self):
        self.start()
        result = self.service.dispatch("candidates", {"app": "kate"}, self.ctx)
        self.assertTrue(result["ok"])
        entries = [json.loads(line) for line in (Path(self.tmp.name) / "audit.jsonl").read_text().splitlines()]
        entry = entries[-1]
        self.assertRegex(entry["request_id"], r"^[0-9a-f]{32}$")
        self.assertIsInstance(entry["duration_ms"], (int, float))
        self.assertGreaterEqual(entry["duration_ms"], 0)


if __name__ == "__main__":
    unittest.main()
