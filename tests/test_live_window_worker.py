from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jevdesktop.desktop_worker import Worker


PROFILE = {
    "id": "example-editor",
    "command": ["example-editor"],
    "window_app_ids": ["org.example.Editor"],
    "atspi_app": "Example Editor",
}


def _window(window_id, app, pid, *, parent=None, active=False, popup=False, dialog=False, rect=None):
    rect = rect or {"x": 0, "y": 0, "width": 800, "height": 600}
    return {"id": window_id, "app": app, "caption": "same title", "pid": pid,
            "active": active, "popup": popup, "dialog": dialog,
            "transient": parent is not None, "transient_for": parent,
            "frame": dict(rect), "client": dict(rect)}


class AttachedEngine:
    def __init__(self):
        self._session = None
        self.launch_calls = 0
        self.stop_calls = 0
        self.atspi_results = []

    def session_connect(self, keep_screenshots=False):
        return "Connected to live KWin session. Input backend: KWin EIS"

    def session_stop(self):
        self.stop_calls += 1
        return "Disconnected from live KWin session."

    def _get_session(self):
        raise AssertionError("attaching must not launch an app")

    def _ensure_atspi_worker(self):
        return None

    def _run_atspi(self, operation, **kwargs):
        assert operation == "find"
        assert kwargs["app_name"] == PROFILE["atspi_app"]
        return {"ok": True, "result": list(self.atspi_results)}

    def mouse_click(self, x, y, *, button="left", double=False):
        return {"x": x, "y": y, "button": button, "double": double}

    def mouse_drag(self, x, y, end_x, end_y, *, button="left"):
        return {"start": (x, y), "end": (end_x, end_y), "button": button}


