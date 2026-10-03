"""Single local owner for Jev desktop sessions, exposed over a private Unix socket."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import socket
import socketserver
import stat
import sys
import threading
import re
from pathlib import Path
from typing import Any

MAX_REQUEST_BYTES = 1_048_576
MAX_RESPONSE_BYTES = 8_388_608
SOCKET_ENV = "JEV_DESKTOP_SOCKET"
CONFIG_ENV = "JEV_DESKTOP_CONFIG"
ALLOW_APPS_ENV = "JEV_DESKTOP_ALLOW_APPS"

# Configuration carries no secrets. A reader key is read from the environment
# or the configured dotenv file; the config only names the environment variable.
_CONFIG_KEYS = frozenset({
    "allowed_apps", "run_dir", "audit", "socket", "dotenv",
    "idle_timeout", "max_session_lifetime", "max_actions", "max_observations", "reader",
})
_READER_KEYS = frozenset({
    "provider", "base_url", "model", "key_env", "timeout_seconds",
    "total_timeout_seconds", "max_calls_per_session",
})
_NUMBER_DEFAULTS = {
    "idle_timeout": 180.0,
    "max_session_lifetime": 1800.0,
    "max_actions": 64,
    "max_observations": 256,
}


def default_run_dir() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "jev-desktop"
    return Path("/tmp") / f"jev-desktop-{os.getuid()}"


def default_socket_path() -> Path:
    configured = os.environ.get(SOCKET_ENV)
    return Path(configured) if configured else default_run_dir() / "owner.sock"


def default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "jev-desktop" / "config.json"


def load_config(path: Path) -> dict[str, Any]:
    """Load the private JSON config. Unknown keys are rejected so typos fail fast."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise RuntimeError("config file could not be read") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("config file is not valid JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError("config file must contain a JSON object")
    unknown = sorted(set(data) - _CONFIG_KEYS)
    if unknown:
        raise RuntimeError(f"config file has unknown keys: {', '.join(unknown)}")
    apps = data.get("allowed_apps")
    if apps is not None and (not isinstance(apps, list) or not all(isinstance(item, str) for item in apps)):
        raise RuntimeError("config allowed_apps must be a list of strings")
    reader = data.get("reader")
    if reader is not None:
        if not isinstance(reader, dict):
            raise RuntimeError("config reader must be an object")
        unknown_reader = sorted(set(reader) - _READER_KEYS)
        if unknown_reader:
            raise RuntimeError(f"config reader has unknown keys: {', '.join(unknown_reader)}")
        if reader.get("provider") not in (None, "deepseek", "mimo", "generic"):
            raise RuntimeError("config reader provider must be deepseek, mimo or generic")
    for key in _NUMBER_DEFAULTS:
        value = data.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise RuntimeError(f"config {key} must be a number")
    for key in ("run_dir", "audit", "socket", "dotenv"):
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            raise RuntimeError(f"config {key} must be a string path")
    return data


def _env_allowed_apps() -> list[str]:
    raw = os.environ.get(ALLOW_APPS_ENV, "")
    return [item for item in re.split(r"[,\s]+", raw.strip()) if item]


