"""Private JSON-lines worker for one pinned kwin-mcp virtual session."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote


ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / "benchmark_fixtures" / "action.html"


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
        return {"stopped": True, "cleanup_paths": []}

    def start(self, app: str, journal_path: str | None = None) -> dict:
        if app not in {"kate", "firefox", "kcalc"} or self.engine is not None:
            raise ValueError("invalid_worker_start")
        if not isinstance(journal_path, str) or not journal_path:
            raise ValueError("invalid_worker_start")
        self.journal_path = Path(journal_path)
        from kwin_mcp.core import AutomationEngine

        env = {"QT_ACCESSIBILITY": "1", "GTK_MODULES": "gail:atk-bridge"}
        if app == "kate":
            fd, raw = tempfile.mkstemp(prefix="jev-desktop-draft-", suffix=".txt")
            os.close(fd)
            draft = Path(raw)
            self.paths.append(draft)
            command = f"kate {shlex.quote(str(draft))}"
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
            self._write_journal()
            return {"started": True, "atspi": flags, "cleanup_paths": [str(p) for p in self.paths]}
        except Exception:
            self.stop()
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
            return self.engine.keyboard_type_unicode(value) if not value.isascii() else self.engine.keyboard_type(value)
        raise ValueError("worker_method_not_allowed")

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
                       "worker_method_not_allowed"}
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
