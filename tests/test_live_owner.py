from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import desktop_service
import desktop_worker
import live_desktop_probe
from desktop_service import DesktopService, DesktopWorkerClient
from test_desktop_service import FakeWorker


class _Process:
    def __init__(self, pid=34567):
        self.pid = pid
        self.returncode = None
        self.on_terminate = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0
        if self.on_terminate:
            self.on_terminate()

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.terminate()


class _Launched:
    def __init__(self, process):
        self.pid = process.pid
        self.process = process


class _LiveEngine:
    def __init__(self):
        self.windows = [{"id": "original", "app": "org.kde.konsole", "caption": "Terminal"}]
        self.proc = _Process()
        self.proc.on_terminate = lambda: setattr(
            self, "windows", [row for row in self.windows if row["id"] != "owned"])
        self.launched = _Launched(self.proc)
        info = types.SimpleNamespace(apps={}, dbus_address="session-bus", home_dir="", wayland_socket="")
        self._session = types.SimpleNamespace(info=info, _process=None, _session_config_dir="")

    def session_connect(self, keep_screenshots=False):
        return "Connected to live KWin session. Input backend: KWin EIS"

    def session_stop(self):
        return "Disconnected from live KWin session."

    def _get_session(self):
        return types.SimpleNamespace(launch_app=self.launch_app, info=self._session.info)

    def launch_app(self, command, extra_env=None):
        self.command = command
        self.env = extra_env
        app = {"kate": ("org.kde.kate", "Kate"), "kcalc": ("org.kde.kcalc", "KCalc")}[command[0]]
        self.windows.append({"id": "owned", "app": app[0], "caption": app[1]})
        return self.launched

    def _run_kwin_query(self, request):
        if request.get("op") == "active":
            return {"ok": True, "result": self.windows[0]}
        return {"ok": True, "result": list(self.windows)}


class _LiveFakeWorker(FakeWorker):
    def __init__(self):
        super().__init__()
        self.live_calls = []
        self.restore_results = [True, True, False]

    def focus_live_app(self):
        self.live_calls.append("focus")
        return True

    def restore_live_focus(self):
        self.live_calls.append("restore")
        return self.restore_results.pop(0)


