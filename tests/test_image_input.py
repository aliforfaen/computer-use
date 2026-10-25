from __future__ import annotations

import threading
import unittest
from datetime import datetime, timezone
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from jevdesktop.image_input import perform_image_input
from jevdesktop.transactions import AuditLog, AutonomyMode


def make_ref(capture_id: str, captured_at: str, *, window_id: str = "window-1"):
    mapping = {
        "selected_rect": {"x": 100, "y": 50, "width": 200, "height": 120},
        "window_rect": {"x": 100, "y": 50, "width": 200, "height": 120},
        "source_dimensions": {"width": 800, "height": 600},
    }
    return SimpleNamespace(capture_id=capture_id, captured_at=captured_at,
                           session_id="session-1", window_id=window_id, scope="app",
                           width=200, height=120, source_width=800, source_height=600,
                           mapping=mapping)


class FakeStore:
    def __init__(self, ref):
        self.ref = ref

    def get(self, capture_id):
        if capture_id != self.ref.capture_id:
            raise KeyError(capture_id)
        return SimpleNamespace(ref=self.ref)


class FakeAdapter:
    def __init__(self, ref, post_ref=None):
        self.store = FakeStore(ref)
        self.post_ref = post_ref
        self._resolve_window = Mock(return_value={
            "id": ref.window_id,
            "frame": {"x": 100, "y": 50, "width": 200, "height": 120},
        })
        self.capture = Mock(return_value=post_ref)
        self.observe = Mock(return_value=SimpleNamespace(metadata={"capture_id": "after"}))


