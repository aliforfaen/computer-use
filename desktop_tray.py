"""KDE StatusNotifierItem tray for the local desktop owner.

Runs as its own process and talks to the owner over the same private Unix
socket as the CLI and MCP clients. It never reads or writes session state, and
it cannot pre-authorize a live task: starting a physical-desktop task always
needs the owner's explicit confirmation in a dialog for that one task.

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

from desktop_cli import ipc_call
from desktop_daemon import default_config_dir

MODE_VIRTUAL = "virtual"
MODE_LIVE = "live"
VALID_MODES = (MODE_VIRTUAL, MODE_LIVE)
VALID_AUTONOMY = ("supervised", "guarded", "yolo")
ICON_CANDIDATES = ("input-mouse", "input-tablet", "preferences-desktop", "computer")
SERVICE_VERBS = ("start", "stop", "enable", "disable")

DEFAULT_SETTINGS: dict[str, Any] = {
    "desktop_mode": MODE_VIRTUAL,
    "autonomy_mode": "guarded",
    "poll_seconds": 3.0,
    "confirm_kill_switch": True,
    "confirm_cleanup": True,
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
    return settings


def save_settings(path: Path, settings: dict[str, Any]) -> None:
    validated = dict(DEFAULT_SETTINGS)
    validated.update({key: value for key, value in settings.items() if key in DEFAULT_SETTINGS})
    if validated["desktop_mode"] not in VALID_MODES or validated["autonomy_mode"] not in VALID_AUTONOMY:
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


class DesktopTray:
    """GTK main-loop object; every owner or systemctl call runs off-thread."""

    def __init__(self, *, socket: Path | None, unit: str, gtk: Any, glib: Any, appindicator: Any,
                 settings_path: Path | None = None) -> None:
        self.gtk = gtk
        self.glib = glib
        self.socket = socket
        self.unit = unit
        self.settings_path = settings_path or default_settings_path()
        self.settings = load_settings(self.settings_path)
        self.status: dict[str, Any] | None = None
        self.capabilities: dict[str, Any] | None = None
        self.service_state: tuple[str, str] = ("unknown", "unknown")
        self.owner_state = "unknown"
        self.error: str | None = None
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
            "jev-desktop", _icon_name(gtk), appindicator.IndicatorCategory.APPLICATION_STATUS)
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
        if not apps:
            placeholder = self.gtk.MenuItem(label="Owner not running")
            placeholder.set_sensitive(False)
            self.start_menu.append(placeholder)
        else:
            for app in sorted(apps):
                item = self.gtk.MenuItem(label=app)
                item.connect("activate", self._on_start, app)
                self.start_menu.append(item)
        self.start_menu.show_all()

    # ---------------------------------------------------------------- refresh
    def _session_summary(self, session: dict[str, Any]) -> str:
        summary = (f"{session.get('app')} · {session.get('desktop_mode')} · {session.get('state')}"
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
        else:
            self.item_session.set_label("Session: none")
            self.item_cleanup.set_sensitive(self.owner_state == "running")
            self.start_root.set_sensitive(self.owner_state == "running" and bool(
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
            self._inform("Physical desktop selected",
                         "A live task is only started after you confirm, in the dialog, that you are "
                         "present and that temporary accessibility is allowed for that one task.")
        self._refresh_labels()

    def _on_start(self, item: Any, app: str) -> None:
        mode = self.settings["desktop_mode"]
        params: dict[str, Any] = {"app": app, "mode": self.settings["autonomy_mode"], "desktop_mode": mode}
        if mode == MODE_LIVE:
            if not self._confirm_live(app):
                return
            params.update(owner_present_override=True, temporary_a11y=True)

        def done(ok: bool, value: Any) -> None:
            if ok:
                self.status = value
                self.error = None
            else:
                self.error = str(value)
                self._inform("Task could not start", str(value))
            self._poll_once()

        self._async(lambda: self.call("session_start", params), done)

    def _on_cleanup(self, item: Any) -> None:
        if self.settings["confirm_cleanup"] and not self._confirm(
                "Stop the active session and close its owned app?",
                "Cleanup restores focus and accessibility; the result reports confirmed or unconfirmed."):
            return

        def done(ok: bool, value: Any) -> None:
            if ok:
                cleanup = value.get("cleanup") if isinstance(value, dict) else None
                detail = f"Cleanup: {cleanup}" if cleanup else "Cleanup reported without a confirmation field."
                self._inform("Desktop cleaned up" if cleanup == "confirmed" else "Cleanup unconfirmed", detail)
            else:
                self.error = str(value)
                self._inform("Cleanup failed", str(value))
            self._poll_once()

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

        for row, (label, widget) in enumerate((
                ("Autonomy for tray-started tasks", autonomy),
                ("Desktop mode preference", desktop),
                ("Status refresh seconds", poll),
                ("", confirm_kill),
                ("", confirm_cleanup))):
            if label:
                grid.attach(gtk.Label(label=label), 0, row, 1, 1)
            grid.attach(widget, 1, row, 1, 1)
        area.add(gtk.Label(label="Live tasks always require a per-task confirmation dialog."))
        dialog.show_all()
        response = dialog.run()
        if response == gtk.ResponseType.OK:
            self.settings.update(
                autonomy_mode=VALID_AUTONOMY[autonomy.get_active()],
                desktop_mode=VALID_MODES[desktop.get_active()],
                poll_seconds=float(poll.get_value()),
                confirm_kill_switch=bool(confirm_kill.get_active()),
                confirm_cleanup=bool(confirm_cleanup.get_active()))
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

    def _confirm_live(self, app: str) -> bool:
        gtk = self.gtk
        dialog = gtk.Dialog(title="Physical desktop task")
        dialog.add_buttons("Cancel", gtk.ResponseType.CANCEL, f"Start {app} live", gtk.ResponseType.OK)
        area = dialog.get_content_area()
        area.set_spacing(8)
        area.add(gtk.Label(label=f"Starting {app} on the physical desktop types into your live session."))
        present = gtk.CheckButton(label="The owner is present and watching this task")
        a11y = gtk.CheckButton(label="Allow temporary accessibility for this task only")
        area.add(present)
        area.add(a11y)
        area.add(gtk.Label(label="Focus is restored after every call; the owned app is closed on cleanup."))
        dialog.show_all()
        response = dialog.run()
        confirmed = (response == gtk.ResponseType.OK and present.get_active() and a11y.get_active())
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
    tray = DesktopTray(socket=socket, unit=unit, gtk=Gtk, glib=GLib, appindicator=AyatanaAppIndicator3,
                       settings_path=settings_path)
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
