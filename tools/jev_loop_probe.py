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
        --expect One=1 --expect Two=12 --expect Three=123 --final-expected 123 --report-dir run/jev-loop-kcalc

`--expect LABEL=VALUE` is how the loop verifies: a click on a button with that
label must leave the display reading exactly VALUE (`display_text`). The
expectation comes from this command line, never from the model. A click with no
expectation is refused by policy rather than acted on, so **the loop never takes
an action it cannot verify** — which is why it fails closed instead of guessing.

Kate document actions are supported with caller-supplied text:

    uv run python -m tools.jev_loop_probe --app kate --goal "replace the document and save it" \
        --replace-text-file draft.txt --replace-text-file revision.txt \
        --final-expected-file revision.txt --report-dir run/jev-loop-kate

Each `replace_document` consumes the next `--replace-text-file` (`-` reads
stdin once) and is verified by `target_text`; `save_document` is verified by
`document_saved`. The model never produces text. A document task is
`done_verified` only when the latest verified action is a save whose content
matches `--final-expected-file`; an earlier saved draft is not completion.

`done` is never trusted: the loop reports `done_verified` only when the latest
action passed a strong check matching the caller's explicit final contract,
otherwise `done_unverified`. `type_text` and `navigate_url` are refused: only a
text-argument helper may produce typed text (AGENTS.md). Reports carry text
digests and lengths, never the text itself.

Each step's goal is `--goal` (with the loop's own action history appended to the
state), or the matching entry of `--goal-sequence` when the plan is scripted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from jevdesktop import jev_selector as js
from jevdesktop.desktop_daemon import _load_dotenv_key, default_config_dir, default_socket_path
from jevdesktop.jev_selector import JevConfig, MAX_OPTIONS, SelectorError, estimate_tokens, jev_enabled
from tools.local_service_probe import _socket_request

MAX_STEPS_HARD_CAP = 20
MAX_CALLS_HARD_CAP = 20
MAX_REPLACE_TEXT_CHARS = 4096
SUPPORTED_ACTIONS = {"click", "scroll", "replace_document", "save_document"}
TARGETLESS_ACTIONS = {"wait", "done", "blocked"}
# Only a caller-supplied text file may produce typed text; the model never does.
REFUSED_ACTIONS = {"type_text", "navigate_url"}

# The seam in `src/jevdesktop/jev_selector.py` enumerates only the original
# action vocabulary. This study must not change the runtime, so the probe carries
# its own study-local extension for the two document actions the owner already
# supports. It reuses the seam's rendering, budget and validation helpers; only
# the enumerated action list grows, so the model can still only choose an index we
# created and the caller still owns all text and every verification.
DOCUMENT_ACTIONS = ("replace_document", "save_document")
DOCUMENT_ACTION_CRITERIA = {
    "replace_document": "replace the whole document text with the caller-supplied text",
    "save_document": "save the document to disk",
}


def state_from_probe_candidates(app: str, window_title: str, goal: str, candidates: Any, *,
                                limit: int = MAX_OPTIONS, history: Any = ()) -> Any:
    """`state_from_candidates` plus the study-local document actions."""
    state = js.state_from_candidates(app, window_title, goal, candidates, limit=limit, history=history)
    raw_by_ref = {item["ref"]: item for item in candidates
                  if isinstance(item, dict) and isinstance(item.get("ref"), str)}
    options = []
    for option in state.options:
        extra = tuple(sorted(str(action) for action in raw_by_ref.get(option.ref, {}).get("actions", [])
                             if action in DOCUMENT_ACTIONS))
        merged = tuple(sorted(set(option.actions) | set(extra)))
        options.append(option if merged == option.actions else js.DecisionOption(
            ref=option.ref, role=option.role, label=option.label, states=option.states, actions=merged))
    return js.DecisionState(app=state.app, window_title=state.window_title, goal=state.goal,
                            options=tuple(options), history=state.history)


