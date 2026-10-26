"""Fail-closed, image-grounded input against one fresh stored capture.

The helper is a dispatcher primitive, not a planning or verification layer.
Every accepted input consumes the current capture set and requires a fresh
post-input capture before another image-grounded input can proceed.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from jevdesktop.observation import ObservationError
from jevdesktop.transactions import SHORTCUT_KEYS, _APP, _ID

MAX_CAPTURE_AGE_SECONDS = 15.0
MAX_TEXT_CHARS = 4096
MAX_SCROLL_STEPS = 8
SUPPORTED_ACTIONS = frozenset({
    "click", "double_click", "right_click", "keyboard_shortcut", "type_text", "scroll", "drag",
})


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("capture_timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("capture_timestamp_invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("capture_timestamp_invalid")
    return parsed.astimezone(timezone.utc)


def _rect(value: Any, error: str) -> tuple[int, int, int, int]:
    if not isinstance(value, Mapping):
        raise ValueError(error)
    names = ("x", "y", "width", "height")
    raw = tuple(value.get(name) for name in names)
    if any(type(number) is not int for number in raw):
        raise ValueError(error)
    x, y, width, height = raw
    if width <= 0 or height <= 0:
        raise ValueError(error)
    return x, y, width, height


def _inside(point: tuple[int, int], rect: tuple[int, int, int, int]) -> bool:
    x, y = point
    left, top, width, height = rect
    return left <= x < left + width and top <= y < top + height


def _point(params: Mapping[str, Any], prefix: str = "") -> tuple[int, int]:
    x_key, y_key = ("x", "y") if not prefix else (f"{prefix}_x", f"{prefix}_y")
    x, y = params.get(x_key), params.get(y_key)
    if type(x) is not int or type(y) is not int:
        raise ValueError("image_coordinates_invalid")
    return x, y


def _resolve_capture(session: Any, params: Mapping[str, Any], now: datetime) -> tuple[Any, Any, tuple[int, int, int, int], tuple[int, int, int, int]]:
    capture_id = params.get("capture_id")
    if not isinstance(capture_id, str) or capture_id not in getattr(session, "valid_input_captures", set()):
        raise ValueError("image_capture_not_current")
    try:
        stored = session.adapter.store.get(capture_id)
    except ObservationError as exc:
        raise ValueError("image_capture_expired_or_unknown") from exc
    ref = stored.ref
    if ref.capture_id != capture_id or ref.session_id != session.session_id:
        raise ValueError("image_capture_session_mismatch")
    if ref.window_id == "__desktop__":
        raise ValueError("image_window_selection_required")
    age = (now - _as_utc(ref.captured_at)).total_seconds()
    if age < 0 or age > MAX_CAPTURE_AGE_SECONDS:
        raise ValueError("image_capture_stale")

    # Resolve the currently selected window via the same KWin metadata path used
    # by observation capture. Do not trust the caller to name a window.
    current = session.adapter._resolve_window(session.app, None)
    if current.get("id") != ref.window_id:
        raise ValueError("image_window_changed")
    current_rect = _rect(current.get("frame"), "image_window_geometry_invalid")
    mapping = ref.mapping
    selected_rect = _rect(mapping.get("selected_rect"), "image_mapping_invalid")
    source_dimensions = mapping.get("source_dimensions")
    if (not isinstance(source_dimensions, Mapping)
            or source_dimensions.get("width") != ref.source_width
            or source_dimensions.get("height") != ref.source_height
            or selected_rect[2:] != (ref.width, ref.height)):
        raise ValueError("image_mapping_invalid")

    captured_window = mapping.get("window_rect")
    if captured_window is None:
        # App-scoped captures have selected_rect equal to the window frame.
        # Full/crop scopes need the frame recorded separately to prove it did
        # not move between capture and input.
        if ref.scope != "app":
            raise ValueError("image_window_mapping_unavailable")
        captured_rect = selected_rect
    else:
        captured_rect = _rect(captured_window, "image_mapping_invalid")
    if captured_rect != current_rect:
        raise ValueError("image_window_geometry_changed")
    return stored, ref, selected_rect, current_rect


def _screen_point(image_point: tuple[int, int], selected_rect: tuple[int, int, int, int],
                  window_rect: tuple[int, int, int, int]) -> tuple[int, int]:
    selected_x, selected_y, selected_w, selected_h = selected_rect
    if not _inside(image_point, (0, 0, selected_w, selected_h)):
        raise ValueError("image_coordinates_out_of_bounds")
    screen = (selected_x + image_point[0], selected_y + image_point[1])
    if not _inside(screen, window_rect):
        raise ValueError("image_coordinates_outside_window")
    return screen


def _validate_params(params: Mapping[str, Any]) -> tuple[str, str]:
    allowed = {"app", "capture_id", "action", "approved", "x", "y", "end_x", "end_y",
               "key", "text", "direction", "steps"}
    if set(params) - allowed:
        raise ValueError("invalid_params")
    action = params.get("action")
    if not isinstance(action, str) or action not in SUPPORTED_ACTIONS:
        raise ValueError("image_action_unsupported")
    pointer_action = action in {"click", "double_click", "right_click", "type_text", "scroll", "drag"}
    if pointer_action != ("x" in params or "y" in params):
        raise ValueError("invalid_params")
    if action == "drag":
        if ("end_x" in params) != ("end_y" in params):
            raise ValueError("invalid_params")
    elif "end_x" in params or "end_y" in params:
        raise ValueError("invalid_params")
    if (action == "keyboard_shortcut") != ("key" in params):
        raise ValueError("invalid_params")
    if (action == "type_text") != ("text" in params):
        raise ValueError("invalid_params")
    if (action == "scroll") != ("direction" in params or "steps" in params):
        raise ValueError("invalid_params")
    return action, ""


def perform_image_input(session: Any, params: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Perform one explicitly authorized input and return fresh uncertain state.

    ``session`` is the active desktop owner session. Approval is required for
    supervised and guarded modes because image targets have no semantic risk
    classification. `yolo` still observes allowlists and session budgets at the
    service layer. Delivery is never retried.
    """
    if not isinstance(params, dict):
        raise ValueError("invalid_params")
    action, _ = _validate_params(params)
    mode_value = getattr(session.mode, "value", session.mode)
    if mode_value not in {"supervised", "guarded", "yolo"}:
        raise ValueError("autonomy_mode_invalid")
    if "approved" in params and type(params["approved"]) is not bool:
        raise ValueError("invalid_params")
    if mode_value in {"supervised", "guarded"} and params.get("approved") is not True:
        raise ValueError("approval_required")
    if getattr(session, "visual_input_uncertain", False):
        raise ValueError("visual_input_recovery_required")
    if session.actions >= session.max_actions:
        raise ValueError("task_action_budget_exceeded")
    if session.observations + 1 > session.max_observations:
        raise ValueError("task_observation_budget_exceeded")
    if session.cancel.is_set():
        raise ValueError("cancelled")

    stored, ref, selected_rect, window_rect = _resolve_capture(session, params, now or _now_utc())
    action = params["action"]
    worker = session.worker

    mapped: dict[str, Any] = {}
    if action in {"click", "double_click", "right_click", "type_text", "scroll", "drag"}:
        mapped["point"] = _screen_point(_point(params), selected_rect, window_rect)
    if action == "drag":
        mapped["end_point"] = _screen_point(_point(params, "end"), selected_rect, window_rect)
    if action == "keyboard_shortcut":
        key = params.get("key")
        if not isinstance(key, str) or key not in SHORTCUT_KEYS:
            raise ValueError("unsupported_key")
        mapped["key"] = key
    if action == "type_text":
        text = params.get("text")
        if not isinstance(text, str) or not text or len(text) > MAX_TEXT_CHARS or "\x00" in text:
            raise ValueError("text_argument_invalid")
        mapped["text"] = text
    if action == "scroll":
        direction, steps = params.get("direction"), params.get("steps", 1)
        if not isinstance(direction, str) or direction not in {"up", "down", "left", "right"} or type(steps) is not int or not 1 <= steps <= MAX_SCROLL_STEPS:
            raise ValueError("scroll_argument_invalid")
        mapped["delta"] = -steps if direction in {"up", "left"} else steps
        mapped["horizontal"] = direction in {"left", "right"}
        mapped["steps"] = steps

    # Validate capabilities before consuming the capture or latching uncertainty.
    required_methods = {
        "click": ("mouse_click",), "double_click": ("mouse_click",),
        "right_click": ("mouse_click",), "keyboard_shortcut": ("keyboard_key",),
        "type_text": ("mouse_click", "keyboard_type", "active_window"), "scroll": ("mouse_scroll",),
        "drag": ("mouse_drag",),
    }[action]
    if any(not callable(getattr(worker, method, None)) for method in required_methods):
        raise ValueError("image_input_driver_unsupported")
    if getattr(session, "access_scope", "selected_application") == "full_virtual_desktop":
        check_active = getattr(worker, "rpc", None)
        if not callable(check_active):
            raise ValueError("virtual_window_state_unavailable")
        check_active("check_selected_window_active")
    if session.cancel.is_set():
        raise ValueError("cancelled")
    audit = getattr(getattr(session, "tx", None), "_audit", None)
    if not callable(audit):
        raise ValueError("image_input_audit_unavailable")

    # Captures are one-use and all older captures become unusable before any
    # input delivery attempt. If the input or follow-up capture fails, the
    # uncertainty latch stays set until explicit reconciliation.
    pending_captures = getattr(session, "pending_input_captures", None)
    if pending_captures is None:
        # A session without the service-owned pending set cannot safely expose
        # a post-action capture for a later image-grounded input.
        raise ValueError("image_capture_tracking_unavailable")
    audit(session.app, session.mode, f"image_{action}", "pending", "input_pending")
    session.valid_input_captures.clear()
    pending_captures.clear()
    session.visual_input_uncertain = True
    session.actions += 1
    try:
        x, y = mapped.get("point", (None, None))
        if action in {"click", "double_click", "right_click"}:
            worker.mouse_click(x, y,
                               button="right" if action == "right_click" else "left",
                               double=action == "double_click")
        elif action == "keyboard_shortcut":
            worker.keyboard_key(mapped["key"])
        elif action == "type_text":
            # Coordinate grounding means the requested text goes to the field
            # visibly under this point; typing alone would target stale focus.
            worker.mouse_click(x, y, button="left", double=False)
            active_text = worker.active_window()
            active_ids = _ID.findall(active_text) if isinstance(active_text, str) else []
            active_rows = [row for row in _APP.finditer(active_text)
                           if row.group("active")] if isinstance(active_text, str) else []
            if (len(active_ids) != 1 or active_ids[0] != ref.window_id
                    or len(active_rows) != 1):
                raise ValueError("image_target_not_active")
            worker.keyboard_type(mapped["text"])
        elif action == "scroll":
            worker.mouse_scroll(x, y, mapped["delta"], horizontal=mapped["horizontal"], discrete=True, steps=mapped["steps"])
        else:  # drag
            end_x, end_y = mapped["end_point"]
            worker.mouse_drag(x, y, end_x, end_y, button="left")

        window_changed = False
        if getattr(session, "desktop_mode", "virtual") == "live":
            focus_live = getattr(worker, "focus_live_app", None)
            if not callable(focus_live):
                raise ValueError("live_focus_unavailable")
            focus = focus_live()
            if not isinstance(focus, dict) or focus.get("focused") is not True:
                raise ValueError("live_target_focus_failed")
            selected_id = focus.get("selected_window_id")
            if isinstance(selected_id, str):
                worker.selected_window_id = selected_id
            selected_window = focus.get("selected_window")
            if isinstance(selected_window, dict):
                worker.selected_window = selected_window
            window_changed = selected_id != ref.window_id

        fresh_ref = session.adapter.capture(session.app, scope="app")
        if session.cancel.is_set():
            raise ValueError("cancelled")
        window_changed = window_changed or fresh_ref.window_id != ref.window_id
        fresh_obs = session.adapter.observe(fresh_ref.capture_id, mode="metadata")
        audit(session.app, session.mode, f"image_{action}", "ok", "input_delivered_effect_uncertain")
        session.observations += 1
        # The frame exists after delivery, but is not actionable until observe
        # explicitly returns its image to the caller.
        pending_captures.add(fresh_ref.capture_id)
        session.visual_input_uncertain = False
        return {"status": "observed", "action": action, "input_delivered": True,
                "verified": False, "effect": "uncertain", "window_changed": window_changed,
                "fresh_state": fresh_obs.metadata}
    except Exception as exc:
        try:
            audit(session.app, session.mode, f"image_{action}", "failed", "input_or_postcapture_failed")
        except Exception:
            pass
        raise ValueError("visual_input_effect_uncertain") from None