class LiveWindowWorkerTests(unittest.TestCase):
    def test_window_relationships_use_transient_and_process_identity_not_caption(self):
        worker = Worker()
        worker.profile = PROFILE
        worker.live_root_window_id = "root"
        worker.live_root_pid = 10
        rows = [
            _window("root", "org.example.Editor", 10),
            _window("dialog", "org.example.Editor", 99, parent="root", dialog=True),
            _window("same-caption", "org.other.App", 21),
            _window("external-popup", "org.wallet.Prompt", 22, parent="root", popup=True),
            _window("same-process-dialog", "org.example.Editor", 10, dialog=True),
            _window("other-document-dialog", "org.example.Editor", 10, parent="other-root", dialog=True),
        ]
        state = worker._task_window_state(rows)
        self.assertEqual({row["id"] for row in state["related_windows"]},
                         {"root", "dialog", "same-process-dialog"})
        self.assertEqual({row["id"] for row in state["background_windows"]},
                         {"same-caption", "external-popup", "other-document-dialog"})
        self.assertEqual([row["id"] for row in state["external_transients"]], ["external-popup"])

    def test_candidate_is_assigned_to_smallest_containing_transient_window(self):
        worker = Worker()
        rows = [
            _window("root", "org.example.Editor", 10),
            _window("dialog", "org.example.Editor", 99, parent="root", dialog=True,
                    rect={"x": 200, "y": 200, "width": 200, "height": 100}),
        ]
        element = {"mapped": True, "x": 220, "y": 220, "width": 30, "height": 20}
        self.assertEqual(worker._window_for_element(element, rows), "dialog")

    def test_attach_binds_exact_window_without_launch_or_a11y_reads(self):
        original = _window("target-id", "org.example.Editor", 42, active=True)
        worker = Worker()
        engine = AttachedEngine()
        with (patch("kwin_mcp.core.AutomationEngine", return_value=engine),
              patch("jevdesktop.native_windows.collect_windows", return_value=[original]),
              patch("jevdesktop.live_desktop_probe._a11y_flags", side_effect=AssertionError("must not read"))):
            result = worker.start("example-editor", str(Path(tempfile.gettempdir()) / "worker-journal.json"),
                                  "live", profile=PROFILE, window_id="target-id", attach=True,
                                  semantic_candidates=False)
        self.assertTrue(result["started"])
        self.assertTrue(result["attached"])
        self.assertEqual(result["root_window_id"], "target-id")
        self.assertIsNone(result["task_owned_window_id"])
        self.assertEqual(engine.launch_calls, 0)
        self.assertEqual(engine.stop_calls, 0)

    def test_attached_cleanup_does_not_terminate_or_close_target(self):
        class PoisonProcess:
            def terminate(self):
                raise AssertionError("attached target process must not be terminated")

            def poll(self):
                return None

        worker = Worker()
        worker.desktop_mode = "live"
        worker.live_attached = True
        worker.live_process = PoisonProcess()
        worker.original_window = {"id": "original"}
        with (patch.object(worker, "restore_live_focus", return_value=True),
              patch.object(worker, "_restore_a11y_flags", return_value=True)):
            self.assertTrue(worker._stop_live_app())

    def test_unrelated_active_popup_is_reported_without_activation(self):
        worker = Worker()
        worker.desktop_mode = "live"
        worker.engine = object()
        worker.profile = PROFILE
        worker.live_root_window_id = "root"
        worker.live_root_pid = 10
        worker.live_window_id = "root"
        rows = [
            _window("root", "org.example.Editor", 10),
            _window("popup", "org.wallet.Prompt", 20, active=True, popup=True),
        ]
        with (patch.object(worker, "_native_windows", return_value=rows),
              patch("jevdesktop.native_windows.activate_window_id") as activate):
            state = worker._focus_live_app()
        self.assertFalse(state["focused"])
        self.assertEqual(state["interruption"]["id"], "popup")
        self.assertEqual([row["id"] for row in state["unrelated_popups"]], ["popup"])
        activate.assert_not_called()

    def test_missing_task_root_clears_cached_selection_and_refuses_stale_window(self):
        worker = Worker()
        worker.desktop_mode = "live"
        worker.engine = AttachedEngine()
        worker.profile = PROFILE
        worker.live_root_window_id = "root"
        worker.live_root_pid = 10
        worker.live_window_id = "root"
        cached = _window("root", "org.example.Editor", 10)
        worker.live_selected_window = cached
        unrelated = _window("other", "org.other.App", 20, active=True)
        with (patch.object(worker, "_native_windows", return_value=[unrelated]),
              patch("jevdesktop.native_windows.activate_window_id") as activate):
            focus = worker._focus_live_app()
            self.assertFalse(focus["focused"])
            self.assertIsNone(focus["selected_window_id"])
            self.assertIsNone(focus["selected_window"])
            self.assertEqual(focus["interruption"]["reason"], "task_window_unavailable")
            activate.assert_not_called()
            query = worker.call("window_query", {})
        self.assertFalse(query["selected_window_present"])
        self.assertNotIn("selected_window", query)
        self.assertEqual(query["result"][0]["id"], "other")

    def test_workspace_discovery_reuses_live_engine_without_stopping_it(self):
        worker = Worker()
        engine = AttachedEngine()
        worker.engine = engine
        worker.desktop_mode = "live"
        expected = {"windows": [], "active_window_id": None, "outputs": None}
        with patch("jevdesktop.native_windows.workspace_snapshot", return_value=expected):
            self.assertEqual(worker.call("workspace_discover", {}), expected)
        self.assertEqual(engine.stop_calls, 0)

    def test_pointer_input_is_limited_to_active_selected_window_bounds(self):
        worker = Worker()
        worker.engine = AttachedEngine()
        worker.desktop_mode = "live"
        worker.profile = PROFILE
        worker.live_root_window_id = worker.live_window_id = "selected"
        worker.live_root_pid = 10
        selected = _window("selected", "org.example.Editor", 10, active=True,
                           rect={"x": 10, "y": 20, "width": 100, "height": 80})
        with patch.object(worker, "_native_windows", return_value=[selected]):
            result = worker.call("mouse_click", {"x": 30, "y": 40, "button": "right"})
            self.assertEqual(result["button"], "right")
            with self.assertRaisesRegex(ValueError, "live_input_outside_target"):
                worker.call("mouse_click", {"x": 200, "y": 40})
            with self.assertRaisesRegex(ValueError, "live_input_outside_target"):
                worker.call("mouse_drag", {"x": 30, "y": 40, "end_x": 200, "end_y": 40})

    def test_pointer_input_does_not_focus_away_from_unrelated_popup(self):
        worker = Worker()
        worker.engine = AttachedEngine()
        worker.desktop_mode = "live"
        worker.profile = PROFILE
        worker.live_root_window_id = worker.live_window_id = "selected"
        worker.live_root_pid = 10
        rows = [
            _window("selected", "org.example.Editor", 10),
            _window("popup", "org.wallet.Prompt", 20, active=True, popup=True),
        ]
        with patch.object(worker, "_native_windows", return_value=rows):
            with self.assertRaisesRegex(ValueError, "live_selected_window_not_active"):
                worker.call("mouse_click", {"x": 30, "y": 40})

    def test_live_atspi_request_is_disabled_without_semantic_candidates(self):
        worker = Worker()
        worker.engine = AttachedEngine()
        worker.desktop_mode = "live"
        worker.profile = PROFILE
        worker.semantic_candidates = False
        with self.assertRaisesRegex(ValueError, "semantic_candidates_disabled"):
            worker.call("atspi_find", {"app": "Example Editor"})

    def test_atspi_find_returns_only_elements_in_selected_task_window(self):
        worker = Worker()
        worker.engine = AttachedEngine()
        worker.desktop_mode = "live"
        worker.profile = PROFILE
        worker.app = PROFILE["id"]
        worker.semantic_candidates = True
        worker.live_root_window_id = "selected"
        worker.live_root_pid = 10
        worker.live_window_id = "selected"
        selected = _window("selected", "org.example.Editor", 10, active=True)
        background = _window("other", "org.example.Editor", 10,
                             rect={"x": 900, "y": 0, "width": 400, "height": 300})
        worker.engine.atspi_results = [
            {"role": "button", "name": "Save", "mapped": True,
             "x": 20, "y": 20, "width": 50, "height": 20},
            {"role": "button", "name": "Other window", "mapped": True,
             "x": 920, "y": 20, "width": 80, "height": 20},
        ]
        with patch.object(worker, "_native_windows", return_value=[selected, background]):
            result = worker.call("atspi_find", {"app": "example-editor"})
        self.assertEqual([item["name"] for item in result["result"]], ["Save"])
        self.assertEqual(result["result"][0]["window_id"], "selected")

    def test_generic_virtual_profile_launches_argv_and_uses_profile_window_identity(self):
        from shlex import split

        custom = {"id": "sample-editor", "command": ["sample-editor", "--label", "two words"],
                  "window_app_ids": ["org.example.Sample"], "atspi_app": "Sample Editor"}

        class VirtualEngine:
            def session_start(self, **kwargs):
                self.start_kwargs = kwargs
                return "Session started. Input backend: KWin EIS"

            def session_stop(self):
                return "Session stopped"

        engine = VirtualEngine()
        worker = Worker()
        with (patch("kwin_mcp.core.AutomationEngine", return_value=engine),
              patch.object(worker, "_write_journal"),
              patch.object(worker, "_wait_for_app_window") as wait,
              patch("jevdesktop.benchmark_capture._enable_virtual_atspi",
                    side_effect=AssertionError("semantic candidates are disabled"))):
            result = worker.start("sample-editor", "/tmp/jev-test-journal.json", "virtual",
                                  profile=custom, semantic_candidates=False)
        self.assertTrue(result["started"])
        self.assertEqual(split(engine.start_kwargs["app_command"]), custom["command"])
        self.assertEqual(engine.start_kwargs["env"], {})
        self.assertEqual(worker.profile["window_app_ids"], ["org.example.Sample"])
        wait.assert_called_once_with("sample-editor", document_path=None, timeout_seconds=8.0)

    def test_generic_virtual_atspi_uses_profile_app_identity(self):
        worker = Worker()
        worker.engine = AttachedEngine()
        worker.desktop_mode = "virtual"
        worker.profile = {"id": "sample-editor", "command": ["sample-editor"],
                          "window_app_ids": ["org.example.Sample"], "atspi_app": "Sample Editor",
                          "aliases": []}
        worker.semantic_candidates = True
        with patch.object(worker.engine, "_run_atspi", return_value={"ok": True, "result": []}) as run:
            result = worker.call("atspi_find", {"app": "sample-editor"})
        run.assert_called_once_with("find", query="", app_name="Sample Editor")
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