def build_probe_request(state: Any, *, model: str = "jev-latest", include_target: bool = True) -> dict[str, Any]:
    """`build_request` plus the study-local document action criteria."""
    questions = js.build_questions(state, include_target=include_target)
    questions["action"]["criteria"].update(DOCUMENT_ACTION_CRITERIA)
    state_text = js.render_state_text(state)
    js.check_budget(state_text, questions)
    return {"model": model, "state": state_text, "questions": questions}


def validate_probe_answer(answers: Any, *, options: Any) -> Any:
    """`validate_answer`, extended for the two study-local document actions.

    The document branch repeats the seam's `type == choice` check and, like the
    seam now does for every action, rejects an action/target pair the target does
    not support. An invalid pair is never reinterpreted as a valid click.
    """
    action_answer = answers.get("action") if isinstance(answers, dict) else None
    if isinstance(action_answer, dict) and action_answer.get("choice") in DOCUMENT_ACTIONS:
        if action_answer.get("type") != "choice":
            raise js.SelectorError("action_answer_invalid")
        action = action_answer["choice"]
        target_answer = answers.get("target")
        if not isinstance(target_answer, dict) or target_answer.get("type") != "choice":
            raise js.SelectorError("target_answer_missing")
        index = js._option_index(target_answer.get("choice"))
        if index is None or not 1 <= index <= len(options):
            raise js.SelectorError("unenumerated_target")
        option = options[index - 1]
        if action not in option.actions:
            raise js.SelectorError("action_not_supported_by_target")
        return js.SelectedDecision(action=action, target_ref=option.ref,
                                   confidence=js._confidence(action_answer.get("confidence")),
                                   probabilities=js._probabilities(action_answer.get("probabilities")))
    return js.validate_answer(answers, options=options)


class ProbeRecordedSelector:
    """`RecordedSelector` that accepts the study-local document actions."""

    def __init__(self, answers: dict[str, Any] | list[dict[str, Any]], *, model: str | None = None):
        self.answers = answers
        self.model = model
        self.calls = 0

    def _next_answer(self) -> dict[str, Any]:
        if isinstance(self.answers, list):
            if self.calls >= len(self.answers):
                raise js.SelectorError("recorded_answers_exhausted")
            answer = self.answers[self.calls]
        else:
            answer = self.answers
        self.calls += 1
        return answer

    def select(self, request: dict[str, Any], *, options: Any) -> Any:
        decision = validate_probe_answer(self._next_answer(), options=options)
        return js.SelectedDecision(action=decision.action, target_ref=decision.target_ref,
                                   confidence=decision.confidence, probabilities=decision.probabilities,
                                   model=self.model or str(request.get("model") or ""), usage={})


class ProbeHttpSelector:
    """`HttpSelector` with the study-local document vocabulary.

    It reuses the seam's `_post`, so the `JEV_ENABLED` gate, private key lookup,
    hard call cap and transport error mapping are exactly the shipped ones. Only
    the response validation differs, and only by the two extra action names.
    """

    def __init__(self, config: JevConfig):
        self._http = js.HttpSelector(config)
        self.models: list[str] = []

    @property
    def calls(self) -> int:
        return self._http.calls

    @property
    def transport_calls(self) -> int:
        """Attempted HTTP requests, incremented before the request is sent."""
        return self._http.calls

    def select(self, request: dict[str, Any], *, options: Any) -> Any:
        try:
            response = self._http._post(request)
        except js.SelectorError:
            raise
        except Exception as exc:  # transport failures stay safe and typed
            raise js.SelectorError("selector_transport_failed") from exc
        status = getattr(response, "status_code", None)
        if status != 200:
            raise js.SelectorError(f"selector_http_{status}" if isinstance(status, int) else "selector_http_error")
        try:
            payload = response.json()
        except Exception as exc:
            raise js.SelectorError("selector_response_not_json") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("answers"), dict):
            raise js.SelectorError("selector_response_invalid")
        decision = validate_probe_answer(payload["answers"], options=options)
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        model = payload.get("model") if isinstance(payload.get("model"), str) else None
        if model:
            self.models.append(model)
        return js.SelectedDecision(action=decision.action, target_ref=decision.target_ref,
                                   confidence=decision.confidence, probabilities=decision.probabilities,
                                   model=model, usage=usage, raw=payload)


