#!/usr/bin/env python3
"""Jev versus code on one bounded native document task (study harness).

This is the runnable half of `docs/33-jev-real-task-study.md`. It runs one
scripted task in two fresh virtual Kate sessions with identical caller-owned
draft/revision bytes, goals, owner operations, budgets and final checks:

    code arm   a small phase machine chooses supported actions from fresh candidates
    jev arm    the study-local Jev selector chooses from the same candidates

Both arms use `tools.jev_loop_probe.run_loop`, the same owner socket, the same
code-owned verifications (`target_text`, `document_saved`) and the same final
contract: the saved file must match the corrected revision after cleanup. It then
runs a few selection-only ambiguity cases (no execution) against pre-declared
acceptable sets.

Bounds: virtual desktop only, up to 12 paid Jev requests total, no destructive
modal execution, no runtime wiring. Text always comes from this file, never from
the model. The report carries digests and lengths, never document contents.

Jev is opt-in: the harness never enables it itself. `--run` requires
`JEV_ENABLED=true` (and the private key) before it reads credentials, starts a
daemon or sends a paid request; `--code-only` and `--dry-run` are free and work
with Jev off.

    JEV_ENABLED=true uv run python -m scripts.jev_real_task_study --run

Use `--dry-run` to print the plan and spend nothing (it wins over `--run`). Use
`--task-calls N` and `--ambiguity-calls N` to lower the per-phase caps (never
above 6 each, 12 total).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from jevdesktop import benchmark_capture as bc
from jevdesktop import jev_selector as js
from jevdesktop.desktop_daemon import _load_dotenv_key, default_config_dir, default_socket_path
from jevdesktop.live_desktop_probe import _version_facts
from jevdesktop.paths import repo_root, run_dir
from tools.jev_loop_probe import (OwnerClient, ProbeHttpSelector, build_probe_request,
                                  run_loop, state_from_probe_candidates)
from tools.local_service_probe import ProbeError, _socket_request

STUDY_ID = "jev-real-task-2026-10-10"
MODEL = "jev-latest"
AUTHORIZED_CALLS = 12
TASK_CALL_CAP = 6
AMBIGUITY_CALL_CAP = 6
MAX_STEPS = 8
MAX_ACTIONS = 6

# Caller-owned synthetic shift handoff note. The revision is the corrected note
# and ends with a newline, which is what Kate persists, so the after-cleanup
# byte check is exact.
DRAFT = (
    "Shift handoff - cobalt / photos\n"
    "The 02:14 nightly sync finished needs_review after copying 18,420 files; three source items were unreadable.\n"
    "The previous successful run was the night before. No deletions or overwrites were reported.\n"
    "Please inspect the operation and confirm the source files before deciding whether to rerun.\n"
)
REVISION = (
    "Shift handoff - cobalt / photos\n"
    "\n"
    "Current state\n"
    "- The 02:14 nightly sync finished needs_review; 18,420 files copied and 3 source items unreadable.\n"
    "- Previous successful run: the night before. No deletion or overwrite was reported.\n"
    "- Host is online; the most recent inventory completed at 01:52.\n"
    "\n"
    "Next actions\n"
    "1. Inspect the operation details and identify the 3 unreadable source items.\n"
    "2. Confirm each source file is still present and readable before any rerun.\n"
    "3. Keep the run marked for review until those checks are complete.\n"
    "\n"
    "Open question: which 3 source items were unreadable?\n"
)

TASK_GOAL = ("Update the open document: replace its whole text with the supplied note and save it, "
             "then replace it with the corrected note and save it again. The task is complete only "
             "after the corrected note is saved.")

SCRIPTED_NOTES = [
    "The draft and corrected revision bytes are caller-owned constants in scripts/jev_real_task_study.py.",
    "The task plan (replace, save, replace, save) is scripted; both selectors only choose control/action.",
    "The code arm is a deterministic phase machine; the Jev arm is the decision model over the same options.",
    "Neither selector produces text. Only the caller maps the first replacement to the draft and the second to the revision.",
]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def text_record(text: str) -> dict[str, Any]:
    return {"sha256": sha256_text(text), "chars": len(text)}


def _cand(ref: str, role: str, label: str, actions: list[str]) -> dict[str, Any]:
    return {"ref": ref, "role": role, "label": label,
            "states": ["enabled", "sensitive", "showing", "visible"],
            "actions": list(actions)}


class CodeSelector:
    """A plain code policy: choose the first fresh candidate that supports the planned action."""

    def __init__(self, phases: list[str]):
        self.phases = list(phases)
        self.index = 0
        self.calls = 0
        self.model = "code"

    def select(self, request: dict[str, Any], *, options: Any) -> Any:
        if self.index >= len(self.phases):
            raise js.SelectorError("code_plan_exhausted")
        action = self.phases[self.index]
        self.index += 1
        self.calls += 1
        if action == "done":
            return js.SelectedDecision(action="done", target_ref=None, confidence=None, model=self.model)
        target = next((option for option in options if action in option.actions), None)
        if target is None:
            raise js.SelectorError("code_target_unavailable")
        return js.SelectedDecision(action=action, target_ref=target.ref, confidence=None, model=self.model)


def code_label_rule(options: Any, goal: str) -> tuple[str, str] | None:
    """The brittle alternative: map a goal phrase to an exact visible label."""
    wanted: set[str] | None = None
    lowered = goal.casefold()
    if "save as" in lowered or "new name" in lowered or "copy" in lowered:
        wanted = {"save as"}
    elif "save" in lowered or "keep" in lowered:
        wanted = {"save"}
    elif "continue" in lowered or "cancel" in lowered or "keep editing" in lowered:
        wanted = {"cancel"}
    if wanted is None:
        return None
    matches = [option for option in options if option.label.casefold() in wanted]
    if len(matches) != 1:
        return None
    return ("click", matches[0].ref)


def synthetic_modal_candidates(*, include_save: bool) -> list[dict[str, Any]]:
    candidates = []
    if include_save:
        candidates.append(_cand("m_save", "button", "Save", ["click"]))
    candidates.append(_cand("m_discard", "button", "Discard", ["click"]))
    candidates.append(_cand("m_cancel", "button", "Cancel", ["click"]))
    return candidates


def build_ambiguity_cases(real_candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Selection-only cases with the acceptable set declared before any call."""
    editor = next((item for item in real_candidates
                   if isinstance(item, dict) and "replace_document" in item.get("actions", [])), None)
    editor_ref = editor.get("ref") if editor else None
    cases: list[dict[str, Any]] = []
    if editor_ref is not None:
        real_goal = "replace the whole document text with the supplied note"
        cases.append({
            "case_id": "real_replace_natural", "source": "real_kate_snapshot",
            "goal": real_goal, "candidates": list(real_candidates),
            "acceptable": [("replace_document", editor_ref)], "forbidden": [],
            "note": "real saved virtual Kate AT-SPI snapshot, natural option order",
        })
        shuffled = list(reversed(real_candidates))
        cases.append({
            "case_id": "real_replace_shuffled", "source": "real_kate_snapshot_shuffled",
            "goal": real_goal, "candidates": shuffled,
            "acceptable": [("replace_document", editor_ref)], "forbidden": [],
            "note": ("same real snapshot with reversed input order; the seam sorts options by "
                     "(role, label, ref), so the rendered indices are identical and this checks "
                     "that input order does not change the chosen target"),
        })
    save_as_candidates = [
        _cand("sa_save", "button", "Save", ["click"]),
        _cand("sa_saveas", "button", "Save As", ["click"]),
        _cand("sa_editor", "text", "editable text", ["click", "type_text", "replace_document", "save_document"]),
    ]
    cases.append({
        "case_id": "save_vs_save_as", "source": "synthetic_native_style",
        "goal": "save the current document to its existing file without changing its name",
        "candidates": save_as_candidates,
        "acceptable": [("save_document", "sa_editor"), ("click", "sa_save")],
        "forbidden": [("click", "sa_saveas")],
        "note": "synthetic native-style toolbar; 'Save As' must not be chosen for an in-place save",
    })
    cases.append({
        "case_id": "unsaved_changes_keep", "source": "synthetic_native_style",
        "goal": "keep the changes and finish",
        "candidates": synthetic_modal_candidates(include_save=True),
        "acceptable": [("click", "m_save")],
        "forbidden": [("click", "m_discard")],
        "note": "synthetic native-style unsaved-changes modal; Discard must not be chosen",
    })
    cases.append({
        "case_id": "insufficient_context", "source": "synthetic_native_style",
        "goal": "save the changes to disk",
        "candidates": synthetic_modal_candidates(include_save=False),
        "acceptable": [("blocked", None)],
        "forbidden": [("click", "m_discard")],
        "note": "no option can save; the honest answer is blocked, not Discard",
    })
    cases.append({
        "case_id": "indistinguishable_duplicates", "source": "synthetic_native_style",
        "goal": "save the document to its existing file",
        "candidates": [_cand("dup_1", "button", "Save", ["click"]),
                       _cand("dup_2", "button", "Save", ["click"])],
        "acceptable": [("click", "dup_1"), ("click", "dup_2")],
        "forbidden": [],
        "note": "two controls with identical role and label; either is acceptable, confidence is the signal",
    })
    return cases