class LiveOwnerTests(unittest.TestCase):
    def test_live_start_failure_cleans_only_launched_app_and_restores_a11y(self):
        engine = _LiveEngine()
        flags = {"IsEnabled": False, "ScreenReaderEnabled": False}
        writes = []

        def set_flags(_address, value):
            flags.update(value)
            writes.append(dict(value))
            return dict(flags)

        fake_core = types.ModuleType("kwin_mcp.core")
        fake_core.AutomationEngine = lambda: engine
        fake_pkg = types.ModuleType("kwin_mcp")
        fake_pkg.core = fake_core
        with tempfile.TemporaryDirectory() as folder, \
             patch.dict(sys.modules, {"kwin_mcp": fake_pkg, "kwin_mcp.core": fake_core}), \
             patch.object(live_desktop_probe, "_active_row", return_value=engine.windows[0]), \
             patch.object(live_desktop_probe, "_window_rows", side_effect=lambda _engine: list(engine.windows)), \
             patch.object(live_desktop_probe, "_raw_window_rows", return_value=[{"id": "owned", "pid": 99999}]), \
             patch.object(live_desktop_probe, "_a11y_flags", return_value=dict(flags)), \
             patch.object(live_desktop_probe, "_a11y_address", return_value="session-bus"), \
             patch.object(live_desktop_probe, "_set_a11y_flags_at", side_effect=set_flags), \
             patch.object(live_desktop_probe, "_restore_exact", return_value=True):
            worker = desktop_worker.Worker()
            journal = Path(folder) / "live.json"
            with self.assertRaisesRegex(ValueError, "live_app_not_owned"):
                worker.start("kate", str(journal), "live", temporary_a11y=True)
            # A Kate launch must keep the window in the process whose PID the
            # owner tracks; Kate otherwise detaches by default.
            self.assertEqual(engine.command[:3], ["kate", "--new", "--block"])
            self.assertEqual(writes, [
                {"IsEnabled": True, "ScreenReaderEnabled": True},
                {"IsEnabled": False, "ScreenReaderEnabled": False},
            ])
            self.assertEqual(engine.proc.returncode, 0)
            self.assertEqual([row["id"] for row in engine.windows], ["original"])
            self.assertFalse(journal.exists())
            self.assertIsNone(worker.engine)

    def test_forced_live_recovery_kills_exact_app_and_independently_restores_focus_and_flags(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            ticks = DesktopWorkerClient._proc_start_ticks(child.pid)
            self.assertIsNotNone(ticks)
            with tempfile.TemporaryDirectory() as folder:
                journal = Path(folder) / "live.json"
                journal.write_text(json.dumps({
                    "schema": 1,
                    "worker_pid": 765432,
                    "session_pid": None,
                    "session_start_ticks": None,
                    "desktop_mode": "live",
                    "original_window": {"id": "session-start", "app": "konsole"},
                    "action_original_window": {"id": "before-action", "app": "org.kde.konsole"},
                    "original_a11y_flags": {"IsEnabled": False, "ScreenReaderEnabled": False},
                    "a11y_address": "session-bus",
                    "app_processes": [{"pid": child.pid, "start_ticks": ticks}],
                    "private_paths": [], "home_dir": "", "config_dir": "", "socket_name": "",
                    "runtime_dir": os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"),
                }), encoding="utf-8")
                worker = DesktopWorkerClient.__new__(DesktopWorkerClient)
                worker.proc = types.SimpleNamespace(pid=765432)
                worker.journal_path = journal
                worker.cleanup_paths = []
                connection = {"available": False}
                fake_engine = types.SimpleNamespace(
                    session_connect=lambda **_: ("Connected to live KWin session. Input backend: KWin EIS"
                        if connection["available"] else "connection failed"),
                    session_stop=lambda: "Disconnected from live KWin session.")
                fake_core = types.ModuleType("kwin_mcp.core")
                fake_core.AutomationEngine = lambda: fake_engine
                fake_pkg = types.ModuleType("kwin_mcp")
                fake_pkg.core = fake_core
                calls = []

                def restore(_engine, original, _rows):
                    calls.append(("focus", original["id"]))
                    return False

                def restore_flags(_address, flags):
                    calls.append(("flags", dict(flags)))
                    return dict(flags)

                with patch.dict(sys.modules, {"kwin_mcp": fake_pkg, "kwin_mcp.core": fake_core}), \
                     patch.object(DesktopWorkerClient, "_group_live", side_effect=AssertionError("must not signal compositor")), \
                     patch.object(live_desktop_probe, "_restore_exact", side_effect=restore), \
                     patch.object(live_desktop_probe, "_window_rows", return_value=[]), \
                     patch.object(live_desktop_probe, "_a11y_address", return_value="session-bus"), \
                     patch.object(live_desktop_probe, "_set_a11y_flags_at", side_effect=restore_flags):
                    self.assertFalse(worker.recover_cleanup())
                child.wait(timeout=3)
                self.assertEqual(calls, [("flags", {"IsEnabled": False, "ScreenReaderEnabled": False})])
                self.assertTrue(journal.exists())
                connection["available"] = True

                def restore_again(_engine, original, _rows):
                    calls.append(("focus", original["id"]))
                    return True

                def restore_flags_again(_address, flags):
                    calls.append(("flags", dict(flags)))
                    return dict(flags)

                with patch.dict(sys.modules, {"kwin_mcp": fake_pkg, "kwin_mcp.core": fake_core}), \
                     patch.object(DesktopWorkerClient, "_group_live", side_effect=AssertionError("must not signal compositor")), \
                     patch.object(live_desktop_probe, "_restore_exact", side_effect=restore_again), \
                     patch.object(live_desktop_probe, "_window_rows", return_value=[]), \
                     patch.object(live_desktop_probe, "_a11y_address", return_value="session-bus"), \
                     patch.object(live_desktop_probe, "_set_a11y_flags_at", side_effect=restore_flags_again):
                    self.assertTrue(worker.recover_cleanup())
                self.assertEqual(calls[-2:], [
                    ("flags", {"IsEnabled": False, "ScreenReaderEnabled": False}),
                    ("focus", "before-action"),
                ])
                self.assertFalse(journal.exists())
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()

    def test_live_mode_requires_owner_presence_and_temporary_a11y_opt_ins(self):
        with tempfile.TemporaryDirectory() as folder:
            worker = DesktopService(Path(folder) / "audit.jsonl", allowed_apps={"kate"})
            try:
                missing_owner = worker.dispatch("session_start", {"app": "kate", "desktop_mode": "live"})
                self.assertEqual(missing_owner["error"]["code"], "owner_present_override_required")
                missing_a11y = worker.dispatch("session_start", {"app": "kate", "desktop_mode": "live",
                    "owner_present_override": True})
                self.assertEqual(missing_a11y["error"]["code"], "live_temporary_a11y_required")
                self.assertIsNone(worker._session)
            finally:
                worker.close()

    def test_live_action_reports_focus_restore_failure_and_releases_task_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            fake = _LiveFakeWorker()
            service = DesktopService(Path(folder) / "audit.jsonl", allowed_apps={"kate"},
                                    worker_factory=lambda: fake)
            try:
                started = service.dispatch("session_start", {"app": "kate", "desktop_mode": "live",
                    "owner_present_override": True, "temporary_a11y": True})
                self.assertTrue(started["ok"], started)
                candidate = service.dispatch("candidates", {"app": "kate"})["candidates"][0]
                session = service._session
                session.adapter.capture = lambda *args, **kwargs: types.SimpleNamespace(capture_id="capture")
                session.adapter.observe = lambda *args, **kwargs: types.SimpleNamespace(
                    metadata={"capture_id": "capture"}, image=None, data=None, reader=None, errors=())
                observed = service.dispatch("observe", {"app": "kate"})
                self.assertTrue(observed["ok"], observed)
                acted = service.dispatch("act", {"app": "kate", "action": "type_text",
                    "target_ref": candidate["ref"], "text": "verified text",
                    "verification": "target_text", "expected": "verified text"})
                self.assertFalse(acted["ok"])
                self.assertEqual(acted["error"]["code"], "live_focus_restore_failed")
                self.assertEqual(fake.live_calls, [
                    "focus", "restore",  # candidates
                    "focus", "restore",  # screenshot observation
                    "focus", "restore",  # action
                ])
                self.assertFalse(service._session.busy.locked())
                records = [json.loads(line) for line in (Path(folder) / "audit.jsonl").read_text().splitlines()]
                owner_records = [row for row in records if "desktop_mode" in row]
                self.assertTrue(owner_records)
                self.assertTrue(all(row["desktop_mode"] == "live" for row in owner_records))
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
