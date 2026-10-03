from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import tempfile
import threading
import types
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from desktop_daemon import _apply_config, _make_service, load_config
from desktop_service_unit import UNIT_NAME, TRAY_UNIT_NAME, _install, _status, _uninstall, render_tray_unit, render_unit
from desktop_tray import (DesktopTray, DEFAULT_SETTINGS, MODE_LIVE, MODE_VIRTUAL, load_settings,
                          save_settings)


def _args(**overrides) -> Namespace:
    base = dict(allow_app=[], config=None, run_dir=None, audit=None, socket=None, dotenv=None,
                idle_timeout=None, max_session_lifetime=None, max_actions=None, max_observations=None,
                reader_provider=None, reader_base_url=None, reader_model=None, reader_key_env=None,
                reader_timeout=None, reader_total_timeout=None, max_reader_calls=None,
                allowed_apps=None, unit_dir=None, service_command=None, stop=False, tray=False)
    base.update(overrides)
    return Namespace(**base)


class DaemonConfigTests(unittest.TestCase):
    def test_unknown_keys_and_bad_shapes_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text('{"allow_app": ["kate"]}', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "unknown keys"):
                load_config(path)
            path.write_text('{"allowed_apps": "kate"}', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "list of strings"):
                load_config(path)
            path.write_text('{"reader": {"provder": "deepseek"}}', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "reader has unknown keys"):
                load_config(path)
            path.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "JSON object"):
                load_config(path)
            self.assertEqual(load_config(Path(temp) / "absent.json"), {})

    def test_cli_then_env_then_config_precedence(self):
        config = {"allowed_apps": ["kcalc"], "idle_timeout": 11.0,
                  "reader": {"provider": "mimo", "max_calls_per_session": 2}}
        args = _args()
        _apply_config(args, config)
        self.assertEqual(args.allowed_apps, ["kcalc"])
        self.assertEqual(args.idle_timeout, 11.0)
        self.assertEqual(args.reader_provider, "mimo")
        self.assertEqual(args.max_reader_calls, 2)
        self.assertEqual(args.max_session_lifetime, 1800.0)
        self.assertEqual(args.dotenv, Path(".env"))

        old = os.environ.get("JEV_DESKTOP_ALLOW_APPS")
        os.environ["JEV_DESKTOP_ALLOW_APPS"] = "kate, firefox"
        try:
            env_args = _args()
            _apply_config(env_args, config)
            self.assertEqual(env_args.allowed_apps, ["kate", "firefox"])
        finally:
            if old is None:
                os.environ.pop("JEV_DESKTOP_ALLOW_APPS", None)
            else:
                os.environ["JEV_DESKTOP_ALLOW_APPS"] = old

        cli_args = _args(allow_app=["kate"], idle_timeout=5.0)
        _apply_config(cli_args, config)
        self.assertEqual(cli_args.allowed_apps, ["kate"])
        self.assertEqual(cli_args.idle_timeout, 5.0)

    def test_empty_allowlist_still_fails_closed(self):
        args = _args()
        _apply_config(args, {})
        self.assertEqual(args.allowed_apps, [])
        with self.assertRaisesRegex(RuntimeError, "allowlist is required"):
            _make_service(args)


