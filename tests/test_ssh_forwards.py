from __future__ import annotations

import json
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jevdesktop.ssh_forwards import (
    ForwardConfigError,
    SSHForwardManager,
    parse_forward,
    unit_name,
)


def entry(name: str, direction: str = "local") -> dict[str, object]:
    value: dict[str, object] = {
        "name": name,
        "ssh_host": "tailnet-host",
        "direction": direction,
        "endpoint": "127.0.0.1:9100",
        "endpoint_path": "/mcp",
    }
    value["local_port" if direction == "local" else "forwarded_port"] = 9101
    return value


class SSHForwardConfigTests(unittest.TestCase):
    def test_rejects_non_loopback_endpoint_and_unsafe_path(self):
        invalid = entry("bad")
        invalid["endpoint"] = "10.0.0.2:9100"
        with self.assertRaises(ForwardConfigError):
            parse_forward(invalid)
        invalid = entry("bad")
        invalid["endpoint_path"] = "/mcp?admin=true"
        with self.assertRaises(ForwardConfigError):
            parse_forward(invalid)

    def test_atomic_config_is_private_and_direction_urls_are_distinct(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manager = SSHForwardManager(config=root / "jev" / "forwards.json",
                                        unit_dir=root / "units")
            local = manager.add(entry("local"))
            reverse = manager.add(entry("reverse", "reverse"))
            self.assertEqual(local.local_port, 9101)
            self.assertEqual(reverse.forwarded_port, 9101)
            self.assertEqual(stat.S_IMODE(manager.config.stat().st_mode), 0o600)
            self.assertEqual(manager.config.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual([f.name for f in manager.list()], ["local", "reverse"])
            self.assertEqual(unit_name("one-two"), r"jev-ssh-forward@one\x2dtwo.service")


class SSHForwardSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.manager = SSHForwardManager(config=root / "jev" / "forwards.json",
                                         unit_dir=root / "units")
        self.manager.add(entry("first"))
        self.manager.add(entry("second", "reverse"))

    def tearDown(self):
        self.temp.cleanup()

    def test_snapshot_keeps_each_entry_when_a_status_check_raises(self):
        good = {
            "name": "second", "direction": "reverse", "checked_at": "2026-10-25T12:00:00Z",
            "process_active": True, "process_health": "active", "lifecycle_state": "active",
            "installed": True, "enabled": False, "enabled_state": "disabled",
            "local_listener_reachable": None, "tcp_health": "not_checked",
            "mcp_health": "not_checked", "local_url": None,
            "remote_url": "http://127.0.0.1:9101/mcp", "last_error": None,
        }
        with patch.object(self.manager, "status", side_effect=[RuntimeError("token=secret\nfailed"), good]):
            result = self.manager.snapshot()
        self.assertEqual([f["name"] for f in result["forwards"]], ["first", "second"])
        self.assertEqual(result["forwards"][0]["process_health"], "unavailable")
        self.assertIn("token=[redacted]", result["forwards"][0]["last_error"])
        self.assertNotIn("secret", result["forwards"][0]["last_error"])
        self.assertEqual(result["forwards"][1]["mcp_health"], "not_checked")
        self.assertTrue(result["checked_at"])

    def test_not_found_load_state_is_definitely_inactive_even_on_nonzero_exit(self):
        show = {"ok": False, "stdout": "LoadState=not-found\n", "stderr": "No unit", "returncode": 1}
        enabled = {"ok": False, "stdout": "not-found", "stderr": "", "returncode": 4}
        with patch("jevdesktop.ssh_forwards._run", side_effect=[show, enabled]):
            state = self.manager.status("first")
        self.assertFalse(state["process_active"])
        self.assertEqual(state["lifecycle_state"], "not_installed")
        self.assertFalse(state["unavailable"])

    def test_reconnecting_state_has_bounded_systemd_failure_metadata(self):
        show = {
            "ok": True,
            "stdout": "\n".join((
                "ActiveState=activating", "SubState=auto-restart", "LoadState=loaded",
                "Result=exit-code", "ExecMainStatus=255", "NRestarts=12",
                "ActiveEnterTimestamp=Sat 2026-10-25 12:00:00 CET",
                "InactiveEnterTimestamp=Sat 2026-10-25 11:59:59 CET",
            )),
            "stderr": "", "returncode": 0,
        }
        enabled = {"ok": True, "stdout": "enabled", "stderr": "", "returncode": 0}
        with patch("jevdesktop.ssh_forwards._run", side_effect=[show, enabled]) as run:
            state = self.manager.status("first")
        self.assertEqual(state["lifecycle_state"], "reconnecting")
        self.assertIsNone(state["process_active"])
        self.assertEqual(state["process_health"], "unavailable")
        self.assertEqual(state["systemd_result"], "exit-code")
        self.assertEqual(state["exec_main_status"], 255)
        self.assertEqual(state["restart_count"], 12)
        self.assertEqual(state["active_enter_timestamp"], "Sat 2026-10-25 12:00:00 CET")
        self.assertIn("systemd_result=exit-code", state["last_error"])
        self.assertIn("exec_main_status=255", state["last_error"])
        self.assertTrue(all(call.kwargs["timeout"] == 2.0 for call in run.call_args_list))
        self.assertNotIn("journal", state)

    def test_failed_state_uses_result_enum_not_unbounded_journal_text(self):
        show = {
            "ok": True,
            "stdout": "ActiveState=failed\nSubState=failed\nLoadState=loaded\nResult=watchdog\nExecMainStatus=9\nNRestarts=99999999999999999999",
            "stderr": "", "returncode": 0,
        }
        enabled = {"ok": True, "stdout": "disabled", "stderr": "", "returncode": 0}
        with patch("jevdesktop.ssh_forwards._run", side_effect=[show, enabled]):
            state = self.manager.status("first")
        self.assertEqual(state["lifecycle_state"], "failed")
        self.assertEqual(state["systemd_result"], "watchdog")
        self.assertEqual(state["exec_main_status"], 9)
        self.assertIsNone(state["restart_count"])
        self.assertEqual(state["last_error"], "systemd_result=watchdog exec_main_status=9")

    def test_systemctl_status_queries_use_short_timeout(self):
        from jevdesktop.ssh_forwards import _run

        with patch("jevdesktop.ssh_forwards.subprocess.run", side_effect=subprocess.TimeoutExpired("systemctl", 2.0)) as run:
            result = _run("show", "unit.service", timeout=2.0)
        self.assertFalse(result["ok"])
        self.assertIsNone(result["returncode"])
        self.assertEqual(run.call_args.kwargs["timeout"], 2.0)

    def test_missing_manager_is_unknown_not_healthy(self):
        unavailable = {"ok": False, "stdout": "", "stderr": "system bus unavailable", "returncode": None}
        with patch("jevdesktop.ssh_forwards._run", return_value=unavailable):
            result = self.manager.snapshot()
        self.assertTrue(all(not item["process_active"] is True for item in result["forwards"]))
        self.assertTrue(all(item["unavailable"] for item in result["forwards"]))
        self.assertTrue(all(item["mcp_health"] == "not_checked" for item in result["forwards"]))


class SSHForwardLifecycleTests(unittest.TestCase):
    def test_uninstall_preserves_unit_when_disable_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manager = SSHForwardManager(config=root / "jev" / "forwards.json",
                                        unit_dir=root / "units")
            manager.add(entry("named"))
            unit = manager.unit_dir / unit_name("named")
            unit.parent.mkdir(parents=True)
            unit.write_text("unit", encoding="utf-8")
            state = {"process_active": False, "installed": True, "enabled_state": "disabled",
                     "unit": unit.name}
            with patch.object(manager, "status", return_value=state), \
                 patch.object(manager, "disable", return_value={"ok": False, "stderr": "bus unavailable"}):
                result = manager.uninstall("named")
            self.assertFalse(result["ok"])
            self.assertTrue(unit.exists())

    def test_install_refuses_active_or_unknown_state_before_writing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manager = SSHForwardManager(config=root / "jev" / "forwards.json",
                                        unit_dir=root / "units")
            manager.add(entry("named"))
            for process_active in (True, None):
                with patch.object(manager, "status", return_value={
                    "process_active": process_active, "unit": unit_name("named")
                }):
                    result = manager.install("named")
                self.assertFalse(result["ok"])
                self.assertFalse((manager.unit_dir / unit_name("named")).exists())

    def test_delete_refuses_installed_unit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manager = SSHForwardManager(config=root / "jev" / "forwards.json",
                                        unit_dir=root / "units")
            manager.add(entry("named"))
            with patch.object(manager, "status", return_value={"installed": True, "process_active": False}):
                with self.assertRaisesRegex(ForwardConfigError, "uninstall"):
                    manager.delete("named")
            self.assertEqual(json.loads(manager.config.read_text())["forwards"][0]["name"], "named")


if __name__ == "__main__":
    unittest.main()