def _resolve_config_path(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    configured = os.environ.get(CONFIG_ENV)
    return Path(configured) if configured else default_config_path()


def _apply_config(args: argparse.Namespace, config: dict[str, Any]) -> None:
    """Merge config under CLI values; CLI flags and env keep precedence."""
    args.allowed_apps = list(args.allow_app) or _env_allowed_apps() or list(config.get("allowed_apps") or [])
    if args.run_dir is None and config.get("run_dir"):
        args.run_dir = Path(config["run_dir"])
    if args.audit is None and config.get("audit"):
        args.audit = Path(config["audit"])
    if args.socket is None and config.get("socket"):
        args.socket = Path(config["socket"])
    if args.dotenv is None:
        args.dotenv = Path(config["dotenv"]) if config.get("dotenv") else Path(".env")
    for key, fallback in _NUMBER_DEFAULTS.items():
        value = getattr(args, key)
        if value is None:
            setattr(args, key, config.get(key, fallback))
    reader = config.get("reader") or {}
    if args.reader_provider is None:
        args.reader_provider = reader.get("provider")
    if args.reader_base_url is None:
        args.reader_base_url = reader.get("base_url")
    if args.reader_model is None:
        args.reader_model = reader.get("model")
    if args.reader_key_env is None:
        args.reader_key_env = reader.get("key_env")
    if args.reader_timeout is None:
        args.reader_timeout = reader.get("timeout_seconds", 10.0)
    if args.reader_total_timeout is None:
        args.reader_total_timeout = reader.get("total_timeout_seconds", 15.0)
    if args.max_reader_calls is None:
        args.max_reader_calls = reader.get("max_calls_per_session")


def _safe_error(code: str, message: str, retryable: bool = False) -> dict[str, Any]:
    return {"code": code[:80], "message": message[:300], "retryable": retryable}


class DesktopSocketServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = False
    block_on_close = True

    def __init__(self, socket_path: Path, service: Any):
        self.service = service
        self.socket_path = socket_path
        self.shutdown_requested = threading.Event()
        self.closing = threading.Event()
        super().__init__(str(socket_path), RequestHandler)
        self.socket_identity = (socket_path.stat().st_dev, socket_path.stat().st_ino)

    def dispatch(self, method: str, params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        if method == "shutdown":
            if params:
                return {"ok": False, "error": _safe_error("invalid_params", "shutdown takes no parameters")}
            if self.closing.is_set():
                return {"ok": False, "error": _safe_error("owner_closing", "desktop owner is shutting down", True)}
            self.closing.set()
            try:
                result = self.service.dispatch("stop_all", {}, context)
            except Exception:
                result = {"ok": False, "error": _safe_error("owner_error", "owner shutdown failed")}
            self.shutdown_requested.set()
            threading.Thread(target=self.shutdown, daemon=True).start()
            return result if isinstance(result, dict) else {"ok": True, "result": result}
        if self.closing.is_set():
            return {"ok": False, "error": _safe_error("owner_closing", "desktop owner is shutting down", True)}
        try:
            result = self.service.dispatch(method, params, context)
        except Exception:
            return {"ok": False, "error": _safe_error("owner_error", "desktop owner request failed")}
        if not isinstance(result, dict):
            return {"ok": False, "error": _safe_error("invalid_owner_response", "desktop owner returned an invalid response")}
        return result


class RequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            self.connection.settimeout(5.0)
            raw = self.rfile.readline(MAX_REQUEST_BYTES + 1)
            if not raw or len(raw) > MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
                self._write({"id": None, "error": _safe_error("invalid_request", "request line is missing or too large")})
                return
            try:
                request = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._write({"id": None, "error": _safe_error("invalid_json", "request is not valid JSON")})
                return
            if not isinstance(request, dict) or not isinstance(request.get("id"), (str, int)):
                self._write({"id": None, "error": _safe_error("invalid_request", "request must include an id")})
                return
            method = request.get("method")
            params = request.get("params", {})
            context = request.get("context", {})
            if (not isinstance(method, str) or len(method) > 80 or not isinstance(params, dict)
                    or not isinstance(context, dict)):
                self._write({"id": request["id"], "error": _safe_error("invalid_request", "method, params or context has an invalid shape")})
                return
            result = self.server.dispatch(method, params, context)  # type: ignore[attr-defined]
            self._write({"id": request["id"], "result": result})
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def _write(self, response: dict[str, Any]) -> None:
        encoded = (json.dumps(response, separators=(",", ":"), ensure_ascii=True) + "\n").encode()
        if len(encoded) > MAX_RESPONSE_BYTES:
            encoded = (json.dumps({"id": response.get("id"), "error": _safe_error("response_too_large", "response exceeds the IPC limit")}) + "\n").encode()
        self.wfile.write(encoded)
        self.wfile.flush()


class OwnerLock:
    """flock owns a stable lock file; the file itself is never unlinked."""

    def __init__(self, path: Path):
        self.path = path
        self.fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_info = self.path.parent.stat()
        if parent_info.st_uid != os.getuid() or stat.S_IMODE(parent_info.st_mode) & 0o077:
            raise RuntimeError("IPC directory must be owned by this user and private (mode 0700)")
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0), 0o600)
        os.fchmod(self.fd, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self.fd)
            self.fd = None
            raise RuntimeError("desktop owner is already running") from exc

    def close(self) -> None:
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


