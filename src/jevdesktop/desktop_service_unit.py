"""systemd --user unit management for the local desktop owner.

Headless on purpose: `jev-desktop service ...` must work over SSH and without a
GTK stack. The tray imports the same helpers for its service submenu.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from jevdesktop import paths
from jevdesktop.desktop_daemon import ALLOW_APPS_ENV, _env_allowed_apps, default_config_path

UNIT_NAME = "jev-desktop.service"
TRAY_UNIT_NAME = "jev-desktop-tray.service"

UNIT_TEMPLATE = """\
[Unit]
Description=Jev desktop computer-use owner (single local owner)
Documentation=https://github.com/aliforfaen/computer-use
PartOf=graphical-session.target
After=graphical-session.target
ConditionEnvironment=WAYLAND_DISPLAY

[Service]
Type=exec
ExecStart={exec_start}
Restart=on-failure
RestartSec=2
# The owner stops sessions, restores focus and closes owned apps before exiting.
TimeoutStopSec=30
KillMode=mixed
NoNewPrivileges=yes
EnvironmentFile=-%h/.config/jev-desktop/daemon.env

[Install]
WantedBy=graphical-session.target
"""

STARTER_CONFIG_NOTE = (
    "The starter config holds no secrets. A reader key belongs in the environment "
    "or an optional ~/.config/jev-desktop/daemon.env (see EnvironmentFile)."
)

TRAY_UNIT_TEMPLATE = """\
[Unit]
Description=Jev desktop tray indicator (client of the local owner)
Documentation=https://github.com/aliforfaen/computer-use
PartOf=graphical-session.target
After=graphical-session.target jev-desktop.service
ConditionEnvironment=WAYLAND_DISPLAY

[Service]
Type=exec
ExecStart={exec_start}
Restart=on-failure
RestartSec=2
NoNewPrivileges=yes

