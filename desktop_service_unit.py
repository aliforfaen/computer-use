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

from desktop_daemon import ALLOW_APPS_ENV, _env_allowed_apps, default_config_path

UNIT_NAME = "jev-desktop.service"

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
    return f"{_systemd_quote(sys.executable)} -m desktop_cli"


def render_unit(config_path: Path) -> str:
    exec_start = " ".join((
        _entry_point(), "daemon", "--foreground",
        "--config", _systemd_quote(str(config_path)),
    ))
    return UNIT_TEMPLATE.format(exec_start=exec_start)


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

    unit_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    unit_path = unit_dir / UNIT_NAME
    rendered = render_unit(config_path)
    stale_content = unit_path.read_text(encoding="utf-8") if unit_path.exists() else None
    unit_path.write_text(rendered, encoding="utf-8")
    unit_path.chmod(0o644)
    reload_proc = _systemctl("daemon-reload")
    activelines = [] if reload_proc is None else [line for line in reload_proc.stdout.splitlines() if line.strip()]
    _emit({"ok": True, "command": "install", "unit": str(unit_path),
           "unit_replaced": stale_content is not None,
           "config": str(config_path), "config_created": config_created,
           "allow_apps": apps, "daemon_reload": "ok" if reload_proc is not None and reload_proc.returncode == 0
                                             else "failed" if reload_proc is not None else "unavailable",
           "daemon_reload_output": activelines,
           "next": ["systemctl --user enable --now " + UNIT_NAME, "jev-desktop tray"],
           "note": STARTER_CONFIG_NOTE})
    return 0


def _uninstall(args: Any, unit_dir: Path) -> int:
    unit_path = unit_dir / UNIT_NAME
    if not unit_path.exists():
        _emit({"ok": True, "command": "uninstall", "unit": str(unit_path), "removed": False,
               "message": "unit file is not installed"})
        return 0
    active, enabled = _unit_state(UNIT_NAME)
    if active == "active" and not getattr(args, "stop", False):
        # Stopping the owner closes owned apps; require the explicit flag.
        _emit({"ok": False, "command": "uninstall", "error": "unit_is_active",
               "message": "the unit is active; re-run with --stop to stop it first",
               "unit_state": {"active": active, "enabled": enabled}})
        return 2
    steps: dict[str, str] = {}
    if active == "active":
        proc = _systemctl("stop", UNIT_NAME)
        steps["stop"] = "ok" if proc is not None and proc.returncode == 0 else "failed"
    proc = _systemctl("disable", UNIT_NAME)
    steps["disable"] = "ok" if proc is not None and proc.returncode == 0 else "skipped"
    unit_path.unlink()
    reload_proc = _systemctl("daemon-reload")
    steps["daemon_reload"] = "ok" if reload_proc is not None and reload_proc.returncode == 0 else "failed"
    _emit({"ok": True, "command": "uninstall", "unit": str(unit_path), "removed": True, "steps": steps})
    return 0


def _status(args: Any, unit_dir: Path) -> int:
    config_path = Path(args.config) if getattr(args, "config", None) else default_config_path()
    unit_path = unit_dir / UNIT_NAME
    installed = unit_path.exists()
    active, enabled = _unit_state(UNIT_NAME)
    drift = None
    if installed:
        expected = render_unit(config_path)
        drift = unit_path.read_text(encoding="utf-8") != expected
    _emit({"ok": True, "command": "status", "unit": UNIT_NAME, "unit_path": str(unit_path),
           "installed": installed, "active": active, "enabled": enabled,
           "config": str(config_path), "config_exists": config_path.exists(),
           "allow_apps": _load_config_apps(config_path) if config_path.exists() else [],
           "drift": drift,
           "systemd_available": _systemctl("--version") is not None,
           "hint": ("installed unit differs from the current template or entry point; re-run install"
                    if drift else None)})
    return 0
