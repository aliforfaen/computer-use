"""Bounded, owner-present live KCalc verification probe.

This module is intentionally small and fixed-purpose. It starts only a new,
tracked KCalc process, presses only a fresh mapped semantic ``One`` button,
verifies blank -> 1 through AT-SPI, captures KCalc through ObservationAdapter,
then restores the original exact KWin window id before disconnecting.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Callable

from observation import ObservationAdapter, ObservationError


APP = "kcalc"
MAX_RUNTIME_SECONDS = 90.0
ATOMIC_WAIT_SECONDS = 10.0
VERIFY_WAIT_SECONDS = 5.0
POLL_SECONDS = 0.1


class ProbeError(RuntimeError):
    pass


def _window_rows(engine: Any) -> list[dict[str, Any]]:
    response = engine._run_kwin_query({})
    if not isinstance(response, dict) or response.get("ok") is not True or not isinstance(response.get("result"), list):
        raise ProbeError("KWin window inventory is unavailable")
    return [row for row in response["result"] if isinstance(row, dict)]


def _raw_window_rows(engine: Any) -> list[dict[str, Any]]:
    """Read kwin-mcp's typed raw KWin rows, which retain each window PID."""
    session_bus = engine._session_env().get("DBUS_SESSION_BUS_ADDRESS")
    process_bus = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    if not isinstance(session_bus, str) or not session_bus or process_bus != session_bus:
        raise ProbeError("raw KWin ownership query is not bound to the connected session bus")
    try:
        from kwin_mcp.geometry import collect_windows

        rows = collect_windows(timeout=5.0)
    except Exception as exc:
        raise ProbeError(f"raw KWin window inventory unavailable ({type(exc).__name__})") from None
    return [row for row in rows if isinstance(row, dict)]


def _raw_pid_for_window_id(rows: list[dict[str, Any]], window_id: str) -> int:
    matches = [row for row in rows if row.get("id") == window_id]
    if len(matches) != 1 or type(matches[0].get("pid")) is not int or matches[0]["pid"] <= 0:
        raise ProbeError("raw KWin PID is missing or ambiguous for the owned window id")
    return matches[0]["pid"]


def _active_row(engine: Any) -> dict[str, Any]:
    response = engine._run_kwin_query({"op": "active"})
    row = response.get("result") if isinstance(response, dict) and response.get("ok") is True else None
    if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
        raise ProbeError("no stable original active-window id is available")
    return row


def _is_kcalc(row: dict[str, Any]) -> bool:
    app = row.get("app")
    return isinstance(app, str) and app.casefold() in {"kcalc", "org.kde.kcalc"}


def _a11y_address(engine: Any) -> str:
    session = engine._get_session()
    address = session.info.dbus_address
    if not isinstance(address, str) or not address:
        raise ProbeError("live session D-Bus address is unavailable")
    return address


def _a11y_flags_at(address: str) -> dict[str, bool]:
    """Read status on the live session bus, without touching desktop settings."""
    try:
        import dbus
        bus = dbus.bus.BusConnection(address)
        try:
            obj = bus.get_object("org.a11y.Bus", "/org/a11y/bus")
            properties = dbus.Interface(obj, "org.freedesktop.DBus.Properties")
            values = properties.GetAll("org.a11y.Status")
            return {key: bool(values.get(key, False)) for key in ("IsEnabled", "ScreenReaderEnabled")}
        finally:
            bus.close()
    except Exception as exc:
        raise ProbeError(f"cannot read existing AT-SPI flags ({type(exc).__name__})") from None


def _set_a11y_flags_at(address: str, flags: dict[str, bool]) -> dict[str, bool]:
    """Set and read back only the two task-scoped org.a11y.Status properties."""
    try:
        import dbus

        bus = dbus.bus.BusConnection(address)
        try:
            obj = bus.get_object("org.a11y.Bus", "/org/a11y/bus")
            properties = dbus.Interface(obj, "org.freedesktop.DBus.Properties")
            for key in ("IsEnabled", "ScreenReaderEnabled"):
                properties.Set("org.a11y.Status", key, dbus.Boolean(bool(flags[key])))
            actual = properties.GetAll("org.a11y.Status")
            return {key: bool(actual.get(key, False)) for key in ("IsEnabled", "ScreenReaderEnabled")}
        finally:
            bus.close()
    except Exception as exc:
        raise ProbeError(f"cannot update task-scoped AT-SPI flags ({type(exc).__name__})") from None


