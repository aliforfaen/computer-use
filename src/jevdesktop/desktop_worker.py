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


from jevdesktop import paths

ROOT = paths.repo_root() or Path(__file__).resolve().parent
FIXTURE = paths.fixtures_dir() / "action.html"
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
            [sys.executable, "-m", "jevdesktop.jev_accessibility_worker", "--serve"],
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
        self.desktop_mode = "virtual"
        self.original_window: dict | None = None
        self.action_original_window: dict | None = None
        self.live_process = None
        self.live_window_id: str | None = None
        self.original_a11y_flags: dict[str, bool] | None = None
        self.a11y_address: str | None = None

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
        apps = ([] if self.desktop_mode == "live" else
                [{"pid": int(pid), "start_ticks": self._start_ticks(int(pid))}
                 for pid in getattr(info, "apps", {})])
        launched_pid = getattr(self.live_process, "pid", None)
        if self.live_process is not None and isinstance(launched_pid, int):
            apps.append({"pid": launched_pid, "start_ticks": self._start_ticks(launched_pid)})
        process = getattr(session, "_process", None)
        pid = getattr(process, "pid", None)
        atspi = getattr(self.engine, "_atspi_proc", None)
        atspi_pid = getattr(atspi, "pid", None)
        record = {"schema": 1, "worker_pid": os.getpid(), "session_pid": pid,
                  "session_start_ticks": self._start_ticks(pid), "app_processes": apps,
                  "desktop_mode": self.desktop_mode,
                  "original_window": ({"id": self.original_window.get("id"), "app": self.original_window.get("app")}
                                      if self.original_window else None),
                  "action_original_window": ({"id": self.action_original_window.get("id"),
                                              "app": self.action_original_window.get("app")}
                                             if self.action_original_window else None),
                  "original_a11y_flags": self.original_a11y_flags,
                  "a11y_address": self.a11y_address,
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
        live_cleanup_ok = True
        if self.desktop_mode == "live" and self.engine is not None:
            live_cleanup_ok = self._stop_live_app()
        if self.engine is not None:
            result = self.engine.session_stop()
            if not isinstance(result, str) or not ("Session stopped" in result or "No session running" in result
                                                   or (self.desktop_mode == "live" and "Disconnected" in result)):
                raise ValueError("session_stop_unconfirmed")
            self.engine = None
        self.cleanup()
        if record is not None and not self._resources_gone(record):
            raise ValueError("session_cleanup_unconfirmed")
        if not live_cleanup_ok:
            raise ValueError("live_cleanup_unconfirmed")
        if self.journal_path is not None:
            self.journal_path.unlink(missing_ok=True)
        self.app = None
        self.document_path = None
        self.desktop_mode = "virtual"
        self.original_window = None
        self.action_original_window = None
        self.live_process = None
        self.live_window_id = None
        self.original_a11y_flags = None
        self.a11y_address = None
        self.journal_path = None
        return {"stopped": True, "cleanup_paths": []}

    def start(self, app: str, journal_path: str | None = None, desktop_mode: str = "virtual",
              temporary_a11y: bool = False) -> dict:
        if app not in {"kate", "firefox", "kcalc"} or self.engine is not None:
            raise ValueError("invalid_worker_start")
        if desktop_mode not in {"virtual", "live"}:
            raise ValueError("invalid_worker_start")
        if not isinstance(journal_path, str) or not journal_path:
            raise ValueError("invalid_worker_start")
        self.journal_path = Path(journal_path)
        self.app = app
        self.desktop_mode = desktop_mode
        from kwin_mcp.core import AutomationEngine

        if desktop_mode == "live":
            return self._start_live(app, temporary_a11y=temporary_a11y)

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
            from jevdesktop.benchmark_capture import _enable_virtual_atspi
            flags = _enable_virtual_atspi(self.engine)
            if not all(flags.values()):
                raise ValueError("atspi_setup_failed")
            self._wait_for_app_window(app, document_path=self.document_path, timeout_seconds=8.0)
            self._write_journal()
            return {"started": True, "atspi": flags, "cleanup_paths": [str(p) for p in self.paths],
                    **({"document_path": str(self.document_path)} if self.document_path else {})}
        except Exception:
            failed_document = self.document_path
            self.stop()
            if failed_document is not None:
                self._remove_empty_failed_document(failed_document)
            raise

    def _start_live(self, app: str, *, temporary_a11y: bool = False) -> dict:
        """Connect to the owner desktop and launch one PID-owned allowlisted app."""
        from kwin_mcp.core import AutomationEngine
        from jevdesktop.live_desktop_probe import (_active_row, _a11y_address, _a11y_flags, _raw_window_rows,
                                        _set_a11y_flags_at,
                                        _raw_pid_for_window_id, _restore_exact, _window_rows)
        self.engine = AutomationEngine()
        try:
            connected = self.engine.session_connect(keep_screenshots=False)
            if not isinstance(connected, str) or "Connected to live KWin session" not in connected:
                raise ValueError("live_session_unavailable")
            if "Input backend: KWin EIS" not in connected:
                raise ValueError("live_input_unavailable")
            self.original_window = _active_row(self.engine)
            initial_windows = _window_rows(self.engine)
            app_ids = {"kate": {"kate", "org.kde.kate"},
                       "firefox": {"firefox", "org.mozilla.firefox"},
                       "kcalc": {"kcalc", "org.kde.kcalc"}}[app]
            if any(str(row.get("app", "")).casefold() in app_ids for row in initial_windows):
                raise ValueError("live_app_already_open")
            flags = _a11y_flags(self.engine)
            if not all(flags.values()):
                if not temporary_a11y:
                    raise ValueError("live_temporary_a11y_required")
                self.original_a11y_flags = dict(flags)
                self.a11y_address = _a11y_address(self.engine)
                self._write_journal()
                enabled = _set_a11y_flags_at(self.a11y_address,
                                             {"IsEnabled": True, "ScreenReaderEnabled": True})
                if not all(enabled.values()):
                    raise ValueError("live_atspi_unavailable")
            self._write_journal()
            if app == "kate":
                self.document_path = self._new_document()
                # Kate detaches by default, so launch_app() would track only
                # the short-lived CLI process while the window belongs to an
                # untracked child. --block keeps the owned window in the
                # launched process for PID verification and exact cleanup.
                command = ["kate", "--new", "--block", str(self.document_path)]
                env = {"QT_ACCESSIBILITY": "1", "GTK_MODULES": "gail:atk-bridge"}
                self.engine._ensure_atspi_worker = MethodType(_ensure_bounded_atspi_worker, self.engine)
            elif app == "firefox":
                profile = _profile()
                self.paths.append(profile)
                command = ["firefox", "--no-remote", "--new-instance", "--profile", str(profile)]
                env = {"MOZ_ENABLE_ACCESSIBILITY": "1"}
            else:
                command = ["kcalc"]
                env = {"QT_ACCESSIBILITY": "1", "GTK_MODULES": "gail:atk-bridge"}
            self.live_process = self.engine._get_session().launch_app(command, extra_env=env)
            self._write_journal()
            deadline = time.monotonic() + 8.0
            selected = None
            while time.monotonic() < deadline:
                rows = _window_rows(self.engine)
                matches = [row for row in rows if str(row.get("app", "")).casefold() in app_ids]
                if len(matches) > 1:
                    raise ValueError("live_app_window_ambiguous")
                if matches:
                    selected = matches[0]
                    raw_pid = _raw_pid_for_window_id(_raw_window_rows(self.engine), selected["id"])
                    if raw_pid != self.live_process.pid:
                        raise ValueError("live_app_not_owned")
                    self.live_window_id = selected["id"]
                    break
                time.sleep(0.1)
            if selected is None:
                raise ValueError("live_app_window_unavailable")
            self._write_journal()
            if not _restore_exact(self.engine, self.original_window, _window_rows(self.engine)):
                raise ValueError("live_focus_restore_failed")
            return {"started": True, "desktop_mode": "live", "input_idle_detection": "unavailable",
                    "owner_present_override": True, "task_owned_window_id": self.live_window_id,
                    **({"document_path": str(self.document_path)} if self.document_path else {})}
        except Exception:
            failed_document = self.document_path
            try:
                self.stop()
            except Exception:
                pass
            if failed_document is not None:
                self._remove_empty_failed_document(failed_document)
            raise

    def _focus_live_app(self) -> bool:
        if self.desktop_mode != "live" or self.engine is None or not self.live_window_id:
            return False
        from jevdesktop.live_desktop_probe import _active_row, _restore_needle, _window_rows
        rows = _window_rows(self.engine)
        matches = [row for row in rows if row.get("id") == self.live_window_id]
        if len(matches) != 1 or not isinstance(matches[0].get("app"), str):
            return False
        row = matches[0]
        caption = row.get("caption")
        if not isinstance(caption, str) or not caption:
            return False
        needle = _restore_needle(row, rows)
        if needle is None:
            return False
        response = self.engine._run_kwin_query({"op": "activate", "app_name": needle})
        return (isinstance(response, dict) and response.get("ok") is True and bool(response.get("result"))
                and _active_row(self.engine).get("id") == self.live_window_id)

    def restore_live_focus(self, *, baseline: bool = False) -> bool:
        target = self.action_original_window or (self.original_window if baseline else None)
        if self.desktop_mode != "live" or self.engine is None or not target:
            return False
        from jevdesktop.live_desktop_probe import _restore_exact, _window_rows
        restored = _restore_exact(self.engine, target, _window_rows(self.engine))
        if restored and self.action_original_window is not None:
            self.action_original_window = None
            self._write_journal()
        return restored

    def prepare_live_action(self) -> dict:
        if self.desktop_mode != "live" or self.engine is None:
            raise ValueError("live_mode_required")
        from jevdesktop.live_desktop_probe import _active_row, _restore_needle, _window_rows
        original = _active_row(self.engine)
        if _restore_needle(original, _window_rows(self.engine)) is None:
            raise ValueError("live_original_focus_unrestorable")
        self.action_original_window = original
        self._write_journal()
        if not self._focus_live_app():
            raise ValueError("live_target_focus_failed")
        return {"focused": True}

    def _stop_live_app(self) -> bool:
        """Terminate only the exact process returned by this task's launch."""
        from jevdesktop.live_desktop_probe import _active_row, _restore_needle, _window_rows
        focus_snapshot_ok = True
        if self.action_original_window is None:
            try:
                current = _active_row(self.engine)
                if current.get("id") != self.live_window_id:
                    if _restore_needle(current, _window_rows(self.engine)) is None:
                        focus_snapshot_ok = False
                    else:
                        self.action_original_window = current
                        self._write_journal()
            except Exception:
                focus_snapshot_ok = False
        launched = self.live_process
        process = getattr(launched, "process", launched)
        process_ok = True
        if process is not None:
            try:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except Exception:
                        process.kill()
                        process.wait(timeout=2)
                process_ok = process.poll() is not None
            except Exception:
                process_ok = False
        window_ok = True
        if self.live_window_id:
            try:
                until = time.monotonic() + 3
                while time.monotonic() < until:
                    if not any(row.get("id") == self.live_window_id for row in _window_rows(self.engine)):
                        break
                    time.sleep(0.1)
                window_ok = not any(row.get("id") == self.live_window_id for row in _window_rows(self.engine))
            except Exception:
                window_ok = False
        try:
            restored = (self.restore_live_focus(baseline=True) if focus_snapshot_ok else False)
        except Exception:
            restored = False
        a11y_ok = True
        if self.original_a11y_flags is not None and self.a11y_address is not None:
            try:
                from jevdesktop.live_desktop_probe import _set_a11y_flags_at
                actual = _set_a11y_flags_at(self.a11y_address, self.original_a11y_flags)
                a11y_ok = actual == self.original_a11y_flags
            except Exception:
                a11y_ok = False
        return process_ok and window_ok and restored and a11y_ok

    def _wait_for_app_window(self, app: str, *, document_path: Path | None = None,
                             timeout_seconds: float = 8.0, poll_seconds: float = 0.1) -> None:
        """Wait for one fresh, mapped window of the app launched in this virtual session."""
        app_ids = {
            "kate": {"kate", "org.kde.kate"},
            "firefox": {"firefox", "org.mozilla.firefox"},
            "kcalc": {"kcalc", "org.kde.kcalc"},
        }.get(app)
        if app_ids is None:
            raise ValueError("invalid_worker_start")
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                response = self.engine._run_kwin_query({})
                windows = response.get("result", []) if isinstance(response, dict) and response.get("ok") is True else []
                ready = []
                for window in windows:
                    if not isinstance(window, dict) or str(window.get("app", "")).casefold() not in app_ids:
                        continue
                    frame = window.get("frame")
                    mapped = (isinstance(window.get("id"), str) and isinstance(frame, dict)
                              and all(type(frame.get(key)) is int for key in ("x", "y", "width", "height"))
                              and frame["width"] > 0 and frame["height"] > 0)
                    title_ready = document_path is None or (
                        isinstance(window.get("caption"), str) and document_path.name in window["caption"])
                    if mapped and title_ready:
                        ready.append(window)
                if len(ready) == 1:
                    return
            except Exception:
                # Session startup races include KWin becoming queryable after
                # the app process; keep polling only within this bounded gate.
                pass
            time.sleep(poll_seconds)
        raise ValueError("document_open_unconfirmed" if document_path is not None else "app_window_open_unconfirmed")

    def call(self, method: str, params: dict):
        if method == "start":
            return self.start(params.get("app"), params.get("journal_path"), params.get("desktop_mode", "virtual"),
                              params.get("temporary_a11y", False))
        if method == "focus_live_app":
            return self.prepare_live_action()
        if method == "restore_live_focus":
            return {"restored": self.restore_live_focus()}
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
        if method == "mouse_scroll":
            delta, steps = params.get("delta"), params.get("steps")
            if (type(delta) is not int or not 1 <= abs(delta) <= 8
                    or type(steps) is not int or not 1 <= steps <= 8):
                raise ValueError("scroll_argument_invalid")
            return self.engine.mouse_scroll(params["x"], params["y"], delta,
                                            discrete=True, steps=steps)
        if method == "keyboard_key":
            key = params.get("key")
            if key not in {"Return", "ctrl+a"}:
                raise ValueError("unsupported_key")
            return self.engine.keyboard_key(key)
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
                       "live_session_unavailable", "live_input_unavailable", "live_app_already_open",
                       "live_atspi_unavailable", "live_temporary_a11y_required", "live_app_window_ambiguous",
                       "live_app_not_owned", "live_app_window_unavailable", "live_focus_restore_failed",
                       "live_cleanup_unconfirmed", "worker_method_not_allowed", "document_unavailable",
                       "document_operation_not_allowed", "document_create_failed", "document_too_large",
                       "document_open_unconfirmed"}
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
        from jevdesktop.desktop_service import DesktopWorkerClient
        return DesktopWorkerClient
    raise AttributeError(name)


if __name__ == "__main__":
    raise SystemExit(main())
