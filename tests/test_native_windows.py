from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import jevdesktop.native_windows as native_windows


def _raw_window(window_id="w1", *, parent=None):
    return {
        "id": window_id,
        "app": "org.example.Editor",
        "caption": "document",
        "pid": 321,
        "active": True,
        "normal": True,
        "popup": False,
        "dialog": parent is not None,
        "transient": parent is not None,
        "transient_for": parent,
        "frame": {"x": 0, "y": 0, "width": 800, "height": 600},
        "client": {"x": 0, "y": 0, "width": 800, "height": 600},
    }


class NativeWindowTests(unittest.TestCase):
    def test_collect_windows_preserves_exact_transient_and_pid_metadata(self):
        raw = [_raw_window("root"), _raw_window("dialog", parent="root")]
        with patch("kwin_mcp.geometry.run_kwin_script", return_value=json.dumps(raw)) as run:
            rows = native_windows.collect_windows()
        self.assertEqual([row["id"] for row in rows], ["root", "dialog"])
        self.assertEqual(rows[1]["transient_for"], "root")
        self.assertEqual(rows[1]["pid"], 321)
        self.assertIsNone(rows[0]["current_desktop_id"])
        self.assertEqual(rows[0]["desktop_ids"], [])
        self.assertIsNone(rows[0]["on_current_desktop"])
        self.assertIsNone(rows[0]["minimized"])
        self.assertIsNone(rows[0]["notification"])
        self.assertIsNone(rows[0]["output_name"])
        self.assertIsNone(rows[0]["stacking_order"])
        run.assert_called_once()

    def test_collect_windows_normalizes_desktop_and_output_context(self):
        raw = _raw_window()
        raw.update(current_desktop_id="desktop-a", desktop_ids=["desktop-a", "desktop-b"],
                   on_current_desktop=True, minimized=False, notification=True,
                   output_name="DP-1", stacking_order=7)
        with patch("kwin_mcp.geometry.run_kwin_script", return_value=json.dumps([raw])):
            row = native_windows.collect_windows()[0]
        self.assertEqual(row["current_desktop_id"], "desktop-a")
        self.assertEqual(row["desktop_ids"], ["desktop-a", "desktop-b"])
        self.assertTrue(row["on_current_desktop"])
        self.assertFalse(row["minimized"])
        self.assertTrue(row["notification"])
        self.assertEqual(row["output_name"], "DP-1")
        self.assertEqual(row["stacking_order"], 7)

    def test_activate_uses_serialized_exact_window_id(self):
        target = 'window"; closeWindow(); //'
        with patch("kwin_mcp.geometry.run_kwin_script", return_value='{"activated":true}') as run:
            self.assertTrue(native_windows.activate_window_id(target))
        self.assertIn(json.dumps(target), run.call_args.args[0])
        self.assertNotIn("caption", run.call_args.args[0])

    def test_workspace_snapshot_includes_active_id_and_outputs(self):
        rows = [_raw_window("root")]
        outputs = {"screens": [], "virtual": None}
        with (patch.object(native_windows, "_collect_snapshot",
                           return_value=(rows, "desktop-a", "activity-a")),
              patch("kwin_mcp.geometry.outputs", return_value=outputs)):
            result = native_windows.workspace_snapshot()
        self.assertEqual(result["active_window_id"], "root")
        self.assertEqual(result["outputs"], outputs)
        self.assertEqual(result["current_desktop_id"], "desktop-a")
        self.assertEqual(result["currentActivity"], "activity-a")
        self.assertTrue(result["checked_at"])


if __name__ == "__main__":
    unittest.main()
