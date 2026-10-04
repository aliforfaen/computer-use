#!/usr/bin/env python3
"""Jev-driven control loop, proof of concept (unwired).

The selector picks one enumerated option; this probe closes the loop around it:

    candidates -> Jev select -> act with a code-owned verification -> re-enumerate

Nothing here is wired into the daemon. The probe is a client of the same owner
socket the CLI uses, so execution, policy, budgets, focus handling and the audit
stay exactly where they are. The only new thing is the loop, and the only new
authority it has is the ability to stop.

    # free: enumerate once and show the state the loop would send
    uv run python -m tools.jev_loop_probe --app kcalc --goal "enter 1 2 3" --dry-run

    # the real thing: one Jev call per step, every click verified on the display
    uv run python -m tools.jev_loop_probe --app kcalc --goal "enter the digits 1, 2 then 3" \
        --expect One=1 --expect Two=12 --expect Three=123 --report-dir run/jev-loop-kcalc

`--expect LABEL=VALUE` is how the loop verifies: a click on a button with that
label must leave the display reading exactly VALUE (`display_text`). The
expectation comes from this command line, never from the model. A click with no
expectation is refused by policy rather than acted on, so **the loop never takes
an action it cannot verify** — which is why it fails closed instead of guessing.

`done` is never trusted: the loop reports `done_verified` only when a strong
display check already passed, otherwise `done_unverified`. Actions that need free
text (`type_text` and friends) are refused: only a text-argument helper may
produce typed text (AGENTS.md).

Each step's goal is `--goal` (with the loop's own action history appended to the
state), or the matching entry of `--goal-sequence` when the plan is scripted.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

from desktop_daemon import _load_dotenv_key, default_config_dir, default_socket_path
from jev_selector import (HttpSelector, JevConfig, MAX_OPTIONS, RecordedSelector, SelectorError,
                          build_request, estimate_tokens, state_from_candidates)
from tools.local_service_probe import _socket_request

MAX_STEPS_HARD_CAP = 20
MAX_CALLS_HARD_CAP = 20
SUPPORTED_ACTIONS = {"click", "scroll"}
TARGETLESS_ACTIONS = {"wait", "done", "blocked"}
REFUSED_ACTIONS = {"type_text", "replace_document", "save_document", "navigate_url"}


class OwnerClient:
    """Thin client for the owner socket: the CLI's own protocol, no new surface."""

    def __init__(self, socket_path: Path):
        self.socket_path = socket_path

    def candidates(self, app: str) -> dict[str, Any]:
        return _socket_request(self.socket_path, "candidates", {"app": app})

    def act(self, app: str, action: str, target_ref: str, verification: str, *,
            expected: str | None = None, direction: str | None = None, steps: int = 1) -> dict[str, Any]:
        params: dict[str, Any] = {"app": app, "action": action, "target_ref": target_ref,
                                  "verification": verification}
        if expected is not None:
            params["expected"] = expected
        if action == "scroll":
            params["direction"] = direction
            params["steps"] = steps
        return _socket_request(self.socket_path, "act", params)


def _finish(report: dict[str, Any], stop_reason: str, outcome: str, *, error: str | None = None,
            note: str | None = None) -> dict[str, Any]:
    report["stop_reason"] = stop_reason
    report["outcome"] = outcome
    if error is not None:
        report["error"] = error
    if note is not None:
        report["note"] = note
    return report