class OwnerClient:
    """Thin client for the owner socket: the CLI's own protocol, no new surface."""

    def __init__(self, socket_path: Path):
        self.socket_path = socket_path

    def candidates(self, app: str) -> dict[str, Any]:
        return _socket_request(self.socket_path, "candidates", {"app": app})

    def act(self, app: str, action: str, target_ref: str, verification: str, *,
            expected: str | None = None, text: str | None = None,
            direction: str | None = None, steps: int = 1) -> dict[str, Any]:
        params: dict[str, Any] = {"app": app, "action": action, "target_ref": target_ref,
                                  "verification": verification}
        if expected is not None:
            params["expected"] = expected
        if text is not None:
            params["text"] = text
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


def _text_digest(text: str | None) -> dict[str, Any]:
    """Digest for caller-owned text so reports never carry document contents."""
    if text is None:
        return {"sha256": None, "chars": None}
    return {"sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "chars": len(text)}


def _completion_matches(report: dict[str, Any], final_expected: str | None) -> bool:
    """The task-level contract: the latest verified effect must equal the caller's final value."""
    if final_expected is None:
        return False
    evidence = report.get("verified_postcondition")
    if not isinstance(evidence, dict):
        return False
    if evidence.get("kind") == "display_text":
        return evidence.get("expected") == final_expected
    if evidence.get("kind") == "document_saved":
        return evidence.get("sha256") == hashlib.sha256(final_expected.encode("utf-8")).hexdigest()
    return False


def _transport_calls(selector: Any) -> int | None:
    """A transport-backed selector's attempted-request counter, if it exposes one.

    Offline selectors only count successful selections, so they are deliberately
    not consulted here; the loop keeps its own attempt count separate.
    """
    value = getattr(selector, "transport_calls", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def run_loop(*, client: Any, selector: Any, app: str, goal: str, model: str = "jev-latest",
             expectations: dict[str, str] | None = None, final_expected: str | None = None,
             text_plan: list[str] | None = None, goals: list[str] | None = None,
             max_steps: int = 8, max_calls: int = 8, max_actions: int | None = None,
             counts_provider_calls: bool = False,
             limit: int = MAX_OPTIONS, scroll_direction: str = "down",
             clock: Any = time.monotonic) -> dict[str, Any]:
    """Drive select -> act -> verify until the model stops or a bound trips.

    Every stop reason is code-owned; no step is taken without a verification that
    policy accepts, and no `done` is reported as success without a passed check.

    Kate document actions take their text from `text_plan`, which the caller owns:
    each successful `replace_document` consumes the next entry. The model never
    produces text, and no text reaches the selector state, history or report.
    A document task is only `done_verified` when the most recent verified action
    is a save whose content digest equals `final_expected`; any other action
    between the replacement and the save invalidates that remembered digest.

    `provider_calls` counts attempted selector requests (so a failed paid attempt
    is never reported as zero); `provider_answers` counts the ones that produced a
    valid decision, and `provider_attempts` mirrors a transport counter when the
    selector exposes one.
    """
    expectations = {label.casefold(): value for label, value in (expectations or {}).items()}
    final_digest = _text_digest(final_expected)
    report: dict[str, Any] = {
        "app": app, "goal": goal, "model": model, "steps": [], "provider_calls": 0,
        "provider_answers": 0, "provider_attempts": None, "actions": 0,
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "verified_postcondition": None,
        "final_expected_sha256": final_digest["sha256"], "final_expected_chars": final_digest["chars"],
        "owner_ms": 0.0, "selector_ms": 0.0,
        "stop_reason": None, "outcome": None, "started": clock(),
    }
    pending_text = list(text_plan) if text_plan else []
    last_replace_digest: dict[str, Any] | None = None
    history: list[str] = []
    for number in range(1, max_steps + 1):
        step_goal = goals[number - 1] if goals and number - 1 < len(goals) else goal
        if goals and number - 1 >= len(goals):
            return _finish(report, "goal_sequence_exhausted", "budget_exhausted")
        owner_started = clock()
        snapshot = client.candidates(app)
        report["owner_ms"] += (clock() - owner_started) * 1000.0
        if snapshot.get("ok") is not True:
            return _finish(report, "owner_error", "owner_error",
                           error=(snapshot.get("error") or {}).get("code", "owner_error"))
        try:
            state = state_from_probe_candidates(app, snapshot.get("window_title") or "", step_goal,
                                                snapshot.get("candidates") or [], limit=limit, history=history)
        except (SelectorError, ValueError) as exc:
            return _finish(report, "invalid_state", "invalid_state", error=str(exc)[:80])
        if not state.options:
            return _finish(report, "no_options", "no_options",
                           note="the state has no usable option to choose from")
        request = build_probe_request(state, model=model)
        if report["provider_calls"] >= max_calls:
            return _finish(report, "call_budget_exhausted", "budget_exhausted")
        if counts_provider_calls:
            report["provider_calls"] += 1
        selector_started = clock()
        try:
            decision = selector.select(request, options=state.options)
        except SelectorError as exc:
            return _finish(report, "selector_error", "invalid_answer", error=str(exc)[:80])
        finally:
            report["selector_ms"] += (clock() - selector_started) * 1000.0
            if counts_provider_calls:
                transport = _transport_calls(selector)
                if transport is not None:
                    report["provider_attempts"] = transport
                    report["provider_calls"] = max(report["provider_calls"], transport)
        if counts_provider_calls:
            report["provider_answers"] += 1
        usage = decision.usage if isinstance(decision.usage, dict) else {}
        for key in ("input_tokens", "output_tokens"):
            if isinstance(usage.get(key), int):
                report["usage"][key] += usage[key]
        step: dict[str, Any] = {"n": number, "goal": step_goal, "action": decision.action,
                                "confidence": decision.confidence, "probabilities": decision.probabilities,
                                "target_ref": decision.target_ref, "target_label": None,
                                "verification": None, "verification_kind": None, "expected": None,
                                "expected_sha256": None, "expected_chars": None,
                                "ok": False, "usage": usage,
                                "state_tokens_estimate": estimate_tokens(request["state"])}
        if decision.action in TARGETLESS_ACTIONS:
            step["ok"] = True
            report["steps"].append(step)
            if decision.action == "done":
                complete = _completion_matches(report, final_expected)
                return _finish(report, "model_done",
                               "done_verified" if complete else "done_unverified",
                               note=None if complete else
                               "the model proposed done but no strong check matched an explicit final expectation")
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
        if max_actions is not None and report["actions"] >= max_actions:
            report["steps"].append(step)
            return _finish(report, "action_budget_exhausted", "budget_exhausted",
                           note=f"the {max_actions}-action limit was reached before another action")
        report["actions"] += 1
        target = next((option for option in state.options if option.ref == decision.target_ref), None)
        if target is None:
            report["steps"].append(step)
            return _finish(report, "unenumerated_target", "invalid_answer")
        step["target_label"] = target.label
        text: str | None = None
        act_expected: str | None = None
        if decision.action == "scroll":
            step["verification_kind"] = "scroll_changed"
        elif decision.action == "replace_document":
            if "replace_document" not in target.actions:
                report["steps"].append(step)
                return _finish(report, "unsupported_target", "unsupported_action", error="replace_document")
            if not pending_text:
                report["steps"].append(step)
                return _finish(report, "text_plan_exhausted", "unsupported_action", error="text_plan_exhausted",
                               note="the model asked for another replacement but the caller supplied no more text")
            text = pending_text.pop(0)
            if len(text) > MAX_REPLACE_TEXT_CHARS:
                report["steps"].append(step)
                return _finish(report, "replace_text_too_long", "unsupported_action", error="replace_text_too_long")
            digest = _text_digest(text)
            step["verification_kind"] = "target_text"
            step["expected_sha256"] = digest["sha256"]
            step["expected_chars"] = digest["chars"]
            act_expected = text
        elif decision.action == "save_document":
            if "save_document" not in target.actions:
                report["steps"].append(step)
                return _finish(report, "unsupported_target", "unsupported_action", error="save_document")
            step["verification_kind"] = "document_saved"
        else:
            expected = expectations.get(target.label.casefold())
            step["expected"] = expected
            step["verification_kind"] = "display_text" if expected is not None else "target_focused"
            act_expected = expected
        owner_started = clock()
        acted = client.act(app, decision.action, decision.target_ref, step["verification_kind"],
                           expected=act_expected, text=text,
                           direction=scroll_direction if decision.action == "scroll" else None)
        report["owner_ms"] += (clock() - owner_started) * 1000.0
        step["verification"] = acted.get("verification")
        step["evidence"] = acted.get("evidence")
        if acted.get("ok") is not True:
            code = (acted.get("error") or {}).get("code", "act_failed")
            report["steps"].append(step)
            if code == "verification_failed":
                return _finish(report, "verification_failed", "wrong_choice", error=code,
                               note="the action ran but the code-owned check did not pass")
            if code in {"action_precondition_failed", "unsupported_verification", "unchanged_document_text"}:
                return _finish(report, "verification_unavailable", "verification_unavailable", error=code,
                               note=f"no acceptable check for {decision.action} on {target.label!r}; "
                                    "the loop refuses to act unverified")
            return _finish(report, "owner_error", "owner_error", error=code)
        step["ok"] = acted.get("status") == "ok" and step["verification"] == "passed"
        report["steps"].append(step)
        if not step["ok"]:
            return _finish(report, "verification_failed", "unverified_action")
        # A later action invalidates earlier completion evidence.
        report["verified_postcondition"] = None
        if step["verification_kind"] not in {"target_text", "document_saved"}:
            # Any other action could have mutated the document, so the remembered
            # replacement digest must not be reused as save evidence later.
            last_replace_digest = None
        history.append(f"{decision.action} {target.label!r}")
        if step["verification_kind"] == "display_text":
            report["verified_postcondition"] = {"kind": "display_text", "step": number,
                                                "target_label": target.label,
                                                "expected": step["expected"], "evidence": step["evidence"]}
        elif step["verification_kind"] == "target_text":
            last_replace_digest = _text_digest(text)
            report["verified_postcondition"] = {"kind": "document_replaced", "step": number,
                                                "target_label": target.label,
                                                "expected_sha256": last_replace_digest["sha256"],
                                                "expected_chars": last_replace_digest["chars"],
                                                "evidence": step["evidence"]}
        elif step["verification_kind"] == "document_saved":
            digest = last_replace_digest or {"sha256": None, "chars": None}
            report["verified_postcondition"] = {"kind": "document_saved", "step": number,
                                                "target_label": target.label,
                                                "sha256": digest["sha256"], "chars": digest["chars"],
                                                "evidence": step["evidence"]}
    return _finish(report, "step_budget_exhausted", "budget_exhausted")


def _parse_expectations(items: list[str]) -> dict[str, str]:
    expectations: dict[str, str] = {}
    for item in items:
        label, separator, value = item.partition("=")
        if not separator or not label.strip():
            raise SystemExit(f"--expect wants LABEL=VALUE, got {item!r}")
        expectations[label.strip()] = value
    return expectations


def _read_text_source(source: str) -> str:
    """Read caller-owned document text from a file or stdin ('-'). Never the model."""
    if source == "-":
        return sys.stdin.read()
    return Path(source).read_text(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jev-loop-probe", description=__doc__.splitlines()[0])
    parser.add_argument("--app", required=True, help="allowlisted app to drive, for example kcalc")
    parser.add_argument("--goal", required=True, help="the bounded task for this session")
    parser.add_argument("--goal-sequence", default=None,
                        help="optional '|'-separated per-step goals, when the plan is scripted")
    parser.add_argument("--expect", action="append", default=[], metavar="LABEL=VALUE",
                        help="expected display reading after clicking that label; repeatable")
    parser.add_argument("--final-expected", default=None,
                        help="caller-owned final display value required for done_verified")
    parser.add_argument("--replace-text-file", action="append", default=[], metavar="PATH",
                        help="successive caller-owned texts for replace_document; repeatable, '-' reads stdin once")
    parser.add_argument("--final-expected-file", type=Path, default=None, metavar="PATH",
                        help="caller-owned final document text required for a Kate done_verified")
    parser.add_argument("--max-steps", type=int, default=8, help=f"step cap (maximum {MAX_STEPS_HARD_CAP})")
    parser.add_argument("--max-calls", type=int, default=8, help=f"provider call cap (maximum {MAX_CALLS_HARD_CAP})")
    parser.add_argument("--max-actions", type=int, default=None,
                        help="optional cap on executed actions, separate from steps and calls")
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
    if args.max_actions is not None and args.max_actions < 1:
        print(json.dumps({"ok": False, "error": "max_actions_out_of_range"}))
        return 2
    expectations = _parse_expectations(args.expect)
    text_plan = [_read_text_source(item) for item in args.replace_text_file]
    if any(len(text) > MAX_REPLACE_TEXT_CHARS for text in text_plan):
        print(json.dumps({"ok": False, "error": "replace_text_too_long",
                          "max_chars": MAX_REPLACE_TEXT_CHARS}))
        return 2
    final_expected = args.final_expected
    if final_expected is None and args.final_expected_file is not None:
        final_expected = _read_text_source(str(args.final_expected_file))
    goals = [part.strip() for part in args.goal_sequence.split("|")] if args.goal_sequence else None
    socket_path = args.socket or default_socket_path()
    client = OwnerClient(socket_path)

    if args.dry_run:
        snapshot = client.candidates(args.app)
        if snapshot.get("ok") is not True:
            print(json.dumps({"ok": False, "mode": "dry_run",
                              "error": (snapshot.get("error") or {}).get("code", "owner_error")}))
            return 2
        state = state_from_probe_candidates(args.app, snapshot.get("window_title") or "", args.goal,
                                            snapshot.get("candidates") or [], limit=args.limit)
        request = build_probe_request(state, model=args.model)
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
        selector: Any = ProbeRecordedSelector(answers, model="recorded")
        counts_provider_calls = False
    else:
        if not jev_enabled():
            print(json.dumps({"ok": False, "error": "jev_disabled",
                              "hint": "set JEV_ENABLED=true to permit Jev API calls; nothing was sent"}))
            return 2
        key_name = JevConfig().key_env
        dotenv = args.dotenv or (default_config_dir() / "jev.env")
        if not os.environ.get(key_name):
            _load_dotenv_key(dotenv, key_name)
        if not os.environ.get(key_name):
            print(json.dumps({"ok": False, "error": "jev_api_key_missing", "dotenv": str(dotenv),
                              "hint": f"export {key_name} or put it in that private file (0600); "
                                      "nothing was sent"}))
            return 2
        selector = ProbeHttpSelector(JevConfig(model=args.model, max_calls=args.max_calls))
        counts_provider_calls = True

    report = run_loop(client=client, selector=selector, app=args.app, goal=args.goal, model=args.model,
                      expectations=expectations, final_expected=final_expected, text_plan=text_plan,
                      goals=goals, max_steps=args.max_steps, max_calls=args.max_calls,
                      max_actions=args.max_actions, counts_provider_calls=counts_provider_calls,
                      limit=args.limit)
    report["ok"] = report["outcome"] in {"done_verified"}
    report["elapsed_ms"] = round((time.monotonic() - report["started"]) * 1000, 1)
    report.pop("started", None)
    report["owner_ms"] = round(report["owner_ms"], 1)
    report["selector_ms"] = round(report["selector_ms"], 1)
    report["replace_text_chars"] = [len(text) for text in text_plan]
    report["expectations"] = expectations
    if args.report_dir:
        args.report_dir.mkdir(parents=True, exist_ok=True)
        (args.report_dir / "report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        report["report_path"] = str(args.report_dir / "report.json")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