class ImageInputTests(unittest.TestCase):
    def session(self, *, mode="yolo", ref=None, post_ref=None):
        stamp = datetime.now(timezone.utc).isoformat()
        ref = ref or make_ref("before", stamp)
        post_ref = post_ref or make_ref("after", stamp)
        worker = SimpleNamespace(
            mouse_click=Mock(), mouse_drag=Mock(), mouse_scroll=Mock(),
            keyboard_key=Mock(), keyboard_type=Mock(),
            active_window=Mock(return_value='id: window-1\n- org.kde.kate "Editor" [active]'),
        )
        tx = SimpleNamespace(_audit=Mock())
        session = SimpleNamespace(
            session_id="session-1", app="kate", mode=mode, worker=worker,
            adapter=FakeAdapter(ref, post_ref), valid_input_captures={ref.capture_id},
            pending_input_captures={ref.capture_id},
            visual_input_uncertain=False, actions=0, observations=0,
            max_actions=10, max_observations=20, cancel=threading.Event(), tx=tx,
        )
        return session, worker

    def test_click_maps_image_point_and_returns_fresh_uncertain_state(self):
        session, worker = self.session()
        now = datetime.now(timezone.utc)
        result = perform_image_input(session, {
            "capture_id": "before", "action": "click", "x": 10, "y": 20,
        }, now=now)
        worker.mouse_click.assert_called_once_with(110, 70, button="left", double=False)
        self.assertEqual(result["status"], "observed")
        self.assertFalse(result["verified"])
        self.assertEqual(result["effect"], "uncertain")
        self.assertEqual(session.actions, 1)
        self.assertEqual(session.observations, 1)
        self.assertFalse(session.visual_input_uncertain)
        self.assertEqual(session.valid_input_captures, set())
        self.assertEqual(session.pending_input_captures, {"after"})
        # A fresh metadata-only frame is not an authorized image target. It
        # must pass through observe(image/both) before service code arms it.
        with self.assertRaisesRegex(ValueError, "capture_not_current"):
            perform_image_input(session, {"capture_id": "after", "action": "click", "x": 1, "y": 1}, now=now)

    def test_guarded_requires_explicit_approval_before_consuming_capture(self):
        session, worker = self.session(mode="guarded")
        with self.assertRaisesRegex(ValueError, "approval_required"):
            perform_image_input(session, {"capture_id": "before", "action": "click", "x": 1, "y": 1})
        worker.mouse_click.assert_not_called()
        self.assertEqual(session.valid_input_captures, {"before"})
        self.assertEqual(session.actions, 0)

    def test_capture_is_one_use_and_window_bounds_are_enforced(self):
        session, worker = self.session()
        with self.assertRaisesRegex(ValueError, "coordinates_out_of_bounds"):
            perform_image_input(session, {"capture_id": "before", "action": "click", "x": 200, "y": 1})
        worker.mouse_click.assert_not_called()
        perform_image_input(session, {"capture_id": "before", "action": "click", "x": 1, "y": 1}, now=datetime.now(timezone.utc))
        with self.assertRaisesRegex(ValueError, "capture_not_current"):
            perform_image_input(session, {"capture_id": "before", "action": "click", "x": 1, "y": 1}, now=datetime.now(timezone.utc))

    def test_stale_capture_is_rejected(self):
        old = make_ref("before", "2026-10-25T11:59:44+00:00")
        session, worker = self.session(ref=old)
        with self.assertRaisesRegex(ValueError, "capture_stale"):
            perform_image_input(session, {"capture_id": "before", "action": "click", "x": 1, "y": 1},
                                now=datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc))
        worker.mouse_click.assert_not_called()

    def test_driver_failure_latches_uncertainty_and_invalidates_old_captures(self):
        session, worker = self.session()
        worker.mouse_click.side_effect = RuntimeError("driver failed")
        with self.assertRaisesRegex(ValueError, "effect_uncertain"):
            perform_image_input(session, {"capture_id": "before", "action": "click", "x": 1, "y": 1}, now=datetime.now(timezone.utc))
        self.assertTrue(session.visual_input_uncertain)
        self.assertEqual(session.valid_input_captures, set())
        self.assertEqual(session.actions, 1)
        with self.assertRaisesRegex(ValueError, "recovery_required"):
            perform_image_input(session, {"capture_id": "before", "action": "click", "x": 1, "y": 1})

    def test_mapping_window_change_and_key_allowlist_fail_before_input(self):
        session, worker = self.session()
        session.adapter._resolve_window.return_value = {
            "id": "different-window", "frame": {"x": 100, "y": 50, "width": 200, "height": 120},
        }
        with self.assertRaisesRegex(ValueError, "window_changed"):
            perform_image_input(session, {"capture_id": "before", "action": "click", "x": 1, "y": 1}, now=datetime.now(timezone.utc))
        session.adapter._resolve_window.return_value = {
            "id": "window-1", "frame": {"x": 100, "y": 50, "width": 200, "height": 120},
        }
        with self.assertRaisesRegex(ValueError, "unsupported_key"):
            perform_image_input(session, {"capture_id": "before", "action": "keyboard_shortcut", "key": "ctrl+q"}, now=datetime.now(timezone.utc))
        worker.keyboard_key.assert_not_called()

    def test_type_text_rechecks_active_window_after_coordinate_click(self):
        session, worker = self.session(mode="guarded")
        result = perform_image_input(session, {
            "capture_id": "before", "action": "type_text", "approved": True,
            "x": 12, "y": 8, "text": "hello",
        }, now=datetime.now(timezone.utc))
        worker.mouse_click.assert_called_once_with(112, 58, button="left", double=False)
        worker.active_window.assert_called_once_with()
        worker.keyboard_type.assert_called_once_with("hello")
        self.assertFalse(result["verified"])

    def test_type_text_refuses_when_clicked_target_is_no_longer_active(self):
        session, worker = self.session()
        worker.active_window.return_value = 'id: other-window\n- org.kde.kate "Dialog" [active]'
        with self.assertRaisesRegex(ValueError, "effect_uncertain"):
            perform_image_input(session, {
                "capture_id": "before", "action": "type_text", "x": 12, "y": 8, "text": "hello",
            }, now=datetime.now(timezone.utc))
        worker.mouse_click.assert_called_once()
        worker.keyboard_type.assert_not_called()
        self.assertTrue(session.visual_input_uncertain)

    def test_drag_and_scroll_use_bounded_mapped_coordinates(self):
        session, worker = self.session()
        perform_image_input(session, {
            "capture_id": "before", "action": "drag", "x": 1, "y": 2, "end_x": 20, "end_y": 30,
        }, now=datetime.now(timezone.utc))
        worker.mouse_drag.assert_called_once_with(101, 52, 120, 80, button="left")

        session, worker = self.session()
        perform_image_input(session, {
            "capture_id": "before", "action": "scroll", "x": 10, "y": 10,
            "direction": "up", "steps": 3,
        }, now=datetime.now(timezone.utc))
        worker.mouse_scroll.assert_called_once_with(110, 60, -3, horizontal=False, discrete=True, steps=3)

    def test_audit_keeps_only_fixed_image_action_and_uncertain_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            audit = AuditLog(path)
            audit.write(tool="input", app="kate", mode=AutonomyMode.YOLO,
                        event="image_double_click", status="ok",
                        reason="input_delivered_effect_uncertain")
            record = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(record["event"], "image_double_click")
        self.assertEqual(record["action"], "double_click")
        self.assertEqual(record["verification"], "unavailable")
        self.assertIs(record["verified"], False)
        self.assertEqual(record["effect"], "uncertain")
        self.assertEqual(record["evidence"]["postcondition"], None)


if __name__ == "__main__":
    unittest.main()