def _a11y_flags(engine: Any) -> dict[str, bool]:
    """Read live AT-SPI status without changing accessibility flags."""
    return _a11y_flags_at(_a11y_address(engine))


def _editor_text(rows: list[dict[str, Any]]) -> str:
    fields = []
    for row in rows:
        states = {str(state).casefold() for state in row.get("states", [])}
        if (str(row.get("role", "")).casefold() in {"text", "text entry", "entry"}
                and "editable" in states and "showing" in states and "visible" in states):
            value = row.get("text", "")
            if not isinstance(value, str):
                raise ProbeError("KCalc display text is unavailable")
            fields.append(value)
    if len(fields) != 1:
        raise ProbeError(f"expected one visible editable KCalc display; found {len(fields)}")
    return fields[0]


def _one_button(rows: list[dict[str, Any]]) -> dict[str, Any]:
    matches = []
    for row in rows:
        states = {str(state).casefold() for state in row.get("states", [])}
        if (str(row.get("role", "")).casefold() == "button" and row.get("name") == "One"
                and {"enabled", "sensitive", "showing", "visible"}.issubset(states)
                and "Press" in row.get("actions", []) and row.get("mapped") is True):
            matches.append(row)
    if len(matches) != 1:
        raise ProbeError(f"expected one fresh mapped, enabled One button; found {len(matches)}")
    button = matches[0]
    if any(type(button.get(key)) is not int for key in ("x", "y", "width", "height")):
        raise ProbeError("One button has no integer mapped bounds")
    if button["width"] <= 0 or button["height"] <= 0:
        raise ProbeError("One button has empty mapped bounds")
    return button