[Install]
WantedBy=graphical-session.target
"""


def default_unit_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "systemd" / "user"


def _systemd_quote(value: str) -> str:
    if any(char in value for char in ' \\"'):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def _entry_point() -> str:
    found = shutil.which("jev-desktop")
    if found:
        return _systemd_quote(found)
    return f"{_systemd_quote(sys.executable)} -m jevdesktop.desktop_cli"


def _desktop_asset_targets() -> tuple[Path, Path]:
    """Return the installed ``(desktop entry, icon)`` paths under XDG data home."""
    apps_dir = paths.data_home() / "applications"
    icon_dir = paths.data_home() / "icons" / "hicolor" / "scalable" / "apps"
    return apps_dir / paths.DESKTOP_ENTRY_NAME, icon_dir / paths.ICON_NAME


def _install_desktop_assets() -> dict[str, str]:
    """Copy the launcher entry and app icon so menus show the Jev icon."""
    entry = paths.desktop_entry_path()
    icon = paths.icon_path()
    if entry is None or icon is None:
        return {}
    entry_target, icon_target = _desktop_asset_targets()
    entry_target.parent.mkdir(parents=True, exist_ok=True)
    icon_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(entry, entry_target)
    shutil.copyfile(icon, icon_target)
    return {"entry": str(entry_target), "icon": str(icon_target)}


def _remove_desktop_assets() -> list[str]:
    removed: list[str] = []
    for target in _desktop_asset_targets():
        if target.exists():
            target.unlink()
            removed.append(str(target))
    return removed


def _desktop_asset_status() -> dict[str, Any]:
    entry_target, icon_target = _desktop_asset_targets()
    return {"entry": str(entry_target), "icon": str(icon_target),
            "installed": entry_target.exists() and icon_target.exists()}


def render_unit(config_path: Path) -> str:
    exec_start = " ".join((
        _entry_point(), "daemon", "--foreground",
        "--config", _systemd_quote(str(config_path)),
    ))
    return UNIT_TEMPLATE.format(exec_start=exec_start)


def render_tray_unit() -> str:
    return TRAY_UNIT_TEMPLATE.format(exec_start=f"{_entry_point()} tray")


def _systemctl(*arguments: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(["systemctl", "--user", *arguments],
                              capture_output=True, text=True, timeout=20)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def _unit_state(unit: str) -> tuple[str, str]:
    active = _systemctl("is-active", unit)
    enabled = _systemctl("is-enabled", unit)
    return ((active.stdout.strip() if active else "unavailable") or "inactive",
            (enabled.stdout.strip() if enabled else "unavailable") or "disabled")


def _load_config_apps(config_path: Path) -> list[str]:
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return []
    apps = data.get("allowed_apps") if isinstance(data, dict) else None
    return [app for app in apps if isinstance(app, str)] if isinstance(apps, list) else []


def _write_starter_config(config_path: Path, apps: list[str]) -> None:
    config_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = json.dumps({"allowed_apps": apps}, indent=2) + "\n"
    fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))


def create_session_argv(config_path: Path) -> str:
    """The ExecStart line, exposed for tests and diagnostics."""
    return render_unit(config_path).split("ExecStart=", 1)[1].splitlines()[0]


def service_command(args: Any) -> int:
    command = getattr(args, "service_command", None)
    unit_dir = Path(args.unit_dir) if getattr(args, "unit_dir", None) else default_unit_dir()
    if command == "install":
        return _install(args, unit_dir)
    if command == "uninstall":
        return _uninstall(args, unit_dir)
    if command == "status":
        return _status(args, unit_dir)
    print(f"unknown service command: {command}", file=sys.stderr)
    return 2


def _install(args: Any, unit_dir: Path) -> int:
    config_path = Path(args.config) if getattr(args, "config", None) else default_config_path()
    apps = list(getattr(args, "allow_app", []) or []) or _env_allowed_apps() or _load_config_apps(config_path)
    config_created = False
    if not config_path.exists():
        if not apps:
            _emit({"ok": False, "command": "install",
                   "error": "allowlist_required",
                   "message": "--allow-app, " + ALLOW_APPS_ENV + " or an existing config file is required"})
            return 2
        try:
            _write_starter_config(config_path, apps)
        except OSError as exc:
            _emit({"ok": False, "command": "install", "error": "config_write_failed", "message": str(exc)})
            return 2
        config_created = True
    elif apps and apps != _load_config_apps(config_path):
        # Never rewrite a config the owner already edited.
        _emit({"ok": False, "command": "install", "error": "config_exists_unchanged",
               "message": f"{config_path} already exists; edit it or remove it, then re-run install"})
        return 2

    rendered = {UNIT_NAME: render_unit(config_path)}
    if getattr(args, "tray", False):
        rendered[TRAY_UNIT_NAME] = render_tray_unit()

    unit_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    written: dict[str, str] = {}
    for name, content in rendered.items():
        unit_path = unit_dir / name
        replaced = unit_path.exists()
        unit_path.write_text(content, encoding="utf-8")
        unit_path.chmod(0o644)
        written[name] = "replaced" if replaced else "created"
    desktop_assets = _install_desktop_assets()
    reload_proc = _systemctl("daemon-reload")
    activelines = [] if reload_proc is None else [line for line in reload_proc.stdout.splitlines() if line.strip()]
    _emit({"ok": True, "command": "install", "unit": str(unit_dir / UNIT_NAME),
           "units": {name: str(unit_dir / name) for name in rendered},
           "unit_states": written,
           "config": str(config_path), "config_created": config_created,
           "allow_apps": apps,
           "desktop_entry": desktop_assets,
           "daemon_reload": "ok" if reload_proc is not None and reload_proc.returncode == 0
                                             else "failed" if reload_proc is not None else "unavailable",
           "daemon_reload_output": activelines,
           "next": ["systemctl --user enable --now " + UNIT_NAME]
                   + (["systemctl --user enable --now " + TRAY_UNIT_NAME] if getattr(args, "tray", False) else [])
                   + ["jev-desktop tray"],
           "note": STARTER_CONFIG_NOTE})
    return 0


def _uninstall(args: Any, unit_dir: Path) -> int:
    names = [UNIT_NAME, TRAY_UNIT_NAME]
    installed = [name for name in names if (unit_dir / name).exists()]
    if not installed:
        _emit({"ok": True, "command": "uninstall", "unit": str(unit_dir / UNIT_NAME), "removed": False,
               "message": "unit files are not installed"})
        return 0
    states = {name: _unit_state(name) for name in installed}
    active = [name for name, (state, _) in states.items() if state == "active"]
    if active and not getattr(args, "stop", False):
        # Stopping the owner closes owned apps; require the explicit flag.
        _emit({"ok": False, "command": "uninstall", "error": "unit_is_active",
               "message": "a unit is active; re-run with --stop to stop it first",
               "unit_state": {name: {"active": state, "enabled": enabled}
                              for name, (state, enabled) in states.items()}})
        return 2
    steps: dict[str, str] = {}
    for name in active:
        proc = _systemctl("stop", name)
        steps[f"stop:{name}"] = "ok" if proc is not None and proc.returncode == 0 else "failed"
    for name in installed:
        proc = _systemctl("disable", name)
        steps[f"disable:{name}"] = "ok" if proc is not None and proc.returncode == 0 else "skipped"
        (unit_dir / name).unlink()
    removed_assets = _remove_desktop_assets()
    reload_proc = _systemctl("daemon-reload")
    steps["daemon_reload"] = "ok" if reload_proc is not None and reload_proc.returncode == 0 else "failed"
    _emit({"ok": True, "command": "uninstall", "unit": str(unit_dir / UNIT_NAME), "removed": True,
           "units": installed, "desktop_entry": removed_assets, "steps": steps})
    return 0


def _status(args: Any, unit_dir: Path) -> int:
    config_path = Path(args.config) if getattr(args, "config", None) else default_config_path()
    unit_path = unit_dir / UNIT_NAME
    tray_path = unit_dir / TRAY_UNIT_NAME
    installed = unit_path.exists()
    active, enabled = _unit_state(UNIT_NAME)
    drift = None
    if installed:
        drift = unit_path.read_text(encoding="utf-8") != render_unit(config_path)
    tray_installed = tray_path.exists()
    tray_state = _unit_state(TRAY_UNIT_NAME) if tray_installed else ("inactive", "disabled")
    tray_drift = None
    if tray_installed:
        tray_drift = tray_path.read_text(encoding="utf-8") != render_tray_unit()
    _emit({"ok": True, "command": "status", "unit": UNIT_NAME, "unit_path": str(unit_path),
           "installed": installed, "active": active, "enabled": enabled,
           "config": str(config_path), "config_exists": config_path.exists(),
           "allow_apps": _load_config_apps(config_path) if config_path.exists() else [],
           "drift": drift,
           "tray": {"unit": TRAY_UNIT_NAME, "unit_path": str(tray_path), "installed": tray_installed,
                    "active": tray_state[0], "enabled": tray_state[1], "drift": tray_drift},
           "desktop_entry": _desktop_asset_status(),
           "systemd_available": _systemctl("--version") is not None,
           "hint": ("installed unit differs from the current template or entry point; re-run install"
                    if drift or tray_drift else None)})
    return 0
