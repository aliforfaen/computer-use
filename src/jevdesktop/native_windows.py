"""Small KWin scripting helpers for exact native-window identity.

The pinned kwin-mcp window report includes IDs, PIDs and geometry but omits
transient-parent links. This helper uses the same installed KWin scripting
runner and adds KWin's documented ``Window.transientFor`` relationship.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


_WINDOWS_SCRIPT = r"""
var out = [];
var windows = workspace.windowList();
var desktop = workspace.currentDesktop;
var currentDesktopId = desktop ? String(desktop.id) : null;
for (var i = 0; i < windows.length; i++) {
    var w = windows[i];
    var parent = w.transientFor;
    var frame = w.frameGeometry;
    var client = w.clientGeometry;
    var desktops = w.desktops;
    var desktopIds = [];
    if (desktops) {
        for (var j = 0; j < desktops.length; j++) {
            if (desktops[j] && desktops[j].id !== undefined) {
                desktopIds.push(String(desktops[j].id));
            }
        }
    }
    var outputName = w.output && w.output.name !== undefined ? String(w.output.name) : null;
    out.push({
        id: String(w.internalId),
        app: String(w.resourceClass),
        caption: String(w.caption),
        pid: Number(w.pid),
        active: w === workspace.activeWindow,
        normal: Boolean(w.normalWindow),
        popup: Boolean(w.popupWindow),
        dialog: Boolean(w.dialog),
        transient: Boolean(w.transient),
        transient_for: parent ? String(parent.internalId) : null,
        current_desktop_id: currentDesktopId,
        desktop_ids: desktopIds,
        on_current_desktop: currentDesktopId === null ? null :
            (Boolean(w.onAllDesktops) || desktopIds.indexOf(currentDesktopId) !== -1),
        minimized: typeof w.minimized === "boolean" ? w.minimized : null,
        notification: typeof w.notification === "boolean" ? w.notification : null,
        output_name: outputName,
        stacking_order: typeof w.stackingOrder === "number" ? w.stackingOrder : null,
        frame: {x: frame.x, y: frame.y, width: frame.width, height: frame.height},
        client: {x: client.x, y: client.y, width: client.width, height: client.height}
    });
}
var activity = typeof workspace.currentActivity === "string" ? workspace.currentActivity : null;
callDBus("{sink}", "{path}", "{sink}", "Report", JSON.stringify({
    windows: out,
    current_desktop_id: currentDesktopId,
    currentActivity: activity
}));
"""

_ACTIVATE_ID_SCRIPT = r"""
var target = {target};
var activated = false;
var windows = workspace.windowList();
for (var i = 0; i < windows.length; i++) {
    var w = windows[i];
    if (String(w.internalId) === target) {
        workspace.activeWindow = w;
        activated = String(workspace.activeWindow.internalId) === target;
        break;
    }
}
callDBus("{sink}", "{path}", "{sink}", "Report", JSON.stringify({activated: activated}));
"""


def _valid_rect(value: Any) -> bool:
    return (isinstance(value, dict) and all(
        isinstance(value.get(key), (int, float)) and not isinstance(value.get(key), bool)
        for key in ("x", "y", "width", "height")) and
        value["width"] >= 0 and value["height"] >= 0)


def _collect_snapshot(timeout: float = 5.0) -> tuple[list[dict[str, Any]], str | None, str | None]:
    """Return normalized rows and optional KWin workspace context from one query."""
    from kwin_mcp.geometry import run_kwin_script

    try:
        raw = json.loads(run_kwin_script(_WINDOWS_SCRIPT, timeout=timeout))
    except (json.JSONDecodeError, RuntimeError, TypeError) as exc:
        raise RuntimeError("native_window_query_failed") from exc
    if isinstance(raw, dict):
        raw_windows = raw.get("windows")
        current_desktop_id = raw.get("current_desktop_id")
        current_activity = raw.get("currentActivity")
        if current_desktop_id is not None and not isinstance(current_desktop_id, str):
            current_desktop_id = None
        if current_activity is not None and not isinstance(current_activity, str):
            current_activity = None
    elif isinstance(raw, list):
        raw_windows, current_desktop_id, current_activity = raw, None, None
    else:
        raise RuntimeError("native_window_query_invalid")
    if not isinstance(raw_windows, list):
        raise RuntimeError("native_window_query_invalid")
    rows: list[dict[str, Any]] = []
    for item in raw_windows:
        if not isinstance(item, dict):
            continue
        window_id, app, pid = item.get("id"), item.get("app"), item.get("pid")
        if (not isinstance(window_id, str) or not window_id or not isinstance(app, str)
                or (pid is not None and type(pid) is not int) or not _valid_rect(item.get("frame"))
                or not _valid_rect(item.get("client"))):
            continue
        parent_id = item.get("transient_for")
        if parent_id is not None and not isinstance(parent_id, str):
            continue
        item_current_desktop = item.get("current_desktop_id", current_desktop_id)
        if item_current_desktop is not None and not isinstance(item_current_desktop, str):
            item_current_desktop = None
        desktop_ids_raw = item.get("desktop_ids")
        desktop_ids = ([value for value in desktop_ids_raw if isinstance(value, str)]
                       if isinstance(desktop_ids_raw, list) else [])
        on_current_desktop = item.get("on_current_desktop")
        minimized = item.get("minimized")
        notification = item.get("notification")
        stacking_order = item.get("stacking_order")
        output_name = item.get("output_name")
        rows.append({
            "id": window_id,
            "app": app,
            "caption": item.get("caption") if isinstance(item.get("caption"), str) else "",
            "pid": pid,
            "active": item.get("active") is True,
            "normal": item.get("normal") is True,
            "popup": item.get("popup") is True,
            "dialog": item.get("dialog") is True,
            "transient": item.get("transient") is True,
            "transient_for": parent_id,
            "current_desktop_id": item_current_desktop,
            "desktop_ids": desktop_ids,
            "on_current_desktop": on_current_desktop if type(on_current_desktop) is bool else None,
            "minimized": minimized if type(minimized) is bool else None,
            "notification": notification if type(notification) is bool else None,
            "output_name": output_name if isinstance(output_name, str) else None,
            "stacking_order": stacking_order if type(stacking_order) is int else None,
            "frame": {key: round(float(item["frame"][key])) for key in ("x", "y", "width", "height")},
            "client": {key: round(float(item["client"][key])) for key in ("x", "y", "width", "height")},
        })
    return rows, current_desktop_id, current_activity


def collect_windows(timeout: float = 5.0) -> list[dict[str, Any]]:
    """Return current KWin windows with exact identity and optional desktop context."""
    return _collect_snapshot(timeout)[0]


def activate_window_id(window_id: str, timeout: float = 5.0) -> bool:
    """Activate one exact KWin internal ID, never a caption/app substring."""
    if not isinstance(window_id, str) or not window_id:
        return False
    from kwin_mcp.geometry import run_kwin_script

    script = _ACTIVATE_ID_SCRIPT.replace("{target}", json.dumps(window_id))
    try:
        result = json.loads(run_kwin_script(script, timeout=timeout))
    except (json.JSONDecodeError, RuntimeError, TypeError):
        return False
    return isinstance(result, dict) and result.get("activated") is True


def workspace_snapshot(timeout: float = 5.0) -> dict[str, Any]:
    """Return current window, desktop, activity and output context."""
    windows, current_desktop_id, current_activity = _collect_snapshot(timeout)
    active = [row for row in windows if row["active"]]
    if len(active) > 1:
        raise RuntimeError("native_active_window_ambiguous")
    outputs = None
    try:
        from kwin_mcp.geometry import outputs as output_geometry

        outputs = output_geometry(timeout)
    except Exception:
        pass
    result = {
        "windows": windows,
        "active_window_id": active[0]["id"] if active else None,
        "outputs": outputs,
        "current_desktop_id": current_desktop_id,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    if current_activity is not None:
        result["currentActivity"] = current_activity
    return result