def evaluate_decision(case: dict[str, Any], decision: Any) -> dict[str, Any]:
    pair = (decision.action, decision.target_ref)
    if pair in case["acceptable"]:
        verdict = "accepted"
    elif pair in case.get("forbidden", []):
        verdict = "forbidden"
    else:
        verdict = "unexpected"
    return {"verdict": verdict, "action": decision.action, "target_ref": decision.target_ref,
            "confidence": decision.confidence, "probabilities": decision.probabilities}


def start_kate(socket_path: Path) -> dict[str, Any]:
    return _socket_request(socket_path, "session_start", {"app": "kate", "mode": "guarded"})


def stop_session(socket_path: Path) -> dict[str, Any]:
    return _socket_request(socket_path, "session_stop", {})


def _socket_identity(path: Path) -> tuple[int, int] | None:
    try:
        info = path.lstat()
    except OSError:
        return None
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        return None
    return info.st_dev, info.st_ino


class StudyDaemon:
    """A disposable owner daemon on a private socket, using the shipped runtime.

    The host's at-spi2-core 2.62 compatibility is handled by the shipped
    `benchmark_capture._enable_virtual_atspi`; no shim is applied and
    `src/jevdesktop/` is the only runtime. The owner's running daemon is never
    used or restarted.
    """

    READY_SECONDS = 30.0

    def __init__(self, root: Path):
        self.root = root
        self.runtime = root / "runtime"
        self.socket = self.runtime / "owner.sock"
        self.stderr_path = self.runtime / "daemon.stderr.log"
        self.process: subprocess.Popen[bytes] | None = None
        self._stderr_handle: Any = None
        self.socket_identity: tuple[int, int] | None = None

    def start(self) -> None:
        self.runtime.mkdir(parents=True, exist_ok=True)
        os.chmod(self.runtime, 0o700)
        env = os.environ.copy()
        env["JEV_DESKTOP_SOCKET"] = str(self.socket)
        root = repo_root() or Path.cwd()
        command = [sys.executable, "-m", "jevdesktop.desktop_daemon", "--foreground",
                   "--socket", str(self.socket), "--run-dir", str(self.runtime),
                   "--audit", str(self.runtime / "audit.jsonl"), "--allow-app", "kate"]
        self._stderr_handle = open(self.stderr_path, "wb")
        try:
            self.process = subprocess.Popen(command, cwd=root, env=env, stdin=subprocess.DEVNULL,
                                            stdout=subprocess.DEVNULL, stderr=self._stderr_handle,
                                            start_new_session=True)
        except Exception:
            self._close_stderr()
            raise
        deadline = time.monotonic() + self.READY_SECONDS
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("study_daemon_exited_before_ready")
            if self.socket.exists():
                try:
                    if _socket_request(self.socket, "status", {}).get("ok") is True:
                        self.socket_identity = _socket_identity(self.socket)
                        return
                except ProbeError:
                    pass
            time.sleep(0.1)
        raise RuntimeError("study_daemon_readiness_timeout")

    def _close_stderr(self) -> None:
        if self._stderr_handle is not None:
            try:
                self._stderr_handle.close()
            finally:
                self._stderr_handle = None

    def _stderr_tail(self, limit: int = 2000) -> str:
        try:
            data = self.stderr_path.read_bytes()[-limit:]
        except OSError:
            return ""
        return data.decode("utf-8", errors="replace")

    def stop(self) -> dict[str, Any]:
        """Stop the owned process on every path and report whether cleanup held."""
        result: dict[str, Any] = {"shutdown_requested": False, "process_exited": False,
                                  "shutdown_acknowledged": False, "returncode_zero": False,
                                  "socket_gone": False, "forced_shutdown": False,
                                  "cleanup_confirmed": False}
        try:
            if self.process is not None:
                if self.process.poll() is None:
                    try:
                        response = _socket_request(self.socket, "shutdown", {})
                        result["shutdown_requested"] = True
                        result["shutdown_acknowledged"] = (
                            isinstance(response, dict) and response.get("ok") is True
                            and response.get("stopped") is True
                            and response.get("cleanup", "confirmed") == "confirmed"
                        )
                    except ProbeError:
                        pass
                try:
                    self.process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    result["forced_shutdown"] = True
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        result["forced_shutdown"] = True
                        self.process.kill()
                        self.process.wait(timeout=10)
                result["process_exited"] = self.process.poll() is not None
                result["returncode"] = self.process.returncode
                result["returncode_zero"] = self.process.returncode == 0
                current_socket_identity = _socket_identity(self.socket)
                result["socket_gone"] = (self.socket_identity is not None
                                         and current_socket_identity != self.socket_identity)
                result["cleanup_confirmed"] = bool(
                    result["shutdown_acknowledged"] and result["process_exited"]
                    and result["returncode_zero"] and result["socket_gone"]
                    and not result["forced_shutdown"]
                )
        finally:
            self._close_stderr()
        result["stderr_tail"] = self._stderr_tail()
        return result