def _remove_own_socket(server: DesktopSocketServer) -> None:
    try:
        info = server.socket_path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISSOCK(info.st_mode) and (info.st_dev, info.st_ino) == server.socket_identity:
        server.socket_path.unlink()


def _make_service(args: argparse.Namespace) -> Any:
    from desktop_service import DesktopService

    allowed = getattr(args, "allowed_apps", None)
    if allowed is None:
        allowed = getattr(args, "allow_app", [])
    allowed_apps = frozenset(app.casefold() for app in allowed)
    if not allowed_apps:
        raise RuntimeError("an explicit allowlist is required: use --allow-app, JEV_DESKTOP_ALLOW_APPS or allowed_apps in the config file")
    audit_path = args.audit or str((args.run_dir or default_run_dir()) / "audit.jsonl")
    reader = None
    if args.reader_provider:
        if args.max_reader_calls is None or args.max_reader_calls <= 0:
            raise RuntimeError("--reader-provider requires a positive --max-reader-calls opt-in cap")
        from vision_reader import IsolatedVisionReader, ReaderConfig

        defaults = {
            "deepseek": ("https://api.deepseek.com", "deepseek-flash", "DEEPSEEK_API_KEY"),
            "mimo": ("https://api.xiaomimimo.com/v1", "mimo-v2.6-flash", "MIMO_API_KEY"),
            "generic": (None, None, None),
        }
        default_url, default_model, default_key = defaults[args.reader_provider]
        base_url = args.reader_base_url or default_url
        model = args.reader_model or default_model
        key_env = args.reader_key_env or default_key
        if not base_url or not model or not key_env:
            raise RuntimeError("generic reader requires --reader-base-url, --reader-model and --reader-key-env")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", key_env):
            raise RuntimeError("--reader-key-env must be an environment variable name")
        try:
            config = ReaderConfig(provider=args.reader_provider, base_url=base_url, model=model, key_env=key_env,
                                  timeout_seconds=args.reader_timeout,
                                  total_timeout_seconds=args.reader_total_timeout)
        except ValueError as exc:
            raise RuntimeError("reader configuration is invalid") from exc
        if not os.environ.get(key_env):
            _load_dotenv_key(args.dotenv, key_env)
        if not os.environ.get(key_env):
            raise RuntimeError("reader API key is not available in the environment or configured dotenv file")
        reader = IsolatedVisionReader(config)
    if not args.reader_provider and (args.max_reader_calls is not None or args.reader_base_url or args.reader_model or args.reader_key_env):
        raise RuntimeError("reader options require --reader-provider")
    return DesktopService(audit_path=audit_path, allowed_apps=allowed_apps, reader=reader,
                          max_reader_calls=args.max_reader_calls or 0,
                          idle_timeout=args.idle_timeout,
                          max_session_lifetime=args.max_session_lifetime,
                          max_actions=args.max_actions,
                          max_observations=args.max_observations)


def _load_dotenv_key(path: Path | None, key_name: str) -> None:
    """Load only the configured API key from a simple dotenv file, without printing it."""
    if path is None:
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() == key_name and key_name not in os.environ:
            os.environ[key_name] = value.strip().strip("\"'")
            return


