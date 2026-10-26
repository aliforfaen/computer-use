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
from typing import Any
from urllib.parse import quote


from jevdesktop import paths
from jevdesktop.transactions import SHORTCUT_KEYS

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
        self.access_scope = "selected_application"
        self.profile: dict[str, Any] | None = None
        self.semantic_candidates = True
        self.original_window: dict | None = None
        self.action_original_window: dict | None = None
        self.live_process = None
        self.live_window_id: str | None = None
        self.live_root_window_id: str | None = None
        self.live_root_pid: int | None = None
        self.live_selected_window: dict[str, Any] | None = None
        self.live_attached = False
        self.original_a11y_flags: dict[str, bool] | None = None
        self.a11y_address: str | None = None
        self.access_scope = "selected_application"
        self.selected_window_id: str | None = None
        self.selected_window: dict[str, Any] | None = None

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
                  "access_scope": self.access_scope,
                  "original_window": ({"id": self.original_window.get("id"), "app": self.original_window.get("app")}
                                      if self.original_window else None),
                  "action_original_window": ({"id": self.action_original_window.get("id"),
                                              "app": self.action_original_window.get("app")}
                                             if self.action_original_window else None),
                  "original_a11y_flags": self.original_a11y_flags,
                  "attached_window": self.live_attached,
                  "live_root_window_id": self.live_root_window_id,
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
        self.profile = None
        self.semantic_candidates = True
        self.original_window = None
        self.action_original_window = None
        self.live_process = None
        self.live_window_id = None
        self.live_root_window_id = None
        self.live_root_pid = None
        self.live_selected_window = None
        self.live_attached = False
        self.original_a11y_flags = None
        self.a11y_address = None
        self.journal_path = None
        return {"stopped": True, "cleanup_paths": []}

    @staticmethod
    def _validate_profile(app: str, profile: dict[str, Any] | None) -> dict[str, Any]:
        if profile is None:
            from jevdesktop.app_profiles import profile_for

            resolved = profile_for(app)
            profile = {"id": resolved.id, "command": list(resolved.command),
                       "window_app_ids": list(resolved.window_app_ids),
                       "atspi_app": resolved.atspi_app, "aliases": list(resolved.aliases)}
        if (not isinstance(profile, dict) or profile.get("id") != app
                or set(profile) - {"id", "command", "window_app_ids", "atspi_app", "aliases"}):
            raise ValueError("invalid_worker_profile")
        command = profile.get("command")
        app_ids = profile.get("window_app_ids")
        atspi_app = profile.get("atspi_app")
        aliases = profile.get("aliases", [])
        if (not isinstance(command, list) or not command or len(command) > 32
                or any(not isinstance(item, str) or not item or "\x00" in item for item in command)
                or not isinstance(app_ids, list) or not app_ids
                or any(not isinstance(item, str) or not item for item in app_ids)
                or not isinstance(atspi_app, str) or not atspi_app
                or not isinstance(aliases, list)
                or any(not isinstance(item, str) or not item for item in aliases)):
            raise ValueError("invalid_worker_profile")
        return {"id": app, "command": list(command), "window_app_ids": list(app_ids),
                "atspi_app": atspi_app, "aliases": list(aliases)}

    @staticmethod
    def _profile_for_window_identity(identity: str) -> dict[str, Any]:
        from jevdesktop.app_profiles import AppProfile, profile_for_window_identity

        profile = profile_for_window_identity(identity) or AppProfile(
            identity, (), (identity,), identity, ())
        return {"id": profile.id, "command": list(profile.command),
                "window_app_ids": list(profile.window_app_ids), "atspi_app": profile.atspi_app,
                "aliases": list(profile.aliases)}

    def start(self, app: str, journal_path: str | None = None, desktop_mode: str = "virtual",
              temporary_a11y: bool = False, *, profile: dict[str, Any] | None = None,
              window_id: str | None = None, attach: bool = False,
              semantic_candidates: bool = True, access_scope: str = "selected_application") -> dict:
        if (not isinstance(app, str) or (not app and access_scope == "selected_application") or self.engine is not None
                or type(temporary_a11y) is not bool or type(attach) is not bool
                or type(semantic_candidates) is not bool):
            raise ValueError("invalid_worker_start")
        if desktop_mode not in {"virtual", "live"}:
            raise ValueError("invalid_worker_start")
        if desktop_mode == "virtual" and (attach or window_id is not None):
            raise ValueError("invalid_worker_start")
        if access_scope not in {"selected_application", "full_virtual_desktop", "full_live_desktop"}:
            raise ValueError("invalid_worker_start")
        if desktop_mode == "live" and access_scope == "selected_application" and (attach is not (window_id is not None)):
            raise ValueError("invalid_worker_start")
        if window_id is not None and (not isinstance(window_id, str) or not window_id):
            raise ValueError("invalid_worker_start")
        if not isinstance(journal_path, str) or not journal_path:
            raise ValueError("invalid_worker_start")
        self.access_scope = access_scope
        self.profile = self._validate_profile(app, profile) if access_scope == "selected_application" else None
        self.journal_path = Path(journal_path)
        self.app = app
        self.desktop_mode = desktop_mode
        self.semantic_candidates = semantic_candidates
        from kwin_mcp.core import AutomationEngine

        if desktop_mode == "live":
            return self._start_live(temporary_a11y=temporary_a11y, window_id=window_id, attach=attach,
                                    full_scope=access_scope == "full_live_desktop")

        if access_scope == "full_virtual_desktop":
            self.engine = AutomationEngine()
            try:
                start = self.engine.session_start(app_command="", screen_width=1280, screen_height=800,
                                                  isolate_home=True, keep_home=False, keep_screenshots=False)
                if not isinstance(start, str) or "Input backend: KWin EIS" not in start:
                    raise ValueError("virtual_session_unavailable")
                self._write_journal()
                return {"started": True, "desktop_mode": "virtual", "access_scope": access_scope,
                        "semantic_candidates": False, "selected_window_id": None, "cleanup_paths": []}
            except Exception:
                try: self.stop()
                except Exception: pass
                raise

        env = ({"QT_ACCESSIBILITY": "1", "GTK_MODULES": "gail:atk-bridge"}
               if self.semantic_candidates else {})
        profile = self.profile
        if profile is None:
            raise ValueError("invalid_worker_profile")
        command_argv = list(profile["command"])
        if profile["id"] == "kate":
            document = self._new_document()
            self.document_path = document
            command_argv.append(str(document))
        elif profile["id"] == "firefox":
            if not FIXTURE.is_file():
                raise ValueError("fixture_unavailable")
            firefox_profile = _profile()
            self.paths.append(firefox_profile)
            command_argv.extend(("--profile", str(firefox_profile),
                                 "file://" + quote(str(FIXTURE.resolve()))))
            if self.semantic_candidates:
                env["MOZ_ENABLE_ACCESSIBILITY"] = "1"
        command = shlex.join(command_argv)
        self.engine = AutomationEngine()
        # kwin-mcp 0.10.0 launches its accessibility reader in a child process.
        # Kate needs complete text for owned-document verification, so reuse
        # its bus identity, retry, timeout and teardown machinery with our
        # bounded text cap only for Kate sessions.
        if profile["id"] == "kate" and self.semantic_candidates:
            self.engine._ensure_atspi_worker = MethodType(_ensure_bounded_atspi_worker, self.engine)
        try:
            start = self.engine.session_start(app_command=command, screen_width=1280, screen_height=800,
                                              isolate_home=True, keep_home=False, keep_screenshots=False, env=env)
            if not isinstance(start, str) or "Input backend: KWin EIS" not in start:
                raise ValueError("virtual_session_unavailable")
            self._write_journal()
            flags = {}
            if self.semantic_candidates:
                from jevdesktop.benchmark_capture import _enable_virtual_atspi
                flags = _enable_virtual_atspi(self.engine)
                if not all(flags.values()):
                    raise ValueError("atspi_setup_failed")
            self._wait_for_app_window(app, document_path=self.document_path, timeout_seconds=8.0)
            self._write_journal()
            return {"started": True, "atspi": flags,
                    "semantic_candidates": self.semantic_candidates,
                    "cleanup_paths": [str(p) for p in self.paths],
                    **({"document_path": str(self.document_path)} if self.document_path else {})}
        except Exception:
            failed_document = self.document_path
            self.stop()
            if failed_document is not None:
                self._remove_empty_failed_document(failed_document)
            raise

    def _start_live(self, *, temporary_a11y: bool = False, window_id: str | None = None,
                    attach: bool = False, full_scope: bool = False) -> dict[str, Any]:
        """Connect to KWin and bind this task to an exact launched or attached window."""
        from kwin_mcp.core import AutomationEngine
        from jevdesktop.live_desktop_probe import _a11y_address, _a11y_flags, _active_row, _set_a11y_flags_at
        self.engine = AutomationEngine()
        self.live_attached = attach
        try:
            connected = self.engine.session_connect(keep_screenshots=False)
            if not isinstance(connected, str) or "Connected to live KWin session" not in connected:
                raise ValueError("live_session_unavailable")
            if "Input backend: KWin EIS" not in connected:
                raise ValueError("live_input_unavailable")
            from jevdesktop.native_windows import collect_windows

            initial_rows = collect_windows()
            active = next((row for row in initial_rows if row.get("active")), None)
            if active is None and not full_scope:
                raise ValueError("live_original_focus_unavailable")
            self.original_window = active

            if self.semantic_candidates:
                flags = _a11y_flags(self.engine)
                if not all(flags.values()):
                    if not temporary_a11y:
                        raise ValueError("live_temporary_a11y_required")
                    self.original_a11y_flags = dict(flags)
                    self.a11y_address = _a11y_address(self.engine)
                    self._write_journal()
                    enabled = _set_a11y_flags_at(self.a11y_address, {key: True for key in flags})
                    if not all(enabled.values()):
                        raise ValueError("live_atspi_unavailable")

            if full_scope:
                selected = next((row for row in initial_rows if row.get("id") == window_id), None) if window_id else active
                if window_id is not None and selected is None:
                    raise ValueError("live_selected_window_unavailable")
                self.live_attached = True
                self.live_root_window_id = selected.get("id") if selected else None
                self.live_root_pid = selected.get("pid") if selected else None
                self.live_selected_window = selected
                self.live_window_id = self.live_root_window_id
                if selected is not None:
                    self.profile = self._profile_for_window_identity(str(selected.get("app", "")))
                self._write_journal()
                if selected is not None and not self._focus_live_app(allow_switch=True).get("focused"):
                    raise ValueError("live_target_focus_failed")
                return {"started": True, "desktop_mode": "live", "access_scope": self.access_scope,
                        "semantic_candidates": self.semantic_candidates, "input_idle_detection": "unavailable",
                        "attached": True, "task_owned_window_id": None, "root_window_id": self.live_root_window_id,
                        "selected_window_id": self.live_window_id, "selected_window": selected,
                        "atspi_flags": _a11y_flags(self.engine) if self.semantic_candidates else None}
            profile = self.profile
            if profile is None:
                raise ValueError("invalid_worker_profile")
            app_ids = {value.casefold() for value in profile["window_app_ids"]}
            if attach:
                matches = [row for row in initial_rows if row.get("id") == window_id]
                if len(matches) != 1:
                    raise ValueError("live_selected_window_unavailable")
                selected = matches[0]
                if str(selected.get("app", "")).casefold() not in app_ids:
                    raise ValueError("live_window_app_mismatch")
                if type(selected.get("pid")) is not int or selected["pid"] <= 0:
                    raise ValueError("live_window_pid_unavailable")
                self.live_root_window_id = selected["id"]
                self.live_root_pid = selected["pid"]
                self.live_selected_window = selected
            else:
                if any(str(row.get("app", "")).casefold() in app_ids for row in initial_rows):
                    raise ValueError("live_app_already_open")
                command = list(profile["command"])
                env = ({"QT_ACCESSIBILITY": "1", "GTK_MODULES": "gail:atk-bridge"}
                       if self.semantic_candidates else {})
                if profile["id"] == "kate":
                    self.document_path = self._new_document()
                    command.append(str(self.document_path))
                    if self.semantic_candidates:
                        self.engine._ensure_atspi_worker = MethodType(_ensure_bounded_atspi_worker, self.engine)
                elif profile["id"] == "firefox" and self.semantic_candidates:
                    env = {"MOZ_ENABLE_ACCESSIBILITY": "1"}
                self.live_process = self.engine._get_session().launch_app(command, extra_env=env)
                self._write_journal()
                deadline = time.monotonic() + 8.0
                selected = None
                while time.monotonic() < deadline:
                    rows = collect_windows()
                    matches = [row for row in rows
                               if str(row.get("app", "")).casefold() in app_ids
                               and row.get("pid") == self.live_process.pid]
                    if len(matches) > 1:
                        roots = [row for row in matches if row.get("normal") and not row.get("dialog")
                                 and not row.get("popup") and not row.get("transient")]
                        if len(roots) != 1:
                            raise ValueError("live_app_window_ambiguous")
                        selected = roots[0]
                    elif matches:
                        selected = matches[0]
                    if selected is not None:
                        break
                    time.sleep(0.1)
                if selected is None:
                    raise ValueError("live_app_window_unavailable")
                self.live_root_window_id = selected["id"]
                self.live_root_pid = selected["pid"]
                self.live_selected_window = selected

            self.live_window_id = self.live_root_window_id
            self._write_journal()
            focus = self._focus_live_app(allow_switch=True)
            if not focus.get("focused"):
                raise ValueError("live_target_focus_failed")
            self._write_journal()
            return {
                "started": True,
                "desktop_mode": "live",
                "semantic_candidates": self.semantic_candidates,
                "input_idle_detection": "unavailable",
                "attached": self.live_attached,
                "task_owned_window_id": None if self.live_attached else self.live_root_window_id,
                "root_window_id": self.live_root_window_id,
                "selected_window_id": self.live_window_id,
                "selected_window": self.live_selected_window,
                "atspi_flags": _a11y_flags(self.engine) if self.semantic_candidates else None,
                **({"document_path": str(self.document_path)} if self.document_path else {}),
            }
        except Exception:
            failed_document = self.document_path
            try:
                self.stop()
            except Exception:
                pass
            if failed_document is not None:
                self._remove_empty_failed_document(failed_document)
            raise

    def _native_windows(self) -> list[dict[str, Any]]:
        from jevdesktop.native_windows import collect_windows

        return collect_windows()

    @staticmethod
    def _format_window_row(row: dict[str, Any], *, active: bool | None = None) -> str:
        frame, client = row.get("frame"), row.get("client")
        if not isinstance(frame, dict) or not isinstance(client, dict):
            raise ValueError("live_window_geometry_unavailable")
        marker = " [active]" if (row.get("active") if active is None else active) else ""
        title = json.dumps(str(row.get("caption", "")), ensure_ascii=False)[1:-1]
        return (f'Windows (1):\n- {row["app"]} "{title}"{marker}\n'
                f'    id:     {row["id"]}\n'
                f'    frame:  ({frame["x"]}, {frame["y"]}, {frame["width"]}x{frame["height"]})\n'
                f'    client: ({client["x"]}, {client["y"]}, {client["width"]}x{client["height"]})')

    def _workspace_discover(self) -> dict[str, Any]:
        from jevdesktop.native_windows import workspace_snapshot

        if self.engine is not None and self.desktop_mode == "live":
            return workspace_snapshot()
        if self.engine is not None and self.desktop_mode == "virtual":
            from jevdesktop.native_windows import workspace_snapshot_with_env
            return {**workspace_snapshot_with_env(self.engine._session_env()),
                    "desktop_mode": "virtual", "isolated": True}
        from kwin_mcp.core import AutomationEngine

        engine = AutomationEngine()
        try:
            connected = engine.session_connect(keep_screenshots=False)
            if not isinstance(connected, str) or "Connected to live KWin session" not in connected:
                raise ValueError("live_session_unavailable")
            return workspace_snapshot()
        finally:
            try:
                engine.session_stop()
            except Exception:
                pass

    @staticmethod
    def _window_for_element(element: dict[str, Any], rows: list[dict[str, Any]]) -> str | None:
        if element.get("mapped") is not True:
            return None
        try:
            x, y, width, height = (int(element[key]) for key in ("x", "y", "width", "height"))
        except (KeyError, TypeError, ValueError):
            return None
        if width <= 0 or height <= 0:
            return None
        matches = []
        for row in rows:
            client = row.get("client")
            if not isinstance(client, dict):
                continue
            left, top = client["x"], client["y"]
            right, bottom = left + client["width"], top + client["height"]
            if x >= left and y >= top and x + width <= right and y + height <= bottom:
                matches.append((client["width"] * client["height"], row["id"]))
        if not matches:
            return None
        smallest = min(area for area, _window_id in matches)
        winners = [window_id for area, window_id in matches if area == smallest]
        return winners[0] if len(winners) == 1 else None

    def _require_live_focus(self) -> dict[str, Any] | None:
        if self.desktop_mode != "live":
            return None
        if self.engine is None or not self.live_window_id:
            raise ValueError("live_target_window_unavailable")
        state = self._task_window_state(self._native_windows())
        active = state["active_window"]
        if (not state["active_is_related"] or active is None
                or active.get("id") != self.live_window_id):
            raise ValueError("live_selected_window_not_active")
        return active

    def _require_live_point(self, x: int, y: int) -> None:
        row = self._require_live_focus()
        if row is None:
            return
        client = row.get("client")
        if not isinstance(client, dict):
            raise ValueError("live_target_geometry_unavailable")
        if not (client["x"] <= x < client["x"] + client["width"]
                and client["y"] <= y < client["y"] + client["height"]):
            raise ValueError("live_input_outside_target")

    def _task_window_state(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        """Classify app windows by selected ID, PID and KWin transient ancestry."""
        root_id = self.live_root_window_id
        profile = self.profile
        if not root_id or not profile:
            raise ValueError("live_target_window_unavailable")
        by_id = {row["id"]: row for row in rows if isinstance(row.get("id"), str)}
        root = by_id.get(root_id)
        app_ids = {str(value).casefold() for value in profile["window_app_ids"]}
        root_pid = self.live_root_pid if self.live_root_pid is not None else (root.get("pid") if root else None)
        related_ids = {root_id}
        changed = True
        while changed:
            changed = False
            for row in rows:
                row_id = row.get("id")
                if not isinstance(row_id, str) or row_id in related_ids:
                    continue
                parent_id = row.get("transient_for")
                identity_match = (str(row.get("app", "")).casefold() in app_ids
                                  or (type(root_pid) is int and root_pid > 0 and row.get("pid") == root_pid))
                same_process_dialog = (not parent_id and type(root_pid) is int and root_pid > 0 and row.get("pid") == root_pid
                                       and (row.get("dialog") or row.get("popup") or row.get("transient")))
                if ((parent_id in related_ids and identity_match) or same_process_dialog):
                    related_ids.add(row_id)
                    changed = True
        related = [row for row in rows if row.get("id") in related_ids]
        background = [row for row in rows if row.get("id") not in related_ids]
        external_transients = [row for row in background if row.get("transient_for") in related_ids]
        active = next((row for row in rows if row.get("active") is True), None)
        return {
            "root_window": root,
            "related_windows": related,
            "background_windows": background,
            "external_transients": external_transients,
            "active_window": active,
            "active_is_related": bool(active and active.get("id") in related_ids),
            "related_ids": related_ids,
        }

    def _focus_live_app(self, *, allow_switch: bool = False) -> dict[str, Any]:
        if self.desktop_mode != "live" or self.engine is None or not self.live_root_window_id:
            raise ValueError("live_mode_required")
        from jevdesktop.native_windows import activate_window_id

        rows = self._native_windows()
        state = self._task_window_state(rows)
        active = state["active_window"]
        selected = None
        interruption = None
        if state["active_is_related"] and active is not None:
            selected = active
        elif active and not allow_switch and (active.get("popup") or active.get("dialog") or active.get("transient")):
            interruption = active
        else:
            target = state["root_window"]
            if target is None:
                remaining = [row for row in state["related_windows"] if row.get("id") != self.live_root_window_id]
                if len(remaining) == 1:
                    target = remaining[0]
                elif remaining:
                    interruption = {"reason": "task_window_ambiguous", "windows": remaining}
                else:
                    interruption = {"reason": "task_window_unavailable"}
            if target is None:
                self.live_window_id = None
                self.live_selected_window = None
                return {
                    "focused": False,
                    "root_window_id": self.live_root_window_id,
                    "selected_window_id": None,
                    "selected_window": None,
                    "related_windows": state["related_windows"],
                    "background_windows": state["background_windows"],
                    "unrelated_popups": [row for row in state["background_windows"]
                                         if row.get("popup") or row.get("dialog") or row.get("transient")],
                    "interruption": interruption,
                }
            if not activate_window_id(target["id"]):
                return {"focused": False, "root_window_id": self.live_root_window_id,
                        "selected_window_id": None, "selected_window": None,
                        "related_windows": state["related_windows"],
                        "background_windows": state["background_windows"],
                        "unrelated_popups": [], "interruption": {"reason": "live_target_focus_failed"}}
            rows = self._native_windows()
            state = self._task_window_state(rows)
            active = state["active_window"]
            if state["active_is_related"] and active is not None:
                selected = active
            else:
                return {"focused": False, "root_window_id": self.live_root_window_id,
                        "selected_window_id": None, "selected_window": None,
                        "related_windows": state["related_windows"],
                        "background_windows": state["background_windows"],
                        "unrelated_popups": [row for row in state["background_windows"]
                                             if row.get("popup") or row.get("dialog") or row.get("transient")],
                        "interruption": {"reason": "live_target_focus_failed"}}
        if selected is not None:
            self.live_window_id = selected["id"]
            self.live_selected_window = selected
        result = {
            "focused": selected is not None,
            "root_window_id": self.live_root_window_id,
            "selected_window_id": self.live_window_id,
            "selected_window": self.live_selected_window,
            "related_windows": state["related_windows"],
            "background_windows": state["background_windows"],
            "unrelated_popups": [row for row in state["background_windows"]
                                 if row.get("popup") or row.get("dialog") or row.get("transient")],
            "interruption": interruption,
        }
        return result

    def restore_live_focus(self, *, baseline: bool = False) -> bool:
        """Restore the focus captured when the live task started, normally at stop."""
        if self.desktop_mode != "live" or self.engine is None:
            return False
        if self.original_window is None:
            return self.access_scope == "full_live_desktop"
        from jevdesktop.native_windows import activate_window_id

        target_id = self.original_window.get("id")
        return (isinstance(target_id, str) and activate_window_id(target_id)
                and next((row.get("id") for row in self._native_windows() if row.get("active")), None) == target_id)

    def prepare_live_action(self) -> dict[str, Any]:
        """Keep task focus in front; surface unrelated popups without acting on them."""
        return self._focus_live_app()

    def _restore_a11y_flags(self) -> bool:
        if self.original_a11y_flags is None or self.a11y_address is None:
            return True
        try:
            from jevdesktop.live_desktop_probe import _set_a11y_flags_at

            return _set_a11y_flags_at(self.a11y_address, self.original_a11y_flags) == self.original_a11y_flags
        except Exception:
            return False

    def _stop_live_app(self) -> bool:
        """Stop only a task-launched process; an attached application is never closed."""
        from jevdesktop.native_windows import collect_windows

        process_ok = True
        window_ok = True
        if not self.live_attached:
            launched = self.live_process
            process = getattr(launched, "process", launched)
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
            if self.live_root_window_id:
                try:
                    until = time.monotonic() + 3
                    while time.monotonic() < until:
                        if not any(row.get("id") == self.live_root_window_id for row in collect_windows()):
                            break
                        time.sleep(0.1)
                    window_ok = not any(row.get("id") == self.live_root_window_id for row in collect_windows())
                except Exception:
                    window_ok = False
        try:
            restored = self.restore_live_focus(baseline=True)
        except Exception:
            restored = False
        return process_ok and window_ok and restored and self._restore_a11y_flags()

    def _wait_for_app_window(self, app: str, *, document_path: Path | None = None,
                             timeout_seconds: float = 8.0, poll_seconds: float = 0.1) -> None:
        """Wait for one fresh, mapped window of the app launched in this virtual session."""
        app_ids = ({value.casefold() for value in self.profile["window_app_ids"]}
                   if self.profile is not None else {
                       "kate": {"kate", "org.kde.kate"},
                       "firefox": {"firefox", "org.mozilla.firefox"},
                       "kcalc": {"kcalc", "org.kde.kcalc"},
                   }.get(app))
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
                              params.get("temporary_a11y", False),
                              profile=params.get("profile"), window_id=params.get("window_id"),
                              attach=params.get("attach", False),
                              semantic_candidates=params.get("semantic_candidates", True),
                              access_scope=params.get("access_scope", "selected_application"))
        if method == "select_window":
            if self.access_scope not in {"full_live_desktop", "full_virtual_desktop"}:
                raise ValueError("access_scope_mismatch")
            target_id = params.get("window_id")
            if not isinstance(target_id, str) or not target_id:
                raise ValueError("invalid_params")
            if self.access_scope == "full_virtual_desktop":
                from jevdesktop.native_windows import workspace_snapshot_with_env
                rows = workspace_snapshot_with_env(self.engine._session_env())["windows"]
            else:
                rows = self._native_windows()
            selected = next((row for row in rows if row.get("id") == target_id), None)
            if selected is None:
                raise ValueError("live_selected_window_unavailable")
            if self.access_scope == "full_virtual_desktop":
                from jevdesktop.native_windows import activate_window_id_with_env
                focused = activate_window_id_with_env(target_id, self.engine._session_env())
            else:
                from jevdesktop.native_windows import activate_window_id
                focused = activate_window_id(target_id)
            if not focused:
                raise ValueError("live_target_focus_failed")
            self.live_root_window_id = target_id
            self.live_root_pid = selected.get("pid")
            self.live_window_id = target_id
            self.live_selected_window = selected
            self.selected_window_id = target_id
            self.selected_window = selected
            identity = str(selected.get("app", ""))
            if identity:
                self.profile = self._profile_for_window_identity(identity)
            return {"selected_window_id": target_id, "selected_window": selected, "access_scope": self.access_scope}
        if method == "check_selected_window_active":
            if self.access_scope != "full_virtual_desktop" or self.desktop_mode != "virtual":
                raise ValueError("access_scope_mismatch")
            if not isinstance(self.selected_window_id, str) or not self.selected_window_id:
                raise ValueError("window_selection_required")
            from jevdesktop.native_windows import workspace_snapshot_with_env
            snapshot = workspace_snapshot_with_env(self.engine._session_env())
            rows = snapshot["windows"]
            selected = next((row for row in rows if row.get("id") == self.selected_window_id), None)
            active = next((row for row in rows if row.get("active")), None)
            if selected is None:
                raise ValueError("live_selected_window_unavailable")
            if active is None or active.get("id") != self.selected_window_id:
                raise ValueError("virtual_selected_window_changed")
            return {"selected_window_id": self.selected_window_id, "active_window_id": active["id"]}
        if method == "launch_app":
            if self.desktop_mode != "virtual" or self.access_scope != "full_virtual_desktop":
                raise ValueError("access_scope_mismatch")
            app = params.get("profile_id")
            if not isinstance(app, str) or not app:
                raise ValueError("invalid_params")
            from jevdesktop.app_profiles import profile_for
            profile = profile_for(app)
            process = self.engine._get_session().launch_app(list(profile.command))
            self.selected_window_id = None
            self.selected_window = None
            self.live_root_window_id = None
            self.live_root_pid = None
            self.live_window_id = None
            self.live_selected_window = None
            self.profile = {"id": profile.id, "command": list(profile.command),
                            "window_app_ids": list(profile.window_app_ids), "atspi_app": profile.atspi_app,
                            "aliases": list(profile.aliases)}
            self.app = profile.id
            self.live_process = process  # recorded as an owned app process in the cleanup journal
            self._write_journal()
            return {"launched": True, "profile_id": profile.id, "pid": process.pid}
        if method == "workspace_discover":
            return self._workspace_discover()
        if method == "focus_live_app":
            return self.prepare_live_action()
        if method == "restore_live_focus":
            return {"restored": self.restore_live_focus()}
        if method == "stop":
            return self.stop()
        if self.engine is None:
            raise ValueError("session_not_started")
        if method == "window_query":
            if self.desktop_mode == "live":
                rows = self._native_windows()
                state = (self._task_window_state(rows) if self.live_root_window_id else None)
                related_ids = state["related_ids"] if state else set()
                result = [{**row, "task_related": row.get("id") in related_ids,
                           "selected": row.get("id") == self.live_window_id,
                           "root": row.get("id") == self.live_root_window_id}
                          for row in rows]
                selected_row = next((row for row in rows if row.get("id") == self.live_window_id), None)
                return {"ok": True, "result": result,
                        "root_window_id": self.live_root_window_id,
                        "selected_window_id": self.live_window_id,
                        "selected_window_present": selected_row is not None,
                        **({"selected_window": selected_row} if selected_row is not None else {})}
            if self.access_scope == "full_virtual_desktop":
                from jevdesktop.native_windows import workspace_snapshot_with_env
                snapshot = workspace_snapshot_with_env(self.engine._session_env())
                return {"ok": True, "result": snapshot["windows"],
                        "selected_window_id": self.selected_window_id}
            return self.engine._run_kwin_query({})
        if method == "atspi_find":
            app = params.get("app")
            if not self.semantic_candidates:
                raise ValueError("semantic_candidates_disabled")
            if self.desktop_mode == "live":
                if self.profile is None or app not in {self.profile["id"], self.profile["atspi_app"]}:
                    raise ValueError("app_not_allowed")
                app = self.profile["atspi_app"]
            else:
                if self.profile is None or app not in {self.profile["id"], self.profile["atspi_app"]}:
                    raise ValueError("app_not_allowed")
                app = self.profile["atspi_app"]
            # Make the pinned private worker explicit and journal its PID
            # before the first AT-SPI request can outlive a failed RPC.
            self.engine._ensure_atspi_worker()
            self._write_journal()
            result = self.engine._run_atspi("find", query="", app_name=app)
            self._write_journal()
            if self.desktop_mode == "live" and isinstance(result, dict) and result.get("ok") is True:
                rows = self._native_windows()
                state = self._task_window_state(rows)
                active = state["active_window"]
                if (not state["active_is_related"] or active is None
                        or active.get("id") != self.live_window_id):
                    raise ValueError("live_selected_window_changed")
                visible_rows = state["related_windows"]
                selected_id = self.live_window_id
                elements = result.get("result")
                if not isinstance(elements, list):
                    raise ValueError("atspi_result_invalid")
                scoped = []
                for element in elements:
                    if not isinstance(element, dict):
                        continue
                    mapped_id = self._window_for_element(element, visible_rows)
                    if mapped_id == selected_id:
                        scoped.append({**element, "window_id": mapped_id})
                result = {**result, "result": scoped, "selected_window_id": selected_id}
            return result
        if method == "window_geometry":
            if self.desktop_mode == "live":
                rows = self._native_windows()
                target = next((row for row in rows if row.get("id") == self.live_window_id), None)
                if target is None:
                    raise ValueError("live_target_window_unavailable")
                self.live_selected_window = target
                return self._format_window_row(target)
            if self.access_scope == "full_virtual_desktop" and self.selected_window_id is not None:
                from jevdesktop.native_windows import workspace_snapshot_with_env
                rows = workspace_snapshot_with_env(self.engine._session_env())["windows"]
                target = next((row for row in rows if row.get("id") == self.selected_window_id), None)
                if target is None:
                    raise ValueError("live_target_window_unavailable")
                self.selected_window = target
                return self._format_window_row(target)
            return self.engine.window_geometry(app_name=params["app"])
        if method == "active_window":
            if self.desktop_mode == "live":
                active = next((row for row in self._native_windows() if row.get("active")), None)
                return self._format_window_row(active) if active else "No active window."
            if self.access_scope == "full_virtual_desktop":
                from jevdesktop.native_windows import workspace_snapshot_with_env
                snapshot = workspace_snapshot_with_env(self.engine._session_env())
                active = next((row for row in snapshot["windows"] if row.get("active")), None)
                return self._format_window_row(active) if active else "No active window."
            return self.engine.active_window()
        if method == "screenshot":
            return self.engine.screenshot(include_cursor=False)
        if method == "mouse_click":
            x, y = params.get("x"), params.get("y")
            button = params.get("button", "left")
            double = params.get("double", False)
            if (type(x) is not int or type(y) is not int or button not in {"left", "middle", "right"}
                    or type(double) is not bool):
                raise ValueError("mouse_argument_invalid")
            self._require_live_point(x, y)
            return self.engine.mouse_click(x, y, button=button, double=double)
        if method == "mouse_drag":
            coordinates = tuple(params.get(key) for key in ("x", "y", "end_x", "end_y"))
            button = params.get("button", "left")
            if (any(type(value) is not int for value in coordinates)
                    or button not in {"left", "middle", "right"}):
                raise ValueError("mouse_argument_invalid")
            self._require_live_point(coordinates[0], coordinates[1])
            self._require_live_point(coordinates[2], coordinates[3])
            return self.engine.mouse_drag(*coordinates, button=button)
        if method == "mouse_scroll":
            delta, steps = params.get("delta"), params.get("steps")
            horizontal = params.get("horizontal", False)
            if (type(params.get("x")) is not int or type(params.get("y")) is not int
                    or type(delta) is not int or not 1 <= abs(delta) <= 8
                    or type(steps) is not int or not 1 <= steps <= 8
                    or type(horizontal) is not bool):
                raise ValueError("scroll_argument_invalid")
            self._require_live_point(params["x"], params["y"])
            return self.engine.mouse_scroll(params["x"], params["y"], delta,
                                            horizontal=horizontal, discrete=True, steps=steps)
        if method == "keyboard_key":
            key = params.get("key")
            if key not in SHORTCUT_KEYS and key != "Return":
                raise ValueError("unsupported_key")
            self._require_live_focus()
            return self.engine.keyboard_key(key)
        if method == "keyboard_type":
            value = params["text"]
            self._require_live_focus()
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
            self._require_live_focus()
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
