"""KDE StatusNotifierItem tray for the local desktop owner.

Runs as its own process and talks to the owner over the same private Unix
socket as the CLI and MCP clients. It never reads or writes session state, and
it cannot pre-authorize a live task: starting a physical-desktop task always
needs the owner's explicit confirmation in a dialog for that one task.
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

POLL_SECONDS = 3.0
MODE_VIRTUAL = "virtual"
MODE_LIVE = "live"
ICON_CANDIDATES = ("input-mouse", "input-tablet", "preferences-desktop", "computer")
SERVICE_VERBS = ("start", "stop", "enable", "disable")


def default_tray_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "jev-desktop" / "tray.json"


def load_preference(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {"desktop_mode": MODE_VIRTUAL}
    mode = data.get("desktop_mode") if isinstance(data, dict) else None
    return {"desktop_mode": mode if mode in (MODE_VIRTUAL, MODE_LIVE) else MODE_VIRTUAL}


def save_preference(path: Path, mode: str) -> None:
    if mode not in (MODE_VIRTUAL, MODE_LIVE):
        raise ValueError("invalid_desktop_mode")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (json.dumps({"desktop_mode": mode}) + "\n").encode("utf-8"))
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
                 preference_path: Path | None = None) -> None:
        self.gtk = gtk
        self.glib = glib
        self.socket = socket
        self.unit = unit
        self.preference_path = preference_path or default_tray_config_path()
        self.preference = load_preference(self.preference_path)
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
        for item in (self.item_owner, self.item_session, self.item_service):
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
            self.mode_virtual.set_active(self.preference["desktop_mode"] == MODE_VIRTUAL)
            self.mode_live.set_active(self.preference["desktop_mode"] == MODE_LIVE)
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
            for app in apps:
                item = self.gtk.MenuItem(label=app)
                item.connect("activate", self._on_start, app)
                self.start_menu.append(item)
        self.start_menu.show_all()

    # ---------------------------------------------------------------- refresh
    def _refresh_labels(self) -> None:
        if self.owner_state == "running":
            self.item_owner.set_label(f"Owner: running{self._owner_suffix()}")
        else:
            self.item_owner.set_label("Owner: stopped (or unreachable)")
        session = (self.status or {}).get("session")
        if isinstance(session, dict):
            self.item_session.set_label(
                f"Session: {session.get('app')} · {session.get('desktop_mode')} · {session.get('state')}"
                f" · {int(session.get('age_seconds') or 0)}s")
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
        elif active in {"inactive", "unknown", "failed"} and enabled == "disabled":
            self.item_service.set_label("Service: not installed/active")
        else:
            self.item_service.set_label(f"Service: {active} · {enabled}")
        mode = self.preference["desktop_mode"]
        self.indicator.set_title(f"Jev desktop ({mode})")
        self._set_modes()
        if self.preference["desktop_mode"] == MODE_LIVE:
            self.item_owner.set_tooltip_text(
                "Physical mode still requires a per-task owner-present confirmation in the dialog.")

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
        self._poll_once()
        self._stop.wait(POLL_SECONDS)
        while not self._stop.is_set():
            self._poll_once()
            self._stop.wait(POLL_SECONDS)

    # --------------------------------------------------------------- handlers
    def _on_mode(self, item: Any, mode: str) -> None:
        if self._syncing:
            return
        self.preference["desktop_mode"] = mode
        try:
            save_preference(self.preference_path, mode)
        except (OSError, ValueError) as exc:
            self.error = str(exc)
        if mode == MODE_LIVE:
            self._inform("Physical desktop selected",
                         "A live task is only started after you confirm, in the dialog, that you are "
                         "present and that temporary accessibility is allowed for that one task.")
        self._refresh_labels()

    def _on_start(self, item: Any, app: str) -> None:
        mode = self.preference["desktop_mode"]
        params: dict[str, Any] = {"app": app, "mode": "guarded", "desktop_mode": mode}
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
        if not self._confirm("Stop all sessions and shut down the owner?",
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


def tray_main(*, socket: Path | None = None, unit: str = "jev-desktop") -> int:
    try:
        import gi

        gi.require_version("AyatanaAppIndicator3", "0.1")
        gi.require_version("Gtk", "3.0")
        from gi.repository import AyatanaAppIndicator3, GLib, Gtk
    except (ImportError, ValueError) as exc:
        print("tray_unavailable: PyGObject and libayatana-appindicator are required "
              f"({type(exc).__name__})", file=sys.stderr)
        return 2
    tray = DesktopTray(socket=socket, unit=unit, gtk=Gtk, glib=GLib, appindicator=AyatanaAppIndicator3)
    return tray.run()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jev-desktop-tray",
                                     description="Tray indicator for the local Jev desktop owner.")
    parser.add_argument("--socket", type=Path, default=None, help="owner Unix socket path")
    parser.add_argument("--unit", default="jev-desktop", help="systemd --user unit name the tray controls")
    args = parser.parse_args(argv)
    return tray_main(socket=args.socket, unit=args.unit)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
