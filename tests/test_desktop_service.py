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
from vision_reader import ReaderResult


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
        self.app = "kate"
        self.keys = []
        self.scroll_calls = []
        self.delayed_scroll_reads = 0
        self.pending_scroll_delta = 0

    def start(self, app):
        if self.fail_start: raise RuntimeError("start_failed")
        self.app = app
        return {"started": True}
    def stop(self): self.stopped += 1; return {"stopped": True}
    def window_query(self):
        app_id = {"kate": "org.kde.kate", "firefox": "org.mozilla.firefox", "kcalc": "org.kde.kcalc"}[self.app]
        return {"ok": True, "result": [{"id": "window-1", "app": app_id, "caption": "private caption",
                                         "frame": {"x": 0, "y": 0, "width": 640, "height": 400}}]}
    def atspi_find(self, app):
        if self.block:
            self.entered.set(); self.release.wait(2)
        if self.pending_scroll_delta:
            self.delayed_scroll_reads -= 1
            if self.delayed_scroll_reads <= 0:
                for row in self.rows:
                    if row.get("role") == "scroll bar": row["value"] = row.get("value", 0) + self.pending_scroll_delta * 120
                    elif row.get("role") == "link": row["y"] -= self.pending_scroll_delta * 120
                self.pending_scroll_delta = 0
        return {"ok": True, "result": [dict(row) for row in self.rows]}
    def window_geometry(self, app=None, *, app_name=None):
        app_id = {"kate": "org.kde.kate", "firefox": "org.mozilla.firefox", "kcalc": "org.kde.kcalc"}[self.app]
        return (f'Windows (1):\n- {app_id} "private caption"\n    id: window-1\n'
                '    frame: 0, 0, 640x400\n    client: (0, 0, 640x400)')
    def active_window(self):
        app_id = {"kate": "org.kde.kate", "firefox": "org.mozilla.firefox", "kcalc": "org.kde.kcalc"}[self.app]
        return f'Active window:\n- {app_id} "private caption" [active]\n    id: window-1'
    def mouse_click(self, x, y, button="left"): pass
    def mouse_scroll(self, x, y, delta, *, discrete=False, steps=1):
        self.scroll_calls.append((x, y, delta, steps))
        if self.delayed_scroll_reads:
            self.pending_scroll_delta = delta
            return
        for row in self.rows:
            if row.get("role") == "scroll bar": row["value"] = row.get("value", 0) + delta * 120
            elif row.get("role") == "link": row["y"] -= delta * 120
    def keyboard_type(self, text): self.rows[0]["text"] = text
    def keyboard_key(self, key): self.keys.append(key)
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

    def start(self, app="kate"):
        response = self.service.dispatch("session_start", {"app": app}, self.ctx)
        self.assertTrue(response["ok"], response)
        return response["session"]["session_id"]

    def start_firefox(self):
        self.service.close()
        self.worker = FakeWorker()
        self.service = DesktopService(Path(self.tmp.name) / "firefox-audit.jsonl", allowed_apps={"firefox"},
                                      worker_factory=lambda: self.worker, stop_timeout=0.05)
        return self.start("firefox")

    def test_allowlist_single_owner_and_status_does_not_hold_lock(self):
        capabilities = self.service.dispatch("capabilities")
        self.assertEqual(capabilities["allowed_apps"], ["kate"])
        self.assertFalse(capabilities["reader"]["available"])
        self.assertNotIn("project_provider_spent_usd", capabilities["reader"])
        denied = self.service.dispatch("session_start", {"app": "kcalc"})
        self.assertEqual(denied["error"]["code"], "app_not_allowed")
        self.start()
        duplicate = self.service.dispatch("session_start", {"app": "kate"})
        self.assertEqual(duplicate["error"]["code"], "session_exists")
        self.assertEqual(self.service.dispatch("status")["session"]["state"], "running")
        self.assertEqual(self.service.dispatch("status")["session"]["reader"]["calls_used"], 0)
        candidates = self.service.dispatch("candidates", {"app": "kate"})
        self.assertTrue(candidates["ok"], candidates)

    def test_reader_call_cap_and_reported_usage_are_per_session(self):
        class FakeReader:
            def __init__(self, audit_path): self.calls = 0; self.audit_path = audit_path; self.pending_seen_before_request = False
            def capabilities(self): return {"provider": "test", "model": "test-model", "images": True}
            def interpret(self, image, questions):
                self.calls += 1
                rows = [json.loads(line) for line in self.audit_path.read_text().splitlines()]
                self.pending_seen_before_request = any(row.get("event") == "reader_call_started" and row.get("status") == "pending" for row in rows)
                return ReaderResult("ok", data={"text": "visible"}, provider="test", model="test-model",
                                    usage={"prompt_tokens": 8, "completion_tokens": 2}, latency_ms=12.5)
            def close(self): pass

        audit_path = Path(self.tmp.name) / "reader-audit.jsonl"
        reader = FakeReader(audit_path)
        service = DesktopService(audit_path, allowed_apps={"kate"},
                                 worker_factory=lambda: FakeWorker(), reader=reader, max_reader_calls=1)
        try:
            started = service.dispatch("session_start", {"app": "kate"}, self.ctx)
            self.assertTrue(started["ok"], started)
            session = service._session
            from observation import CaptureRef
            import hashlib
            image = b"same capture"
            capture_id = "reader-cap"
            ref = CaptureRef(capture_id, "now", session.session_id, "window-1", "org.kde.kate", "private",
                             1, 1, 1, 1, hashlib.sha256(image).hexdigest(), "source", "app", {})
            session.adapter.store.put(ref, image)
            first = service.dispatch("observe", {"app": "kate", "capture_id": capture_id, "output": "both",
                "questions": [{"field": "text", "type": "string", "description": "visible text"}]}, self.ctx)
            self.assertTrue(first["ok"], first)
            self.assertEqual(first["observation"]["data"], {"text": "visible"})
            self.assertIn("image_base64", first["observation"])
            second = service.dispatch("observe", {"app": "kate", "capture_id": capture_id, "output": "data",
                "questions": [{"field": "text", "type": "string", "description": "visible text"}]}, self.ctx)
            self.assertTrue(second["ok"], second)
            self.assertEqual(second["observation"]["errors"][0]["code"], "reader_call_budget_exceeded")
            self.assertEqual(reader.calls, 1)
            self.assertTrue(reader.pending_seen_before_request)
            state = service.dispatch("status")["session"]["reader"]
            self.assertEqual((state["calls_used"], state["max_calls"], state["successful_calls"]), (1, 1, 1))
            self.assertEqual(state["reported_usage"], {"prompt_tokens": 8, "completion_tokens": 2})
            self.assertEqual(state["latency_total_ms"], 12.5)
            audit_lines = [json.loads(line) for line in (Path(self.tmp.name) / "reader-audit.jsonl").read_text().splitlines()]
            self.assertEqual((audit_path.stat().st_mode & 0o777), 0o600)
            reader_events = [row for row in audit_lines if row.get("event", "").startswith("reader_call_")]
            self.assertEqual([row["event"] for row in reader_events], ["reader_call_started", "reader_call_completed"])
            self.assertEqual([row["status"] for row in reader_events], ["pending", "ok"])
            self.assertEqual(reader_events[-1]["reported_usage"], {"prompt_tokens": 8, "completion_tokens": 2})
            self.assertNotIn("image", json.dumps(reader_events))
            self.assertNotIn("questions", json.dumps(reader_events))
            self.assertNotIn("visible", json.dumps(reader_events))
        finally:
            service.close()

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

    def test_firefox_navigation_uses_fresh_address_candidate_and_exact_url_check(self):
        self.start_firefox()
        self.worker.rows = [{"role": "combo box", "name": "Address and search bar",
                             "states": ["editable", "enabled", "sensitive", "showing", "visible", "focused"],
                             "actions": ["SetFocus"], "x": 10, "y": 10, "width": 500, "height": 24,
                             "mapped": True, "text": "https://old.example/"}]
        candidates = self.service.dispatch("candidates", {"app": "firefox"})
        self.assertTrue(candidates["ok"], candidates)
        candidate = candidates["candidates"][0]
        self.assertIn("navigate_url", candidate["actions"])
        url = "https://www.python.org/about/"
        result = self.service.dispatch("act", {"app": "firefox", "action": "navigate_url",
            "target_ref": candidate["ref"], "verification": "navigation_url", "expected": url, "text": url}, self.ctx)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["evidence"]["address_bar_destination_verified"])
        self.assertEqual(result["evidence"]["submitted_scheme"], "https")
        self.assertEqual(self.worker.keys, ["ctrl+a", "Return"])

    def test_firefox_omnibox_scheme_elision_is_normalized_without_changing_path(self):
        from desktop_service import _same_address_destination
        self.assertTrue(_same_address_destination("www.python.org/about/", "https://www.python.org/about/"))
        self.assertFalse(_same_address_destination("www.python.org/downloads/", "https://www.python.org/about/"))

    def test_firefox_scroll_verification_requires_one_position_and_requested_direction(self):
        self.start_firefox()
        self.worker.rows = [
            {"role": "document frame", "name": "Page", "states": ["enabled", "sensitive", "showing", "visible"],
             "actions": [], "x": 10, "y": 40, "width": 500, "height": 300, "mapped": True, "text": ""},
            {"role": "scroll bar", "name": "Page scroll", "states": ["enabled", "sensitive", "showing", "visible"],
             "actions": [], "x": 510, "y": 40, "width": 12, "height": 300, "mapped": True,
             "text": "", "value": 0.0, "value_max": 100.0},
        ]
        candidates = self.service.dispatch("candidates", {"app": "firefox"})["candidates"]
        page = next(candidate for candidate in candidates if candidate["role"] == "document frame")
        result = self.service.dispatch("act", {"app": "firefox", "action": "scroll", "target_ref": page["ref"],
            "verification": "scroll_changed", "direction": "down", "steps": 1}, self.ctx)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["evidence"], {"scroll_position_changed_in_requested_direction": True,
                                               "measurement": "accessibility_scroll_value",
                                               "verification_reads": 1})
        self.assertEqual(self.worker.scroll_calls, [(260, 190, 1, 1)])

    def test_firefox_scroll_uses_fresh_link_bounds_when_scrollbar_value_is_missing(self):
        self.start_firefox()
        self.worker.rows = [
            {"role": "scroll pane", "name": "", "states": ["enabled", "sensitive", "showing", "visible"],
             "actions": [], "x": 0, "y": 40, "width": 640, "height": 360, "mapped": True, "text": ""},
            {"role": "link", "name": "Next section", "states": ["enabled", "sensitive", "showing", "visible"],
             "actions": ["Jump"], "x": 30, "y": 180, "width": 100, "height": 24, "mapped": True, "text": ""},
            {"role": "link", "name": "Previous section", "states": ["enabled", "sensitive", "showing", "visible"],
             "actions": ["Jump"], "x": 30, "y": 220, "width": 110, "height": 24, "mapped": True, "text": ""},
            {"role": "button", "name": "Fixed toolbar", "states": ["enabled", "sensitive", "showing", "visible"],
             "actions": ["Click"], "x": 300, "y": 240, "width": 110, "height": 24, "mapped": True, "text": ""},
        ]
        candidates = self.service.dispatch("candidates", {"app": "firefox"})["candidates"]
        pane = next(candidate for candidate in candidates if candidate["role"] == "scroll pane")
        result = self.service.dispatch("act", {"app": "firefox", "action": "scroll", "target_ref": pane["ref"],
            "verification": "scroll_changed", "direction": "down", "steps": 1}, self.ctx)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["evidence"], {"scroll_position_changed_in_requested_direction": True,
                                               "measurement": "semantic_content_bounds",
                                               "verification_reads": 1})

    def test_firefox_scroll_resolves_visible_pane_when_a_hidden_duplicate_exists(self):
        # Regression: Firefox exposes a hidden second scroll pane with the same
        # role, label and bounds as the visible one, so a (role, label) or bounds
        # lookup is ambiguous and the semantic fallback must still resolve it.
        self.start_firefox()
        self.worker.rows = [
            {"role": "scroll pane", "name": "", "states": ["enabled", "sensitive", "showing", "visible"],
             "actions": [], "x": 0, "y": 40, "width": 640, "height": 360, "mapped": True, "text": ""},
            {"role": "scroll pane", "name": "", "states": ["enabled", "sensitive", "visible"],
             "actions": [], "x": 0, "y": 40, "width": 640, "height": 360, "mapped": True, "text": ""},
            {"role": "link", "name": "First section", "states": ["enabled", "sensitive", "showing", "visible"],
             "actions": ["Jump"], "x": 30, "y": 180, "width": 120, "height": 24, "mapped": True, "text": ""},
            {"role": "link", "name": "Second section", "states": ["enabled", "sensitive", "showing", "visible"],
             "actions": ["Jump"], "x": 30, "y": 220, "width": 130, "height": 24, "mapped": True, "text": ""},
        ]
        candidates = self.service.dispatch("candidates", {"app": "firefox"})["candidates"]
        panes = [candidate for candidate in candidates if candidate["role"] == "scroll pane"]
        visible = [pane for pane in panes if not pane.get("unavailable_reason")]
        self.assertEqual(len(panes), 2)
        self.assertEqual(len(visible), 1)
        result = self.service.dispatch("act", {"app": "firefox", "action": "scroll",
            "target_ref": visible[0]["ref"], "verification": "scroll_changed",
            "direction": "down", "steps": 1}, self.ctx)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["evidence"], {"scroll_position_changed_in_requested_direction": True,
                                               "measurement": "semantic_content_bounds",
                                               "verification_reads": 1})

    def test_failed_verification_records_bounded_evidence_in_the_audit(self):
        self.start_firefox()
        self.worker.rows = [
            {"role": "scroll pane", "name": "", "states": ["enabled", "sensitive", "showing", "visible"],
             "actions": [], "x": 0, "y": 40, "width": 640, "height": 360, "mapped": True, "text": ""},
            {"role": "link", "name": "Only section", "states": ["enabled", "sensitive", "showing", "visible"],
             "actions": ["Jump"], "x": 30, "y": 180, "width": 120, "height": 24, "mapped": True, "text": ""},
        ]
        candidates = self.service.dispatch("candidates", {"app": "firefox"})["candidates"]
        pane = next(candidate for candidate in candidates if candidate["role"] == "scroll pane")
        result = self.service.dispatch("act", {"app": "firefox", "action": "scroll", "target_ref": pane["ref"],
            "verification": "scroll_changed", "direction": "down", "steps": 1}, self.ctx)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "verification_failed")
        entries = [json.loads(line) for line in (Path(self.tmp.name) / "firefox-audit.jsonl").read_text().splitlines()]
        failed = [entry for entry in entries if entry["status"] == "failed" and entry["tool"] == "act"]
        self.assertTrue(failed)
        self.assertEqual(failed[-1]["verification"]["measurement"], "semantic_content_bounds")
        self.assertFalse(failed[-1]["verification"]["scroll_position_changed_in_requested_direction"])

    def test_firefox_scroll_waits_for_delayed_semantic_movement_within_bound(self):
        self.start_firefox()
        self.worker.rows = [
            {"role": "scroll pane", "name": "", "states": ["enabled", "sensitive", "showing", "visible"],
             "actions": [], "x": 0, "y": 40, "width": 640, "height": 360, "mapped": True, "text": ""},
            {"role": "link", "name": "First", "states": ["enabled", "sensitive", "showing", "visible"],
             "actions": ["Jump"], "x": 30, "y": 180, "width": 100, "height": 24, "mapped": True, "text": ""},
            {"role": "link", "name": "Second", "states": ["enabled", "sensitive", "showing", "visible"],
             "actions": ["Jump"], "x": 30, "y": 220, "width": 100, "height": 24, "mapped": True, "text": ""},
        ]
        self.worker.delayed_scroll_reads = 2
        candidates = self.service.dispatch("candidates", {"app": "firefox"})["candidates"]
        pane = next(candidate for candidate in candidates if candidate["role"] == "scroll pane")
        result = self.service.dispatch("act", {"app": "firefox", "action": "scroll", "target_ref": pane["ref"],
            "verification": "scroll_changed", "direction": "down", "steps": 1}, self.ctx)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["evidence"], {"scroll_position_changed_in_requested_direction": True,
                                               "measurement": "semantic_content_bounds",
                                               "verification_reads": 2})

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
