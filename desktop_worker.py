"""Private JSON-lines worker for one pinned kwin-mcp virtual session."""

from __future__ import annotations

import base64
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from types import MethodType
from pathlib import Path
from urllib.parse import quote


ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / "benchmark_fixtures" / "action.html"
DOCUMENTS = ROOT / "run" / "documents"


def _ensure_bounded_atspi_worker(engine):
    """Use the pinned driver's reuse lifecycle with our local text-cap shim."""
    env = engine._session_env()
    bus = env.get("DBUS_SESSION_BUS_ADDRESS", "")
    a11y = engine._a11y_bus_address(env)
    proc = engine._atspi_proc
    if proc is not None and (proc.poll() is not None or bus != engine._atspi_bus or a11y != engine._atspi_a11y):
        engine._teardown_atspi_worker()
        proc = None
    if proc is None:
        proc = subprocess.Popen(
            [sys.executable, "-m", "jev_accessibility_worker", "--serve"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            env=env,
        )
        engine._atspi_proc = proc
        engine._atspi_bus = bus
        engine._atspi_a11y = a11y
        engine._atspi_buffer = b""
    return proc


def _profile() -> Path:
    path = Path(tempfile.mkdtemp(prefix="jev-desktop-firefox-"))
    path.chmod(0o700)
    prefs = {
        "browser.startup.homepage_override.mstone": "ignore",
        "browser.startup.firstrunSkipsHomepage": True,
        "browser.startup.page": 0,
        "browser.aboutwelcome.enabled": False,
        "browser.shell.checkDefaultBrowser": False,
        "datareporting.policy.firstRunURL": "",
    }
    (path / "user.js").write_text(
        "\n".join(f"user_pref({json.dumps(k)}, {json.dumps(v)});" for k, v in prefs.items()) + "\n",
        encoding="utf-8",
    )
    return path


class Worker:
    def __init__(self):
        self.engine = None
        self.paths: list[Path] = []
        self.journal_path: Path | None = None
        self.app: str | None = None
        self.document_path: Path | None = None

    @staticmethod
    def _start_ticks(pid: int | None):
        if not isinstance(pid, int): return None
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            return int(stat[stat.rfind(")") + 2:].split()[19])
        except (OSError, ValueError, IndexError):
            return None

    @classmethod
    def _proc_info(cls, pid):
        try:
            stat = Path(f"/proc/{int(pid)}/stat").read_text()
            fields = stat[stat.rfind(")") + 2:].split()
            return {"state": fields[0], "pgrp": int(fields[2]), "start_ticks": int(fields[19])}
        except (OSError, ValueError, IndexError, TypeError): return None

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
    def _same_live(cls, pid, ticks):
        info = cls._proc_info(pid)
        return bool(info and info["start_ticks"] == ticks and info["state"] not in {"Z", "X"})

    def _resources_gone(self, record):
        pid = record.get("session_pid")
        if type(pid) is int and self._group_live(pid): return False
        for key in ("app_processes",):
            for process in record.get(key, []):
                if self._same_live(process.get("pid"), process.get("start_ticks")): return False
        if self._same_live(record.get("atspi_pid"), record.get("atspi_start_ticks")): return False
        for key, prefix in (("home_dir", "kwin-mcp-home-"), ("config_dir", "kwin-mcp-config-")):
            value = record.get(key, "")
            if not value: continue
            path = Path(value)
            if path.exists() and path.name.startswith(prefix) and path.resolve(strict=False).parent == Path(tempfile.gettempdir()).resolve(): return False
            if path.exists(): return False
        runtime = record.get("runtime_dir", "")
        socket = record.get("socket_name", "")
        if socket and (Path(runtime) / socket).exists(): return False
        if socket and (Path(runtime) / f"{socket}.lock").exists(): return False
        for value in record.get("private_paths", []):
            path = Path(value)
            if path.exists(): return False
        return True

    def _write_journal(self):
        if self.journal_path is None or self.engine is None: return
        session = getattr(self.engine, "_session", None)
        info = getattr(session, "info", None)
        if info is None: return
        apps = [{"pid": int(pid), "start_ticks": self._start_ticks(int(pid))}
                for pid in getattr(info, "apps", {})]
        process = getattr(session, "_process", None)
        pid = getattr(process, "pid", None)
        atspi = getattr(self.engine, "_atspi_proc", None)
        atspi_pid = getattr(atspi, "pid", None)
        record = {"schema": 1, "worker_pid": os.getpid(), "session_pid": pid,
                  "session_start_ticks": self._start_ticks(pid), "app_processes": apps,
                  "atspi_pid": atspi_pid, "atspi_start_ticks": self._start_ticks(atspi_pid),
                  "home_dir": str(getattr(info, "home_dir", "") or ""),
                  "config_dir": str(getattr(session, "_session_config_dir", "") or ""),
                  "socket_name": str(getattr(info, "wayland_socket", "")),
                  "runtime_dir": os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"),
                  "private_paths": [str(path) for path in self.paths]}
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.journal_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, separators=(",", ":")), encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, self.journal_path)

    def stop(self):
        record = None
        if self.journal_path is not None and self.journal_path.is_file():
            record = json.loads(self.journal_path.read_text(encoding="utf-8"))
        if self.engine is not None:
            result = self.engine.session_stop()
            if not isinstance(result, str) or not ("Session stopped" in result or "No session running" in result):
                raise ValueError("session_stop_unconfirmed")
            self.engine = None
        self.cleanup()
        if record is not None and not self._resources_gone(record):
            raise ValueError("session_cleanup_unconfirmed")
        if self.journal_path is not None:
            self.journal_path.unlink(missing_ok=True)
        self.app = None
        self.document_path = None
        self.journal_path = None
        return {"stopped": True, "cleanup_paths": []}

    def start(self, app: str, journal_path: str | None = None) -> dict:
        if app not in {"kate", "firefox", "kcalc"} or self.engine is not None:
            raise ValueError("invalid_worker_start")
        if not isinstance(journal_path, str) or not journal_path:
            raise ValueError("invalid_worker_start")
        self.journal_path = Path(journal_path)
        self.app = app
        from kwin_mcp.core import AutomationEngine

        env = {"QT_ACCESSIBILITY": "1", "GTK_MODULES": "gail:atk-bridge"}
        if app == "kate":
            document = self._new_document()
            self.document_path = document
            command = f"kate {shlex.quote(str(document))}"
        elif app == "firefox":
            if not FIXTURE.is_file():
                raise ValueError("fixture_unavailable")
            profile = _profile()
            self.paths.append(profile)
            command = " ".join(("firefox", "--no-remote", "--new-instance", "--profile",
                                shlex.quote(str(profile)), shlex.quote("file://" + quote(str(FIXTURE.resolve())))))
            env["MOZ_ENABLE_ACCESSIBILITY"] = "1"
        else:
            command = "kcalc"
        self.engine = AutomationEngine()
        # kwin-mcp 0.10.0 launches its accessibility reader in a child process.
        # Kate needs complete text for owned-document verification, so reuse
        # its bus identity, retry, timeout and teardown machinery with our
        # bounded text cap only for Kate sessions.
        if app == "kate":
            self.engine._ensure_atspi_worker = MethodType(_ensure_bounded_atspi_worker, self.engine)
        try:
            start = self.engine.session_start(app_command=command, screen_width=1280, screen_height=800,
                                              isolate_home=True, keep_home=False, keep_screenshots=False, env=env)
            if not isinstance(start, str) or "Input backend: KWin EIS" not in start:
                raise ValueError("virtual_session_unavailable")
            self._write_journal()
            from benchmark_capture import _enable_virtual_atspi
            flags = _enable_virtual_atspi(self.engine)
            if not all(flags.values()):
                raise ValueError("atspi_setup_failed")
            if self.document_path is not None:
                deadline = time.monotonic() + 8.0
                opened = False
                while time.monotonic() < deadline:
                    geometry = self.engine.window_geometry(app_name="kate")
                    if self.document_path.name in geometry:
                        opened = True
                        break
                    time.sleep(0.2)
                if not opened:
                    raise ValueError("document_open_unconfirmed")
            self._write_journal()
            return {"started": True, "atspi": flags, "cleanup_paths": [str(p) for p in self.paths],
                    **({"document_path": str(self.document_path)} if self.document_path else {})}
        except Exception:
            failed_document = self.document_path
            self.stop()
            if failed_document is not None:
                self._remove_empty_failed_document(failed_document)
            raise

    def call(self, method: str, params: dict):
        if method == "start":
            return self.start(params.get("app"), params.get("journal_path"))
        if method == "stop":
            return self.stop()
        if self.engine is None:
            raise ValueError("session_not_started")
        if method == "window_query":
            return self.engine._run_kwin_query({})
        if method == "atspi_find":
            app = params.get("app")
            if app not in {"kate", "firefox", "kcalc"}:
                raise ValueError("app_not_allowed")
            # Make the pinned private worker explicit and journal its PID
            # before the first AT-SPI request can outlive a failed RPC.
            self.engine._ensure_atspi_worker()
            self._write_journal()
            result = self.engine._run_atspi("find", query="", app_name=app)
            self._write_journal()
            return result
        if method == "window_geometry":
            return self.engine.window_geometry(app_name=params["app"])
        if method == "active_window":
            return self.engine.active_window()
        if method == "screenshot":
            return self.engine.screenshot(include_cursor=False)
        if method == "mouse_click":
            return self.engine.mouse_click(params["x"], params["y"], button="left")
        if method == "keyboard_type":
            value = params["text"]
            # Long ASCII notes are slow when sent character-by-character through
            # EIS. Kate's document workflow can use the driver's bounded paste
            # path, which snapshots and restores the clipboard. Keep other app
            # fixture typing behavior unchanged.
            if self.app == "kate":
                return self.engine.keyboard_type_unicode(value)
            return self.engine.keyboard_type_unicode(value) if not value.isascii() else self.engine.keyboard_type(value)
        if method == "document_key":
            if self.app != "kate" or self.document_path is None:
                raise ValueError("document_unavailable")
            # Keep the input surface closed: callers cannot supply key names or
            # combinations. Transactions select only these two app operations.
            keys = {"select_all": "ctrl+a", "save": "ctrl+s"}
            operation = params.get("operation")
            if operation not in keys:
                raise ValueError("document_operation_not_allowed")
            return self.engine.keyboard_key(keys[operation])
        if method == "document_bytes":
            path = self._owned_document()
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise ValueError("document_unavailable")
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    data = stream.read(1_048_577)
            finally:
                os.close(fd)
            if len(data) > 1_048_576:
                raise ValueError("document_too_large")
            return {"utf8_base64": base64.b64encode(data).decode("ascii")}
        raise ValueError("worker_method_not_allowed")

    @staticmethod
    def _new_document() -> Path:
        root = ROOT.resolve(strict=True)
        run_dir = ROOT / "run"
        if run_dir.is_symlink():
            raise ValueError("document_create_failed")
        run_dir.mkdir(mode=0o700, exist_ok=True)
        run_resolved = run_dir.resolve(strict=True)
        if run_resolved.parent != root or DOCUMENTS.is_symlink():
            raise ValueError("document_create_failed")
        DOCUMENTS.mkdir(mode=0o700, parents=True, exist_ok=True)
        if DOCUMENTS.resolve(strict=True).parent != run_resolved:
            raise ValueError("document_create_failed")
        try:
            DOCUMENTS.chmod(0o700)
        except OSError:
            pass
        # O_EXCL is the no-overwrite guarantee, including across concurrent
        # owners. The random token is task-owned metadata, not caller input.
        for _ in range(8):
            path = DOCUMENTS / f"handover-{os.urandom(12).hex()}.txt"
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                continue
            else:
                os.close(fd)
                return path
        raise ValueError("document_create_failed")

    def _owned_document(self) -> Path:
        path = self.document_path
        if self.app != "kate" or path is None:
            raise ValueError("document_unavailable")
        try:
            root = ROOT.resolve(strict=True)
            run_dir = ROOT / "run"
            if run_dir.is_symlink() or DOCUMENTS.is_symlink():
                raise ValueError("document_unavailable")
            run_resolved = run_dir.resolve(strict=True)
            resolved = path.resolve(strict=True)
            parent = DOCUMENTS.resolve(strict=True)
            if run_resolved.parent != root or parent.parent != run_resolved:
                raise ValueError("document_unavailable")
            if resolved.parent != parent or path.is_symlink() or not resolved.is_file():
                raise ValueError("document_unavailable")
            if resolved.stat().st_size > 1_048_576:
                raise ValueError("document_too_large")
            return resolved
        except OSError:
            raise ValueError("document_unavailable") from None

    @staticmethod
    def _remove_empty_failed_document(path: Path) -> None:
        """Remove only the just-created empty file after confirmed session stop."""
        try:
            root = ROOT.resolve(strict=True)
            run_dir = ROOT / "run"
            if run_dir.is_symlink() or DOCUMENTS.is_symlink() or path.is_symlink():
                return
            run_resolved = run_dir.resolve(strict=True)
            parent = DOCUMENTS.resolve(strict=True)
            if (run_resolved.parent != root or parent.parent != run_resolved
                    or path.parent != DOCUMENTS or not stat.S_ISREG(path.lstat().st_mode)
                    or path.stat().st_size != 0):
                return
            path.unlink()
        except OSError:
            return

    def cleanup(self):
        for path in self.paths:
            if path.is_dir(): shutil.rmtree(path)
            else: path.unlink(missing_ok=True)
        self.paths.clear()


