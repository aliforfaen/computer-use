from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import live_desktop_probe as probe


def _window(window_id, app, caption, pid, *, active=False):
    return {"id": window_id, "app": app, "caption": caption,
            "client": {"x": 0, "y": 0, "width": 1280, "height": 800}, "active": active}


def _button():
    return {"role": "button", "name": "One", "states": ["enabled", "sensitive", "showing", "visible"],
            "actions": ["Press"], "x": 200, "y": 200, "width": 40, "height": 30, "mapped": True}


def _display(value):
    return {"role": "text", "name": "Display", "states": ["editable", "enabled", "sensitive", "showing", "visible"],
            "actions": ["SetFocus"], "x": 100, "y": 100, "width": 300, "height": 60,
            "mapped": True, "text": value}


class FakeProcess:
    pid = 4321

    def __init__(self, on_terminate=None):
        self.returncode = None
        self.on_terminate = on_terminate

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0
        if self.on_terminate:
            self.on_terminate()

    def wait(self, timeout=None):
        return self.returncode


class FakeLiveSession:
    def __init__(self, engine):
        self.engine = engine
        self.process = FakeProcess()

    def launch_app(self, command):
        if command != ["kcalc"]:
            raise AssertionError("unexpected launch command")
        self.engine.windows.append(_window("kcalc-1", "org.kde.kcalc", "KCalc", self.process.pid))
        self.process.on_terminate = lambda: setattr(
            self.engine, "windows", [row for row in self.engine.windows if row["id"] != "kcalc-1"])
        return type("App", (), {"pid": self.process.pid, "process": self.process})()


class FakeEngine:
    def __init__(self):
        self.session = FakeLiveSession(self)
        self.windows = [_window("orig-1", "org.kde.konsole", "Terminal", 77, active=True)]
        self.active_id = "orig-1"
        self.display = ""
        self.clicked = []
        self.connected = False

    def session_connect(self, keep_screenshots=False):
        assert keep_screenshots is False
        self.connected = True
        return "Connected to live KWin session. Input backend: KWin EIS"

    def _get_session(self):
        return self.session

    def _run_kwin_query(self, request):
        op = request.get("op")
        if op == "active":
            row = next(row for row in self.windows if row["id"] == self.active_id)
            return {"ok": True, "result": row}
        if op == "activate":
            needle = request.get("app_name", "").casefold()
            rows = [row for row in self.windows if needle in f"{row['app']} {row['caption']}".casefold()]
            if len(rows) != 1:
                return {"ok": True, "result": ""}
            self.active_id = rows[0]["id"]
            return {"ok": True, "result": rows[0]["caption"]}
        if op:
            return {"ok": False, "error": "unsupported"}
        return {"ok": True, "result": list(self.windows)}

    def _run_atspi(self, op, **kwargs):
        assert op == "find" and kwargs.get("app_name") == "kcalc"
        return {"ok": True, "result": [_button(), _display(self.display)]}

    def mouse_click(self, x, y, button="left"):
        self.clicked.append((x, y, button))
        self.display = "1"
        self.windows[0]["caption"] = "Terminal - updated"

    def session_stop(self):
        self.session.process.terminate()
        self.connected = False
        return "Disconnected from live KWin session."


class FakeAdapter:
    def __init__(self, *args, **kwargs):
        self.calls = 0

    def capture(self, app, scope):
        assert (app, scope) == ("kcalc", "app")
        self.calls += 1
        return object()

    def observe(self, capture, mode):
        assert mode == "metadata"
        return type("Observation", (), {"metadata": {
            "capture_id": f"capture-{self.calls}", "image_sha256": f"hash-{self.calls}",
            "dimensions": {"width": 640, "height": 480}, "scope": "app"}})()


