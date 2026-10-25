"""Named OpenSSH TCP forwards managed as opt-in systemd user units.

This module never starts or enables a tunnel implicitly. It uses argv for all
systemctl/ssh interactions; values are validated before being embedded in a
systemd ExecStart line.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import socket
import tempfile
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ForwardConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Forward:
    name: str
    ssh_host: str
    direction: str  # local (-L) or reverse (-R)
    local_port: int | None
    forwarded_port: int | None
    endpoint: str  # host:port target as seen by the opposite SSH endpoint
    endpoint_path: str = "/mcp"
    ssh_config: str | None = None  # optional OpenSSH config file (-F)


def config_path() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "jev-desktop" / "ssh-forwards.json"


def _host(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 253:
        raise ForwardConfigError(f"{field} must be a non-empty host")
    # OpenSSH alias, user@host, DNS name or IP only. Excludes options and config syntax.
    if value.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.@:-]+", value):
        raise ForwardConfigError(f"invalid {field}")
    if value.count("@") > 1:
        raise ForwardConfigError(f"invalid {field}")
    return value


def _port(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ForwardConfigError(f"{field} must be an integer port from 1 to 65535")
    return value


def _endpoint(value: Any) -> str:
    if not isinstance(value, str) or any(c in value for c in "\r\n\x00"):
        raise ForwardConfigError("endpoint must be host:port")
    # Require bracket syntax for IPv6 so the colon separator is unambiguous.
    match = re.fullmatch(r"(?:\[([0-9A-Fa-f:.]+)\]|([A-Za-z0-9_.-]+)):(\d{1,5})", value)
    if not match:
        raise ForwardConfigError("endpoint must be host:port (bracket IPv6 addresses)")
    host = match.group(1) or match.group(2)
    try:
        if match.group(1):
            if ipaddress.IPv6Address(host) != ipaddress.IPv6Address("::1"):
                raise ForwardConfigError("endpoint must use a loopback address")
        else:
            if host.lower() != "localhost":
                address = ipaddress.ip_address(host) if re.fullmatch(r"[0-9.]+", host) else None
                if address is None or not address.is_loopback:
                    raise ForwardConfigError("endpoint must use localhost or a loopback IP address")
    except ValueError as exc:
        raise ForwardConfigError("invalid endpoint IP address") from exc
    return f"[{host}]:{_port(int(match.group(3)), 'endpoint port')}" if match.group(1) else f"{host}:{_port(int(match.group(3)), 'endpoint port')}"


def parse_forward(value: Any) -> Forward:
    if not isinstance(value, dict):
        raise ForwardConfigError("each forward must be an object")
    allowed = {"name", "ssh_host", "direction", "local_port", "forwarded_port",
               "endpoint", "endpoint_path", "ssh_config"}
    if set(value) - allowed:
        raise ForwardConfigError("forward has unknown fields")
    name = value.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,47}", name):
        raise ForwardConfigError("name must be 1-48 safe ASCII letters, digits, dot, underscore or dash")
    direction = value.get("direction")
    if direction not in ("local", "reverse"):
        raise ForwardConfigError("direction must be local or reverse")
    lp = value.get("local_port")
    fp = value.get("forwarded_port")
    if direction == "local":
        if fp is not None or lp is None:
            raise ForwardConfigError("local forwards require local_port and omit forwarded_port")
        lp = _port(lp, "local_port")
    else:
        if lp is not None or fp is None:
            raise ForwardConfigError("reverse forwards require forwarded_port and omit local_port")
        fp = _port(fp, "forwarded_port")
    endpoint_path = value.get("endpoint_path", "/mcp")
    if (not isinstance(endpoint_path, str) or not endpoint_path.startswith("/") or
            not re.fullmatch(r"/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*", endpoint_path) or
            ".." in endpoint_path.split("/") or
            any(endpoint_path[i] == "%" and (i + 2 >= len(endpoint_path) or
                not re.fullmatch(r"[0-9A-Fa-f]{2}", endpoint_path[i + 1:i + 3]))
                for i in range(len(endpoint_path)))):
        raise ForwardConfigError("endpoint_path must be a safe absolute URL path without query, fragment or dot-dot segments")
    ssh_config = value.get("ssh_config")
    if ssh_config is not None and (not isinstance(ssh_config, str) or not Path(ssh_config).is_absolute() or any(c in ssh_config for c in "\r\n\x00")):
        raise ForwardConfigError("ssh_config must be an absolute OpenSSH config path")
    return Forward(name, _host(value.get("ssh_host"), "ssh_host"), direction, lp, fp,
                   _endpoint(value.get("endpoint")), endpoint_path, ssh_config)


def load_forwards(path: Path | None = None) -> list[Forward]:
    source = path or config_path()
    try:
        mode = source.stat().st_mode & 0o777
        if mode & 0o077:
            raise ForwardConfigError("forwards config must be private (0600)")
        doc = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except ForwardConfigError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise ForwardConfigError(f"cannot read forwards config: {exc}") from exc
    if not isinstance(doc, dict) or set(doc) != {"forwards"} or not isinstance(doc["forwards"], list):
        raise ForwardConfigError("config must be an object containing only a forwards array")
    result = [parse_forward(item) for item in doc["forwards"]]
    if len({entry.name for entry in result}) != len(result):
        raise ForwardConfigError("forward names must be unique")
    return result


def _unit_escape(name: str) -> str:
    # systemd unit-name escaping for this restricted ASCII name subset.
    return "".join(c if c.isalnum() or c in ":_." else f"\\x{ord(c):02x}" for c in name)


def unit_name(forward: Forward | str) -> str:
    name = forward.name if isinstance(forward, Forward) else forward
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,47}", name):
        raise ForwardConfigError("invalid forward name")
    return f"jev-ssh-forward@{_unit_escape(name)}.service"


def ssh_argv(forward: Forward, ssh: str = "ssh") -> list[str]:
    args = [ssh, "-N", "-T", "-n", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3"]
    args += ["-o", "GatewayPorts=no", "-o", "ForkAfterAuthentication=no",
             "-o", "ControlMaster=no", "-o", "ControlPath=none"]
    if forward.ssh_config:
        args += ["-F", forward.ssh_config]
    if forward.direction == "local":
        args += ["-L", f"127.0.0.1:{forward.local_port}:{forward.endpoint}"]
    else:
        # Request remote loopback binding; server forwarding policy also applies.
        args += ["-R", f"127.0.0.1:{forward.forwarded_port}:{forward.endpoint}"]
    args.append(forward.ssh_host)
    return args


def _systemd_arg(value: str) -> str:
    # systemd.exec quoting: protect quotes/backslashes and suppress specifier expansion.
    value = value.replace("%", "%%").replace("$", "$$").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{value}"' if not re.fullmatch(r"[A-Za-z0-9_./:=+-]+", value) else value


def render_unit(forward: Forward, ssh: str | None = None) -> str:
    executable = ssh or shutil.which("ssh") or "/usr/bin/ssh"
    command = " ".join(_systemd_arg(arg) for arg in ssh_argv(forward, executable))
    return ("[Unit]\nDescription=SSH port forward: " + forward.name + "\nAfter=network-online.target\n\n"
            "[Service]\nType=exec\nExecStart=" + command + "\nRestart=on-failure\nRestartSec=5\n"
            "NoNewPrivileges=yes\n\n[Install]\nWantedBy=default.target\n")


def _run(*args: str, timeout: float = 20.0) -> dict[str, Any]:
    try:
        proc = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True,
                              timeout=timeout)
        return {"ok": proc.returncode == 0, "stdout": proc.stdout.strip(), "stderr": proc.stderr.strip(), "returncode": proc.returncode}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "stdout": "", "stderr": str(exc), "returncode": None}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sanitize_error(value: Any) -> str | None:
    if value is None:
        return None
    message = " ".join(str(value).split())
    message = re.sub(r"(?i)(password|token|secret|api[_-]?key)=\S+", r"\1=[redacted]", message)
    message = re.sub(r"[\x00-\x1f\x7f]", " ", message)
    message = " ".join(message.split())
    return message[:300] or None


def _safe_systemd_value(value: Any, *, limit: int = 64) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > limit or not re.fullmatch(r"[A-Za-z0-9 _:+.,-]+", value):
        return None
    return value


def _safe_systemd_count(value: Any) -> int | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,10}", value.strip()):
        return None
    return int(value)


class SSHForwardManager:
    """Small tray-callable API. No method starts/enables unless explicitly named."""
    def __init__(self, config: Path | None = None, unit_dir: Path | None = None):
        self.config = config or config_path()
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        self.unit_dir = unit_dir or base / "systemd" / "user"

    def list(self) -> list[Forward]:
        return load_forwards(self.config)

    def _write_entries(self, entries: list[dict[str, Any]]) -> None:
        # Validate the whole replacement before touching the existing file.
        parsed = [parse_forward(item) for item in entries]
        if len({item.name for item in parsed}) != len(parsed):
            raise ForwardConfigError("forward names must be unique")
        self.config.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".ssh-forwards-", dir=self.config.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"forwards": entries}, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.config)
            os.chmod(self.config, 0o600)
            directory_fd = os.open(self.config.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def add(self, entry: dict[str, Any]) -> Forward:
        """Validate and atomically append one new named forward."""
        forward = parse_forward(entry)
        entries = [self._entry_dict(item) for item in self.list()]
        if any(item["name"] == forward.name for item in entries):
            raise ForwardConfigError("forward name already exists")
        entries.append(self._entry_dict(forward))
        self._write_entries(entries)
        return forward

    def edit(self, name: str, entry: dict[str, Any]) -> Forward:
        """Replace a named entry, preserving its identity name."""
        current = self.list()
        if not any(item.name == name for item in current):
            raise ForwardConfigError("unknown forward name")
        state = self.status(name)
        if state["process_active"] is not False:
            raise ForwardConfigError("cannot edit while systemd state is active or unknown")
        if (self.unit_dir / unit_name(name)).exists():
            raise ForwardConfigError("uninstall the forward unit before editing its config")
        replacement = dict(entry)
        replacement["name"] = name
        forward = parse_forward(replacement)
        entries = [self._entry_dict(item) for item in current if item.name != name]
        entries.append(self._entry_dict(forward))
        self._write_entries(entries)
        return forward

    def delete(self, name: str) -> None:
        """Delete an uninstalled, definitely inactive config entry."""
        current = self.list()
        if not any(item.name == name for item in current):
            raise ForwardConfigError("unknown forward name")
        state = self.status(name)
        if state["installed"] or state["process_active"] is not False:
            raise ForwardConfigError("uninstall the unit and confirm it is inactive before deleting")
        self._write_entries([self._entry_dict(item) for item in current if item.name != name])

    def remove(self, name: str) -> dict[str, Any]:
        """Remove an inactive installed unit and its config entry; refuse active units."""
        forward = next((item for item in self.list() if item.name == name), None)
        if forward is None:
            raise ForwardConfigError("unknown forward name")
        target = self.unit_dir / unit_name(forward)
        had_unit = target.exists()
        if target.exists():
            result = self.uninstall(name)
            if not result.get("ok"):
                return result
        self.delete(name)
        return {"ok": True, "removed_config": True, "removed_unit": had_unit}

    @staticmethod
    def _entry_dict(item: Forward) -> dict[str, Any]:
        result: dict[str, Any] = {"name": item.name, "ssh_host": item.ssh_host,
                                  "direction": item.direction, "endpoint": item.endpoint}
        if item.local_port is not None:
            result["local_port"] = item.local_port
        if item.forwarded_port is not None:
            result["forwarded_port"] = item.forwarded_port
        if item.ssh_config is not None:
            result["ssh_config"] = item.ssh_config
        result["endpoint_path"] = item.endpoint_path
        return result

    def install(self, name: str) -> dict[str, Any]:
        forward = next((f for f in self.list() if f.name == name), None)
        if forward is None:
            raise ForwardConfigError("unknown forward name")
        target = self.unit_dir / unit_name(forward)
        state = self.status(name)
        if state["process_active"] is not False:
            return {"ok": False, "error": "active_or_unknown_unit_state", "unit": state["unit"]}
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".jev-ssh-forward-", dir=target.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(render_unit(forward))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        result = _run("daemon-reload")
        return {**result, "unit": unit_name(forward), "path": str(target)}

    def enable(self, name: str) -> dict[str, Any]:
        return _run("enable", unit_name(name))

    def disable(self, name: str) -> dict[str, Any]:
        return _run("disable", unit_name(name))

    def start(self, name: str) -> dict[str, Any]:
        return _run("start", unit_name(name))

    def stop(self, name: str) -> dict[str, Any]:
        return _run("stop", unit_name(name))

    def status(self, name: str) -> dict[str, Any]:
        forward = next((f for f in self.list() if f.name == name), None)
        if forward is None:
            raise ForwardConfigError("unknown forward name")
        unit = unit_name(forward)
        shown = _run(
            "show", unit,
            "--property=ActiveState", "--property=SubState", "--property=LoadState",
            "--property=Result", "--property=ExecMainStatus", "--property=NRestarts",
            "--property=ActiveEnterTimestamp", "--property=InactiveEnterTimestamp",
            timeout=2.0,
        )
        enabled = _run("is-enabled", unit, timeout=2.0)
        properties = dict(line.split("=", 1) for line in shown["stdout"].splitlines() if "=" in line)
        active_state = _safe_systemd_value(properties.get("ActiveState"), limit=32) or "unknown"
        sub_state = _safe_systemd_value(properties.get("SubState"), limit=32) or "unknown"
        load_state = _safe_systemd_value(properties.get("LoadState"), limit=32) or "unknown"
        systemd_result = _safe_systemd_value(properties.get("Result"), limit=32) or "unknown"
        exec_main_status = _safe_systemd_count(properties.get("ExecMainStatus"))
        restart_count = _safe_systemd_count(properties.get("NRestarts"))
        active_enter = _safe_systemd_value(properties.get("ActiveEnterTimestamp"))
        inactive_enter = _safe_systemd_value(properties.get("InactiveEnterTimestamp"))
        process_active = True if active_state == "active" else False if active_state in ("inactive", "failed") else None
        # systemctl show may return a nonzero status for an absent unit while
        # still providing structured LoadState=not-found. Missing-manager
        # failures have no such recognized property and remain unknown.
        if (process_active is None and load_state == "not-found" and
                active_state == "unknown" and shown["returncode"] in (0, 1)):
            process_active = False
        listening = None
        # A listening local socket only proves the local bind. It does not prove
        # the ultimate endpoint can accept connections.
        if forward.direction == "local" and process_active is True:
            try:
                with socket.create_connection(("127.0.0.1", forward.local_port), timeout=0.4):
                    listening = True
            except OSError:
                listening = False
        enabled_state = _safe_systemd_value(enabled["stdout"], limit=32) or "unknown"
        last_error = None
        if process_active is None:
            last_error = _sanitize_error(shown["stderr"] or shown.get("error"))
        if enabled_state == "unknown" and not last_error:
            last_error = _sanitize_error(enabled["stderr"] or enabled.get("error"))
        if systemd_result not in {"unknown", "success"}:
            status_text = f"systemd_result={systemd_result}"
            if exec_main_status not in (None, 0):
                status_text += f" exec_main_status={exec_main_status}"
            last_error = status_text
        elif active_state == "failed" and not last_error:
            last_error = "unit_failed"
        if sub_state in {"auto-restart", "restart-scheduled", "activating"} or (
                active_state == "activating" and load_state == "loaded"):
            lifecycle_state = "reconnecting"
        elif process_active is True:
            lifecycle_state = "active"
        elif active_state == "failed":
            lifecycle_state = "failed"
        elif process_active is False and (self.unit_dir / unit).is_file():
            lifecycle_state = "inactive"
        elif process_active is False:
            lifecycle_state = "not_installed"
        else:
            lifecycle_state = "unknown"
        process_health = ("active" if process_active is True else
                          "inactive" if process_active is False else "unavailable")
        return {"name": name, "unit": unit, "direction": forward.direction,
                "ssh_host": forward.ssh_host, "endpoint": forward.endpoint,
                "endpoint_path": forward.endpoint_path,
                "checked_at": _utc_now(),
                "stale": False,
                "unavailable": process_active is None,
                "installed": (self.unit_dir / unit).is_file(),
                "lifecycle_state": lifecycle_state,
                "process_active": process_active,
                "process_health": process_health,
                "active_state": active_state,
                "sub_state": sub_state,
                "load_state": load_state,
                "systemd_result": systemd_result,
                "exec_main_status": exec_main_status,
                "restart_count": restart_count,
                "active_enter_timestamp": active_enter,
                "inactive_enter_timestamp": inactive_enter,
                "enabled": enabled_state == "enabled",
                "enabled_state": enabled_state,
                "local_listener_reachable": listening,
                "tcp_health": ("reachable" if listening else "unreachable") if listening is not None else "not_checked",
                "local_url": (f"http://127.0.0.1:{forward.local_port}{forward.endpoint_path}"
                              if forward.direction == "local" else None),
                "remote_url": (f"http://127.0.0.1:{forward.forwarded_port}{forward.endpoint_path}"
                               if forward.direction == "reverse" else None),
                "mcp_health": "not_checked",
                "last_error": last_error,
                "endpoint_health": "not_checked",
                "systemd_query_ok": shown["ok"],
                "systemd_returncode": shown["returncode"]}

    def snapshot(self) -> dict[str, Any]:
        """Read-only aggregate suitable for tray display or owner relay.

        Each configured entry gets its own error boundary so one broken unit
        does not hide the rest. Only local loopback TCP listener reachability is
        checked; no SSH host or MCP method is contacted.
        """
        checked_at = _utc_now()
        try:
            forwards = self.list()
        except Exception as exc:
            return {"checked_at": checked_at, "stale": False, "available": False, "forwards": [],
                    "config_error": _sanitize_error(exc)}
        results = []
        for forward in forwards:
            try:
                state = self.status(forward.name)
                state["last_error"] = _sanitize_error(state.get("last_error"))
                results.append(state)
            except Exception as exc:
                results.append({
                    "name": forward.name,
                    "direction": forward.direction,
                    "checked_at": checked_at,
                    "stale": False,
                    "unavailable": True,
                    "lifecycle_state": "unknown",
                    "process_active": None,
                    "process_health": "unavailable",
                    "installed": None,
                    "enabled": None,
                    "tcp_health": "not_checked",
                    "mcp_health": "not_checked",
                    "local_url": (f"http://127.0.0.1:{forward.local_port}{forward.endpoint_path}"
                                  if forward.direction == "local" else None),
                    "remote_url": (f"http://127.0.0.1:{forward.forwarded_port}{forward.endpoint_path}"
                                   if forward.direction == "reverse" else None),
                    "last_error": _sanitize_error(exc),
                })
        return {"checked_at": checked_at, "stale": False,
                "available": True, "forwards": results, "config_error": None}

    def uninstall(self, name: str) -> dict[str, Any]:
        # Refuse to remove a running unit; stopping is an explicit separate action.
        state = self.status(name)
        if state["process_active"] is not False:
            return {"ok": False, "error": "active_or_unknown_unit_state", "unit": state["unit"]}
        if state["installed"] and state["enabled_state"] not in ("enabled", "disabled"):
            return {"ok": False, "error": "unknown_enable_state", "unit": state["unit"]}
        disabled = self.disable(name)
        if not disabled["ok"]:
            return {"ok": False, "error": "disable_failed_unit_file_preserved",
                    "unit": state["unit"], "disabled": disabled}
        target = self.unit_dir / state["unit"]
        if target.exists():
            target.unlink()
        reload_result = _run("daemon-reload")
        return {"ok": disabled["ok"] and reload_result["ok"], "disabled": disabled, "daemon_reload": reload_result}