def run_loop(*, client: Any, selector: Any, app: str, goal: str, model: str = "jev-latest",
             expectations: dict[str, str] | None = None, goals: list[str] | None = None,
             max_steps: int = 8, max_calls: int = 8, counts_provider_calls: bool = False,
             limit: int = MAX_OPTIONS, scroll_direction: str = "down",
             clock: Any = time.monotonic) -> dict[str, Any]:
    """Drive select -> act -> verify until the model stops or a bound trips.

    Every stop reason is code-owned; no step is taken without a verification that
    policy accepts, and no `done` is reported as success without a passed check.
    """
    expectations = {label.casefold(): value for label, value in (expectations or {}).items()}
    report: dict[str, Any] = {
        "app": app, "goal": goal, "model": model, "steps": [], "provider_calls": 0,
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "verified_postcondition": None, "stop_reason": None, "outcome": None,
        "started": clock(),
    }
    history: list[str] = []
    for number in range(1, max_steps + 1):
        step_goal = goals[number - 1] if goals and number - 1 < len(goals) else goal
        if goals and number - 1 >= len(goals):
            return _finish(report, "goal_sequence_exhausted", "budget_exhausted")
        snapshot = client.candidates(app)
        if snapshot.get("ok") is not True:
            return _finish(report, "owner_error", "owner_error",
                           error=(snapshot.get("error") or {}).get("code", "owner_error"))
        try:
            state = state_from_candidates(app, snapshot.get("window_title") or "", step_goal,
                                          snapshot.get("candidates") or [], limit=limit, history=history)
        except (SelectorError, ValueError) as exc:
            return _finish(report, "invalid_state", "invalid_state", error=str(exc)[:80])
        if not state.options:
            return _finish(report, "no_options", "no_options",
                           note="the state has no usable option to choose from")
        request = build_request(state, model=model)
        if report["provider_calls"] >= max_calls:
            return _finish(report, "call_budget_exhausted", "budget_exhausted")
        try:
            decision = selector.select(request, options=state.options)
        except SelectorError as exc:
            return _finish(report, "selector_error", "invalid_answer", error=str(exc)[:80])
        if counts_provider_calls:
            report["provider_calls"] += 1
        usage = decision.usage if isinstance(decision.usage, dict) else {}
        for key in ("input_tokens", "output_tokens"):
            if isinstance(usage.get(key), int):
                report["usage"][key] += usage[key]
        step: dict[str, Any] = {"n": number, "goal": step_goal, "action": decision.action,
                                "confidence": decision.confidence, "probabilities": decision.probabilities,
                                "target_ref": decision.target_ref, "target_label": None,
                                "verification": None, "verification_kind": None, "expected": None,
                                "ok": False, "usage": usage,
                                "state_tokens_estimate": estimate_tokens(request["state"])}
        if decision.action in TARGETLESS_ACTIONS:
            step["ok"] = True
            report["steps"].append(step)
            if decision.action == "done":
                verified = report["verified_postcondition"]
                return _finish(report, "model_done",
                               "done_verified" if verified else "done_unverified",
                               note=None if verified else
                               "the model proposed done but no strong check had passed yet")
            if decision.action == "blocked":
                return _finish(report, "model_blocked", "blocked")
            history.append("wait")
            continue
        if decision.action in REFUSED_ACTIONS:
            report["steps"].append(step)
            return _finish(report, "unsupported_action", "unsupported_action", error=decision.action,
                           note="this probe has no text source; only a text-argument helper may type")
        if decision.action not in SUPPORTED_ACTIONS:
            report["steps"].append(step)
            return _finish(report, "unsupported_action", "unsupported_action", error=decision.action)
        target = next((option for option in state.options if option.ref == decision.target_ref), None)
        if target is None:
            report["steps"].append(step)
            return _finish(report, "unenumerated_target", "invalid_answer")
        step["target_label"] = target.label
        if decision.action == "scroll":
            step["verification_kind"] = "scroll_changed"
        else:
            expected = expectations.get(target.label.casefold())
            step["expected"] = expected
            step["verification_kind"] = "display_text" if expected is not None else "target_focused"
        acted = client.act(app, decision.action, decision.target_ref, step["verification_kind"],
                           expected=step["expected"],
                           direction=scroll_direction if decision.action == "scroll" else None)
        step["verification"] = acted.get("verification")
        step["evidence"] = acted.get("evidence")
        if acted.get("ok") is not True:
            code = (acted.get("error") or {}).get("code", "act_failed")
            report["steps"].append(step)
            if code == "verification_failed":
                return _finish(report, "verification_failed", "wrong_choice", error=code,
                               note="the action ran but the code-owned check did not pass")
            if code in {"action_precondition_failed", "unsupported_verification"}:
                return _finish(report, "verification_unavailable", "verification_unavailable", error=code,
                               note=f"no acceptable check for {decision.action} on {target.label!r}; "
                                    "the loop refuses to act unverified")
            return _finish(report, "owner_error", "owner_error", error=code)
        step["ok"] = acted.get("status") == "ok" and step["verification"] == "passed"
        report["steps"].append(step)
        if not step["ok"]:
            return _finish(report, "verification_failed", "unverified_action")
        history.append(f"{decision.action} {target.label!r}")
        if step["verification_kind"] == "display_text":
            report["verified_postcondition"] = {"step": number, "target_label": target.label,
                                                "expected": step["expected"], "evidence": step["evidence"]}
    return _finish(report, "step_budget_exhausted", "budget_exhausted")


