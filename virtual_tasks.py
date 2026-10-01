#!/usr/bin/env python3
"""Run bounded computer-use tasks in disposable KWin virtual sessions.

This is an integration harness, not a daemon or general task runner. It launches
only the fixed Kate, Firefox and KCalc fixtures below. It never types into a
terminal, opens external pages, saves user files, or calls a vision provider.

Run with: uv run --with kwin-mcp==0.10.0 --with Pillow --with httpx python virtual_tasks.py
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import shlex
import shutil
import tempfile
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from benchmark_capture import _atspi_version, _enable_virtual_atspi, _version
from observation import ObservationAdapter
from transactions import Action, AuditLog, AutonomyMode, Policy, TransactionEngine, TransactionError, Verification, KwinMcpBackend


ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "benchmark_fixtures"
OUTPUT = ROOT / "run" / "virtual-tasks"
SCREEN = (1280, 800)
ALLOWED_APPS = frozenset({"kate", "firefox", "kcalc"})
WAIT_SECONDS = 15.0
EXPECTED_DRAFT = "Draft A\nDraft B"


class HarnessError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_error(exc: BaseException) -> str:
    if isinstance(exc, (TransactionError, HarnessError)):
        value = str(exc)
        if value.replace("_", "").isalnum() and len(value) <= 80:
            return value
    return type(exc).__name__


def _audit(audit: AuditLog, app: str, tool: str, event: str, status: str, reason: str) -> None:
    audit.write(tool=tool, app=app, mode=AutonomyMode.GUARDED, event=event, status=status, reason=reason)


def _elements(engine: Any, app: str, counters: dict[str, int]) -> list[dict[str, Any]]:
    """Read structured AT-SPI find results; do not infer text from tree lines."""
    counters["direct_atspi_find_calls"] += 1
    result = engine._run_atspi("find", query="", app_name=app)
    if not isinstance(result, dict) or result.get("ok") is not True or not isinstance(result.get("result"), list):
        raise HarnessError("atspi_find_failed")
    return [item for item in result["result"] if isinstance(item, dict)]


def _matches_exact(elements: list[dict[str, Any]], expected: str, *, editable_only: bool = False) -> bool:
    for item in elements:
        role = str(item.get("role", "")).casefold()
        states = {str(state).casefold() for state in item.get("states", []) if isinstance(state, str)}
        if editable_only and (role not in {"text", "text entry", "entry"} or "editable" not in states):
            continue
        if item.get("text") == expected or (not editable_only and item.get("name") == expected):
            return True
    return False


def _wait_exact(engine: Any, app: str, expected: str, counters: dict[str, int], *, editable_only: bool = False) -> bool:
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        if _matches_exact(_elements(engine, app, counters), expected, editable_only=editable_only):
            return True
        time.sleep(0.15)
    return False


def _wait_editable(engine: Any, counters: dict[str, int]) -> None:
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        for item in _elements(engine, "kate", counters):
            role = str(item.get("role", "")).casefold()
            states = {str(state).casefold() for state in item.get("states", []) if isinstance(state, str)}
            if role in {"text", "text entry", "entry"} and "editable" in states and item.get("mapped") is True:
                return
        time.sleep(0.15)
    raise HarnessError("editable_editor_unavailable")


def _unique_candidate(snapshot: Any, predicate: Callable[[Any], bool], code: str) -> Any:
    matches = [candidate for candidate in snapshot.candidates if predicate(candidate)]
    if len(matches) != 1:
        raise HarnessError(code)
    return matches[0]


def _observe_image(adapter: ObservationAdapter, app: str, counters: dict[str, int], audit: AuditLog) -> dict[str, Any]:
    started = time.perf_counter()
    _audit(audit, app, "virtual_tasks.capture", "capture_app", "pending", "app_scope")
    try:
        ref = adapter.capture(app, scope="app")
    except Exception:
        _audit(audit, app, "virtual_tasks.capture", "capture_app", "failed", "capture_failed")
        raise
    _audit(audit, app, "virtual_tasks.capture", "capture_app", "ok", "app_scope")
    counters["adapter_capture_calls"] += 1
    _audit(audit, app, "virtual_tasks.observe", "observe_metadata", "pending", "metadata_mode")
    counters["adapter_observation_calls"] += 1
    try:
        meta = adapter.observe(ref, mode="metadata")
    except Exception:
        _audit(audit, app, "virtual_tasks.observe", "observe_metadata", "failed", "metadata_failed")
        raise
    _audit(audit, app, "virtual_tasks.observe", "observe_metadata", "ok", "metadata_mode")
    _audit(audit, app, "virtual_tasks.observe", "observe_image", "pending", "image_mode")
    counters["adapter_observation_calls"] += 1
    try:
        image = adapter.observe(ref, mode="image")
    except Exception:
        _audit(audit, app, "virtual_tasks.observe", "observe_image", "failed", "image_failed")
        raise
    _audit(audit, app, "virtual_tasks.observe", "observe_image", "ok", "image_mode")
    elapsed_ms = (time.perf_counter() - started) * 1000
    if meta.capture.capture_id != image.capture.capture_id or meta.capture.image_sha256 != image.capture.image_sha256:
        raise HarnessError("capture_identity_mismatch")
    if image.image is None or image.data is not None or meta.image is not None:
        raise HarnessError("observation_mode_mismatch")
    counters["capture_ms"] += elapsed_ms
    return {
        "scope": ref.scope,
        "width": ref.width,
        "height": ref.height,
        "capture_id": ref.capture_id,
        "image_sha256": ref.image_sha256,
        "same_capture_metadata_and_image": True,
        "elapsed_ms": round(elapsed_ms, 2),
    }


def _check_session_start(result: str) -> None:
    if not isinstance(result, str) or "Input backend: KWin EIS" not in result:
        raise HarnessError("kwin_eis_unavailable")


def _run_in_session(
    app: str,
    command: str,
    task: Callable[[Any, TransactionEngine, ObservationAdapter, str, AuditLog, dict[str, int]], dict[str, Any]],
    *,
    env: dict[str, str] | None = None,
    profile_dir: Path | None = None,
    temp_files: tuple[Path, ...] = (),
    output_dir: Path = OUTPUT,
    audit: AuditLog | None = None,
) -> dict[str, Any]:
    if app not in ALLOWED_APPS:
        raise HarnessError("app_not_allowed")
    if importlib.metadata.version("kwin-mcp") != "0.10.0":
        raise HarnessError("unsupported_driver_version")
    try:
        from kwin_mcp.core import AutomationEngine
    except ImportError as exc:
        raise HarnessError("kwin_mcp_unavailable") from exc

    session_id = uuid.uuid4().hex
    engine = AutomationEngine()
    session_attempted = False
    result: dict[str, Any] = {"app": app, "session_id": session_id, "status": "failed"}
    counters = {
        "transaction_observe_calls": 0,
        "transaction_action_attempts": 0,
        "transaction_actions_completed": 0,
        "adapter_capture_calls": 0,
        "adapter_observation_calls": 0,
        "direct_atspi_find_calls": 0,
        "capture_ms": 0,
    }
    main_error: BaseException | None = None
    cleanup_error: BaseException | None = None
    session_start_ok = False
    started = time.perf_counter()
    audit = audit or AuditLog(output_dir / "actions.jsonl")
    try:
        _audit(audit, app, "virtual_tasks.session", "session_start", "pending", "disposable_virtual_session")
        session_attempted = True
        start_text = engine.session_start(
            app_command=command,
            screen_width=SCREEN[0],
            screen_height=SCREEN[1],
            isolate_home=True,
            keep_home=False,
            keep_screenshots=False,
            env=env,
        )
        _audit(audit, app, "virtual_tasks.session", "session_start", "ok", "disposable_virtual_session")
        session_start_ok = True
        _check_session_start(start_text)
        atspi_flags = _enable_virtual_atspi(engine)
        adapter = ObservationAdapter(engine, session_id, allowed_apps=set(ALLOWED_APPS))
        backend = KwinMcpBackend(engine)
        tx = TransactionEngine(
            backend,
            audit=audit,
            policy=Policy(
                allowed_apps=ALLOWED_APPS,
                max_actions=8,
                max_observations=24,
                max_duration_seconds=90,
                max_text_chars=128,
            ),
        )
        result.update({
            "versions": _versions_for(app),
            "atspi_enabled": all(atspi_flags.values()),
            "task_result": task(engine, tx, adapter, session_id, audit, counters),
            "status": "passed",
        })
    except BaseException as exc:
        main_error = exc
        result["error"] = _safe_error(exc)
        if not session_start_ok:
            _audit(audit, app, "virtual_tasks.session", "session_start", "failed", result["error"])
        _audit(audit, app, "virtual_tasks.session", "session_task", "failed", result["error"])
    finally:
        if session_attempted:
            try:
                _audit(audit, app, "virtual_tasks.session", "session_stop", "pending", "disposable_virtual_session")
                stop_text = engine.session_stop()
                if not isinstance(stop_text, str) or "Session stopped" not in stop_text:
                    raise HarnessError("session_stop_unconfirmed")
                result["cleanup"] = "passed"
                _audit(audit, app, "virtual_tasks.session", "session_stop", "ok", "disposable_virtual_session")
            except BaseException as exc:
                cleanup_error = exc
                result["cleanup"] = "failed"
                result["cleanup_error"] = _safe_error(exc)
                _audit(audit, app, "virtual_tasks.session", "session_stop", "failed", result["cleanup_error"])
        if profile_dir is not None:
            try:
                shutil.rmtree(profile_dir)
                result["profile_cleanup"] = "passed"
                _audit(audit, app, "virtual_tasks.cleanup", "firefox_profile_cleanup", "ok", "private_profile_removed")
            except FileNotFoundError:
                result["profile_cleanup"] = "passed"
                _audit(audit, app, "virtual_tasks.cleanup", "firefox_profile_cleanup", "ok", "profile_already_removed")
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
                result["profile_cleanup"] = "failed"
                result["profile_cleanup_error"] = _safe_error(exc)
                _audit(audit, app, "virtual_tasks.cleanup", "firefox_profile_cleanup", "failed", result["profile_cleanup_error"])
        for path in temp_files:
            try:
                path.unlink(missing_ok=True)
                _audit(audit, app, "virtual_tasks.cleanup", "temporary_file_cleanup", "ok", "disposable_file_removed")
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
                result["temporary_file_cleanup"] = "failed"
                result["temporary_file_cleanup_error"] = _safe_error(exc)
                _audit(audit, app, "virtual_tasks.cleanup", "temporary_file_cleanup", "failed", result["temporary_file_cleanup_error"])
        if temp_files and "temporary_file_cleanup" not in result:
            result["temporary_file_cleanup"] = "passed"
    result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
    result["counters"] = {
        "transaction_observe_calls": counters["transaction_observe_calls"],
        "transaction_action_attempts": counters["transaction_action_attempts"],
        "transaction_actions_completed": counters["transaction_actions_completed"],
        "adapter_capture_calls": counters["adapter_capture_calls"],
        "adapter_observation_calls": counters["adapter_observation_calls"],
        "direct_atspi_find_calls": counters["direct_atspi_find_calls"],
        "capture_ms": round(counters["capture_ms"], 2),
    }
    if cleanup_error is not None:
        result["status"] = "failed"
        if main_error is None:
            result["error"] = _safe_error(cleanup_error)
    if isinstance(main_error, (KeyboardInterrupt, SystemExit)):
        raise main_error
    return result


def _versions_for(app: str) -> dict[str, str]:
    commands = {
        "kate": ["kate", "--version"],
        "firefox": ["firefox", "--version"],
        "kcalc": ["kcalc", "--version"],
    }
    if app not in commands:
        raise HarnessError("app_not_allowed")
    try:
        driver_version = importlib.metadata.version("kwin-mcp")
    except importlib.metadata.PackageNotFoundError:
        driver_version = "unavailable"
    return {
        "kwin_mcp": driver_version,
        "kwin": _version(["kwin_wayland", "--version"]),
        "atspi": _atspi_version(),
        app: _version(commands[app]),
    }


def _kate_task(engine: Any, tx: TransactionEngine, adapter: ObservationAdapter, session_id: str,
               audit: AuditLog, counters: dict[str, int]) -> dict[str, Any]:
    _wait_editable(engine, counters)
    snapshots = [_observe_image(adapter, "kate", counters, audit)]
    counters["transaction_observe_calls"] += 1
    snap = tx.observe(session_id, "kate")
    editor = _unique_candidate(
        snap,
        lambda item: "type_text" in item.actions and "editable" in item.states,
        "editable_target_ambiguous",
    )
    counters["transaction_action_attempts"] += 1
    focus = tx.act(
        session_id,
        "kate",
        Action("click", editor.ref),
        verifier=lambda after: Verification(
            passed=len([item for item in after.candidates if "type_text" in item.actions]) == 1
            and any(item.role == editor.role and item.label == editor.label
                    and item.bounds == editor.bounds and "focused" in item.states
                    for item in after.candidates),
            evidence={"focus_verified": True},
            reason="focus_verified",
        ),
    )
    counters["transaction_actions_completed"] += 1
    snapshots.append(_observe_image(adapter, "kate", counters, audit))
    if focus.after is None:
        raise HarnessError("focus_snapshot_missing")

    def type_and_verify(text: str, expected: str) -> Any:
        current = _unique_candidate(
            type_and_verify.last_snapshot,
            lambda item: "type_text" in item.actions and "editable" in item.states and "focused" in item.states,
            "focused_editor_ambiguous",
        )
        counters["transaction_action_attempts"] += 1
        result = tx.act(
            session_id,
            "kate",
            Action("type_text", current.ref, text=text),
            verifier=lambda _after: Verification(
                passed=len([item for item in _after.candidates if "type_text" in item.actions]) == 1
                and any(item.role == current.role and item.label == current.label
                        and item.bounds == current.bounds and item.value == expected
                        and "focused" in item.states for item in _after.candidates),
                evidence={"exact_content_verified": True},
                reason="exact_content_verified",
            ),
        )
        if result.after is None:
            raise HarnessError("edit_snapshot_missing")
        type_and_verify.last_snapshot = result.after
        counters["transaction_actions_completed"] += 1
        snapshots.append(_observe_image(adapter, "kate", counters, audit))
        return result

    type_and_verify.last_snapshot = focus.after
    type_and_verify("Draft A\n", "Draft A\n")
    type_and_verify("Draft B", EXPECTED_DRAFT)
    return {"verification": "exact_atspi_text_after_each_edit", "edit_count": 2, "snapshot_count": len(snapshots),
            "snapshots": snapshots}


def _firefox_command(profile_dir: Path) -> str:
    fixture = (FIXTURES / "action.html").resolve()
    if fixture.parent != FIXTURES.resolve() or not fixture.is_file():
        raise HarnessError("local_fixture_unavailable")
    url = "file://" + quote(str(fixture))
    return " ".join(("firefox", "--no-remote", "--new-instance", "--profile", shlex.quote(str(profile_dir)), shlex.quote(url)))


def _firefox_task(engine: Any, tx: TransactionEngine, adapter: ObservationAdapter, session_id: str,
                  audit: AuditLog, counters: dict[str, int]) -> dict[str, Any]:
    if not _wait_exact(engine, "firefox", "State: idle", counters):
        raise HarnessError("fixture_initial_state_unavailable")
    snapshots = [_observe_image(adapter, "firefox", counters, audit)]
    counters["transaction_observe_calls"] += 1
    snap = tx.observe(session_id, "firefox")
    button = _unique_candidate(snap, lambda item: item.role == "button" and item.label == "Advance state", "action_button_unavailable")
    counters["transaction_action_attempts"] += 1
    clicked = tx.act(
        session_id,
        "firefox",
        Action("click", button.ref),
        verifier=lambda _after: Verification(
            passed=_wait_exact(engine, "firefox", "State: complete", counters),
            evidence={"state_transition_verified": True},
            reason="state_transition_verified",
        ),
    )
    counters["transaction_actions_completed"] += 1
    if clicked.after is None:
        raise HarnessError("action_snapshot_missing")
    snapshots.append(_observe_image(adapter, "firefox", counters, audit))

    disabled = _unique_candidate(
        clicked.after,
        lambda item: item.role == "button" and item.label == "Disabled control",
        "disabled_target_unavailable",
    )
    if {"enabled", "sensitive"}.issubset(disabled.states):
        raise HarnessError("fixture_disabled_control_is_enabled")
    try:
        counters["transaction_action_attempts"] += 1
        tx.act(
            session_id,
            "firefox",
            Action("click", disabled.ref),
            verifier=lambda _after: Verification(False, reason="must_refuse_disabled"),
        )
    except TransactionError as exc:
        if str(exc) != "target_disabled_or_hidden":
            raise HarnessError("disabled_refusal_wrong_reason") from None
        disabled_refused = True
    else:
        raise HarnessError("disabled_target_was_acted_on")
    return {"verification": "exact_local_state_transition", "disabled_refusal": disabled_refused,
            "snapshot_count": len(snapshots), "snapshots": snapshots,
            "disabled_target_states": sorted(disabled.states)}


def _kcalc_task(engine: Any, tx: TransactionEngine, adapter: ObservationAdapter, session_id: str,
                audit: AuditLog, counters: dict[str, int]) -> dict[str, Any]:
    counters["transaction_observe_calls"] += 1
    snap = tx.observe(session_id, "kcalc")
    observation = _observe_image(adapter, "kcalc", counters, audit)
    return {"verification": "unfamiliar_app_observed_without_input", "candidate_count": len(snap.candidates),
            "snapshot": observation}


def _new_profile() -> Path:
    profile = Path(tempfile.mkdtemp(prefix="jev-virtual-firefox-"))
    profile.chmod(0o700)
    prefs = {
        "browser.startup.homepage_override.mstone": "ignore",
        "browser.startup.firstrunSkipsHomepage": True,
        "browser.startup.page": 0,
        "browser.aboutwelcome.enabled": False,
        "browser.shell.checkDefaultBrowser": False,
        "datareporting.policy.firstRunURL": "",
    }
    (profile / "user.js").write_text(
        "\n".join(f"user_pref({json.dumps(key)}, {json.dumps(value)});" for key, value in prefs.items()) + "\n",
        encoding="utf-8",
    )
    return profile


def run(output: Path = OUTPUT) -> dict[str, Any]:
    output = output.resolve()
    run_root = (ROOT / "run").resolve()
    if output != run_root and run_root not in output.parents:
        raise HarnessError("output_must_be_inside_run_directory")
    output.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    started_at = _utc_now()
    audit = AuditLog(output / "actions.jsonl")
    cases: list[dict[str, Any]] = []
    fd, kate_path_raw = tempfile.mkstemp(prefix="jev-virtual-draft-", suffix=".txt")
    os.close(fd)
    kate_path = Path(kate_path_raw)
    try:
        kate_command = f"kate --startanon --new {shlex.quote(str(kate_path))}"
        cases.append(_run_in_session("kate", kate_command, _kate_task, temp_files=(kate_path,), output_dir=output, audit=audit))
    except BaseException:
        kate_path.unlink(missing_ok=True)
        raise

    profile = _new_profile()
    try:
        cases.append(_run_in_session(
            "firefox",
            _firefox_command(profile),
            _firefox_task,
            env={"MOZ_ENABLE_ACCESSIBILITY": "1"},
            profile_dir=profile,
            output_dir=output,
            audit=audit,
        ))
    except BaseException:
        # If command construction fails before the session owns the profile,
        # remove the private temporary profile here as well.
        if profile.exists():
            shutil.rmtree(profile, ignore_errors=True)
        raise

    cases.append(_run_in_session("kcalc", "kcalc", _kcalc_task, output_dir=output, audit=audit))
    summary = {
        "schema": "jev-virtual-tasks-v1",
        "started_at": started_at,
        "finished_at": _utc_now(),
        "driver_pin": "kwin-mcp==0.10.0",
        "paid_calls": 0,
        "source": "synthetic local fixtures in disposable virtual sessions",
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        "cases": cases,
        "passed": all(case.get("status") == "passed" and case.get("cleanup") == "passed" for case in cases),
    }
    target = output / "summary.json"
    target.write_text(json.dumps(summary, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT, help="summary directory under repo run/")
    args = parser.parse_args(argv)
    try:
        summary = run(args.output)
    except BaseException as exc:
        safe = {"status": "failed", "error": _safe_error(exc)}
        print(json.dumps(safe, separators=(",", ":")), file=sys.stderr)
        return 1
    printed = {
        "status": "passed" if summary["passed"] else "failed",
        "cases": [{"app": case["app"], "status": case["status"], "error": case.get("error"),
                   "cleanup": case.get("cleanup")} for case in summary["cases"]],
        "summary": str(args.output.resolve() / "summary.json"),
        "paid_calls": 0,
    }
    print(json.dumps(printed, separators=(",", ":")))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