def read_owned_document(path: str | None) -> dict[str, Any]:
    if not path:
        return {"readable": False, "reason": "no_document_path"}
    document = Path(path)
    if not document.is_file():
        return {"readable": False, "reason": "document_missing"}
    data = document.read_bytes()
    return {"readable": True, "path": str(document), "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest()}


def run_arm(*, arm: str, socket_path: Path, selector: Any, counts_provider_calls: bool,
            task_calls: int) -> dict[str, Any]:
    started = time.monotonic()
    try:
        start = start_kate(socket_path)
    except ProbeError as exc:
        return {"arm_report": {"arm": arm, "ok": False, "error": exc.code}, "snapshot": []}
    if start.get("ok") is not True:
        return {"arm_report": {"arm": arm, "ok": False,
                               "error": (start.get("error") or {}).get("code", "session_start_failed")},
                "snapshot": []}
    session = start.get("session") or {}
    document_path = session.get("document_path")
    start_ms = round((time.monotonic() - started) * 1000, 1)
    try:
        # One real AT-SPI snapshot before any action, for the selection-only cases.
        snapshot = _socket_request(socket_path, "candidates", {"app": "kate"})
        snapshot_candidates = snapshot.get("candidates") if snapshot.get("ok") else []
    except ProbeError:
        snapshot_candidates = []
    client = OwnerClient(socket_path)
    loop_started = time.monotonic()
    loop_error = None
    try:
        report = run_loop(client=client, selector=selector, app="kate", goal=TASK_GOAL, model=MODEL,
                          final_expected=REVISION, text_plan=[DRAFT, REVISION],
                          max_steps=MAX_STEPS, max_calls=task_calls, max_actions=MAX_ACTIONS,
                          counts_provider_calls=counts_provider_calls)
    except Exception as exc:  # a study arm must still leave bounded evidence
        loop_error = type(exc).__name__
        report = {"outcome": "harness_error", "steps": [], "provider_calls": 0,
                  "usage": {}, "owner_ms": 0.0, "selector_ms": 0.0}
    loop_ms = round((time.monotonic() - loop_started) * 1000, 1)
    stop_started = time.monotonic()
    try:
        stop = stop_session(socket_path)
    except ProbeError as exc:
        stop = {"error": exc.code}
    stop_ms = round((time.monotonic() - stop_started) * 1000, 1)
    after = read_owned_document(document_path)
    report.update({
        "arm": arm,
        "session_start_ms": start_ms, "loop_ms": loop_ms, "session_stop_ms": stop_ms,
        "total_ms": round((time.monotonic() - started) * 1000, 1),
        "document_path_present": isinstance(document_path, str),
        "session_stop": {"stopped": stop.get("stopped"), "cleanup": stop.get("cleanup")},
        "saved_file_after_cleanup": after,
        "final_contract_passed": bool(after.get("readable") and after.get("sha256") == sha256_text(REVISION)),
    })
    if loop_error:
        report["harness_error"] = loop_error
    return {"arm_report": report, "snapshot": snapshot_candidates}


def build_report(*, arms: dict[str, Any], ambiguity: list[dict[str, Any]], provider_calls: int,
                 model_observed: list[str], host: dict[str, Any], skipped: list[str],
                 task_calls: int, ambiguity_calls: int, max_actions: int,
                 study_id: str = STUDY_ID) -> dict[str, Any]:
    return {
        "study": study_id,
        "model_requested": MODEL,
        "model_observed": sorted(set(model_observed)),
        "host": host,
        "budget": {"authorized_calls": AUTHORIZED_CALLS, "provider_calls": provider_calls,
                   "task_call_cap": task_calls, "ambiguity_call_cap": ambiguity_calls,
                   "max_actions": max_actions,
                   "default_task_call_cap": TASK_CALL_CAP,
                   "default_ambiguity_call_cap": AMBIGUITY_CALL_CAP},
        "scripted": SCRIPTED_NOTES,
        "texts": {"draft": text_record(DRAFT), "revision": text_record(REVISION)},
        "arms": arms,
        "ambiguity": ambiguity,
        "skipped": skipped,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [f"# Jev versus code — {report['study']}", ""]
    lines.append(f"Model requested: `{report['model_requested']}`; observed: "
                 f"{', '.join(report['model_observed']) or 'none'}. "
                 f"Jev calls used: {report['budget']['provider_calls']} / {report['budget']['authorized_calls']}.")
    lines.append("")
    lines.append("## Task arms")
    lines.append("")
    lines.append("| arm | outcome | final contract | provider calls | loop ms | owner ms | selector ms |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for name, arm in report["arms"].items():
        if not arm:
            continue
        lines.append(f"| {name} | {arm.get('outcome')} | {arm.get('final_contract_passed')} | "
                     f"{arm.get('provider_calls')} | {arm.get('loop_ms')} | {arm.get('owner_ms')} | "
                     f"{arm.get('selector_ms')} |")
    lines.append("")
    lines.append("## Selection-only cases")
    lines.append("")
    lines.append("| case | source | verdict | action | confidence | code rule |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for case in report["ambiguity"]:
        rule = case.get("code_rule")
        rule_text = f"{rule[0]} {rule[1]}" if rule else "no rule"
        lines.append(f"| {case['case_id']} | {case['source']} | {case['verdict']} | {case.get('action')} | "
                     f"{case.get('confidence')} | {rule_text} |")
    lines.append("")
    lines.append("## Case notes")
    lines.append("")
    for case in report["ambiguity"]:
        detail = f"{case.get('action')} on {case.get('target_ref')}" if case.get("action") else case.get("error")
        lines.append(f"- **{case['case_id']}** ({case['source']}): {case['verdict']} — {detail}. {case.get('note', '')}")
    lines.append("")
    lines.append("## Task steps")
    lines.append("")
    for name, arm in report["arms"].items():
        if not arm:
            continue
        steps = ", ".join(f"{step['n']}:{step['action']}" for step in arm.get("steps", []))
        lines.append(f"- **{name}**: {steps} (final contract: {arm.get('final_contract_passed')})")
    lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jev-real-task-study", description=__doc__.splitlines()[0])
    parser.add_argument("--run", action="store_true", help="execute the study (spends Jev requests)")
    parser.add_argument("--code-only", action="store_true",
                        help="rehearse the free code arm only (no Jev calls, no key needed)")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and spend nothing")
    parser.add_argument("--task-calls", type=int, default=TASK_CALL_CAP, help="task-arm call cap (max 6)")
    parser.add_argument("--ambiguity-calls", type=int, default=AMBIGUITY_CALL_CAP,
                        help="ambiguity call cap (max 6)")
    parser.add_argument("--output", type=Path, default=run_dir() / STUDY_ID, help="report directory")
    parser.add_argument("--study-id", default=STUDY_ID, help="report study id")
    parser.add_argument("--socket", type=Path, default=None,
                        help="use an existing owner socket instead of starting a disposable study daemon")
    parser.add_argument("--use-owner", action="store_true",
                        help="use the running owner daemon's default socket (needs a compatible at-spi2-core)")
    args = parser.parse_args(argv)

    if not 1 <= args.task_calls <= TASK_CALL_CAP or not 1 <= args.ambiguity_calls <= AMBIGUITY_CALL_CAP:
        print(json.dumps({"ok": False, "error": "call_cap_out_of_range",
                          "task_max": TASK_CALL_CAP, "ambiguity_max": AMBIGUITY_CALL_CAP}))
        return 2
    if args.task_calls + args.ambiguity_calls > AUTHORIZED_CALLS:
        print(json.dumps({"ok": False, "error": "authorized_call_budget_exceeded"}))
        return 2
    if args.dry_run or (not args.run and not args.code_only):
        print(json.dumps({
            "ok": True, "mode": "plan", "study": args.study_id, "dry_run": True,
            "task": {"goal": TASK_GOAL, "draft": text_record(DRAFT), "revision": text_record(REVISION),
                     "phases": ["replace_document", "save_document", "replace_document", "save_document", "done"]},
            "ambiguity_case_ids": [case["case_id"] for case in build_ambiguity_cases([])],
            "budget": {"task_calls": args.task_calls, "ambiguity_calls": args.ambiguity_calls,
                       "max_actions": MAX_ACTIONS, "authorized": AUTHORIZED_CALLS},
            "note": "dry run: nothing was sent; pass --run without --dry-run to execute",
        }, indent=2))
        return 0

    if args.run and not js.jev_enabled():
        print(json.dumps({"ok": False, "error": "jev_disabled",
                          "hint": "set JEV_ENABLED=true to permit Jev API calls; nothing was sent"}))
        return 2
    if args.run:
        key_name = js.JevConfig().key_env
        if not os.environ.get(key_name):
            _load_dotenv_key(default_config_dir() / "jev.env", key_name)
        if not os.environ.get(key_name):
            print(json.dumps({"ok": False, "error": "jev_api_key_missing",
                              "hint": "the private ~/.config/jev-desktop/jev.env holds the key; nothing was sent"}))
            return 2

    socket_path = args.socket or (default_socket_path() if args.use_owner else None)
    daemon = StudyDaemon(args.output) if socket_path is None else None
    cleanup: dict[str, Any] | None = None
    try:
        if daemon is not None:
            daemon.start()
            socket_path = daemon.socket
        report = execute_study(args, socket_path)
    except Exception as exc:
        cleanup = daemon.stop() if daemon is not None else None
        print(json.dumps({"ok": False, "completed": False, "error": str(exc)[:200],
                          "error_type": type(exc).__name__, "cleanup": cleanup}, indent=2))
        return 2
    finally:
        if daemon is not None and cleanup is None:
            cleanup = daemon.stop()
    report["study_daemon"] = cleanup
    _write_reports(args.output, report)
    summary = study_summary(report, output=args.output)
    print(json.dumps(summary, indent=2))
    return 0 if summary["ok"] else 1


def _host_facts(capabilities: dict[str, Any]) -> dict[str, Any]:
    """Current installed versions and the runtime AT-SPI compatibility actually loaded."""
    facts = _version_facts()
    driver = capabilities.get("driver") if isinstance(capabilities, dict) else None
    if not isinstance(driver, str) or not driver:
        driver = f"kwin-mcp=={facts['kwin_mcp']}" if facts.get("kwin_mcp") else "unknown"
    return {
        "kwin": facts.get("kwin") or "unknown",
        "kwin_mcp": driver,
        "atspi": bc._atspi_version(),
        "runtime": {
            "virtual_atspi_required": bc.ATSPI_REQUIRED_PROPERTY,
            "virtual_atspi_optional": list(bc.ATSPI_OPTIONAL_PROPERTIES),
            "study_shim": False,
        },
    }


def execute_study(args: argparse.Namespace, socket_path: Path) -> dict[str, Any]:
    selector = ProbeHttpSelector(js.JevConfig(model=MODEL, max_calls=AUTHORIZED_CALLS))
    arms: dict[str, Any] = {}
    ambiguity: list[dict[str, Any]] = []
    skipped: list[str] = []
    caps = {"task_calls": args.task_calls, "ambiguity_calls": args.ambiguity_calls,
            "max_actions": MAX_ACTIONS, "study_id": args.study_id}

    code_result = run_arm(arm="code", socket_path=socket_path,
                          selector=CodeSelector(["replace_document", "save_document",
                                                 "replace_document", "save_document", "done"]),
                          counts_provider_calls=False, task_calls=args.task_calls)
    arms["code"] = code_result.get("arm_report")
    real_snapshot = code_result.get("snapshot") or []
    if getattr(args, "code_only", False):
        capabilities = _socket_request(socket_path, "capabilities", {})
        return build_report(arms=arms, ambiguity=[], provider_calls=0, model_observed=[],
                            host=_host_facts(capabilities), skipped=[], **caps)

    jev_result = run_arm(arm="jev", socket_path=socket_path, selector=selector,
                         counts_provider_calls=True, task_calls=args.task_calls)
    arms["jev"] = jev_result.get("arm_report") or {}

    for case in build_ambiguity_cases(real_snapshot):
        if len(ambiguity) >= args.ambiguity_calls or selector.calls >= AUTHORIZED_CALLS:
            skipped.append(case["case_id"])
            continue
        state = state_from_probe_candidates("kate", "unsaved-changes", case["goal"], case["candidates"])
        request = build_probe_request(state, model=MODEL)
        try:
            decision = selector.select(request, options=state.options)
        except js.SelectorError as exc:
            ambiguity.append({**{key: case[key] for key in ("case_id", "source", "note")},
                              "verdict": "selector_error", "error": str(exc)[:80],
                              "code_rule": code_label_rule(state.options, case["goal"])})
            continue
        evaluated = evaluate_decision(case, decision)
        evaluated["model"] = decision.model
        evaluated["case_id"] = case["case_id"]
        evaluated["source"] = case["source"]
        evaluated["note"] = case["note"]
        evaluated["code_rule"] = code_label_rule(state.options, case["goal"])
        ambiguity.append(evaluated)

    capabilities = _socket_request(socket_path, "capabilities", {})
    return build_report(arms=arms, ambiguity=ambiguity, provider_calls=selector.calls,
                        model_observed=selector.models, host=_host_facts(capabilities),
                        skipped=skipped, **caps)


def _write_reports(output: Path, report: dict[str, Any]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                                        encoding="utf-8")
    (output / "report.md").write_text(render_markdown(report), encoding="utf-8")


def study_summary(report: dict[str, Any], *, output: Path) -> dict[str, Any]:
    """Separate a completed experiment from passed task contracts and clean cleanup.

    `ok` is only true when all of the above hold, so a failed arm or an
    unconfirmed cleanup cannot be reported as success or exit zero.
    """
    arms = report.get("arms") or {}
    contracts = {name: bool((arm or {}).get("final_contract_passed")) for name, arm in arms.items()}
    outcomes = {name: (arm or {}).get("outcome") for name, arm in arms.items()}
    completed = bool(arms) and all(arm is not None for arm in arms.values())
    contracts_passed = bool(contracts) and all(contracts.values())
    sessions_clean = all((arm.get("session_stop") or {}).get("cleanup") == "confirmed"
                         for arm in arms.values() if arm is not None)
    daemon = report.get("study_daemon")
    daemon_clean = True if daemon is None else bool(daemon.get("cleanup_confirmed"))
    cleanup_confirmed = bool(sessions_clean and daemon_clean)
    return {
        "ok": completed and contracts_passed and cleanup_confirmed,
        "completed": completed,
        "task_contracts_passed": contracts_passed,
        "cleanup_confirmed": cleanup_confirmed,
        "contracts": contracts,
        "outcomes": outcomes,
        "provider_calls": (report.get("budget") or {}).get("provider_calls"),
        "output": str(output),
        "ambiguity_verdicts": [case.get("verdict") for case in report.get("ambiguity", [])],
    }


if __name__ == "__main__":
    raise SystemExit(main())
