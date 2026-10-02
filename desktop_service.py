"""Single-owner, virtual-only computer-use service over a private kwin-mcp child."""

from __future__ import annotations

import base64
import json
import os
import re
import select
import signal
import subprocess
import sys
import threading
import tempfile
import time
import uuid
from urllib.parse import urlsplit
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from observation import ObservationAdapter, ObservationError
from owner_wait import run_owner_wait
from vision_reader import BoundedReader, ReaderResult, _usage_totals
from transactions import (Action, AuditLog, AutonomyMode, Candidate, KwinMcpBackend,
                          Policy, TransactionEngine, TransactionError, Verification, _scroll_witnesses)


APPS = frozenset({"kate", "firefox", "kcalc"})
METHODS = frozenset({"capabilities", "status", "session_start", "session_stop", "observe",
                     "wait", "candidates", "act", "cancel", "stop_all"})


def _safe_error(value: str) -> dict[str, str]:
    code = value if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value or "") else "operation_failed"
    messages = {
        "session_busy": "The virtual session is handling another request.",
        "session_exists": "A virtual session is already active.",
        "session_not_found": "No virtual session is active.",
        "app_not_allowed": "The requested app is not allowed.",
        "invalid_params": "Request parameters are invalid.",
        "unsupported_verification": "The requested verification is not supported.",
        "approval_required": "This action requires explicit approval.",
        "worker_failed": "The virtual session worker failed.",
        "worker_timeout": "The virtual session worker timed out.",
        "worker_protocol_error": "The virtual session worker returned an invalid response.",
        "virtual_session_unavailable": "The virtual desktop session could not be started.",
        "atspi_setup_failed": "Accessibility setup for the virtual session failed.",
        "document_create_failed": "The task document could not be created safely.",
        "document_open_unconfirmed": "Kate did not confirm that the task document opened.",
        "navigation_url_not_allowed": "Browser navigation accepts only an HTTP or HTTPS URL without embedded credentials.",
        "target_ambiguous": "This accessible control is ambiguous; choose a uniquely named fresh target.",
        "scroll_position_unavailable": "The app does not expose a measurable scroll position, so scrolling was not attempted.",
        "scroll_unsupported": "The current driver does not support this scroll action.",
        "scroll_argument_invalid": "Scroll direction or step count is invalid.",
        "capture_failed": "The application screenshot could not be captured.",
        "window_not_found": "The requested application window was not ready or could not be found.",
        "window_ambiguous": "More than one application window matched the request.",
        "mapping_unavailable": "Screenshot coordinate mapping was unavailable.",
        "session_broken": "The virtual session is broken and must be stopped.",
        "cancelled": "The operation was cancelled.",
    }
    return {"code": code, "message": messages.get(code, "The requested operation failed safely.")}


def _context(value: Any, key: str) -> str:
    raw = value.get(key) if isinstance(value, dict) else None
    return raw if isinstance(raw, str) and re.fullmatch(r"[A-Za-z0-9_.:@/-]{1,128}", raw) else "unknown"


def _same_address_destination(observed: Any, requested: str) -> bool:
    """Compare Firefox's omnibox text, which commonly hides the HTTPS scheme."""
    if not isinstance(observed, str) or not observed:
        return False
    expected = urlsplit(requested)
    actual = urlsplit(observed if "://" in observed else f"{expected.scheme}://{observed}")
    try:
        return (actual.hostname is not None and actual.hostname.casefold() == (expected.hostname or "").casefold()
                and actual.port == expected.port and (actual.path.rstrip("/") or "/") == (expected.path.rstrip("/") or "/")
                and actual.query == expected.query and actual.fragment == expected.fragment
                and actual.username is None and actual.password is None)
    except ValueError:
        return False