def main() -> int:
    worker = Worker()
    for line in sys.stdin:
        request = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or not isinstance(request.get("method"), str):
                raise ValueError("invalid_request")
            result = worker.call(request["method"], request.get("params") or {})
            response = {"id": request.get("id"), "result": result}
        except Exception as exc:
            safe = str(exc)
            allowed = {"invalid_request", "invalid_worker_start", "session_stop_unconfirmed", "session_not_started",
                       "app_not_allowed", "fixture_unavailable", "virtual_session_unavailable", "atspi_setup_failed",
                       "worker_method_not_allowed", "document_unavailable", "document_operation_not_allowed",
                       "document_create_failed", "document_too_large", "document_open_unconfirmed"}
            code = safe if safe in allowed else type(exc).__name__
            response = {"id": request.get("id") if isinstance(request, dict) else None,
                        "error": {"code": code, "cleanup_paths": [str(p) for p in worker.paths],
                                  "cleanup_confirmed": worker.engine is None and not worker.paths
                                  and (worker.journal_path is None or not worker.journal_path.exists())}}
        sys.stdout.write(json.dumps(response, separators=(",", ":"), ensure_ascii=True) + "\n")
        sys.stdout.flush()
    if worker.engine is not None:
        try:
            worker.stop()
        except Exception:
            return 2
    return 0


def __getattr__(name):
    if name == "DesktopWorkerClient":
        from desktop_service import DesktopWorkerClient
        return DesktopWorkerClient
    raise AttributeError(name)


if __name__ == "__main__":
    raise SystemExit(main())