def _button_fingerprint(row: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(row.get(key) for key in ("role", "name", "x", "y", "width", "height", "mapped")) + (
        tuple(sorted(str(item) for item in row.get("actions", []))),
        tuple(sorted(str(item).casefold() for item in row.get("states", []))),
    )


def _target_inside_client(button: dict[str, Any], window: dict[str, Any]) -> bool:
    rect = window.get("client")
    if isinstance(rect, dict):
        if any(type(rect.get(key)) is not int for key in ("x", "y", "width", "height")):
            return False
        x, y, width, height = (rect[key] for key in ("x", "y", "width", "height"))
    elif isinstance(rect, list) and len(rect) == 4 and all(type(n) is int for n in rect):
        x, y, width, height = rect
    else:
        return False
    return (width > 0 and height > 0 and button["x"] >= x and button["y"] >= y
            and button["x"] + button["width"] <= x + width
            and button["y"] + button["height"] <= y + height)


def _capture(adapter: Any) -> dict[str, Any]:
    capture_ref = adapter.capture(APP, scope="app")
    observation = adapter.observe(capture_ref, mode="metadata")
    return observation.metadata


def _restore_needle(original: dict[str, Any], all_windows: list[dict[str, Any]]) -> str | None:
    window_id = original.get("id")
    app = original.get("app")
    if not all(isinstance(value, str) and value for value in (window_id, app)):
        return None
    # Captions can legitimately change while the probe runs. Resolve the
    # original stable id in the fresh inventory, then use that row's current
    # app+caption to address the driver's substring-only activate operation.
    matches = [row for row in all_windows if row.get("id") == window_id and row.get("app") == app]
    if len(matches) != 1:
        return None
    caption = matches[0].get("caption")
    if not isinstance(caption, str) or not caption:
        return None
    needle = f"{app} {caption}"
    collisions = [row for row in all_windows
                  if needle.casefold() in f"{row.get('app', '')} {row.get('caption', '')}".casefold()]
    return needle if len(collisions) == 1 else None


def _restore_exact(engine: Any, original: dict[str, Any], all_windows: list[dict[str, Any]]) -> bool:
    window_id = original.get("id")
    needle = _restore_needle(original, all_windows)
    if needle is None:
        return False
    # The pinned geometry helper activates by substring, not id. App + caption
    # must identify one window, and success is accepted only after exact-id readback.
    response = engine._run_kwin_query({"op": "activate", "app_name": needle})
    if not isinstance(response, dict) or response.get("ok") is not True or not response.get("result"):
        return False
    active = _active_row(engine)
    return active.get("id") == window_id


def _version_facts() -> dict[str, str | None]:
    try:
        driver_version = version("kwin-mcp")
    except PackageNotFoundError:
        driver_version = None
    executable = shutil.which("kwin_wayland")
    kwin_version = None
    if executable:
        try:
            result = subprocess.run([executable, "--version"], capture_output=True, text=True,
                                    timeout=3, check=False)
            kwin_version = (result.stdout or result.stderr).strip()[:240] or None
        except (OSError, subprocess.TimeoutExpired):
            pass
    return {"kwin_mcp": driver_version, "kwin": kwin_version}


def _phase(report: dict[str, Any], name: str, fn: Callable[[], Any], *, deadline: float | None = None) -> Any:
    started = time.monotonic()
    row: dict[str, Any] = {"name": name, "status": "running"}
    report["phases"].append(row)
    try:
        value = fn()
        if deadline is not None and time.monotonic() > deadline:
            raise ProbeError("overall probe deadline exceeded")
        row["status"] = "passed"
        return value
    except Exception as exc:
        row["status"] = "failed"
        row["error_type"] = type(exc).__name__
        if isinstance(exc, ProbeError):
            row["error"] = str(exc)
        elif isinstance(exc, ObservationError):
            row["error_code"] = exc.code
            row["error"] = exc.message
        raise
    finally:
        row["duration_seconds"] = round(time.monotonic() - started, 3)


def run_probe(*, output_root: Path = Path("run/live-desktop-probe"), engine_factory=None,
              adapter_factory=None, visible_hold: bool = False,
              temporary_a11y: bool = False) -> dict[str, Any]:
    """Run the fixed live KCalc action. Callers must opt in through the CLI."""
    started_at = datetime.now(timezone.utc)
    stamp = started_at.strftime("%Y%m%dT%H%M%SZ")
    report_path = output_root / f"kcalc-{stamp}-{uuid.uuid4().hex[:8]}" / "report.json"
    report: dict[str, Any] = {
        "schema": 1,
        "started_at": started_at.isoformat(),
        "task": "Enter 1 in a blank KCalc display using a fresh semantic target.",
        "input_idle_detection": "unavailable; probe assumes owner is present and has explicitly authorized this run",
        "capture_policy": "app-only metadata and hashes; screenshots are not saved to the report",
        "visible_hold_seconds": {"before": 2 if visible_hold else 0, "after": 3 if visible_hold else 0},
        "temporary_a11y_requested": temporary_a11y,
        "temporary_a11y_restored": None,
        "timeout_model": "90-second monotonic task deadline checked at phase boundaries; kwin-mcp sync calls may take up to 30 seconds and are not interruptible in flight",
        "versions": _version_facts(),
        "phases": [],
        "captures": [],
        "action": {"attempted": False, "target": "One", "result": "not_attempted"},
        "restored_original_window": False,
        "cleanup": "not_started",
        "owned_process_cleanup": "not_started",
        "owned_window_cleanup": "not_started",
        "result": "failed",
    }
    if report["versions"]["kwin_mcp"] != "0.10.0":
        raise ProbeError("this probe requires kwin-mcp 0.10.0")
    if engine_factory is None:
        from kwin_mcp.core import AutomationEngine

        engine_factory = AutomationEngine
    engine = engine_factory()
    connected = False
    launched = None
    original: dict[str, Any] | None = None
    original_a11y: dict[str, bool] | None = None
    a11y_address: str | None = None
    a11y_touched = False
    cleanup_errors = []
    deadline = time.monotonic() + MAX_RUNTIME_SECONDS
    old_signal_handlers: dict[int, Any] = {}
    if threading.current_thread() is threading.main_thread():
        def interrupted(signum, _frame):
            raise ProbeError(f"probe interrupted by signal {signum}")

        for signum in (signal.SIGINT, signal.SIGTERM):
            old_signal_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, interrupted)

    try:
        def connect():
            nonlocal connected
            result = engine.session_connect(keep_screenshots=False)
            if not isinstance(result, str) or "Connected to live KWin session" not in result:
                raise ProbeError("could not connect to the existing live KWin session")
            connected = True
            if "Input backend: KWin EIS" not in result:
                raise ProbeError("live KWin EIS input is unavailable")
            return result

        _phase(report, "connect", connect, deadline=deadline)
        original = _phase(report, "save_original_focus", lambda: _active_row(engine), deadline=deadline)
        report["original_window"] = {"id": original["id"], "app": original.get("app")}

        flags = _phase(report, "check_existing_atspi_flags", lambda: _a11y_flags(engine), deadline=deadline)
        report["atspi_flags"] = flags
        if not all(flags.values()):
            if not temporary_a11y:
                raise ProbeError("existing live AT-SPI flags are not both enabled; no host settings were changed")
            original_a11y = dict(flags)
            report["atspi_original_flags"] = dict(flags)
            a11y_address = _a11y_address(engine)
            a11y_touched = True  # Restore even if enabling or readback partially fails.
            enabled = _phase(report, "enable_temporary_atspi", lambda: _set_a11y_flags_at(
                a11y_address, {"IsEnabled": True, "ScreenReaderEnabled": True}), deadline=deadline)
            report["atspi_flags_after_enable"] = enabled
            if not all(enabled.values()):
                raise ProbeError("temporary AT-SPI status flags did not enable")
        else:
            report["atspi_original_flags"] = dict(flags)

        before_launch = _phase(report, "check_no_existing_kcalc", lambda: _window_rows(engine), deadline=deadline)
        if any(_is_kcalc(row) for row in before_launch):
            raise ProbeError("KCalc is already open; refusing to target or close an owner window")
        if _restore_needle(original, before_launch) is None:
            raise ProbeError("original active window cannot be restored unambiguously; refusing live input")

        def launch():
            nonlocal launched
            session = engine._get_session()
            launched = session.launch_app(["kcalc"])
            until = min(deadline, time.monotonic() + ATOMIC_WAIT_SECONDS)
            while time.monotonic() < until:
                rows = _window_rows(engine)
                matches = [row for row in rows if _is_kcalc(row)]
                if len(matches) > 1:
                    raise ProbeError("multiple KCalc windows appeared; refusing ambiguous input")
                if len(matches) == 1:
                    raw_pid = _raw_pid_for_window_id(_raw_window_rows(engine), str(matches[0].get("id", "")))
                    report["ownership_check"] = {
                        "window_id": matches[0].get("id"),
                        "tracked_launch_pid": launched.pid,
                        "raw_kwin_window_pid": raw_pid,
                        "matched": raw_pid == launched.pid,
                    }
                    if raw_pid != launched.pid:
                        raise ProbeError("KCalc window is not owned by the tracked launch process")
                    return matches[0]
                time.sleep(POLL_SECONDS)
            raise ProbeError("tracked KCalc window did not appear before timeout")

        kcalc_window = _phase(report, "launch_owned_kcalc", launch, deadline=deadline)
        report["owned_kcalc"] = {"pid": launched.pid, "window_pid": report["ownership_check"]["raw_kwin_window_pid"],
                                  "window_id": kcalc_window["id"]}

        def read_rows():
            response = engine._run_atspi("find", query="", app_name=APP)
            if not isinstance(response, dict) or response.get("ok") is not True or not isinstance(response.get("result"), list):
                raise ProbeError("KCalc AT-SPI read failed")
            return response["result"]

        def wait_accessible():
            until = min(deadline, time.monotonic() + ATOMIC_WAIT_SECONDS)
            last_error = None
            while time.monotonic() < until:
                try:
                    rows = read_rows()
                    if _editor_text(rows) == "":
                        _one_button(rows)
                        return rows
                except ProbeError as exc:
                    last_error = str(exc)
                time.sleep(POLL_SECONDS)
            raise ProbeError(last_error or "KCalc accessible blank display did not appear")

        rows_before = _phase(report, "read_blank_display_and_semantic_target", wait_accessible, deadline=deadline)
        if _editor_text(rows_before) != "":
            raise ProbeError("code-owned task requires a blank KCalc display")

        if adapter_factory is None:
            adapter_factory = ObservationAdapter
        adapter = adapter_factory(engine, "live-kcalc-probe", allowed_apps={APP})
        capture_before = _phase(report, "capture_app_before", lambda: _capture(adapter), deadline=deadline)
        report["captures"].append(capture_before)

        def activate_kcalc():
            # Safe because the preflight proved no KCalc window existed before
            # the process we launched, and the new KCalc identity is unique.
            response = engine._run_kwin_query({"op": "activate", "app_name": "kcalc"})
            if not isinstance(response, dict) or response.get("ok") is not True or not response.get("result"):
                raise ProbeError("could not focus the owned KCalc window")
            active = _active_row(engine)
            if active.get("id") != kcalc_window.get("id"):
                raise ProbeError("focused KCalc window id differs from the tracked window")
            return active

        _phase(report, "focus_owned_kcalc", activate_kcalc, deadline=deadline)

        if visible_hold:
            _phase(report, "owner_watch_before", lambda: time.sleep(2), deadline=deadline)

        def click_fresh_one():
            rows = read_rows()
            if _editor_text(rows) != "":
                raise ProbeError("KCalc display changed before the action")
            fresh = _one_button(rows)
            initial = _one_button(rows_before)
            if _button_fingerprint(fresh) != _button_fingerprint(initial):
                raise ProbeError("fresh semantic One target differs from initial observation")
            windows = [row for row in _window_rows(engine) if row.get("id") == kcalc_window.get("id")]
            if len(windows) != 1 or not _target_inside_client(fresh, windows[0]):
                raise ProbeError("mapped One bounds are outside or ambiguous for the owned KCalc window")
            active = _active_row(engine)
            if active.get("id") != kcalc_window.get("id"):
                raise ProbeError("KCalc lost focus immediately before action")
            report["action"]["attempted"] = True
            engine.mouse_click(fresh["x"] + fresh["width"] // 2,
                               fresh["y"] + fresh["height"] // 2, button="left")
            report["action"]["result"] = "input_sent"
            return fresh

        _phase(report, "click_fresh_semantic_one", click_fresh_one, deadline=deadline)

        def verify_transition():
            until = min(deadline, time.monotonic() + VERIFY_WAIT_SECONDS)
            current = ""
            while time.monotonic() < until:
                current = _editor_text(read_rows())
                if current == "1":
                    report["action"]["result"] = "verified_blank_to_1"
                    return current
                if current not in {"", "1"}:
                    raise ProbeError("KCalc display changed to an unexpected value")
                time.sleep(POLL_SECONDS)
            raise ProbeError("KCalc display did not verify blank to 1 before timeout")

        _phase(report, "verify_blank_to_one", verify_transition, deadline=deadline)
        capture_after = _phase(report, "capture_app_after", lambda: _capture(adapter), deadline=deadline)
        report["captures"].append(capture_after)
        if visible_hold:
            _phase(report, "owner_watch_after", lambda: time.sleep(3), deadline=deadline)
        report["result"] = "passed"
    except Exception as exc:
        failure = {"type": type(exc).__name__}
        if isinstance(exc, ProbeError):
            failure["message"] = str(exc)
        elif isinstance(exc, ObservationError):
            failure["code"] = exc.code
            failure["message"] = exc.message
        else:
            failure["message"] = "probe failed"
        report["failure"] = failure
    finally:
        if connected and original is not None:
            try:
                rows = _window_rows(engine)
                report["restored_original_window"] = _restore_exact(engine, original, rows)
            except Exception:
                report["restored_original_window"] = False
        if launched is not None:
            try:
                process = launched.process
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
                report["owned_process_cleanup"] = "confirmed" if process.poll() is not None else "unconfirmed"
            except Exception as exc:
                report["owned_process_cleanup"] = "unconfirmed"
                cleanup_errors.append(f"owned_process_{type(exc).__name__}")
        if connected:
            # Closing our tracked KCalc can change focus again. Confirm the
            # original exact id is active after that close, then disconnect.
            if original is not None:
                try:
                    until = time.monotonic() + 5.0
                    rows = _window_rows(engine)
                    while (any(_is_kcalc(row) for row in rows)
                           and time.monotonic() < until):
                        time.sleep(POLL_SECONDS)
                        rows = _window_rows(engine)
                    report["owned_window_cleanup"] = "confirmed" if not any(_is_kcalc(row) for row in rows) else "unconfirmed"
                    if report["owned_window_cleanup"] != "confirmed":
                        cleanup_errors.append("owned_kcalc_window_remains")
                    active = _active_row(engine)
                    if active.get("id") != original.get("id"):
                        report["restored_original_window"] = _restore_exact(engine, original, rows)
                    else:
                        report["restored_original_window"] = True
                except Exception:
                    report["restored_original_window"] = False
            if not report.get("restored_original_window"):
                cleanup_errors.append("original_focus_not_restored")
            try:
                stop = engine.session_stop()
                report["cleanup"] = "confirmed" if isinstance(stop, str) and "Disconnected" in stop else "unconfirmed"
                if launched is not None and launched.process.poll() is None:
                    report["owned_process_cleanup"] = "unconfirmed"
                if report["cleanup"] != "confirmed":
                    cleanup_errors.append("session_stop_unconfirmed")
            except Exception as exc:
                report["cleanup"] = "unconfirmed"
                cleanup_errors.append(f"session_stop_{type(exc).__name__}")
        else:
            report["cleanup"] = "not_connected"
        if a11y_touched and original_a11y is not None and a11y_address is not None:
            try:
                restored = _set_a11y_flags_at(a11y_address, original_a11y)
                report["atspi_flags_after_restore"] = restored
                report["temporary_a11y_restored"] = restored == original_a11y
                if not report["temporary_a11y_restored"]:
                    cleanup_errors.append("atspi_flags_restore_mismatch")
            except Exception as exc:
                report["temporary_a11y_restored"] = False
                cleanup_errors.append(f"atspi_flags_restore_{type(exc).__name__}")
        if cleanup_errors:
            report["cleanup_errors"] = cleanup_errors
            if report["result"] == "passed":
                report["result"] = "failed"
                report["failure"] = {"type": "CleanupError", "message": ";".join(cleanup_errors)}
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["duration_seconds"] = round(time.monotonic() - (deadline - MAX_RUNTIME_SECONDS), 3)
        report["report_path"] = str(report_path)
        try:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        finally:
            for signum, handler in old_signal_handlers.items():
                signal.signal(signum, handler)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="perform the fixed owner-present live KCalc action")
    parser.add_argument("--visible-hold", action="store_true",
                        help="hold 2 seconds before input and 3 seconds after verification for owner viewing")
    parser.add_argument("--temporary-a11y", action="store_true",
                        help="temporarily enable live org.a11y.Status flags and restore their exact initial values")
    parser.add_argument("--output-root", type=Path, default=Path("run/live-desktop-probe"))
    args = parser.parse_args(argv)
    if not args.execute:
        parser.error("refusing live input without explicit --execute")
    report = run_probe(output_root=args.output_root, visible_hold=args.visible_hold,
                        temporary_a11y=args.temporary_a11y)
    print(json.dumps({key: report.get(key) for key in (
        "result", "action", "restored_original_window", "cleanup", "duration_seconds", "report_path", "failure")
    }, indent=2))
    return 0 if report.get("result") == "passed" and report.get("restored_original_window") and report.get("cleanup") == "confirmed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