def serve(*, socket_path: Path, service: Any, stop_event: threading.Event | None = None) -> None:
    socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = OwnerLock(socket_path.with_name(socket_path.name + ".lock"))
    lock.acquire()
    server = None
    cleanup_error: BaseException | None = None
    try:
        try:
            info = socket_path.lstat()
        except FileNotFoundError:
            pass
        else:
            # With the exclusive lock held, a same-user socket at this configured
            # endpoint can only be stale. Never remove regular files or foreign sockets.
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise RuntimeError("refusing to replace an unowned IPC path")
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(0.15)
                    probe.connect(str(socket_path))
                raise RuntimeError("desktop socket is serving despite the owner lock")
            except (ConnectionRefusedError, FileNotFoundError, TimeoutError):
                socket_path.unlink()
        server = DesktopSocketServer(socket_path, service)
        os.chmod(socket_path, 0o600)
        if stop_event is not None:
            def waiter() -> None:
                stop_event.wait()
                threading.Thread(target=server.shutdown, daemon=True).start()
            threading.Thread(target=waiter, daemon=True).start()
        server.serve_forever(poll_interval=0.1)
    finally:
        if server is not None:
            server.server_close()
            _remove_own_socket(server)
        close = getattr(service, "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:
                cleanup_error = exc
        lock.close()
    if cleanup_error is not None:
        raise RuntimeError("desktop owner cleanup failed") from cleanup_error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the single-owner local desktop service.")
    parser.add_argument("--foreground", action="store_true", help="run in the current process (the only supported daemon mode)")
    parser.add_argument("--socket", type=Path, default=None, help="Unix socket path; defaults to JEV_DESKTOP_SOCKET or the local runtime directory")
    parser.add_argument("--run-dir", type=Path, default=None, help="owner audit/runtime directory (use a private local directory)")
    parser.add_argument("--audit", type=Path, default=None, help="append-only JSONL audit path")
    parser.add_argument("--config", type=Path, default=None,
                        help=f"private JSON config (default: ${CONFIG_ENV} or the XDG config path); CLI flags win")
    parser.add_argument("--allow-app", action="append", default=[], help="explicitly allow one app identity; deny by default")
    parser.add_argument("--idle-timeout", type=float, default=None,
                        help="stop an inactive virtual session after this many seconds (default: 180)")
    parser.add_argument("--max-session-lifetime", type=float, default=None,
                        help="maximum virtual session lifetime in seconds (default: 1800)")
    parser.add_argument("--max-actions", type=int, default=None,
                        help="maximum verified action attempts per session (default: 64)")
    parser.add_argument("--max-observations", type=int, default=None,
                        help="maximum observe/candidate reads per session (default: 256)")
    parser.add_argument("--reader-provider", choices=("deepseek", "mimo", "generic"), default=None,
                        help="enable structured interpretation of the same app capture returned to the caller")
    parser.add_argument("--reader-base-url", default=None, help="OpenAI-compatible reader endpoint base URL")
    parser.add_argument("--reader-model", default=None, help="reader model name")
    parser.add_argument("--reader-key-env", default=None, help="environment variable name holding the reader key; its value is never displayed")
    parser.add_argument("--dotenv", type=Path, default=None, help="optional dotenv file for the configured reader key; key values are never displayed")
    parser.add_argument("--reader-timeout", type=float, default=None,
                        help="HTTP connect/read idle timeout in seconds (default: 10)")
    parser.add_argument("--reader-total-timeout", type=float, default=None,
                        help="absolute response-body deadline in seconds (default: 15; header trickle is bounded per idle gap only)")
    parser.add_argument("--max-reader-calls", type=int, default=None,
                        help="positive per-session provider call cap; required to enable a reader")
    args = parser.parse_args(argv)
    if not args.foreground:
        parser.error("daemon requires --foreground; automatic background spawning is disabled")
    try:
        _apply_config(args, load_config(_resolve_config_path(args.config)))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    args.run_dir = args.run_dir or default_run_dir()
    socket_path = args.socket or (Path(os.environ[SOCKET_ENV]) if os.environ.get(SOCKET_ENV) else args.run_dir / "owner.sock")
    try:
        service = _make_service(args)
        stopping = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stopping.set())
        signal.signal(signal.SIGINT, lambda *_: stopping.set())
        serve(socket_path=socket_path, service=service, stop_event=stopping)
        return 0
    except (RuntimeError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