class ServiceUnitTests(unittest.TestCase):
    def test_render_unit_points_at_the_config_and_entry_point(self):
        rendered = render_unit(Path("/home/owner/.config/jev-desktop/config.json"))
        self.assertIn("daemon --foreground --config /home/owner/.config/jev-desktop/config.json", rendered)
        self.assertIn("PartOf=graphical-session.target", rendered)
        self.assertIn("NoNewPrivileges=yes", rendered)
        self.assertIn("EnvironmentFile=-%h/.config/jev-desktop/daemon.env", rendered)
        tray = render_tray_unit()
        self.assertIn(" tray", tray)
        self.assertIn("ConditionEnvironment=WAYLAND_DISPLAY", tray)

    def test_install_seeds_private_config_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config.json"
            units = root / "units"
            with patch("desktop_service_unit._systemctl", return_value=None):
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    self.assertEqual(_install(_args(allow_app=["kate", "firefox"], config=config), units), 0)
                self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
                self.assertEqual(json.loads(config.read_text())["allowed_apps"], ["kate", "firefox"])
                self.assertTrue((units / UNIT_NAME).exists())

                # Same effective allowlist: install again without clobbering.
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    self.assertEqual(_install(_args(config=config), units), 0)
                # A different allowlist must not overwrite the owner's config.
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    self.assertEqual(_install(_args(allow_app=["kcalc"], config=config), units), 2)
                self.assertEqual(json.loads(config.read_text())["allowed_apps"], ["kate", "firefox"])

    def test_install_with_tray_writes_both_units_and_uninstall_removes_them(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config.json"
            units = root / "units"
            with patch("desktop_service_unit._systemctl", return_value=None):
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    self.assertEqual(_install(_args(allow_app=["kate"], config=config, tray=True), units), 0)
                self.assertEqual(json.loads(buffer.getvalue())["units"].keys(),
                                 {UNIT_NAME, TRAY_UNIT_NAME})
                self.assertTrue((units / TRAY_UNIT_NAME).exists())
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    self.assertEqual(_status(_args(config=config), units), 0)
                report = json.loads(buffer.getvalue())
                self.assertTrue(report["tray"]["installed"])
                self.assertFalse(report["tray"]["drift"])
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    self.assertEqual(_uninstall(_args(), units), 0)
                self.assertFalse((units / UNIT_NAME).exists())
                self.assertFalse((units / TRAY_UNIT_NAME).exists())

    def test_install_requires_an_allowlist_when_no_config_exists(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                self.assertEqual(_install(_args(config=root / "config.json"), root / "units"), 2)
            self.assertEqual(json.loads(buffer.getvalue())["error"], "allowlist_required")
            self.assertFalse((root / "config.json").exists())

    def test_status_reports_drift_and_uninstall_requires_stop_flag(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config.json"
            units = root / "units"
            with patch("desktop_service_unit._systemctl", return_value=None):
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    _install(_args(allow_app=["kate"], config=config), units)
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    self.assertEqual(_status(_args(config=config), units), 0)
                report = json.loads(buffer.getvalue())
                self.assertTrue(report["installed"])
                self.assertFalse(report["drift"])

                (units / UNIT_NAME).write_text("stale\n", encoding="utf-8")
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    _status(_args(config=config), units)
                self.assertTrue(json.loads(buffer.getvalue())["drift"])

                with patch("desktop_service_unit._unit_state", return_value=("active", "enabled")):
                    buffer = io.StringIO()
                    with contextlib.redirect_stdout(buffer):
                        self.assertEqual(_uninstall(_args(), units), 2)
                    self.assertEqual(json.loads(buffer.getvalue())["error"], "unit_is_active")
                    self.assertTrue((units / UNIT_NAME).exists())

                    buffer = io.StringIO()
                    with contextlib.redirect_stdout(buffer):
                        self.assertEqual(_uninstall(_args(stop=True), units), 0)
                    self.assertFalse((units / UNIT_NAME).exists())


class TraySettingsTests(unittest.TestCase):
    def test_settings_roundtrip_is_private_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "nested" / "tray.json"
            self.assertEqual(load_settings(path), DEFAULT_SETTINGS)
            save_settings(path, {"desktop_mode": MODE_LIVE, "autonomy_mode": "yolo", "poll_seconds": 7,
                                 "confirm_kill_switch": False, "confirm_cleanup": False})
            loaded = load_settings(path)
            self.assertEqual(loaded["desktop_mode"], MODE_LIVE)
            self.assertEqual(loaded["autonomy_mode"], "yolo")
            self.assertEqual(loaded["poll_seconds"], 7.0)
            self.assertFalse(loaded["confirm_kill_switch"])
            self.assertFalse(loaded["confirm_cleanup"])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

            path.write_text('{"desktop_mode": "sideways", "autonomy_mode": "chaos", "poll_seconds": 900}',
                            encoding="utf-8")
            self.assertEqual(load_settings(path), DEFAULT_SETTINGS)
            path.write_text("not json", encoding="utf-8")
            self.assertEqual(load_settings(path), DEFAULT_SETTINGS)
            with self.assertRaises(ValueError):
                save_settings(path, {"desktop_mode": "sideways"})


class _FakeWidget:
    def __init__(self, label=None):
        self.children = []
        self.label = label
        self.sensitive = True
        self.submenu = None
        self.tooltip = None

    def append(self, child):
        self.children.append(child)

    def remove(self, child):
        self.children.remove(child)

    def get_children(self):
        return list(self.children)

    def show_all(self):
        return None

    def set_sensitive(self, value):
        self.sensitive = value

    def set_label(self, value):
        self.label = value

    def get_label(self):
        return self.label

    def set_tooltip_text(self, value):
        self.tooltip = value

    def set_submenu(self, value):
        self.submenu = value

    def connect(self, signal, handler, *args):
        return None

    def join_group(self, other):
        return None

    def set_active(self, value):
        self.active = value

    def get_active(self):
        return getattr(self, "active", False)

    def run(self):
        return self.response

    def destroy(self):
        return None


class _FakeGtk:
    Menu = _FakeWidget
    MenuItem = _FakeWidget
    RadioMenuItem = _FakeWidget
    SeparatorMenuItem = _FakeWidget

    class MessageType:
        QUESTION = "question"
        INFO = "info"

    class ButtonsType:
        OK = "ok"
        OK_CANCEL = "ok_cancel"

    class ResponseType:
        OK = 1
        CANCEL = 0

    class IconTheme:
        @staticmethod
        def get_default():
            return types.SimpleNamespace(has_icon=lambda name: True)

    def __init__(self, response=1):
        self.response = response

    def MessageDialog(self, **kwargs):  # noqa: N802 (mirrors the GTK constructor)
        dialog = _FakeWidget()
        dialog.response = self.response
        dialog.format_secondary_text = lambda text: None
        return dialog


class _FakeAppIndicator:
    class IndicatorCategory:
        APPLICATION_STATUS = "application-status"

    class IndicatorStatus:
        ACTIVE = "active"

    class Indicator:
        @classmethod
        def new(cls, name, icon, category):
            return cls(name, icon, category)

        def __init__(self, name, icon, category):
            self.name = name
            self.icon = icon
            self.category = category
            self.title = None
            self.menu = None

        def set_status(self, value):
            self.status = value

        def set_title(self, value):
            self.title = value

        def set_menu(self, value):
            self.menu = value


class TrayLogicTests(unittest.TestCase):
    def _tray(self):
        tray = DesktopTray.__new__(DesktopTray)
        tray.gtk = _FakeGtk()
        tray.glib = types.SimpleNamespace(idle_add=lambda fn, *a: (fn(*a), False)[1])
        tray.socket = None
        tray.unit = "jev-desktop"
        tray.settings_path = Path("/nonexistent/tray.json")
        tray.settings = dict(DEFAULT_SETTINGS)
        tray._persist_settings = lambda: True
        tray.status = None
        tray.capabilities = {"allowed_apps": ["kate"]}
        tray.service_state = ("inactive", "disabled")
        tray.owner_state = "running"
        tray.error = None
        tray._syncing = False
        tray._ipc_lock = threading.Lock()
        tray._stop = threading.Event()
        tray._build_menu(_FakeAppIndicator)
        tray.calls = []
        tray.call = lambda method, params=None, timeout=30.0: (
            tray.calls.append((method, params or {})), {"ok": True})[1]
        tray._unit_state = lambda: ("inactive", "disabled")
        tray._inform = lambda *_: None
        tray._async = lambda work, done=None: done(True, work())
        return tray

    def test_virtual_start_sends_no_live_authorization(self):
        tray = self._tray()
        tray._on_start(None, "kate")
        method, params = tray.calls[0]
        self.assertEqual(method, "session_start")
        self.assertEqual(params["desktop_mode"], "virtual")
        self.assertNotIn("owner_present_override", params)
        self.assertNotIn("temporary_a11y", params)

    def test_live_start_requires_the_per_task_confirmation(self):
        tray = self._tray()
        tray.settings["desktop_mode"] = MODE_LIVE
        tray._confirm_live = lambda app: False
        tray._on_start(None, "kate")
        self.assertEqual([call for call in tray.calls if call[0] == "session_start"], [])

        tray = self._tray()
        tray.settings["desktop_mode"] = MODE_LIVE
        tray._confirm_live = lambda app: True
        tray._on_start(None, "kate")
        method, params = tray.calls[0]
        self.assertEqual(method, "session_start")
        self.assertEqual(params["desktop_mode"], "live")
        self.assertIs(params["owner_present_override"], True)
        self.assertIs(params["temporary_a11y"], True)

    def test_kill_switch_requires_confirmation(self):
        tray = self._tray()
        tray._confirm = lambda *_: False
        tray._on_kill_switch(None)
        self.assertEqual(tray.calls, [])
        tray._confirm = lambda *_: True
        tray._on_kill_switch(None)
        self.assertEqual([call[0] for call in tray.calls][:2], ["stop_all", "shutdown"])

    def test_autonomy_and_confirmation_settings_reach_the_actions(self):
        tray = self._tray()
        tray.settings["autonomy_mode"] = "supervised"
        tray._on_start(None, "kate")
        self.assertEqual(tray.calls[0][1]["mode"], "supervised")

        tray = self._tray()
        tray.settings["confirm_kill_switch"] = False
        tray._confirm = lambda *_: (_ for _ in ()).throw(AssertionError("must not prompt"))
        tray._on_kill_switch(None)
        self.assertEqual([call[0] for call in tray.calls][:2], ["stop_all", "shutdown"])

        tray = self._tray()
        tray.settings["confirm_cleanup"] = False
        tray._confirm = lambda *_: (_ for _ in ()).throw(AssertionError("must not prompt"))
        tray._on_cleanup(None)
        self.assertEqual([call[0] for call in tray.calls], ["stop_all", "status"])

    def test_labels_reflect_owner_state(self):
        tray = self._tray()
        tray.owner_state = "stopped"
        tray.status = None
        tray._refresh_labels()
        self.assertIn("stopped", tray.item_owner.get_label())
        self.assertEqual(tray.item_session.get_label(), "Session: none")
        tray.status = {"session": {"app": "kate", "desktop_mode": "virtual", "state": "running",
                                  "age_seconds": 4.0, "owner_pid": 10}}
        tray.owner_state = "running"
        tray._refresh_labels()
        self.assertIn("kate", tray.item_session.get_label())
        self.assertTrue(tray.item_cleanup.sensitive)
        self.assertFalse(tray.start_root.sensitive)


if __name__ == "__main__":
    unittest.main()
