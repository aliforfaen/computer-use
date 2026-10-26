"""KDE StatusNotifierItem tray for the local desktop owner.

Runs as its own process and talks to the owner over the same private Unix
socket as the CLI and MCP clients. It never reads or writes session state, and
live starts use either per-task owner confirmation or the owner's explicitly
saved local-tray policy to supply the required owner-present override. Temporary
accessibility is requested only when live semantic candidates are enabled. Other
clients must supply their own required live authorization fields.

Settings live in ``~/.config/jev-desktop/tray.json`` (0600). The desktop-mode
entry is a *preference* for tray-started tasks, never an authorization.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable

from jevdesktop import paths
from jevdesktop.desktop_cli import ipc_call
from jevdesktop.desktop_daemon import default_config_dir

MODE_VIRTUAL = "virtual"
MODE_LIVE = "live"
VALID_MODES = (MODE_VIRTUAL, MODE_LIVE)
VALID_AUTONOMY = ("supervised", "guarded", "yolo")
ICON_CANDIDATES = ("input-mouse", "input-tablet", "preferences-desktop", "computer")
ICON_THEME_NAME = paths.ICON_THEME_NAME
ICON_PRIMARY_SIZE = 48
ICON_HIDPI_SIZE = 96
SERVICE_VERBS = ("start", "stop", "enable", "disable")

DEFAULT_SETTINGS: dict[str, Any] = {
    "desktop_mode": MODE_VIRTUAL,
    "autonomy_mode": "guarded",
    "poll_seconds": 3.0,
    "confirm_kill_switch": False,
    "confirm_cleanup": False,
    "confirm_live_task": True,
    "semantic_candidates_live": False,
}


def default_settings_path() -> Path:
    return default_config_dir() / "tray.json"


def load_settings(path: Path) -> dict[str, Any]:
    """Load tray settings; any missing or invalid value falls back to the default."""
    settings = dict(DEFAULT_SETTINGS)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return settings
    if not isinstance(data, dict):
        return settings
    if data.get("desktop_mode") in VALID_MODES:
        settings["desktop_mode"] = data["desktop_mode"]
    if data.get("autonomy_mode") in VALID_AUTONOMY:
        settings["autonomy_mode"] = data["autonomy_mode"]
    poll = data.get("poll_seconds")
    if isinstance(poll, (int, float)) and not isinstance(poll, bool) and 1 <= poll <= 30:
        settings["poll_seconds"] = float(poll)
    for key in ("confirm_kill_switch", "confirm_cleanup"):
        if isinstance(data.get(key), bool):
            settings[key] = data[key]
    if isinstance(data.get("confirm_live_task"), bool):
        settings["confirm_live_task"] = data["confirm_live_task"]
    if isinstance(data.get("semantic_candidates_live"), bool):
        settings["semantic_candidates_live"] = data["semantic_candidates_live"]
    return settings


def save_settings(path: Path, settings: dict[str, Any]) -> None:
    validated = dict(DEFAULT_SETTINGS)
    validated.update({key: value for key, value in settings.items() if key in DEFAULT_SETTINGS})
    if validated["desktop_mode"] not in VALID_MODES or validated["autonomy_mode"] not in VALID_AUTONOMY:
        raise ValueError("invalid_tray_settings")
    if any(type(validated[key]) is not bool for key in
           ("confirm_kill_switch", "confirm_cleanup", "confirm_live_task", "semantic_candidates_live")):
        raise ValueError("invalid_tray_settings")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (json.dumps(validated, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(temporary, path)


def _icon_name(gtk: Any) -> str:
    theme = gtk.IconTheme.get_default()
    for candidate in ICON_CANDIDATES:
        if theme is not None and theme.has_icon(candidate):
            return candidate
    return ICON_CANDIDATES[0]


def _icon_cache_files(cache_dir: Path) -> tuple[Path, Path]:
    return cache_dir / f"{ICON_THEME_NAME}.png", cache_dir / f"{ICON_THEME_NAME}@2x.png"


def _icon_cache_is_fresh(svg: Path, cache_dir: Path) -> bool:
    try:
        source = svg.stat().st_mtime
    except OSError:
        return False
    for path in _icon_cache_files(cache_dir):
        try:
            if path.stat().st_mtime < source:
                return False
        except OSError:
            return False
    return True


def _render_icon_cache(svg: Path, cache_dir: Path) -> None:
    """Render the SVG into the PNG pair the StatusNotifierItem host displays."""
    import gi

    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf

    cache_dir.mkdir(parents=True, exist_ok=True)
    primary, hidpi = _icon_cache_files(cache_dir)
    for target, size in ((primary, ICON_PRIMARY_SIZE), (hidpi, ICON_HIDPI_SIZE)):
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(svg), size, size, True)
        pixbuf.savev(str(target), "png", [], [])


def prepare_tray_icon(gtk: Any) -> tuple[str, Path | None]:
    """Return ``(icon_name, theme_path)`` for the tray indicator.

    ``theme_path`` is ``None`` when the SVG is missing or cannot be rendered,
    so the caller falls back to a stock theme icon and the tray still starts.
    """
    svg = paths.icon_path()
    if svg is None:
        return _icon_name(gtk), None
    cache_dir = paths.icon_cache_dir()
    try:
        if not _icon_cache_is_fresh(svg, cache_dir):
            _render_icon_cache(svg, cache_dir)
    except Exception as exc:  # pragma: no cover - host without a working SVG loader
        print(f"tray_icon_unavailable: {type(exc).__name__}; using a stock theme icon",
              file=sys.stderr)
        return _icon_name(gtk), None
    return ICON_THEME_NAME, cache_dir


class DesktopTray:
    """GTK main-loop object; every owner or systemctl call runs off-thread."""

    icon_name: str = ICON_CANDIDATES[0]
    icon_dir: Path | None = None

    def __init__(self, *, socket: Path | None, unit: str, gtk: Any, glib: Any, appindicator: Any,
                 settings_path: Path | None = None, icon_name: str | None = None,
                 icon_dir: Path | None = None) -> None:
        self.gtk = gtk
        self.glib = glib
        self.socket = socket
        self.unit = unit
        self.icon_name = icon_name or ICON_CANDIDATES[0]
        self.icon_dir = icon_dir
        self.settings_path = settings_path or default_settings_path()
        self.settings = load_settings(self.settings_path)
        self.status: dict[str, Any] | None = None
        self.capabilities: dict[str, Any] | None = None
        self.service_state: tuple[str, str] = ("unknown", "unknown")
        self.owner_state = "unknown"
        self.error: str | None = None
        self.notice: str | None = None
        self._syncing = False
        self._ipc_lock = threading.Lock()
        self._stop = threading.Event()
        self._build_menu(appindicator)

    # ---------------------------------------------------------------- IPC glue
    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
        with self._ipc_lock:
            return ipc_call(method, params or {}, transport="local-tray",
                            socket_path=self.socket, timeout=timeout)

    def _async(self, work: Callable[[], Any], done: Callable[[bool, Any], None] | None = None) -> None:
        def runner() -> None:
            try:
                value: Any = work()
            except Exception as exc:  # reported in the UI, never raised into GTK
                self.glib.idle_add(self._deliver, done, False, str(exc))
                return
            self.glib.idle_add(self._deliver, done, True, value)

        threading.Thread(target=runner, daemon=True).start()

    def _deliver(self, done: Callable[[bool, Any], None] | None, ok: bool, value: Any) -> bool:
        if done is not None:
            done(ok, value)
        return False

    def _systemctl(self, *arguments: str) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run(["systemctl", "--user", *arguments],
                                  capture_output=True, text=True, timeout=20)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return None

    def _unit_state(self) -> tuple[str, str]:
        active = self._systemctl("is-active", self.unit)
        enabled = self._systemctl("is-enabled", self.unit)
        return ((active.stdout.strip() if active else "unavailable") or "inactive",
                (enabled.stdout.strip() if enabled else "unavailable") or "disabled")

    # ------------------------------------------------------------------- menu
    def _build_menu(self, appindicator: Any) -> None:
        gtk = self.gtk
        menu = gtk.Menu()

        self.item_owner = gtk.MenuItem(label="Owner: checking…")
        self.item_owner.set_sensitive(False)
        self.item_session = gtk.MenuItem(label="Session: checking…")
        self.item_session.set_sensitive(False)
        self.item_service = gtk.MenuItem(label="Service: checking…")
        self.item_service.set_sensitive(False)
        self.item_error = gtk.MenuItem(label="")
        self.item_error.set_sensitive(False)
        for item in (self.item_owner, self.item_session, self.item_service, self.item_error):
            menu.append(item)
        menu.append(gtk.SeparatorMenuItem())

        mode_root = gtk.MenuItem(label="Desktop mode")
        mode_menu = gtk.Menu()
        self.mode_virtual = gtk.RadioMenuItem(label="Virtual desktop (default)")
        self.mode_live = gtk.RadioMenuItem(label="Physical desktop (live)")
        self.mode_live.join_group(self.mode_virtual)
        self.mode_virtual.connect("activate", self._on_mode, MODE_VIRTUAL)
        self.mode_live.connect("activate", self._on_mode, MODE_LIVE)
        mode_menu.append(self.mode_virtual)
        mode_menu.append(self.mode_live)
        mode_root.set_submenu(mode_menu)
        menu.append(mode_root)

        self.start_root = gtk.MenuItem(label="Start task")
        self.start_menu = gtk.Menu()
        self.start_root.set_submenu(self.start_menu)
        menu.append(self.start_root)

        self.item_attach = gtk.MenuItem(label="Attach live window…")
        self.item_attach.connect("activate", self._on_attach)
        menu.append(self.item_attach)

        self.item_cleanup = gtk.MenuItem(label="Clean up session and owned apps")
        self.item_cleanup.connect("activate", self._on_cleanup)
        menu.append(self.item_cleanup)
        menu.append(gtk.SeparatorMenuItem())

        service_root = gtk.MenuItem(label="Service")
        service_menu = gtk.Menu()
        for label, verb in (("Start service", "start"), ("Stop service", "stop"),
                            ("Enable at login", "enable"), ("Disable at login", "disable")):
            item = gtk.MenuItem(label=label)
            item.connect("activate", self._on_service, verb)
            service_menu.append(item)
        self.item_service_detail = gtk.MenuItem(label="Refresh status")
        self.item_service_detail.connect("activate", lambda *_: self._poll_once())
        service_menu.append(gtk.SeparatorMenuItem())
        service_menu.append(self.item_service_detail)
        service_root.set_submenu(service_menu)
        menu.append(service_root)
        menu.append(gtk.SeparatorMenuItem())

        self.item_forwards = gtk.MenuItem(label="SSH forwards…")
        self.item_forwards.connect("activate", self._on_forwards)
        menu.append(self.item_forwards)

        self.item_settings = gtk.MenuItem(label="Settings…")
        self.item_settings.connect("activate", self._on_settings)
        menu.append(self.item_settings)
        self.item_open_config = gtk.MenuItem(label="Open config folder")
        self.item_open_config.connect("activate", self._on_open_config)
        menu.append(self.item_open_config)
        menu.append(gtk.SeparatorMenuItem())

        self.item_kill = gtk.MenuItem(label="Stop all and shut down owner")
        self.item_kill.connect("activate", self._on_kill_switch)
        menu.append(self.item_kill)
        self.item_quit = gtk.MenuItem(label="Quit tray (leave owner running)")
        self.item_quit.connect("activate", self._on_quit)
        menu.append(self.item_quit)

        menu.show_all()
        self.menu = menu
        self.indicator = appindicator.Indicator.new(
            "jev-desktop", self.icon_name, appindicator.IndicatorCategory.APPLICATION_STATUS)
        if self.icon_dir is not None:
            try:
                self.indicator.set_icon_theme_path(str(self.icon_dir))
                self.indicator.set_icon_full(self.icon_name, "Jev desktop")
            except AttributeError:  # older libayatana without explicit theme-path support
                pass
        self.indicator.set_status(appindicator.IndicatorStatus.ACTIVE)
        self.indicator.set_title("Jev desktop")
        self.indicator.set_menu(menu)

    def _set_modes(self) -> None:
        self._syncing = True
        try:
            self.mode_virtual.set_active(self.settings["desktop_mode"] == MODE_VIRTUAL)
            self.mode_live.set_active(self.settings["desktop_mode"] == MODE_LIVE)
        finally:
            self._syncing = False

    def _rebuild_start_menu(self) -> None:
        for child in self.start_menu.get_children():
            self.start_menu.remove(child)
        apps = (self.capabilities or {}).get("allowed_apps") or []
        scopes = set((self.capabilities or {}).get("allowed_access_scopes") or [])
        if not apps and not (scopes - {"selected_application"}):
            placeholder = self.gtk.MenuItem(label="Owner not running")
            placeholder.set_sensitive(False)
            self.start_menu.append(placeholder)
        else:
            for app in sorted(apps):
                item = self.gtk.MenuItem(label=app)
                item.connect("activate", self._on_start, app)
                self.start_menu.append(item)
        for scope, label in (("full_virtual_desktop", "Full isolated virtual desktop"),
                             ("full_live_desktop", "Full live desktop…")):
            if scope in scopes:
                item = self.gtk.MenuItem(label=label)
                item.connect("activate", self._on_start_full_scope, scope)
                self.start_menu.append(item)
        self.start_menu.show_all()

    # ---------------------------------------------------------------- refresh
    def _session_summary(self, session: dict[str, Any]) -> str:
        summary = (f"{session.get('access_scope', session.get('app'))} · {session.get('desktop_mode')} · {session.get('state')}"
                   f" · {int(session.get('age_seconds') or 0)}s")
        actions, cap = session.get("actions"), session.get("action_cap")
        if isinstance(actions, int) and isinstance(cap, int):
            summary += f" · act {actions}/{cap}"
        return summary

    def _refresh_labels(self) -> None:
        if self.owner_state == "running":
            self.item_owner.set_label(f"Owner: running{self._owner_suffix()}")
        else:
            self.item_owner.set_label("Owner: stopped (or unreachable)")
        session = (self.status or {}).get("session")
        if isinstance(session, dict):
            self.item_session.set_label(f"Session: {self._session_summary(session)}")
            self.item_cleanup.set_sensitive(True)
            self.start_root.set_sensitive(False)
            self.item_attach.set_sensitive(False)
        else:
            notice_suffix = f" · {self.notice}" if self.notice else ""
            self.item_session.set_label(f"Session: none{notice_suffix}")
            self.item_cleanup.set_sensitive(self.owner_state == "running")
            self.start_root.set_sensitive(self.owner_state == "running" and bool(
                (self.capabilities or {}).get("allowed_apps") or
                set((self.capabilities or {}).get("allowed_access_scopes") or []) - {"selected_application"}))
            self.item_attach.set_sensitive(self.owner_state == "running" and bool(
                (self.capabilities or {}).get("allowed_apps")))
        active, enabled = self.service_state
        if active == "unavailable":
            self.item_service.set_label("Service: systemctl unavailable")
        elif active in {"inactive", "unknown", "failed"} and enabled in {"disabled", "not-found"}:
            self.item_service.set_label("Service: not installed/active")
        else:
            self.item_service.set_label(f"Service: {active} · {enabled}")
        if self.error:
            self.item_error.set_label(f"Last error: {self.error[:60]}")
            self.item_error.set_sensitive(False)
        else:
            self.item_error.set_label("")
        mode = self.settings["desktop_mode"]
        self.indicator.set_title(f"Jev desktop ({mode})")
        self._set_modes()
        tooltip = f"Jev desktop — {mode} preference, autonomy {self.settings['autonomy_mode']}"
        if isinstance(session, dict):
            tooltip += f"; session {session.get('app')} ({session.get('desktop_mode')})"
        self.item_owner.set_tooltip_text(tooltip)

    def _owner_suffix(self) -> str:
        session = (self.status or {}).get("session")
        if isinstance(session, dict) and session.get("owner_pid"):
            return f" (pid {session['owner_pid']})"
        return ""

    def _poll_once(self) -> None:
        def work() -> tuple[dict[str, Any], dict[str, Any] | None, tuple[str, str]]:
            status = self.call("status", timeout=10.0)
            capabilities = self.capabilities
            if capabilities is None:
                try:
                    capabilities = self.call("capabilities", timeout=10.0)
                except Exception:
                    capabilities = None
            return status, capabilities, self._unit_state()

        def done(ok: bool, value: Any) -> None:
            if ok:
                status, capabilities, service_state = value
                self.status = status
                if capabilities is not None:
                    self.capabilities = capabilities
                self.service_state = service_state
                self.owner_state = "running"
                self.error = None
                self._rebuild_start_menu()
            else:
                self.owner_state = "stopped"
                self.status = None
                self.service_state = self._unit_state()
                self.error = str(value)
            self._refresh_labels()

        self._async(work, done)

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            self._poll_once()
            self._stop.wait(self.settings["poll_seconds"])

    # --------------------------------------------------------------- handlers
    def _on_mode(self, item: Any, mode: str) -> None:
        if self._syncing:
            return
        self.settings["desktop_mode"] = mode
        self._persist_settings()
        if mode == MODE_LIVE:
            if self.settings["confirm_live_task"]:
                policy = ("Each live start asks you to confirm owner presence. "
                          "Temporary accessibility is requested only when live semantic candidates are enabled.")
            else:
                policy = ("Physical presence is not detected; the saved tray policy supplies the required "
                          "owner-present override. Temporary accessibility is requested only when live "
                          "semantic candidates are enabled.")
            self._inform("Physical desktop selected", policy)
        self._refresh_labels()

    def _on_start(self, item: Any, app: str) -> None:
        mode = self.settings["desktop_mode"]
        self.notice = None
        params: dict[str, Any] = {"app": app, "mode": self.settings["autonomy_mode"], "desktop_mode": mode}
        if mode == MODE_VIRTUAL:
            params["semantic_candidates"] = True
        else:
            semantics = bool(self.settings["semantic_candidates_live"])
            if self.settings["confirm_live_task"] and not self._confirm_live(app, semantics):
                return
            params.update(owner_present_override=True, semantic_candidates=semantics)
            if semantics:
                params["temporary_a11y"] = True

        self._submit_start(params)

    def _on_start_full_scope(self, item: Any, scope: str) -> None:
        if scope not in set((self.capabilities or {}).get("allowed_access_scopes") or []):
            return
        params: dict[str, Any] = {"access_scope": scope, "mode": self.settings["autonomy_mode"]}
        if scope == "full_live_desktop":
            semantics = bool(self.settings["semantic_candidates_live"])
            if self.settings["confirm_live_task"] and not self._confirm_live("the full desktop", semantics):
                return
            params["owner_present_override"] = True
            params["semantic_candidates"] = semantics
            if semantics:
                params["temporary_a11y"] = True
        self.notice = None
        self._submit_start(params)

    def _submit_start(self, params: dict[str, Any]) -> None:
        def done(ok: bool, value: Any) -> None:
            if ok:
                self.status = value
                self.error = None
            else:
                self.error = str(value)
                self._inform("Task could not start", str(value))
            self._poll_once()

        self._async(lambda: self.call("session_start", params), done)

    def _on_attach(self, item: Any) -> None:
        """Discover windows read-only, then attach only to a selected allowed window."""
        def work() -> tuple[dict[str, Any], dict[str, Any]]:
            profiles = self.call("profiles", timeout=10.0)
            workspace = self.call("workspace", timeout=20.0)
            return profiles, workspace

        def done(ok: bool, value: Any) -> None:
            if not ok:
                self.error = str(value)
                self._inform("Could not discover live windows", str(value))
                self._refresh_labels()
                return
            profiles, workspace = value
            selected = self._choose_attach_window(profiles, workspace)
            if selected is None:
                return
            app, window_id = selected
            semantics = bool(self.settings["semantic_candidates_live"])
            if self.settings["confirm_live_task"] and not self._confirm_live(app, semantics):
                return
            params: dict[str, Any] = {
                "app": app, "mode": self.settings["autonomy_mode"], "desktop_mode": MODE_LIVE,
                "attach": True, "window_id": window_id, "owner_present_override": True,
                "semantic_candidates": semantics,
            }
            if semantics:
                params["temporary_a11y"] = True
            self.notice = None
            self._submit_start(params)

        self._async(work, done)

    def _choose_attach_window(self, profiles: dict[str, Any],
                              workspace: dict[str, Any]) -> tuple[str, str] | None:
        """Show only windows the owner allowlist maps to an exact configured profile."""
        profile_rows = profiles.get("profiles", []) if isinstance(profiles, dict) else []
        allowed_profiles = {row.get("id") for row in profile_rows
                            if isinstance(row, dict) and row.get("allowed") is True
                            and isinstance(row.get("id"), str)}
        window_rows = workspace.get("windows", []) if isinstance(workspace, dict) else []
        choices = [row for row in window_rows if isinstance(row, dict)
                   and row.get("allowed") is True and row.get("profile_id") in allowed_profiles
                   and isinstance(row.get("id"), str) and row.get("id")]
        if not choices:
            self._inform("No allowed live windows", "No current window matches an allowed app profile.")
            return None
        gtk = self.gtk
        dialog = gtk.Dialog(title="Attach to live window")
        dialog.add_buttons("Cancel", gtk.ResponseType.CANCEL, "Attach", gtk.ResponseType.OK)
        area = dialog.get_content_area()
        area.set_spacing(8)
        area.add(gtk.Label(label="Window discovery does not change focus. Select the exact existing window to attach."))
        chooser = gtk.ComboBoxText()
        by_id: dict[str, dict[str, Any]] = {}
        for row in choices:
            window_id = row["id"]
            app = row["profile_id"]
            caption = row.get("caption") or "(title hidden)"
            chooser.append(window_id, f"{app} · {caption} · {window_id}")
            by_id[window_id] = row
        chooser.set_active(0)
        area.add(chooser)
        dialog.show_all()
        response = dialog.run()
        window_id = chooser.get_active_id()
        dialog.destroy()
        row = by_id.get(window_id) if response == gtk.ResponseType.OK else None
        if row is None:
            return None
        return row["profile_id"], row["id"]

    def _on_cleanup(self, item: Any) -> None:
        if self.settings["confirm_cleanup"] and not self._confirm(
                "Stop the active session and close its owned app?",
                "Cleanup restores focus and accessibility; the result reports confirmed or unconfirmed."):
            return

        def done(ok: bool, value: Any) -> None:
            if ok:
                cleanup = value.get("cleanup") if isinstance(value, dict) else None
                self.notice = (f"cleanup {cleanup}" if cleanup else
                               "cleanup reported without confirmation")
            else:
                self.error = str(value)
                self.notice = f"cleanup failed: {str(value)[:40]}"
            self._poll_once()
            self._refresh_labels()

        self._async(lambda: self.call("stop_all"), done)

    def _on_service(self, item: Any, verb: str) -> None:
        if verb not in SERVICE_VERBS:
            return

        def work() -> tuple[str, str]:
            self._systemctl(verb, self.unit)
            return self._unit_state()

        def done(ok: bool, value: Any) -> None:
            if ok:
                self.service_state = value
                self.error = None
            else:
                self.error = str(value)
            self._refresh_labels()
            self._poll_once()

        self._async(work, done)

    def _on_kill_switch(self, item: Any) -> None:
        if self.settings["confirm_kill_switch"] and not self._confirm(
                "Stop all sessions and shut down the owner?",
                "This stops the active session, closes its owned app and exits the owner."):
            return

        def work() -> dict[str, Any]:
            stopped = self.call("stop_all")
            try:
                self.call("shutdown", timeout=15.0)
            except Exception:
                pass  # shutdown closes the socket before replying
            return stopped

        def done(ok: bool, value: Any) -> None:
            if ok:
                cleanup = value.get("cleanup") if isinstance(value, dict) else None
                self._inform("Owner stopped", f"Session cleanup: {cleanup or 'unknown'}")
            else:
                self.error = str(value)
            self._poll_once()

        self._async(work, done)

    def _on_open_config(self, item: Any) -> None:
        folder = self.settings_path.parent

        def work() -> str:
            try:
                subprocess.run(["xdg-open", str(folder)], capture_output=True, timeout=20)
                return str(folder)
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                return str(folder)

        def done(ok: bool, value: Any) -> None:
            if not ok:
                self._inform("Config folder", str(folder))

        self._async(work, done)

    def _on_forwards(self, item: Any) -> None:
        from jevdesktop.ssh_forwards import SSHForwardManager

        manager = SSHForwardManager()
        gtk = self.gtk
        dialog = gtk.Dialog(title="Named SSH forwards")
        dialog.add_buttons("Close", gtk.ResponseType.CLOSE)
        dialog.set_default_size(520, 360)
        area = dialog.get_content_area()
        area.set_spacing(8)
        selector = gtk.ComboBoxText()
        area.add(selector)
        detail = gtk.Label(label="No forwards configured")
        detail.set_xalign(0)
        detail.set_selectable(True)
        area.add(detail)
        area.add(gtk.Label(label=("Local TCP listener reachability is checked; MCP endpoint health is not probed. "
                                 "Reverse forward URLs are on the SSH host.")))
        settings_button = gtk.Button(label="Settings…")
        area.add(settings_button)
        status = gtk.Label(label="")
        status.set_xalign(0)
        area.add(status)
        actions = gtk.Grid(column_spacing=6, row_spacing=6)
        area.add(actions)
        entries: dict[str, Any] = {}
        action_buttons: dict[str, Any] = {}
        busy = False
        checking = False
        action_error: str | None = None

        def selected_name() -> str | None:
            return selector.get_active_id()

        def load_entries() -> None:
            nonlocal entries
            previous = selected_name()
            try:
                entries = {forward.name: forward for forward in manager.list()}
            except Exception as exc:
                entries = {}
                status.set_text(str(exc))
            selector.remove_all()
            for name in sorted(entries):
                selector.append(name, name)
            if previous in entries:
                selector.set_active_id(previous)
            elif entries:
                selector.set_active(0)
            else:
                detail.set_text("No forwards configured")
                status.set_text("")
            update_selection()

        def update_selection(*_args: Any) -> None:
            nonlocal checking
            name = selected_name()
            if not name or name not in entries:
                detail.set_text("No forwards configured")
                status.set_text("")
                for key, button in action_buttons.items():
                    if key != "add-form":
                        button.set_sensitive(False)
                return
            forward = entries[name]
            source_port = (forward.local_port if forward.direction == "local"
                           else forward.forwarded_port)
            url_scope = "local" if forward.direction == "local" else f"on {forward.ssh_host}"
            detail.set_text(f"{forward.direction} · {forward.ssh_host} · {forward.endpoint} → "
                            f"http://127.0.0.1:{source_port}{forward.endpoint_path} ({url_scope})")
            for key, button in action_buttons.items():
                if key != "add-form":
                    button.set_sensitive(False)
            status.set_text("Checking systemd status…")
            if checking:
                return
            checking = True

            def got_snapshot(ok: bool, value: Any) -> None:
                nonlocal checking
                checking = False
                if not dialog.get_visible() or selected_name() != name:
                    return
                if not ok:
                    status.set_text(str(value))
                    return
                state = next((item for item in value.get("forwards", [])
                              if item.get("name") == name), None)
                if state is None:
                    status.set_text(str(value.get("config_error") or "Forward status unavailable"))
                    return
                active_state = state.get("process_active")
                active = "running" if active_state is True else "stopped" if active_state is False else "unknown state"
                enabled_state = state.get("enabled_state")
                enabled = ("enabled at login" if enabled_state == "enabled" else
                           "disabled at login" if enabled_state == "disabled" else
                           "login enablement unknown")
                listener = state.get("local_listener_reachable")
                listener_text = (" · local listener reachable" if listener is True else
                                 " · local listener unavailable" if listener is False else "")
                error_suffix = f" · Last action failed: {action_error}" if action_error else ""
                diagnostic = state.get("last_error")
                diagnostic_suffix = f" · {diagnostic}" if diagnostic else ""
                checked = state.get("checked_at", "unknown")
                status.set_text(f"{active} · {enabled}{listener_text} · MCP health not checked · checked {checked}{diagnostic_suffix}{error_suffix}")
                installed = state.get("installed") is True
                action_buttons["edit-form"].set_sensitive(active_state is False and not installed)
                action_buttons["install"].set_sensitive(active_state is False and not installed)
                action_buttons["start"].set_sensitive(installed and active_state is False)
                action_buttons["stop"].set_sensitive(active_state is True)
                action_buttons["enable"].set_sensitive(installed and state.get("enabled") is False)
                action_buttons["disable"].set_sensitive(installed and state.get("enabled") is True)
                known_inactive = active_state is False and (not installed or enabled_state in ("enabled", "disabled"))
                action_buttons["remove"].set_sensitive(known_inactive)
                action_buttons["uninstall"].set_sensitive(installed and known_inactive)

            self._async(manager.snapshot, got_snapshot)

        def periodic_forward_refresh() -> bool:
            if not dialog.get_visible():
                return False
            if selected_name() and not checking and not busy:
                update_selection()
            return True

        def finish_action(ok: bool, value: Any) -> None:
            nonlocal busy, action_error
            busy = False
            for button in buttons:
                button.set_sensitive(True)
            action_ok = ok and not (isinstance(value, dict) and value.get("ok") is False)
            if action_ok:
                action_error = None
                status.set_text(str(value.get("error") or value.get("stdout") or "Done")
                                if isinstance(value, dict) else "Done")
                load_entries()
            else:
                action_error = (str(value.get("error") or value.get("stderr") or value)
                                if isinstance(value, dict) else str(value))
                update_selection()

        def run_action(action: str) -> None:
            nonlocal busy
            name = selected_name()
            if busy or not name:
                return
            busy = True
            for button in buttons:
                button.set_sensitive(False)
            progress = {"install": "Installing", "start": "Starting", "stop": "Stopping",
                        "enable": "Enabling", "disable": "Disabling", "uninstall": "Uninstalling",
                        "remove": "Removing"}.get(action, action.title())
            status.set_text(f"{progress} {name}…")

            def work() -> Any:
                return getattr(manager, action)(name)

            self._async(work, finish_action)

        def show_form(existing: Any | None = None) -> None:
            nonlocal busy
            form = gtk.Dialog(title="Edit SSH forward" if existing else "Add SSH forward",
                              transient_for=dialog, modal=True)
            form.add_buttons("Cancel", gtk.ResponseType.CANCEL, "Save", gtk.ResponseType.OK)
            body = form.get_content_area()
            grid = gtk.Grid(column_spacing=10, row_spacing=6)
            body.add(grid)
            fields = {
                "name": gtk.Entry(), "ssh_host": gtk.Entry(),
                "direction": gtk.ComboBoxText(), "local_port": gtk.Entry(),
                "forwarded_port": gtk.Entry(), "endpoint": gtk.Entry(),
                "endpoint_path": gtk.Entry(), "ssh_config": gtk.Entry(),
            }
            fields["direction"].append("local", "Local forward")
            fields["direction"].append("reverse", "Reverse forward")
            values = {
                "name": existing.name if existing else "",
                "ssh_host": existing.ssh_host if existing else "",
                "direction": existing.direction if existing else "local",
                "local_port": str(existing.local_port or "") if existing else "",
                "forwarded_port": str(existing.forwarded_port or "") if existing else "",
                "endpoint": existing.endpoint if existing else "",
                "endpoint_path": existing.endpoint_path if existing else "/mcp",
                "ssh_config": existing.ssh_config or "" if existing else "",
            }
            labels = (("Name", "name"), ("SSH host or alias", "ssh_host"),
                      ("Direction", "direction"), ("Local port (local only)", "local_port"),
                      ("Remote port (reverse only)", "forwarded_port"),
                      ("Endpoint host:port", "endpoint"), ("Endpoint URL path", "endpoint_path"),
                      ("OpenSSH config path (optional)", "ssh_config"))
            for row, (label, key) in enumerate(labels):
                grid.attach(gtk.Label(label=label), 0, row, 1, 1)
                grid.attach(fields[key], 1, row, 1, 1)
                if key == "direction":
                    fields[key].set_active_id(values[key])
                else:
                    fields[key].set_text(values[key])
            if existing:
                fields["name"].set_editable(False)
            form.show_all()
            response = form.run()
            if response == gtk.ResponseType.OK:
                direction = fields["direction"].get_active_id()
                entry = {
                    "name": fields["name"].get_text().strip(),
                    "ssh_host": fields["ssh_host"].get_text().strip(),
                    "direction": direction,
                    "endpoint": fields["endpoint"].get_text().strip(),
                    "endpoint_path": fields["endpoint_path"].get_text().strip(),
                    "ssh_config": fields["ssh_config"].get_text().strip() or None,
                    "local_port": None,
                    "forwarded_port": None,
                }
                port_field = "local_port" if direction == "local" else "forwarded_port"
                port_text = fields[port_field].get_text().strip()
                try:
                    entry[port_field] = int(port_text)
                except Exception as exc:
                    self._inform("Could not save SSH forward", str(exc))
                else:
                    busy = True
                    for button in buttons:
                        button.set_sensitive(False)
                    status.set_text("Saving…")
                    operation = (lambda: manager.edit(existing.name, entry)) if existing else (
                        lambda: manager.add(entry))
                    self._async(operation, finish_action)
            form.destroy()

        def open_forward_settings(*_args: Any) -> None:
            config = manager.config
            target = config if config.is_file() else config.parent

            def work() -> str:
                subprocess.run(["xdg-open", str(target)], check=True,
                               capture_output=True, timeout=20)
                return str(target)

            def done(ok: bool, value: Any) -> None:
                status.set_text(str(value) if ok else f"Could not open settings: {value}")

            self._async(work, done)

        settings_button.connect("clicked", open_forward_settings)

        buttons = []
        for index, (label, action) in enumerate((
                ("Add…", "add-form"), ("Edit…", "edit-form"), ("Remove", "remove"),
                ("Install unit", "install"), ("Start", "start"), ("Stop", "stop"),
                ("Enable at login", "enable"), ("Disable at login", "disable"),
                ("Uninstall unit", "uninstall"))):
            button = gtk.Button(label=label)
            buttons.append(button)
            action_buttons[action] = button
            if action == "add-form":
                button.connect("clicked", lambda *_: show_form())
            elif action == "edit-form":
                button.connect("clicked", lambda *_: show_form(entries.get(selected_name())))
            else:
                button.connect("clicked", lambda _button, operation=action: run_action(operation))
            actions.attach(button, index % 3, index // 3, 1, 1)

        selector.connect("changed", update_selection)
        for key, button in action_buttons.items():
            if key != "add-form":
                button.set_sensitive(False)
        dialog.show_all()
        load_entries()
        refresh_source = self.glib.timeout_add_seconds(5, periodic_forward_refresh)
        dialog.run()
        self.glib.source_remove(refresh_source)
        dialog.destroy()

    def _persist_settings(self) -> bool:
        try:
            save_settings(self.settings_path, self.settings)
            return True
        except (OSError, ValueError) as exc:
            self.error = str(exc)
            return False

    def _on_settings(self, item: Any) -> None:
        gtk = self.gtk
        dialog = gtk.Dialog(title="Jev desktop tray settings")
        dialog.add_buttons("Cancel", gtk.ResponseType.CANCEL, "Save", gtk.ResponseType.OK)
        area = dialog.get_content_area()
        grid = gtk.Grid(column_spacing=12, row_spacing=8)
        area.add(grid)

        autonomy = gtk.ComboBoxText()
        for value in VALID_AUTONOMY:
            autonomy.append_text(value)
        autonomy.set_active(VALID_AUTONOMY.index(self.settings["autonomy_mode"]))
        desktop = gtk.ComboBoxText()
        for value in VALID_MODES:
            desktop.append_text(value)
        desktop.set_active(VALID_MODES.index(self.settings["desktop_mode"]))
        poll = gtk.SpinButton.new_with_range(1, 30, 1)
        poll.set_value(float(self.settings["poll_seconds"]))
        confirm_kill = gtk.CheckButton(label="Confirm stop-all and shutdown")
        confirm_kill.set_active(bool(self.settings["confirm_kill_switch"]))
        confirm_cleanup = gtk.CheckButton(label="Confirm session cleanup")
        confirm_cleanup.set_active(bool(self.settings["confirm_cleanup"]))
        confirm_live = gtk.CheckButton(label="Ask before each tray-started live task")
        confirm_live.set_active(bool(self.settings["confirm_live_task"]))
        semantic_live = gtk.CheckButton(label="Enable live AT-SPI semantic candidates")
        semantic_live.set_active(bool(self.settings["semantic_candidates_live"]))

        for row, (label, widget) in enumerate((
                ("Autonomy for tray-started tasks", autonomy),
                ("Desktop mode preference", desktop),
                ("Status refresh seconds", poll),
                ("", confirm_kill),
                ("", confirm_cleanup),
                ("", confirm_live),
                ("", semantic_live))):
            if label:
                grid.attach(gtk.Label(label=label), 0, row, 1, 1)
            grid.attach(widget, 1, row, 1, 1)
        forwards = gtk.Button(label="Manage SSH forwards…")
        forwards.connect("clicked", self._on_forwards)
        area.add(forwards)
        area.add(gtk.Label(label=("The host cannot detect physical presence. Unchecking this prompt saves "
                                 "a tray policy that supplies the owner-present override. Live semantic "
                                 "candidates default off; enabling them may request temporary accessibility. "
                                 "Other clients must still send their own required live authorization fields.")))
        dialog.show_all()
        response = dialog.run()
        if response == gtk.ResponseType.OK:
            self.settings.update(
                autonomy_mode=VALID_AUTONOMY[autonomy.get_active()],
                desktop_mode=VALID_MODES[desktop.get_active()],
                poll_seconds=float(poll.get_value()),
                confirm_kill_switch=bool(confirm_kill.get_active()),
                confirm_cleanup=bool(confirm_cleanup.get_active()),
                confirm_live_task=bool(confirm_live.get_active()),
                semantic_candidates_live=bool(semantic_live.get_active()))
            self._persist_settings()
        dialog.destroy()
        self._refresh_labels()

    def _on_quit(self, item: Any) -> None:
        self._stop.set()
        self.gtk.main_quit()

    # ----------------------------------------------------------------- dialogs
    def _confirm(self, text: str, secondary: str) -> bool:
        dialog = self.gtk.MessageDialog(message_type=self.gtk.MessageType.QUESTION,
                                        buttons=self.gtk.ButtonsType.OK_CANCEL, text=text)
        dialog.format_secondary_text(secondary)
        response = dialog.run()
        dialog.destroy()
        return response == self.gtk.ResponseType.OK

    def _inform(self, text: str, secondary: str) -> None:
        dialog = self.gtk.MessageDialog(message_type=self.gtk.MessageType.INFO,
                                        buttons=self.gtk.ButtonsType.OK, text=text)
        dialog.format_secondary_text(secondary)
        dialog.run()
        dialog.destroy()

    def _confirm_live(self, app: str, semantic_candidates: bool = False) -> bool:
        gtk = self.gtk
        dialog = gtk.Dialog(title="Physical desktop task")
        dialog.add_buttons("Cancel", gtk.ResponseType.CANCEL, f"Start {app} live", gtk.ResponseType.OK)
        area = dialog.get_content_area()
        area.set_spacing(8)
        area.add(gtk.Label(label=f"Starting {app} on the physical desktop types into your live session."))
        present = gtk.CheckButton(label="The owner is present and watching this task")
        area.add(present)
        a11y = None
        if semantic_candidates:
            a11y = gtk.CheckButton(label="Allow temporary accessibility for this task only")
            area.add(a11y)
        area.add(gtk.Label(label=("The task window remains in the foreground between calls. Original focus is "
                                  "restored when the session stops. An attached app remains open.")))
        dialog.show_all()
        response = dialog.run()
        confirmed = (response == gtk.ResponseType.OK and present.get_active()
                     and (a11y is None or a11y.get_active()))
        dialog.destroy()
        return confirmed

    # -------------------------------------------------------------------- run
    def run(self) -> int:
        self._set_modes()
        self._rebuild_start_menu()
        self._refresh_labels()
        threading.Thread(target=self._poll_loop, daemon=True).start()
        self.gtk.main()
        self._stop.set()
        return 0


def tray_main(*, socket: Path | None = None, unit: str = "jev-desktop",
              settings_path: Path | None = None) -> int:
    try:
        import gi

        gi.require_version("AyatanaAppIndicator3", "0.1")
        gi.require_version("Gtk", "3.0")
        from gi.repository import AyatanaAppIndicator3, GLib, Gtk
    except (ImportError, ValueError) as exc:
        print("tray_unavailable: PyGObject and libayatana-appindicator are required "
              f"({type(exc).__name__})", file=sys.stderr)
        return 2
    icon_name, icon_dir = prepare_tray_icon(Gtk)
    tray = DesktopTray(socket=socket, unit=unit, gtk=Gtk, glib=GLib, appindicator=AyatanaAppIndicator3,
                       settings_path=settings_path, icon_name=icon_name, icon_dir=icon_dir)
    return tray.run()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jev-desktop-tray",
                                     description="Tray indicator for the local Jev desktop owner.")
    parser.add_argument("--socket", type=Path, default=None, help="owner Unix socket path")
    parser.add_argument("--unit", default="jev-desktop", help="systemd --user unit name the tray controls")
    parser.add_argument("--settings", type=Path, default=None,
                        help="tray settings file (default: ~/.config/jev-desktop/tray.json)")
    args = parser.parse_args(argv)
    return tray_main(socket=args.socket, unit=args.unit, settings_path=args.settings)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