class LiveDesktopProbeTests(unittest.TestCase):
    def test_fake_flow_clicks_fresh_semantic_target_restores_and_cleans_owned_process(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(probe, "_a11y_flags", return_value={"IsEnabled": True, "ScreenReaderEnabled": True}), \
             patch.object(probe, "_raw_window_rows", return_value=[{"id": "kcalc-1", "pid": 4321}]), \
             patch.object(probe, "_version_facts", return_value={"kwin_mcp": "0.10.0", "kwin": "test"}):
            engine = FakeEngine()
            report = probe.run_probe(output_root=Path(folder), engine_factory=lambda: engine,
                                     adapter_factory=FakeAdapter)
            self.assertEqual(report["result"], "passed")
            self.assertEqual(engine.clicked, [(220, 215, "left")])
            self.assertTrue(report["restored_original_window"])
            self.assertEqual(report["cleanup"], "confirmed")
            self.assertEqual(report["owned_process_cleanup"], "confirmed")
            self.assertEqual(report["owned_window_cleanup"], "confirmed")
            self.assertEqual(engine.session.process.returncode, 0)
            stored = json.loads(Path(report["report_path"]).read_text())
            self.assertNotIn("caption", stored["original_window"])
            self.assertEqual(len(stored["captures"]), 2)
            self.assertEqual([phase["name"] for phase in stored["phases"] if phase["name"].startswith("owner_watch")], [])

    def test_visible_holds_are_named_and_current_caption_restores_by_stable_id(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(probe, "_a11y_flags", return_value={"IsEnabled": True, "ScreenReaderEnabled": True}), \
             patch.object(probe, "_raw_window_rows", return_value=[{"id": "kcalc-1", "pid": 4321}]), \
             patch.object(probe, "_version_facts", return_value={"kwin_mcp": "0.10.0", "kwin": "test"}), \
             patch.object(probe.time, "sleep") as sleep:
            engine = FakeEngine()
            report = probe.run_probe(output_root=Path(folder), engine_factory=lambda: engine,
                                     adapter_factory=FakeAdapter, visible_hold=True)
            self.assertEqual(report["result"], "passed")
            self.assertTrue(report["restored_original_window"])
            self.assertEqual(engine.active_id, "orig-1")
            names = [phase["name"] for phase in report["phases"]]
            self.assertIn("owner_watch_before", names)
            self.assertIn("owner_watch_after", names)
            self.assertEqual(sleep.call_args_list[:2], [unittest.mock.call(2), unittest.mock.call(3)])

    def test_temporary_atspi_flags_restore_exact_initial_values_after_success(self):
        initial = {"IsEnabled": False, "ScreenReaderEnabled": False}
        enabled = {"IsEnabled": True, "ScreenReaderEnabled": True}
        calls = []

        def set_flags(address, flags):
            calls.append((address, dict(flags)))
            return dict(flags)

        with tempfile.TemporaryDirectory() as folder, \
             patch.object(probe, "_a11y_flags", return_value=initial), \
             patch.object(probe, "_raw_window_rows", return_value=[{"id": "kcalc-1", "pid": 4321}]), \
             patch.object(probe, "_a11y_address", return_value="session-bus"), \
             patch.object(probe, "_set_a11y_flags_at", side_effect=set_flags), \
             patch.object(probe, "_version_facts", return_value={"kwin_mcp": "0.10.0", "kwin": "test"}):
            engine = FakeEngine()
            report = probe.run_probe(output_root=Path(folder), engine_factory=lambda: engine,
                                     adapter_factory=FakeAdapter, temporary_a11y=True)
            self.assertEqual(report["result"], "passed")
            self.assertEqual(calls, [("session-bus", enabled), ("session-bus", initial)])
            self.assertTrue(report["temporary_a11y_restored"])

    def test_temporary_atspi_flags_restore_even_when_capture_fails(self):
        from observation import ObservationError

        initial = {"IsEnabled": False, "ScreenReaderEnabled": False}
        enabled = {"IsEnabled": True, "ScreenReaderEnabled": True}
        calls = []

        def set_flags(address, flags):
            calls.append(dict(flags))
            return dict(flags)

        class BadAdapter(FakeAdapter):
            def capture(self, app, scope):
                raise ObservationError("unsupported_mapping", "mapping unavailable")

        with tempfile.TemporaryDirectory() as folder, \
             patch.object(probe, "_a11y_flags", return_value=initial), \
             patch.object(probe, "_raw_window_rows", return_value=[{"id": "kcalc-1", "pid": 4321}]), \
             patch.object(probe, "_a11y_address", return_value="session-bus"), \
             patch.object(probe, "_set_a11y_flags_at", side_effect=set_flags), \
             patch.object(probe, "_version_facts", return_value={"kwin_mcp": "0.10.0", "kwin": "test"}):
            engine = FakeEngine()
            report = probe.run_probe(output_root=Path(folder), engine_factory=lambda: engine,
                                     adapter_factory=BadAdapter, temporary_a11y=True)
            self.assertEqual(report["result"], "failed")
            self.assertEqual(engine.clicked, [])
            self.assertEqual(calls, [enabled, initial])
            self.assertTrue(report["temporary_a11y_restored"])

    def test_raw_pid_lookup_requires_exact_unique_public_window_id(self):
        self.assertEqual(probe._raw_pid_for_window_id([
            {"id": "window-1", "pid": 4321}, {"id": "window-2", "pid": 4322}], "window-2"), 4322)
        with self.assertRaisesRegex(probe.ProbeError, "missing or ambiguous"):
            probe._raw_pid_for_window_id([{"id": "window-1", "pid": 4321}], "window-2")
        with self.assertRaisesRegex(probe.ProbeError, "missing or ambiguous"):
            probe._raw_pid_for_window_id([
                {"id": "window-1", "pid": 4321}, {"id": "window-1", "pid": 4322}], "window-1")

    def test_capture_mapping_failure_refuses_input_and_still_restores(self):
        from observation import ObservationError

        class BadAdapter(FakeAdapter):
            def capture(self, app, scope):
                raise ObservationError("unsupported_mapping", "mapping unavailable")

        with tempfile.TemporaryDirectory() as folder, \
             patch.object(probe, "_a11y_flags", return_value={"IsEnabled": True, "ScreenReaderEnabled": True}), \
             patch.object(probe, "_version_facts", return_value={"kwin_mcp": "0.10.0", "kwin": "test"}):
            engine = FakeEngine()
            report = probe.run_probe(output_root=Path(folder), engine_factory=lambda: engine,
                                     adapter_factory=BadAdapter)
            self.assertEqual(report["result"], "failed")
            self.assertEqual(engine.clicked, [])
            self.assertTrue(report["restored_original_window"])
            self.assertEqual(report["cleanup"], "confirmed")


if __name__ == "__main__":
    unittest.main()