class DesktopWorkerClient:
    """Synchronous, serialized JSON RPC to one isolated Python driver child."""

    def __init__(self, *, timeout: float = 20.0):
        self.timeout = timeout
        self.proc = subprocess.Popen([sys.executable, str(Path(__file__).with_name("desktop_worker.py"))],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, bufsize=1, start_new_session=True)
        self._lock = threading.Lock()
        self._seq = 0
        self.cleanup_paths: list[str] = []
        self._poisoned = False
        self.last_cleanup_confirmed = False

    def rpc(self, method: str, **params):
        if self._poisoned or self.proc.poll() is not None or self.proc.stdin is None or self.proc.stdout is None:
            raise RuntimeError("worker_failed")
        with self._lock:
            self._seq += 1
            ident = self._seq
            request = json.dumps({"id": ident, "method": method, "params": params}, separators=(",", ":"))
            self.proc.stdin.write(request + "\n")
            self.proc.stdin.flush()
            ready = _readline_timeout(self.proc.stdout, self.timeout)
            if not ready:
                self._poisoned = True
                raise TimeoutError("worker_timeout")
            try:
                response = json.loads(ready)
            except json.JSONDecodeError:
                raise RuntimeError("worker_protocol_error") from None
            if response.get("id") != ident:
                raise RuntimeError("worker_protocol_error")
            if "error" in response:
                paths = response["error"].get("cleanup_paths")
                if isinstance(paths, list) and all(isinstance(p, str) for p in paths): self.cleanup_paths = paths
                self.last_cleanup_confirmed = response["error"].get("cleanup_confirmed") is True
                raise RuntimeError(response["error"].get("code", "worker_failed"))
            return response.get("result")

    def start(self, app: str, journal_path: str | None = None):
        result = self.rpc("start", app=app, journal_path=journal_path)
        self.cleanup_paths = result.get("cleanup_paths", []) if isinstance(result, dict) else []
        return result

    def stop(self):
        result = self.rpc("stop")
        self.cleanup_paths = []
        self.journal_path = None
        return result

    def terminate(self):
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=1.5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                if self.proc.poll() is None:
                    try:
                        os.killpg(self.proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    try:
                        self.proc.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        pass
        # Keep the cleanup journal until the caller has checked the recorded
        # private paths after the worker group is gone.

    def cleanup_recorded_paths(self):
        import shutil
        outcomes = []
        for value in list(self.cleanup_paths):
            path = Path(value)
            temp_root = Path(tempfile.gettempdir()).resolve()
            try: resolved = path.resolve(strict=False)
            except OSError:
                outcomes.append(False); continue
            allowed = ((path.name.startswith("jev-desktop-draft-") and path.suffix == ".txt") or
                       path.name.startswith("jev-desktop-firefox-"))
            if not allowed or resolved.parent != temp_root or path.is_symlink():
                outcomes.append(False)
                continue
            if not path.exists(): outcomes.append(True); continue
            try:
                if path.is_dir(): shutil.rmtree(path)
                else: path.unlink()
                outcomes.append(not path.exists())
            except OSError:
                outcomes.append(False)
        paths_removed = bool(outcomes) and all(outcomes)
        if paths_removed: self.cleanup_paths.clear()
        # A removed profile/draft proves file cleanup only. kwin-mcp may have
        # detached its KWin, bus and app process groups, so this does not prove
        # that the virtual session itself stopped.
        return False

    @staticmethod
    def _proc_info(pid):
        try:
            stat = Path(f"/proc/{int(pid)}/stat").read_text()
            fields = stat[stat.rfind(")") + 2:].split()
            return {"state": fields[0], "pgrp": int(fields[2]), "start_ticks": int(fields[19])}
        except (OSError, ValueError, IndexError, TypeError): return None

    @classmethod
    def _proc_start_ticks(cls, pid):
        info = cls._proc_info(pid)
        return info["start_ticks"] if info else None

    @classmethod
    def _group_live(cls, pgid):
        try:
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit(): continue
                info = cls._proc_info(entry.name)
                if info and info["pgrp"] == pgid and info["state"] not in {"Z", "X"}: return True
        except OSError: return True
        return False

    @classmethod
    def _same_live_process(cls, pid, start_ticks):
        info = cls._proc_info(pid)
        return bool(info and info["start_ticks"] == start_ticks and info["state"] not in {"Z", "X"})

    def recover_cleanup(self):
        """Use only the worker's 0600 exact-PID/session-path journal after death."""
        journal = getattr(self, "journal_path", None)
        if journal is None or not journal.is_file(): return False
        try:
            record = json.loads(journal.read_text(encoding="utf-8"))
            if record.get("schema") != 1 or record.get("worker_pid") != self.proc.pid: return False
            pid, ticks = record.get("session_pid"), record.get("session_start_ticks")
            initial = self._proc_info(pid)
            if type(pid) is not int or type(ticks) is not int: return False
            if initial and initial["start_ticks"] == ticks and initial["pgrp"] == pid:
                os.killpg(pid, signal.SIGTERM)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and self._group_live(pid):
                    time.sleep(0.05)
                if self._group_live(pid):
                    os.killpg(pid, signal.SIGKILL)
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline and self._group_live(pid):
                        time.sleep(0.05)
            elif self._group_live(pid):
                return False
            for app in record.get("app_processes", []):
                app_pid, app_ticks = app.get("pid"), app.get("start_ticks")
                if type(app_pid) is int and type(app_ticks) is int and self._proc_start_ticks(app_pid) == app_ticks:
                    os.kill(app_pid, signal.SIGTERM)
                    deadline = time.monotonic() + 1
                    while time.monotonic() < deadline and self._same_live_process(app_pid, app_ticks):
                        time.sleep(0.05)
                    if self._same_live_process(app_pid, app_ticks): os.kill(app_pid, signal.SIGKILL)
                    if self._same_live_process(app_pid, app_ticks): return False
            atspi_pid, atspi_ticks = record.get("atspi_pid"), record.get("atspi_start_ticks")
            if type(atspi_pid) is int and type(atspi_ticks) is int and self._proc_start_ticks(atspi_pid) == atspi_ticks:
                os.kill(atspi_pid, signal.SIGTERM)
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline and self._same_live_process(atspi_pid, atspi_ticks):
                    time.sleep(0.05)
                if self._same_live_process(atspi_pid, atspi_ticks): os.kill(atspi_pid, signal.SIGKILL)
                if self._same_live_process(atspi_pid, atspi_ticks): return False
            if self._group_live(pid): return False
            private_paths = record.get("private_paths", [])
            if not isinstance(private_paths, list) or not all(isinstance(value, str) for value in private_paths): return False
            self.cleanup_paths = private_paths
            home_value = record.get("home_dir", "")
            if not isinstance(home_value, str): return False
            temp_root = Path(tempfile.gettempdir()).resolve()
            home = Path(home_value) if home_value else None
            if home is not None and home.exists() and (home.name.startswith("kwin-mcp-home-")
                    and home.resolve(strict=False).parent == temp_root and not home.is_symlink()):
                import shutil
                shutil.rmtree(home)
            elif home is not None and home.exists(): return False
            config_value = record.get("config_dir", "")
            if not isinstance(config_value, str): return False
            config = Path(config_value) if config_value else None
            if config is not None and config.exists():
                if (not config.name.startswith("kwin-mcp-config-") or config.resolve(strict=False).parent != temp_root
                        or config.is_symlink()): return False
                import shutil
                shutil.rmtree(config)
            runtime_value = record.get("runtime_dir", "")
            expected_runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
            if runtime_value != expected_runtime: return False
            runtime = Path(runtime_value)
            socket_name = record.get("socket_name", "")
            if socket_name.startswith("wayland-mcp-") and runtime.is_dir():
                for suffix in ("", ".lock"):
                    (runtime / f"{socket_name}{suffix}").unlink(missing_ok=True)
            if self.cleanup_recorded_paths() is False and self.cleanup_paths: return False
            journal.unlink(missing_ok=True)
            self.journal_path = None
            return True
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return False

    def close(self):
        self.terminate()
        recovered = self.recover_cleanup()
        for stream in (self.proc.stdin, self.proc.stdout):
            try:
                if stream: stream.close()
            except OSError: pass
        return recovered

    def window_query(self): return self.rpc("window_query")
    def atspi_find(self, app): return self.rpc("atspi_find", app=app)
    def window_geometry(self, app=None, *, app_name=None): return self.rpc("window_geometry", app=app or app_name)
    def active_window(self): return self.rpc("active_window")
    def screenshot(self, include_cursor=False): return self.rpc("screenshot")
    def mouse_click(self, x, y, button="left"): return self.rpc("mouse_click", x=x, y=y)
    def mouse_scroll(self, x, y, delta, *, horizontal=False, discrete=True, steps=1):
        if (horizontal or not discrete or type(steps) is not int or not 1 <= steps <= 8
                or type(delta) is not int or delta not in {-8, -7, -6, -5, -4, -3, -2, -1,
                                                            1, 2, 3, 4, 5, 6, 7, 8}):
            raise ValueError("scroll_argument_invalid")
        return self.rpc("mouse_scroll", x=x, y=y, delta=delta, steps=steps)
    def keyboard_type(self, text): return self.rpc("keyboard_type", text=text)
    def keyboard_type_unicode(self, text): return self.keyboard_type(text)
    def keyboard_key(self, key):
        if key not in {"Return", "ctrl+a"}: raise ValueError("unsupported_key")
        return self.rpc("keyboard_key", key=key)
    def document_key(self, operation):
        if operation not in {"select_all", "save"}:
            raise ValueError("invalid_document_operation")
        return self.rpc("document_key", operation=operation)
    def document_bytes(self):
        result = self.rpc("document_bytes")
        encoded = result.get("utf8_base64") if isinstance(result, dict) else None
        if not isinstance(encoded, str) or len(encoded) > (1_048_576 * 4 // 3 + 8):
            raise RuntimeError("worker_protocol_error")
        try:
            data = base64.b64decode(encoded, validate=True)
            if len(data) > 1_048_576:
                raise ValueError
            data.decode("utf-8")
            return data
        except (ValueError, UnicodeDecodeError):
            raise RuntimeError("worker_protocol_error") from None
    def _run_kwin_query(self, params): return self.window_query()
    def _run_atspi(self, command, **params): return self.atspi_find(params["app_name"])
    def accessibility_elements(self, app): return self.atspi_find(app)["result"]
    def _get_session(self):
        class Info: session_type = type("SessionType", (), {"value": "virtual"})()
        return type("Session", (), {"info": Info()})()


def _readline_timeout(stream, timeout):
    ready, _, _ = select.select([stream], [], [], timeout)
    return stream.readline() if ready else ""


class _Session:
    def __init__(self, app, mode, worker, session_id, reader=None, audit_path=None, max_reader_calls=0,
                 *, idle_timeout=180.0, max_session_lifetime=1800.0, max_actions=64,
                 max_observations=256, monotonic=time.monotonic):
        self.app = app
        self.mode = mode
        self.worker = worker
        self.session_id = session_id
        self.audit_path = Path(audit_path) if audit_path is not None else Path(__file__).parent / "run" / "desktop-service" / "actions.jsonl"
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.started_clock = monotonic()
        self.last_activity_clock = self.started_clock
        self.last_activity_at = self.started_at
        self.idle_timeout = idle_timeout
        self.max_session_lifetime = max_session_lifetime
        self.max_actions = max_actions
        self.max_observations = max_observations
        self.monotonic = monotonic
        self.state = "running"
        self.busy = threading.Lock()
        self.stopping = False
        self.cancel = threading.Event()
        self.expiring = False
        self.document_path = None
        self.actions = 0
        self.observations = 0
        self.reader_budget = (BoundedReader(reader, max_reader_calls, on_attempt_event=self._reader_attempt_event,
                                            cancel_event=self.cancel)
                              if reader is not None and max_reader_calls > 0 else None)
        self.max_reader_calls = self.reader_budget.max_calls if self.reader_budget else 0
        self.adapter = ObservationAdapter(worker, session_id, reader=self.reader_budget, allowed_apps={app})
        self.tx = TransactionEngine(KwinMcpBackend(worker), audit=AuditLog(audit_path),
                                    policy=Policy(allowed_apps=APPS, max_actions=max_actions,
                                                  max_observations=max_observations,
                                                  max_duration_seconds=max_session_lifetime,
                                                  max_text_chars=4096))

    @property
    def reader_calls(self):
        return self.reader_budget.summary()["calls_used"] if self.reader_budget else 0

    def _reader_attempt_event(self, event: str, ordinal: int, result: ReaderResult | None) -> None:
        context = getattr(getattr(self, "tx", None), "audit", None)
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "reader_call_started" if event == "started" else "reader_call_completed",
            "session_id": self.session_id,
            "caller_node": getattr(context, "caller_node", "unknown"),
            "transport": getattr(context, "transport", "unknown"),
            "provider": self.reader_budget.provider if self.reader_budget else "",
            "model": self.reader_budget.model if self.reader_budget else "",
            "call_ordinal": ordinal,
            "status": "pending" if result is None else (result.status if result.status in {"ok", "uncertain", "error", "invalid_response"} else "error"),
        }
        if result is not None:
            payload["error"] = result.error if isinstance(result.error, str) and re.fullmatch(r"[a-zA-Z0-9_]{1,80}", result.error) else None
            payload["served_model"] = result.served_model if isinstance(result.served_model, str) else None
            payload["latency_ms"] = (round(result.latency_ms, 2) if isinstance(result.latency_ms, (int, float))
                                     and not isinstance(result.latency_ms, bool) and 0 <= result.latency_ms < 1e9 else None)
            payload["owner_elapsed_ms"] = (round(result.owner_elapsed_ms, 2) if isinstance(result.owner_elapsed_ms, (int, float))
                                           and not isinstance(result.owner_elapsed_ms, bool) and 0 <= result.owner_elapsed_ms < 1e9 else None)
            payload["reported_usage"] = _usage_totals(result.usage)
        data = (json.dumps(payload, separators=(",", ":"), ensure_ascii=True) + "\n").encode()
        self.audit_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.audit_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.fchmod(fd, 0o600)
            remaining = memoryview(data)
            while remaining:
                written = os.write(fd, remaining)
                remaining = remaining[written:]
            os.fsync(fd)
        finally:
            os.close(fd)


class DesktopService:
    """Own one active virtual session and serialize all stateful operations."""

    def __init__(self, audit_path: str | os.PathLike[str] | None = None, *, allowed_apps=(), worker_factory=None,
                 reader=None, max_reader_calls: int = 0, request_timeout: float = 20.0, stop_timeout: float = 3.0,
                 idle_timeout: float = 180.0, max_session_lifetime: float = 1800.0,
                 max_actions: int = 64, max_observations: int = 256,
                 monotonic=time.monotonic, watchdog_interval: float = 1.0):
        self.audit_path = Path(audit_path or Path(__file__).parent / "run" / "desktop-service" / "actions.jsonl")
        self.worker_factory = worker_factory or (lambda: DesktopWorkerClient(timeout=request_timeout))
        self._worker = None
        if reader is not None and (type(max_reader_calls) is not int or max_reader_calls <= 0):
            raise ValueError("configured_reader_requires_positive_call_cap")
        if reader is None and max_reader_calls not in (0, None):
            raise ValueError("reader_call_cap_requires_configured_reader")
        self.reader = reader
        if type(max_reader_calls) is not int or max_reader_calls < 0:
            raise ValueError("max_reader_calls must be a non-negative integer")
        self.max_reader_calls = max_reader_calls
        self.allowed_apps = frozenset(allowed_apps)
        if not self.allowed_apps.issubset(APPS):
            raise ValueError("allowed_apps must be a subset of the virtual fixture registry")
        self.stop_timeout = stop_timeout
        for name, value in (("idle_timeout", idle_timeout), ("max_session_lifetime", max_session_lifetime),
                            ("watchdog_interval", watchdog_interval)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < float("inf"):
                raise ValueError(f"{name} must be a finite positive number")
        if type(max_actions) is not int or max_actions <= 0:
            raise ValueError("max_actions must be a positive integer")
        if type(max_observations) is not int or max_observations < 2:
            raise ValueError("max_observations must be an integer of at least 2")
        self.idle_timeout = float(idle_timeout)
        self.max_session_lifetime = float(max_session_lifetime)
        self.max_actions = max_actions
        self.max_observations = max_observations
        self._monotonic = monotonic
        self._watchdog_interval = float(watchdog_interval)
        self._watchdog_stop = threading.Event()
        self._last_stop_reason = None
        self._last_stop_at = None
        self._lock = threading.RLock()
        self._session: _Session | None = None
        self._watchdog = threading.Thread(target=self._watchdog_loop, name="jev-desktop-watchdog", daemon=True)
        self._watchdog.start()

    def _watchdog_loop(self):
        while not self._watchdog_stop.wait(self._watchdog_interval):
            self._expire_if_needed()

    def _expiry_reason(self, s, now=None):
        now = self._monotonic() if now is None else now
        if now - s.started_clock >= s.max_session_lifetime:
            return "max_lifetime"
        if now - s.last_activity_clock >= s.idle_timeout:
            return "idle_timeout"
        return None

    def _expire_if_needed(self):
        with self._lock:
            s = self._session
            reason = (self._expiry_reason(s) if s is not None and not s.expiring
                      and s.state != "starting" else None)
            if reason is not None:
                # Claim expiry while holding the owner lock so a new task call
                # cannot refresh activity between the deadline check and stop.
                s.expiring = True
        if reason is None:
            return False
        # stop_all owns cancellation, lock waiting, worker recovery and cleanup
        # reporting; the watchdog never invents a separate teardown path.
        self.dispatch("session_stop", {"session_id": s.session_id},
                      {"transport": "watchdog", "caller_node": "local-owner",
                       "_stop_reason": reason})
        return True

    def _audit(self, method, ctx, app="", mode="guarded", status="ok", reason="completed", verification=None,
               duration_ms=None, request_id=None):
        record = {"timestamp": datetime.now(timezone.utc).isoformat(),
                  "caller_node": _context(ctx, "caller_node"), "transport": _context(ctx, "transport"),
                  "tool": method, "app": app if app in APPS else "", "autonomy_mode": mode,
                  "event": method, "status": status, "jev_answers": None,
                  "action": method if method == "act" else None,
                  "verification": verification, "reason": reason,
                  "request_id": request_id,
                  "duration_ms": round(max(0.0, duration_ms), 3) if duration_ms is not None else None}
        data = (json.dumps(record, separators=(",", ":"), ensure_ascii=True) + "\n").encode()
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.audit_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try: os.write(fd, data)
        finally: os.close(fd)

    def _status(self, s):
        if s is None: return None
        proc = getattr(getattr(s.worker, "proc", None), "poll", None)
        if callable(proc) and proc() is not None:
            s.state = "broken"
        busy = not s.busy.acquire(False)
        if not busy: s.busy.release()
        now = self._monotonic()
        age = max(0.0, now - s.started_clock)
        idle = max(0.0, now - s.last_activity_clock)
        return {"session_id": s.session_id, "app": s.app, "mode": s.mode.value,
                "state": "stopping" if s.stopping else "busy" if busy else s.state, "started_at": s.started_at,
                "age_seconds": round(age, 3), "last_activity_at": s.last_activity_at,
                "idle_seconds": round(idle, 3), "idle_timeout_seconds": s.idle_timeout,
                "max_session_lifetime_seconds": s.max_session_lifetime,
                "remaining_idle_seconds": round(max(0.0, s.idle_timeout - idle), 3),
                "remaining_lifetime_seconds": round(max(0.0, s.max_session_lifetime - age), 3),
                "owner_pid": os.getpid(), "worker_pid": getattr(getattr(s.worker, "proc", None), "pid", None),
                "active": s.state != "broken",
                "actions": s.actions, "action_cap": s.max_actions,
                "observations": s.observations, "observation_cap": s.max_observations,
                "reader": s.reader_budget.summary() if s.reader_budget else {
                    "calls_used": 0, "max_calls": 0, "successful_calls": 0, "failed_calls": 0,
                    "rate_limited": False, "latency_total_ms": 0.0, "provider_latency_total_ms": 0.0,
                    "owner_elapsed_total_ms": 0.0, "reported_usage": {}},
                "document_path": s.document_path}

    def _require_session(self, params):
        with self._lock: s = self._session
        if s is None: raise ValueError("session_not_found")
        if params.get("session_id") not in (None, s.session_id): raise ValueError("session_not_found")
        if s.expiring: raise ValueError("session_expiring")
        if s.stopping: raise ValueError("session_busy")
        proc = getattr(s.worker, "proc", None)
        if proc is not None and proc.poll() is not None:
            s.state = "broken"
            raise ValueError("session_broken")
        if s.state == "broken": raise ValueError("session_broken")
        return s

    def dispatch(self, method: str, params: dict | None = None, context: dict | None = None) -> dict:
        dispatch_started = self._monotonic()
        request_id = uuid.uuid4().hex
        params = params if isinstance(params, dict) else {}
        context = context if isinstance(context, dict) else {}
        app = params.get("app", "")
        s = None
        try:
            if method not in METHODS: raise ValueError("method_not_found")
            if method == "capabilities":
                result = {"allowed_apps": sorted(self.allowed_apps), "virtual_only": True, "one_active_session": True,
                          "methods": sorted(METHODS), "driver": "kwin-mcp==0.10.0",
                          "lifecycle": {"idle_timeout_seconds": self.idle_timeout,
                                        "max_session_lifetime_seconds": self.max_session_lifetime,
                                        "max_actions": self.max_actions, "max_observations": self.max_observations,
                                        "watchdog": True},
                          "reader": {"available": self.reader is not None and self.max_reader_calls > 0,
                                     "max_calls_per_session": self.max_reader_calls if self.reader else 0,
                                     "configuration": self.reader.capabilities() if self.reader else None,
                                     "accounting": "reported_usage_and_latency_only; provider billing is not measured"}}
            elif method == "status":
                self._expire_if_needed()
                with self._lock: current = self._session
                result = {"session": self._status(current), "lifecycle": {
                    "last_stop_reason": self._last_stop_reason, "last_stop_at": self._last_stop_at,
                    "watchdog_running": self._watchdog.is_alive()}}
            elif method == "session_start":
                if app not in self.allowed_apps: raise ValueError("app_not_allowed")
                mode = AutonomyMode(params.get("mode", "guarded"))
                with self._lock:
                    if self._session is not None: raise ValueError("session_exists")
                    worker = self._worker or self.worker_factory()
                    sid = uuid.uuid4().hex
                    journal_path = self.audit_path.parent / "sessions" / f"{sid}.json"
                    if isinstance(worker, DesktopWorkerClient): worker.journal_path = journal_path
                    s = _Session(app, mode, worker, sid, reader=self.reader, audit_path=self.audit_path,
                                 max_reader_calls=self.max_reader_calls, idle_timeout=self.idle_timeout,
                                 max_session_lifetime=self.max_session_lifetime, max_actions=self.max_actions,
                                 max_observations=self.max_observations, monotonic=self._monotonic)
                    s.state = "starting"
                    self._last_stop_reason = None
                    self._last_stop_at = None
                    try:
                        started = worker.start(app, str(journal_path)) if isinstance(worker, DesktopWorkerClient) else worker.start(app)
                        if isinstance(started, dict):
                            path = started.get("document_path")
                            if isinstance(path, str): s.document_path = path
                    except Exception as exc:
                        safe_start_codes = {
                            "invalid_worker_start", "session_stop_unconfirmed", "session_not_started",
                            "app_not_allowed", "fixture_unavailable", "virtual_session_unavailable",
                            "atspi_setup_failed", "app_window_open_unconfirmed", "document_create_failed", "document_open_unconfirmed",
                            "worker_timeout", "worker_protocol_error", "worker_failed",
                        }
                        failure_code = str(exc) if str(exc) in safe_start_codes else "worker_failed"
                        if hasattr(worker, "terminate"): worker.terminate()
                        recovered = bool(getattr(worker, "last_cleanup_confirmed", False)
                                         or getattr(worker, "recover_cleanup", lambda: False)())
                        if recovered:
                            self._worker = None
                        else:
                            self._worker = worker
                            s.state = "broken"
                            self._session = s
                        raise ValueError(failure_code) from None
                    self._worker = worker
                    s.state = "running"
                    self._session = s
                result = {"session": self._status(s)}
            elif method == "cancel":
                s = self._require_session(params); s.cancel.set()
                result = {"cancelled": True, "session_id": s.session_id}
            elif method in {"session_stop", "stop_all"}:
                with self._lock:
                    s = self._session
                    if method == "session_stop" and s is not None and params.get("session_id") not in (None, s.session_id):
                        raise ValueError("session_not_found")
                    if s is not None:
                        if s.stopping: raise ValueError("session_busy")
                        s.stopping = True
                if s is None: result = {"stopped": True}
                else:
                    s.cancel.set()
                    acquired = s.busy.acquire(timeout=self.stop_timeout)
                    cleanup = "unknown"
                    with self._lock:
                        if self._session is not s:
                            if acquired: s.busy.release()
                            raise ValueError("session_not_found")
                    if not acquired:
                        s.state = "broken"
                        if hasattr(s.worker, "terminate"): s.worker.terminate()
                        cleanup = "confirmed" if getattr(s.worker, "recover_cleanup", lambda: False)() else "unconfirmed"
                    else:
                        try:
                            try:
                                stopped = s.worker.stop()
                                cleanup = "confirmed" if isinstance(stopped, dict) and stopped.get("stopped") is True else "unconfirmed"
                            except Exception:
                                s.state = "broken"
                                if hasattr(s.worker, "terminate"): s.worker.terminate()
                                cleanup = "confirmed" if getattr(s.worker, "recover_cleanup", lambda: False)() else "unconfirmed"
                        finally: s.busy.release()
                    with self._lock:
                        if self._session is s:
                            if cleanup == "confirmed":
                                self._session = None
                            requested_reason = context.get("_stop_reason")
                            if (_context(context, "transport") == "watchdog"
                                    and context.get("caller_node") == "local-owner"
                                    and requested_reason in {"idle_timeout", "max_lifetime"}):
                                self._last_stop_reason = requested_reason
                                self._last_stop_at = datetime.now(timezone.utc).isoformat()
                            elif cleanup == "confirmed" and self._last_stop_reason is None:
                                self._last_stop_reason = "requested" if method == "session_stop" else "stop_all"
                                self._last_stop_at = datetime.now(timezone.utc).isoformat()
                            if cleanup == "confirmed":
                                proc = getattr(s.worker, "proc", None)
                                if (s.state == "broken" or getattr(s.worker, "_poisoned", False)
                                        or (proc is not None and proc.poll() is not None)):
                                    # A recovered child cannot service another session.
                                    # Retain healthy workers only for normal reuse.
                                    close = getattr(s.worker, "close", None)
                                    if callable(close): close()
                                    self._worker = None
                    if cleanup != "confirmed":
                        s.state = "broken"
                        with self._lock:
                            if self._session is s:
                                s.stopping = False
                    result = {"stopped": cleanup == "confirmed", "cleanup": cleanup, "session_id": s.session_id}
            else:
                s = self._require_session(params)
                if app and app != s.app: raise ValueError("app_session_mismatch")
                app = s.app
                if method not in {"observe", "wait", "candidates", "act"}: raise ValueError("method_not_found")
                if self._expiry_reason(s) is not None:
                    self._expire_if_needed()
                    raise ValueError("session_expiring")
                if not s.busy.acquire(blocking=False): raise ValueError("session_busy")
                try:
                    s.cancel.clear()
                    s.tx.audit.caller_node = _context(context, "caller_node")
                    s.tx.audit.transport = _context(context, "transport")
                    if self._monotonic() - s.started_clock > s.max_session_lifetime:
                        raise ValueError("task_time_budget_exceeded")
                    if method == "observe":
                        if s.observations >= s.max_observations: raise ValueError("task_observation_budget_exceeded")
                        if s.cancel.is_set(): raise ValueError("cancelled")
                        capture_id = params.get("capture_id")
                        if capture_id is None:
                            ref = s.adapter.capture(app, scope=params.get("scope", "app"), crop=params.get("crop"))
                            capture_id = ref.capture_id
                        if s.cancel.is_set(): raise ValueError("cancelled")
                        output = params.get("output", "metadata")
                        if output not in {"metadata", "image", "data", "both"}: raise ValueError("invalid_params")
                        if output in {"data", "both"}:
                            if s.adapter.reader is None: raise ValueError("reader_unavailable")
                            if not isinstance(params.get("questions"), list) or not params["questions"]:
                                raise ValueError("invalid_params")
                        obs = s.adapter.observe(capture_id, mode=output, questions=params.get("questions"))
                        if s.cancel.is_set(): raise ValueError("cancelled")
                        result = {"observation": {"capture": obs.metadata,
                                                  **({"image_base64": base64.b64encode(obs.image).decode("ascii")} if obs.image is not None else {}),
                                                  **({"data": obs.data, "reader": asdict(obs.reader)} if obs.reader is not None else {}),
                                                  "errors": list(obs.errors)}}
                        s.observations += 1
                    elif method == "wait":
                        if not isinstance(params.get("expected"), str):
                            raise ValueError("invalid_params")
                        result = run_owner_wait(s, params["expected"], params.get("timeout_seconds", 30.0))
                    elif method == "candidates":
                        if s.observations >= s.max_observations:
                            raise ValueError("task_observation_budget_exceeded")
                        if s.cancel.is_set(): raise ValueError("cancelled")
                        snap = s.tx.observe(s.session_id, app, mode=s.mode)
                        if s.cancel.is_set(): raise ValueError("cancelled")
                        result = {"app": app, "generation": snap.generation,
                                  "candidates": [_candidate(c) for c in snap.candidates]}
                        s.observations += 1
                    elif method == "act":
                        if s.actions >= s.max_actions: raise ValueError("task_action_budget_exceeded")
                        if s.observations + 2 > s.max_observations:
                            raise ValueError("task_observation_budget_exceeded")
                        kind, target, verification = params.get("action"), params.get("target_ref"), params.get("verification")
                        text = params.get("text")
                        if kind not in {"click", "type_text", "replace_document", "save_document", "navigate_url", "scroll"} or not isinstance(target, str): raise ValueError("invalid_params")
                        self._check_verifier(s, verification, params.get("expected"), target)
                        direction, steps = params.get("direction"), params.get("steps", 1)
                        action = Action(kind, target,
                                        text if kind in {"type_text", "replace_document", "navigate_url"} else None,
                                        direction=direction if kind == "scroll" else None,
                                        steps=steps if kind == "scroll" else 1)
                        approval = params.get("approved") is True
                        if s.mode is AutonomyMode.SUPERVISED and not approval: raise ValueError("approval_required")
                        prior = s.tx._last.get((s.session_id, app))
                        candidate = prior.candidate(target) if prior else None
                        if candidate is None: raise ValueError("target_reference_stale")
                        self._check_action_policy(s, kind, candidate, verification, params.get("expected"), text)
                        risk = kind == "click" and any(w in candidate.label.casefold() for w in ("delete", "remove", "submit", "send", "purchase", "format"))
                        if s.mode is AutonomyMode.GUARDED and risk and not approval: raise ValueError("approval_required")
                        self._audit(method, context, app, s.mode.value, "pending",
                                    "approved_action" if approval else "action_pending", verification,
                                    request_id=request_id)
                        before_snapshot = s.tx._last.get((s.session_id, app))
                        verifier = self._verifier(s, verification, params.get("expected"), candidate,
                                                  before_snapshot, direction=direction)
                        precondition = self._precondition(s, kind, verification, params.get("expected"), text, candidate)
                        acted = s.tx.act(s.session_id, app, action, verifier=verifier, mode=s.mode,
                                         approve=(lambda *_: approval), cancel=s.cancel, precondition=precondition)
                        s.actions += 1
                        result = {"status": acted.status, "verification": acted.verification,
                                  "evidence": acted.evidence if isinstance(acted.evidence, (str, int, float, bool, dict, list, type(None))) else None}
                        s.observations += 2
                    else: raise ValueError("method_not_found")
                    # Only completed observe/wait/candidate/action work extends the
                    # inactivity window. Status, cancellation and invalid
                    # requests cannot keep an abandoned app alive.
                    s.last_activity_clock = self._monotonic()
                    s.last_activity_at = datetime.now(timezone.utc).isoformat()
                except Exception as exc:
                    proc = getattr(getattr(s.worker, "proc", None), "poll", None)
                    if (isinstance(exc, (BrokenPipeError, TimeoutError)) or "worker" in str(exc)
                            or (callable(proc) and proc() is not None)):
                        s.state = "broken"
                    raise
                finally: s.busy.release()
            completed_reason = self._last_stop_reason if _context(context, "transport") == "watchdog" else "completed"
            verification = result.get("verification") if isinstance(result, dict) else None
            if method == "wait" and isinstance(result, dict):
                waiting = result.get("wait", {})
                verification = {key: waiting.get(key) for key in
                                ("status", "captures", "judgments", "coalesced_frames", "error_code")}
            self._audit(method, context, app, s.mode.value if s else "guarded", "ok", completed_reason,
                        verification,
                        duration_ms=(self._monotonic() - dispatch_started) * 1000, request_id=request_id)
            return {"ok": True, **result}
        except Exception as exc:
            raw = (exc.code if isinstance(exc, ObservationError) else
                   str(exc) if isinstance(exc, (ValueError, TransactionError, RuntimeError)) else
                   "operation_failed")
            code = raw if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", raw or "") else "operation_failed"
            err = _safe_error(code)
            self._audit(method if isinstance(method, str) else "unknown", context, app,
                        s.mode.value if s else "guarded", "failed", err["code"],
                        duration_ms=(self._monotonic() - dispatch_started) * 1000, request_id=request_id)
            return {"ok": False, "error": err}

    def _check_verifier(self, s, verification, expected, target):
        allowed = {"target_focused", "target_text", "fixture_state", "display_text", "document_saved",
                   "navigation_url", "window_title", "visible_text", "scroll_changed"}
        if verification not in allowed: raise ValueError("unsupported_verification")
        if verification in {"target_text", "navigation_url", "window_title", "visible_text"} and (not isinstance(expected, str) or len(expected) > 4096):
            raise ValueError("invalid_params")
        if verification in {"fixture_state", "display_text"} and (not isinstance(expected, str) or len(expected) > 256):
            raise ValueError("invalid_params")
        if verification == "document_saved" and (s.app != "kate" or expected is not None):
            raise ValueError("unsupported_verification")
        if verification == "fixture_state" and (s.app != "firefox" or expected not in {"State: idle", "State: complete"}):
            raise ValueError("unsupported_verification")
        if verification == "display_text" and (s.app != "kcalc" or expected != "1"):
            raise ValueError("unsupported_verification")
        if verification == "document_saved" and s.app != "kate": raise ValueError("unsupported_verification")
        if verification in {"navigation_url", "window_title", "visible_text", "scroll_changed"} and s.app != "firefox":
            raise ValueError("unsupported_verification")

    def _check_action_policy(self, s, kind, candidate, verification, expected, text):
        if verification == "display_text":
            if not (s.app == "kcalc" and kind == "click" and candidate.role == "button"
                    and candidate.label == "One" and expected == "1"):
                raise ValueError("unsupported_verification")
        elif verification == "fixture_state":
            if not (s.app == "firefox" and kind == "click" and candidate.role == "button"
                    and candidate.label == "Advance state" and expected == "State: complete"):
                raise ValueError("unsupported_verification")
        elif verification == "target_text":
            if not (kind in {"type_text", "replace_document"}
                    and kind in candidate.actions and isinstance(text, str) and text.strip()):
                raise ValueError("unsupported_verification")
            if kind == "replace_document" and (s.app != "kate" or expected != text):
                raise ValueError("unsupported_verification")
        elif verification == "document_saved":
            if not (s.app == "kate" and kind == "save_document" and candidate.role in {"text", "text entry", "entry"}
                    and "save_document" in candidate.actions):
                raise ValueError("unsupported_verification")
        elif verification == "target_focused":
            if candidate.role not in {"text", "text entry", "entry", "combo box"}:
                raise ValueError("unsupported_verification")
        elif verification == "navigation_url":
            from transactions import _valid_web_url
            if not (s.app == "firefox" and kind == "navigate_url" and "navigate_url" in candidate.actions
                    and isinstance(text, str) and text == expected and _valid_web_url(text)):
                raise ValueError("unsupported_verification")
        elif verification == "window_title":
            if not (s.app == "firefox" and kind == "click" and candidate.role == "link" and expected.strip()):
                raise ValueError("unsupported_verification")
        elif verification == "visible_text":
            if not (s.app == "firefox" and kind in {"click", "type_text"}
                    and kind in candidate.actions and expected.strip()):
                raise ValueError("unsupported_verification")
        elif verification == "scroll_changed":
            if not (s.app == "firefox" and kind == "scroll" and candidate.role in {"document frame", "scroll pane", "web area", "scroll bar", "scrollbar"}):
                raise ValueError("unsupported_verification")
            if expected is not None:
                raise ValueError("unsupported_verification")

    def _precondition(self, s, kind, verification, expected, text, prior_candidate):
        def check(snapshot):
            current = [c for c in snapshot.candidates
                       if (c.role, c.label) == (prior_candidate.role, prior_candidate.label)
                       and {"enabled", "sensitive", "showing", "visible"}.issubset(c.states)]
            if len(current) != 1: return False
            target = current[0]
            if verification == "target_text":
                return bool(text and text.strip() and expected != target.value and "type_text" in target.actions)
            if verification == "display_text":
                raw = s.worker.atspi_find("kcalc")
                rows = raw.get("result") if isinstance(raw, dict) and raw.get("ok") is True else []
                display = [row for row in rows if isinstance(row, dict)
                           and "editable" in {str(state).casefold() for state in row.get("states", [])}
                           and {str(state).casefold() for state in row.get("states", [])} & {"showing", "visible"}]
                return (len(display) == 1 and display[0].get("text", display[0].get("name", "")) == ""
                        and target.role == "button" and target.label == "One")
            if verification == "fixture_state":
                raw = s.worker.atspi_find("firefox")
                rows = raw.get("result") if isinstance(raw, dict) and raw.get("ok") is True else []
                states = [row for row in rows if isinstance(row, dict)
                          and (row.get("text") == "State: idle" or row.get("name") == "State: idle")
                          and {str(state).casefold() for state in row.get("states", [])} & {"showing", "visible"}]
                return len(states) == 1 and target.role == "button" and target.label == "Advance state"
            if verification == "target_focused":
                return kind == "click" and target.role in {"text", "text entry", "entry", "combo box"}
            if verification == "document_saved":
                return kind == "save_document" and target.role in {"text", "text entry", "entry"}
            if verification == "navigation_url":
                return kind == "navigate_url" and "focused" in target.states
            if verification == "scroll_changed":
                bars = [c for c in snapshot.candidates if c.role in {"scroll bar", "scrollbar"}
                        and c.value_number is not None]
                return kind == "scroll" and (len(bars) == 1 or bool(_scroll_witnesses(snapshot, target)))
            if verification == "window_title":
                return kind == "click" and target.role == "link" and snapshot.window_title != expected
            if verification == "visible_text":
                if kind == "click" and "click" not in target.actions: return False
                try:
                    raw = s.worker.atspi_find(s.app)
                    rows = raw.get("result") if isinstance(raw, dict) and raw.get("ok") is True else []
                    return not any(isinstance(row, dict)
                                   and {str(state).casefold() for state in row.get("states", [])} & {"showing", "visible"}
                                   and (row.get("text") == expected or row.get("name") == expected)
                                   for row in rows)
                except Exception:
                    return False
            return False
        return check

    def _verifier(self, s, verification, expected, candidate, before_snapshot=None, *, direction=None):
        def verify(snapshot):
            target = [c for c in snapshot.candidates if (c.role, c.label) == (candidate.role, candidate.label)]
            if verification == "target_focused":
                return Verification(len(target) == 1 and "focused" in target[0].states, {"target_focused": len(target) == 1 and "focused" in target[0].states})
            if verification == "target_text":
                ok = len(target) == 1 and target[0].value == expected
                return Verification(ok, {"exact_target_text": ok})
            if verification == "navigation_url":
                started = s.monotonic()
                deadline = started + min(10.0, max(0.0, s.max_session_lifetime -
                                                    (started - s.started_clock)))
                attempts = 0
                while True:
                    if s.cancel.is_set():
                        return Verification(False, {"address_bar_destination_verified": False,
                                                    "reason": "cancelled"})
                    if s.observations + 3 > s.max_observations:
                        break
                    try:
                        s.tx._check_budget(s.session_id)
                    except TransactionError:
                        break
                    s.tx._bump(s.session_id)
                    s.observations += 1
                    attempts += 1
                    try:
                        raw = s.worker.atspi_find(s.app)
                        rows = raw.get("result") if isinstance(raw, dict) and raw.get("ok") is True else []
                        addresses = [row for row in rows if isinstance(row, dict)
                                     and row.get("mapped") is True
                                     and row.get("role") == "combo box"
                                     and re.search(r"(address|location|search.*(web|address)|web.*search)",
                                                    str(row.get("name", "")), re.IGNORECASE)
                                     and {str(state).casefold() for state in row.get("states", [])}
                                     >= {"editable", "showing", "visible"}]
                        if len(addresses) == 1 and _same_address_destination(addresses[0].get("text"), expected):
                            observed = addresses[0].get("text")
                            explicit_scheme = isinstance(observed, str) and "://" in observed
                            return Verification(True, {"address_bar_destination_verified": True,
                                                       "submitted_scheme": urlsplit(expected).scheme,
                                                       "scheme_visible_in_address_bar": explicit_scheme,
                                                       "verification_reads": attempts})
                    except (AttributeError, RuntimeError, ValueError):
                        pass
                    remaining = deadline - s.monotonic()
                    if remaining <= 0:
                        break
                    s.cancel.wait(min(0.2, remaining))
                return Verification(False, {"address_bar_destination_verified": False,
                                            "verification_reads": attempts,
                                            "reason": "address_bar_not_ready_or_destination_mismatch"})
            if verification == "window_title":
                started = s.monotonic()
                deadline = started + min(10.0, max(0.0, s.max_session_lifetime -
                                                    (started - s.started_clock)))
                attempts = 1
                current = snapshot
                while True:
                    if current.window_title == expected:
                        return Verification(True, {"exact_window_title": True,
                                                   "verification_reads": attempts})
                    if s.cancel.is_set() or s.observations + 3 > s.max_observations:
                        break
                    try:
                        s.tx._check_budget(s.session_id)
                    except TransactionError:
                        break
                    remaining = deadline - s.monotonic()
                    if remaining <= 0:
                        break
                    s.cancel.wait(min(0.2, remaining))
                    if s.cancel.is_set():
                        break
                    try:
                        current = s.tx._snapshot(s.session_id, s.app)
                        s.observations += 1
                        attempts += 1
                    except (AttributeError, RuntimeError, TransactionError, ValueError):
                        break
                return Verification(False, {"exact_window_title": False,
                                            "verification_reads": attempts})
            if verification == "scroll_changed":
                started = s.monotonic()
                deadline = started + min(2.0, max(0.0, s.max_session_lifetime -
                                                  (started - s.started_clock)))
                attempts = 1
                current = snapshot
                measurement = "unavailable"
                while True:
                    prior = {(c.role, c.label): c.value_number for c in (before_snapshot.candidates if before_snapshot else ())
                             if c.role in {"scroll bar", "scrollbar"} and c.value_number is not None}
                    values = {(c.role, c.label): c.value_number for c in current.candidates
                              if c.role in {"scroll bar", "scrollbar"} and c.value_number is not None}
                    if len(prior) == len(values) == 1:
                        prior_key, prior_value = next(iter(prior.items()))
                        if prior_key in values:
                            measurement = "accessibility_scroll_value"
                            current_value = values[prior_key]
                            moved = ((direction == "down" and current_value > prior_value)
                                     or (direction == "up" and current_value < prior_value))
                            if moved:
                                return Verification(True, {"scroll_position_changed_in_requested_direction": True,
                                                           "measurement": measurement,
                                                           "verification_reads": attempts})
                    before_viewport = next((c for c in (before_snapshot.candidates if before_snapshot else ())
                                            if (c.role, c.label) == (candidate.role, candidate.label)), None)
                    after_viewports = [c for c in current.candidates
                                       if (c.role, c.label) == (candidate.role, candidate.label)]
                    if before_snapshot is not None and before_viewport is not None and len(after_viewports) == 1:
                        after_viewport = after_viewports[0]
                        if before_viewport.bounds == after_viewport.bounds:
                            before_positions = _scroll_witnesses(before_snapshot, before_viewport)
                            after_positions = _scroll_witnesses(current, after_viewport)
                            common = set(before_positions) & set(after_positions)
                            deltas = [after_positions[key].y - before_positions[key].y for key in common]
                            expected_sign = -1 if direction == "down" else 1
                            agreeing = sum(delta * expected_sign > 0 for delta in deltas)
                            opposing = sum(delta * expected_sign < 0 for delta in deltas)
                            measurement = "semantic_content_bounds"
                            if agreeing >= 2 and opposing == 0:
                                return Verification(True, {"scroll_position_changed_in_requested_direction": True,
                                                           "measurement": measurement,
                                                           "verification_reads": attempts})
                        else:
                            measurement = "viewport_moved"
                    if s.cancel.is_set() or s.observations + 3 > s.max_observations:
                        break
                    try:
                        s.tx._check_budget(s.session_id)
                    except TransactionError:
                        break
                    remaining = deadline - s.monotonic()
                    if remaining <= 0:
                        break
                    s.cancel.wait(min(0.1, remaining))
                    if s.cancel.is_set():
                        break
                    try:
                        current = s.tx._snapshot(s.session_id, s.app)
                        s.observations += 1
                        attempts += 1
                    except (AttributeError, RuntimeError, TransactionError, ValueError):
                        break
                return Verification(False, {"scroll_position_changed_in_requested_direction": False,
                                            "measurement": measurement,
                                            "verification_reads": attempts})
            if verification == "visible_text":
                try:
                    raw = s.worker.atspi_find(s.app)
                    rows = raw.get("result") if isinstance(raw, dict) and raw.get("ok") is True else []
                    matches = [row for row in rows if isinstance(row, dict)
                               and {str(state).casefold() for state in row.get("states", [])} & {"showing", "visible"}
                               and (row.get("text") == expected or row.get("name") == expected)]
                except Exception:
                    matches = []
                ok = len(matches) == 1
                return Verification(ok, {"exact_visible_text": ok})
            if verification == "document_saved":
                ok = len(target) == 1 and isinstance(target[0].value, str)
                if ok:
                    try:
                        ok = s.worker.document_bytes() == target[0].value.encode("utf-8")
                    except (AttributeError, RuntimeError, ValueError):
                        ok = False
                return Verification(ok, {"exact_bytes_saved": ok})
            raw = s.worker.atspi_find(s.app)
            rows = raw.get("result") if isinstance(raw, dict) and raw.get("ok") is True else []
            if verification == "display_text":
                matches = [row for row in rows if isinstance(row, dict)
                           and "editable" in {str(state).casefold() for state in row.get("states", [])}
                           and {str(state).casefold() for state in row.get("states", [])} & {"showing", "visible"}
                           and (row.get("text") == expected or row.get("name") == expected)]
            else:
                matches = [row for row in rows if isinstance(row, dict)
                           and {str(state).casefold() for state in row.get("states", [])} & {"showing", "visible"}
                           and (row.get("text") == expected or row.get("name") == expected)]
            ok = len(matches) == 1
            return Verification(ok, {"exact_visible_text": ok})
        return verify

    def close(self):
        self._watchdog_stop.set()
        if self._watchdog is not threading.current_thread():
            self._watchdog.join(timeout=max(1.0, self._watchdog_interval * 2))
        response = self.dispatch("stop_all", {}, {"transport": "shutdown"})
        confirmed = response.get("ok") is True and response.get("stopped") is True
        try:
            if self._worker is not None:
                if hasattr(self._worker, "close"):
                    recovered = self._worker.close()
                    confirmed = confirmed or recovered is True
                elif hasattr(self._worker, "terminate"):
                    self._worker.terminate()
            if not confirmed:
                raise RuntimeError("session_cleanup_unconfirmed")
            with self._lock: self._session = None
        finally:
            close_reader = getattr(self.reader, "close", None)
            if callable(close_reader): close_reader()


def _candidate(candidate: Candidate) -> dict[str, Any]:
    # Editable contents and coordinates never leave the owner process.
    label = "editable text" if "type_text" in candidate.actions else candidate.label
    return {"ref": candidate.ref, "role": candidate.role, "label": label,
            "states": sorted(candidate.states), "actions": list(candidate.actions),
            **({"unavailable_reason": candidate.unavailable_reason} if candidate.unavailable_reason else {})}
