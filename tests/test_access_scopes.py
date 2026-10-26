"""Offline tests for owner-authorized access scopes and scope transitions."""
from pathlib import Path
import hashlib
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from jevdesktop.desktop_service import DesktopService
from jevdesktop.observation import CaptureRef, ObservationAdapter
from jevdesktop.native_windows import activate_window_id_with_env
from jevdesktop.desktop_worker import Worker
from jevdesktop.image_input import _resolve_capture


class ScopeWorker:
    def __init__(self):
        self.started = []
        self.stopped = 0
        self.killed_apps = []
        self.launch_calls = 0
        self.selected_window_id = None
        self.profile = None
        self.rows = []

    def start(self, app):
        self.started.append(app)
        if app == "desktop" and self.rows:
            return {"selected_window": self.rows[0], "selected_window_id": self.rows[0]["id"]}
        return {"started": True}

    def rpc(self, method, **params):
        if method == "workspace_discover":
            return {"windows": [{"id": "virtual-1", "app": "virtual.app", "caption": "Virtual only"}],
                    "active_window_id": "virtual-1", "isolated": True}
        if method == "select_window":
            row = next((row for row in self.rows if row["id"] == params["window_id"]), None)
            if row is None:
                raise ValueError("live_selected_window_unavailable")
            return {"selected_window": row, "selected_window_id": row["id"]}
        if method == "launch_app":
            self.launch_calls += 1
            return {"launched": True, "profile_id": params["profile_id"], "pid": 123}
        raise AssertionError(method)

    def stop(self):
        self.stopped += 1
        return {"stopped": True}

    def close(self):
        return True


class AccessScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.worker = ScopeWorker()
        self.service = DesktopService(Path(self.tmp.name) / "audit.jsonl", allowed_apps={"kate"},
            allowed_access_scopes={"selected_application", "full_virtual_desktop", "full_live_desktop"},
            worker_factory=lambda: self.worker, watchdog_interval=30)
        self.ctx = {"caller_node": "scope-test", "transport": "unit"}

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def test_selected_application_remains_deny_by_default(self):
        result = self.service.dispatch("session_start", {"app": "unknown.app"}, self.ctx)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "app_not_allowed")

    def test_full_scope_accepts_null_app_from_cli(self):
        started = self.service.dispatch("session_start", {"app": None,
            "access_scope": "full_virtual_desktop"}, self.ctx)
        self.assertTrue(started["ok"], started)
        self.assertEqual(self.service._session.app, "desktop")
        self.assertEqual(self.service._session.tx.audit.access_scope, "full_virtual_desktop")

    def test_profile_access_reports_current_scope_not_only_narrow_allowlist(self):
        self.assertTrue(self.service.dispatch("session_start", {"access_scope": "full_virtual_desktop"}, self.ctx)["ok"])
        rows = {row["id"]: row for row in self.service.dispatch("profiles", {}, self.ctx)["profiles"]}
        self.assertFalse(rows["firefox"]["app_allowlisted"])
        self.assertTrue(rows["firefox"]["allowed"])
        self.assertTrue(rows["firefox"]["launch_allowed"])

    def test_full_live_unknown_app_is_selectable_without_profile_or_allowlist(self):
        self.worker.rows = [{"id": "foreign-window", "app": "unknown.app", "caption": "Owner window",
                             "pid": 124, "active": True, "frame": {"x": 0, "y": 0, "width": 10, "height": 10},
                             "client": {"x": 0, "y": 0, "width": 10, "height": 10}}]
        started = self.service.dispatch("session_start", {"access_scope": "full_live_desktop",
            "owner_present_override": True}, self.ctx)
        self.assertTrue(started["ok"], started)
        self.assertEqual(started["session"]["access_scope"], "full_live_desktop")
        self.assertEqual(self.service._session.app, "unknown.app")
        self.assertNotIn("unknown.app", self.service.allowed_apps)

    def test_full_live_desktop_overview_works_with_active_target_without_refocusing(self):
        self.worker.rows = [{"id": "foreign-window", "app": "unknown.app", "caption": "Owner window",
                             "pid": 124, "active": True, "frame": {"x": 0, "y": 0, "width": 10, "height": 10},
                             "client": {"x": 0, "y": 0, "width": 10, "height": 10}}]
        started = self.service.dispatch("session_start", {"access_scope": "full_live_desktop",
            "owner_present_override": True}, self.ctx)
        self.assertTrue(started["ok"], started)
        adapter = self.service._session.adapter
        adapter.capture = lambda *args, **kwargs: SimpleNamespace(capture_id="overview")
        adapter.observe = lambda *args, **kwargs: SimpleNamespace(
            metadata={"scope": "desktop"}, image=None, data=None, reader=None, errors=[])
        overview = self.service.dispatch("observe", {"scope": "desktop"}, self.ctx)
        self.assertTrue(overview["ok"], overview)
        self.assertEqual(overview["observation"]["capture"]["scope"], "desktop")

    def test_full_scope_uses_known_profile_atspi_identity(self):
        self.worker.rows = [{"id": "firefox-window", "app": "org.mozilla.firefox", "caption": "Browser",
                             "pid": 125, "active": True}]
        started = self.service.dispatch("session_start", {"access_scope": "full_live_desktop",
            "owner_present_override": True}, self.ctx)
        self.assertTrue(started["ok"], started)
        session = self.service._session
        self.assertEqual(session.app, "firefox")
        self.assertEqual(session.profile["atspi_app"], "firefox")
        self.assertEqual(session.worker.profile["window_app_ids"], ["firefox", "org.mozilla.firefox"])

    def test_full_live_switch_invalidates_all_old_reference_state(self):
        self.worker.rows = [{"id": "window-a", "app": "unknown.one", "caption": "A", "pid": 11},
                            {"id": "window-b", "app": "unknown.two", "caption": "B", "pid": 12}]
        started = self.service.dispatch("session_start", {"access_scope": "full_live_desktop",
            "owner_present_override": True, "window_id": "window-a"}, self.ctx)
        self.assertTrue(started["ok"], started)
        session = self.service._session
        data = b"old capture"
        ref = CaptureRef("old-id", "now", session.session_id, "window-a", "unknown.one", "A", 1, 1, 1, 1,
                         hashlib.sha256(data).hexdigest(), "source", "app", {})
        session.adapter.store.put(ref, data)
        session.valid_input_captures.add("old-id")
        session.pending_input_captures.add("pending-id")
        session.tx._last[(session.session_id, "unknown.one")] = object()
        session.visual_input_uncertain = True
        session.tx._blocked[session.session_id] = "uncertain"
        switched = self.service.dispatch("select_window", {"window_id": "window-b"}, self.ctx)
        self.assertTrue(switched["ok"], switched)
        self.assertTrue(switched["references_invalidated"])
        self.assertEqual(session.adapter.store._items, {})
        self.assertFalse(session.valid_input_captures)
        self.assertFalse(session.pending_input_captures)
        self.assertFalse(session.tx._last)
        self.assertTrue(session.visual_input_uncertain)
        self.assertEqual(session.tx._blocked[session.session_id], "uncertain")
        self.assertEqual(session.app, "unknown.two")

    def test_full_virtual_workspace_uses_active_scoped_worker(self):
        started = self.service.dispatch("session_start", {"access_scope": "full_virtual_desktop"}, self.ctx)
        self.assertTrue(started["ok"], started)
        workspace = self.service.dispatch("workspace", {}, self.ctx)
        self.assertTrue(workspace["ok"], workspace)
        self.assertTrue(workspace["isolated"])
        self.assertEqual(workspace["windows"][0]["caption"], "Virtual only")
        self.assertEqual(self.worker.started, ["desktop"])

    def test_selected_virtual_workspace_stays_on_current_isolated_worker_and_redacts(self):
        created = []
        self.service.worker_factory = lambda: created.append(ScopeWorker()) or created[-1]
        started = self.service.dispatch("session_start", {"app": "kate"}, self.ctx)
        self.assertTrue(started["ok"], started)
        workspace = self.service.dispatch("workspace", {}, self.ctx)
        self.assertTrue(workspace["ok"], workspace)
        self.assertTrue(workspace["isolated"])
        self.assertNotIn("caption", workspace["windows"][0])
        self.assertEqual(len(created), 1)

    def test_selected_virtual_worker_workspace_uses_its_private_bus(self):
        worker = Worker()
        private_env = {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/private-virtual-bus"}
        worker.engine = SimpleNamespace(_session_env=lambda: private_env)
        worker.desktop_mode = "virtual"
        worker.access_scope = "selected_application"
        expected = {"windows": [{"id": "inside", "app": "kate"}], "active_window_id": "inside"}
        with patch("jevdesktop.native_windows.workspace_snapshot_with_env", return_value=expected) as read_snapshot, \
                patch("kwin_mcp.core.AutomationEngine", side_effect=AssertionError("must not connect to host")):
            result = worker._workspace_discover()
        self.assertEqual(result["windows"], expected["windows"])
        self.assertTrue(result["isolated"])
        read_snapshot.assert_called_once_with(private_env)

    def test_full_virtual_launcher_accepts_only_profile_ids(self):
        self.service.max_actions = 1
        started = self.service.dispatch("session_start", {"access_scope": "full_virtual_desktop"}, self.ctx)
        self.assertTrue(started["ok"], started)
        self.worker.selected_window_id = "old-window"
        self.worker.selected_window = {"id": "old-window", "app": "old.app"}
        launched = self.service.dispatch("launch_app", {"profile_id": "kate"}, self.ctx)
        self.assertTrue(launched["ok"], launched)
        self.assertEqual(launched["profile_id"], "kate")
        self.assertEqual(self.service._session.app, "kate")
        self.assertIsNone(self.worker.selected_window_id)
        self.assertIsNone(self.worker.selected_window)
        self.assertEqual(self.service._session.actions, 1)
        second = self.service.dispatch("launch_app", {"profile_id": "firefox"}, self.ctx)
        self.assertEqual(second["error"]["code"], "task_action_budget_exceeded")
        self.assertEqual(self.worker.launch_calls, 1)
        rejected = self.service.dispatch("launch_app", {"profile_id": "sh -c whoami"}, self.ctx)
        self.assertEqual(rejected["error"]["code"], "profile_not_found")

    def test_failed_launch_reports_uncertainty_and_consumes_attempt(self):
        self.service.dispatch("session_start", {"access_scope": "full_virtual_desktop"}, self.ctx)
        session = self.service._session
        session.valid_input_captures.add("old")
        with patch.object(self.worker, "rpc", side_effect=TimeoutError()):
            result = self.service.dispatch("launch_app", {"profile_id": "kate"}, self.ctx)
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"]["launch_may_have_succeeded"])
        self.assertFalse(result["error"]["automatic_retry_allowed"])
        self.assertEqual(session.actions, 1)
        self.assertFalse(session.valid_input_captures)

    def test_failed_selection_invalidates_old_captures(self):
        self.service.dispatch("session_start", {"access_scope": "full_virtual_desktop"}, self.ctx)
        session = self.service._session
        session.valid_input_captures.add("old")
        with patch.object(self.worker, "rpc", side_effect=TimeoutError()):
            result = self.service.dispatch("select_window", {"window_id": "unknown"}, self.ctx)
        self.assertFalse(result["ok"])
        self.assertFalse(session.valid_input_captures)

    def test_full_virtual_window_switch_is_exact_and_in_scope(self):
        self.worker.rows = [{"id": "virtual-a", "app": "isolated.one", "caption": "A", "pid": 22}]
        started = self.service.dispatch("session_start", {"access_scope": "full_virtual_desktop"}, self.ctx)
        self.assertTrue(started["ok"], started)
        switched = self.service.dispatch("select_window", {"window_id": "virtual-a"}, self.ctx)
        self.assertTrue(switched["ok"], switched)
        self.assertEqual(self.service._session.app, "isolated.one")
        rejected = self.service.dispatch("select_window", {"window_id": "host-window"}, self.ctx)
        self.assertEqual(rejected["error"]["code"], "live_selected_window_unavailable")

    def test_full_desktop_capture_uses_mapped_pipeline_without_window_target(self):
        from pathlib import Path

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "desktop.ppm"
            path.write_bytes(b"P6\n2 2\n255\n" + bytes([1, 2, 3] * 4))
            class NoWindows:
                pass
            adapter = ObservationAdapter(NoWindows(), "session-1")
            adapter._screenshot = lambda: (path, "Coordinate space: logical; origin (0, 0); size 2x2; source 2x2; coverage full")
            ref = adapter.capture("desktop", scope="desktop")
            self.assertEqual(ref.window_id, "__desktop__")
            self.assertEqual(ref.scope, "desktop")
            self.assertEqual((ref.width, ref.height), (2, 2))
            with self.assertRaisesRegex(ValueError, "image_window_selection_required"):
                _resolve_capture(SimpleNamespace(session_id="session-1", valid_input_captures={ref.capture_id},
                    adapter=adapter), {"capture_id": ref.capture_id}, datetime.now(timezone.utc))

    def test_virtual_exact_activation_requires_and_uses_isolated_dbus_env(self):
        self.assertFalse(activate_window_id_with_env("window-1", {}))
        with patch("subprocess.run", return_value=SimpleNamespace(returncode=0)) as run:
            self.assertTrue(activate_window_id_with_env("window-1", {
                "DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/private-bus"}))
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"], "unix:path=/tmp/private-bus")

    def test_virtual_input_check_refuses_changed_active_window(self):
        worker = Worker()
        worker.engine = SimpleNamespace(_session_env=lambda: {"DBUS_SESSION_BUS_ADDRESS": "private"})
        worker.access_scope = "full_virtual_desktop"
        worker.desktop_mode = "virtual"
        worker.selected_window_id = "selected"
        snapshot = {"windows": [{"id": "selected", "active": False}, {"id": "dialog", "active": True}]}
        with patch("jevdesktop.native_windows.workspace_snapshot_with_env", return_value=snapshot):
            with self.assertRaisesRegex(ValueError, "virtual_selected_window_changed"):
                worker.call("check_selected_window_active", {})

    def test_scopes_cannot_be_escalated_or_mismatched(self):
        locked = DesktopService(Path(self.tmp.name) / "locked.jsonl", allowed_apps={"kate"},
            worker_factory=ScopeWorker, watchdog_interval=30)
        try:
            response = locked.dispatch("session_start", {"access_scope": "full_live_desktop",
                "owner_present_override": True}, self.ctx)
            self.assertEqual(response["error"]["code"], "access_scope_not_allowed")
        finally:
            locked.close()
        mismatch = self.service.dispatch("session_start", {"access_scope": "full_live_desktop",
            "desktop_mode": "virtual", "owner_present_override": True}, self.ctx)
        self.assertEqual(mismatch["error"]["code"], "access_scope_mode_mismatch")

    def test_selecting_or_launching_does_not_escalate_selected_scope(self):
        started = self.service.dispatch("session_start", {"app": "kate"}, self.ctx)
        self.assertTrue(started["ok"], started)
        selected = self.service.dispatch("select_window", {"window_id": "foreign"}, self.ctx)
        self.assertEqual(selected["error"]["code"], "access_scope_mismatch")
        launched = self.service.dispatch("launch_app", {"profile_id": "firefox"}, self.ctx)
        self.assertEqual(launched["error"]["code"], "access_scope_mismatch")

    def test_full_live_stop_does_not_claim_or_kill_user_apps(self):
        started = self.service.dispatch("session_start", {"access_scope": "full_live_desktop",
            "owner_present_override": True}, self.ctx)
        self.assertTrue(started["ok"], started)
        stopped = self.service.dispatch("session_stop", {}, self.ctx)
        self.assertTrue(stopped["stopped"])
        self.assertEqual(self.worker.killed_apps, [])
        self.assertEqual(self.worker.stopped, 1)


if __name__ == "__main__":
    unittest.main()