def _parse_expectations(items: list[str]) -> dict[str, str]:
    expectations: dict[str, str] = {}
    for item in items:
        label, separator, value = item.partition("=")
        if not separator or not label.strip():
            raise SystemExit(f"--expect wants LABEL=VALUE, got {item!r}")
        expectations[label.strip()] = value
    return expectations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jev-loop-probe", description=__doc__.splitlines()[0])
    parser.add_argument("--app", required=True, help="allowlisted app to drive, for example kcalc")
    parser.add_argument("--goal", required=True, help="the bounded task for this session")
    parser.add_argument("--goal-sequence", default=None,
                        help="optional '|'-separated per-step goals, when the plan is scripted")
    parser.add_argument("--expect", action="append", default=[], metavar="LABEL=VALUE",
                        help="expected display reading after clicking that label; repeatable")
    parser.add_argument("--max-steps", type=int, default=8, help=f"step cap (maximum {MAX_STEPS_HARD_CAP})")
    parser.add_argument("--max-calls", type=int, default=8, help=f"provider call cap (maximum {MAX_CALLS_HARD_CAP})")
    parser.add_argument("--model", default="jev-latest", help="pinned model alias")
    parser.add_argument("--limit", type=int, default=MAX_OPTIONS, help="maximum enumerated options")
    parser.add_argument("--socket", type=Path, default=None, help="owner Unix socket path")
    parser.add_argument("--dotenv", type=Path, default=None,
                        help="private dotenv holding JEV_API_KEY; values are never printed")
    parser.add_argument("--dry-run", action="store_true", help="enumerate once, send nothing")
    parser.add_argument("--recorded", type=Path, default=None,
                        help="replay recorded answers JSON (a list replays one answer per step)")
    parser.add_argument("--report-dir", type=Path, default=None, help="write report.json here")
    args = parser.parse_args(argv)

    if not 1 <= args.max_steps <= MAX_STEPS_HARD_CAP:
        print(json.dumps({"ok": False, "error": "max_steps_out_of_range", "hard_cap": MAX_STEPS_HARD_CAP}))
        return 2
    if not 1 <= args.max_calls <= MAX_CALLS_HARD_CAP:
        print(json.dumps({"ok": False, "error": "max_calls_out_of_range", "hard_cap": MAX_CALLS_HARD_CAP}))
        return 2
    expectations = _parse_expectations(args.expect)
    goals = [part.strip() for part in args.goal_sequence.split("|")] if args.goal_sequence else None
    socket_path = args.socket or default_socket_path()
    client = OwnerClient(socket_path)

    if args.dry_run:
        snapshot = client.candidates(args.app)
        if snapshot.get("ok") is not True:
            print(json.dumps({"ok": False, "mode": "dry_run",
                              "error": (snapshot.get("error") or {}).get("code", "owner_error")}))
            return 2
        state = state_from_candidates(args.app, snapshot.get("window_title") or "", args.goal,
                                      snapshot.get("candidates") or [], limit=args.limit)
        request = build_request(state, model=args.model)
        print(json.dumps({"ok": True, "mode": "dry_run", "provider_calls": 0, "socket": str(socket_path),
                          "options": len(state.options), "state_text": request["state"],
                          "state_tokens_estimate": estimate_tokens(request["state"]),
                          "request_tokens_estimate": estimate_tokens(request),
                          "expectations": expectations,
                          "note": "nothing was sent; drop --dry-run to run the loop"}, indent=2))
        return 0

    if args.recorded:
        payload = json.loads(args.recorded.read_text(encoding="utf-8"))
        answers = payload.get("answers") if isinstance(payload, dict) and "answers" in payload else payload
        selector: Any = RecordedSelector(answers, model="recorded")
        counts_provider_calls = False
    else:
        key_name = JevConfig().key_env
        dotenv = args.dotenv or (default_config_dir() / "jev.env")
        if not os.environ.get(key_name):
            _load_dotenv_key(dotenv, key_name)
        if not os.environ.get(key_name):
            print(json.dumps({"ok": False, "error": "jev_api_key_missing", "dotenv": str(dotenv),
                              "hint": f"export {key_name} or put it in that private file (0600); "
                                      "nothing was sent"}))
            return 2
        selector = HttpSelector(JevConfig(model=args.model, max_calls=args.max_calls))
        counts_provider_calls = True

    report = run_loop(client=client, selector=selector, app=args.app, goal=args.goal, model=args.model,
                      expectations=expectations, goals=goals, max_steps=args.max_steps,
                      max_calls=args.max_calls, counts_provider_calls=counts_provider_calls,
                      limit=args.limit)
    report["ok"] = report["outcome"] in {"done_verified"}
    report["elapsed_ms"] = round((time.monotonic() - report["started"]) * 1000, 1)
    report.pop("started", None)
    report["expectations"] = expectations
    if args.report_dir:
        args.report_dir.mkdir(parents=True, exist_ok=True)
        (args.report_dir / "report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        report["report_path"] = str(args.report_dir / "report.json")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
