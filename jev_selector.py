"""Bounded decision-model selector: enumerated options in, one typed choice out.

Jev (TypeSafe "System One") is a decision model, not an LLM: it returns typed
answers over options we enumerate. This module is the *selector seam* only.

Invariants (see AGENTS.md and ADR-011/012):

- The state contains labels, roles, states and action names. It never contains
  coordinates, CSS selectors, shell commands, file paths, typed text or any
  other free-form payload.
- The answer can only name an option we enumerated. Anything else is rejected
  here, in code, before it reaches a caller.
- Selection is advice. Execution, verification and policy stay in
  `transactions.py`; `DONE` from a model is a proposal, not proof.
- One request, no retries, explicit call cap. The caller owns the cap.

Nothing in this module is wired into the running owner yet: ADR-011 is still
proposed. See docs/30-jev-selector-experiment.md.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol

# Actions the selector may choose between. These map to the owner's own action
# vocabulary; the selector never invents a new one.
ALLOWED_ACTIONS = ("click", "type_text", "scroll", "navigate_url", "wait", "done", "blocked")
TARGETLESS_ACTIONS = frozenset({"wait", "done", "blocked"})
MAX_OPTIONS = 120
# Bounds for the driver loop's own history of actions already taken.
HISTORY_ENTRIES = 20
HISTORY_ENTRY_CHARS = 80
DEFAULT_STATE_LIMIT = 32_000
DEFAULT_TOTAL_LIMIT = 64_000


class SelectorError(RuntimeError):
    """Safe selector failure; no provider detail is copied into the message."""


@dataclass(frozen=True)
class DecisionOption:
    """One enumerated choice. `ref` is our own opaque candidate reference."""

    ref: str
    role: str
    label: str
    states: tuple[str, ...]
    actions: tuple[str, ...]


@dataclass(frozen=True)
class DecisionState:
    app: str
    window_title: str
    goal: str
    options: tuple[DecisionOption, ...]
    # Action labels already carried out by the driver loop, oldest first. Labels
    # only: no values, no typed text, no results. A stateless request needs this
    # to know what has already happened.
    history: tuple[str, ...] = ()


@dataclass(frozen=True)
class SelectedDecision:
    action: str
    target_ref: str | None
    confidence: float | None
    probabilities: dict[str, float] = field(default_factory=dict)
    model: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


def _clean_history(history: Iterable[str]) -> tuple[str, ...]:
    """Action labels only, whitespace-collapsed and bounded, newest kept."""
    cleaned: list[str] = []
    for entry in history:
        if not isinstance(entry, str):
            continue
        text = " ".join(entry.split())
        if text:
            cleaned.append(text[:HISTORY_ENTRY_CHARS])
    return tuple(cleaned[-HISTORY_ENTRIES:])


def state_from_candidates(app: str, window_title: str, goal: str,
                          candidates: Iterable[dict[str, Any]], *, limit: int = MAX_OPTIONS,
                          history: Iterable[str] = ()) -> DecisionState:
    """Build a selector state from `candidates` CLI/MCP output.

    Usable candidates only (enabled/sensitive/showing/visible), sorted by
    role+label so the same desktop state renders the same way. Candidate
    `value`, bounds and refs-with-coordinates are never included. `history` is
    the driver loop's own record of actions already taken, as labels.
    """
    if not isinstance(goal, str) or not goal.strip():
        raise SelectorError("goal_required")
    if not 1 <= limit <= MAX_OPTIONS:
        raise SelectorError("invalid_option_limit")
    usable = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        if item.get("unavailable_reason"):
            continue
        states = {str(state).casefold() for state in item.get("states", [])}
        if not {"enabled", "sensitive", "showing", "visible"}.issubset(states):
            continue
        ref, role, label = item.get("ref"), item.get("role"), item.get("label")
        if not all(isinstance(value, str) and value for value in (ref, role, label)):
            continue
        actions = tuple(sorted(str(action) for action in item.get("actions", []) if action in ALLOWED_ACTIONS))
        if not actions:
            continue
        usable.append(DecisionOption(ref=ref, role=role, label=label,
                                     states=tuple(sorted(states)), actions=actions))
    usable.sort(key=lambda option: (option.role, option.label, option.ref))
    return DecisionState(app=app, window_title=window_title, goal=goal.strip(),
                         options=tuple(usable[:limit]), history=_clean_history(history))


def render_state_text(state: DecisionState) -> str:
    """Compact indexed table. No coordinates, no values, no file paths."""
    lines = [f"app: {state.app}", f"window: {state.window_title}", f"goal: {state.goal}"]
    if state.history:
        lines.append("already done (oldest first):")
        lines.extend(f"- {entry}" for entry in state.history)
    lines.append("options:")
    for index, option in enumerate(state.options, start=1):
        lines.append(f"{index}. role={option.role} label={option.label!r} "
                     f"actions={','.join(option.actions)} states={','.join(option.states)}")
    return "\n".join(lines)


def build_questions(state: DecisionState, *, include_target: bool = True) -> dict[str, Any]:
    """Action head (always) and target head (only when a target can be chosen)."""
    criteria = {action: _ACTION_CRITERIA[action] for action in ALLOWED_ACTIONS}
    questions: dict[str, Any] = {
        "action": {"type": "choice", "instructions": "Choose the single next action toward the goal.",
                   "criteria": criteria},
    }
    if include_target and state.options:
        questions["target"] = {
            "type": "choice",
            "instructions": ("Choose the option the action applies to. Ignore this head for "
                             "wait, done and blocked."),
            "criteria": {str(index): f"{option.role}: {option.label}"
                         for index, option in enumerate(state.options, start=1)},
        }
    return questions


_ACTION_CRITERIA = {
    "click": "press the chosen option",
    "type_text": "type into the chosen editable option",
    "scroll": "scroll the chosen scrollable region",
    "navigate_url": "enter a URL in the chosen address field",
    "wait": "the visible state is still changing; observe again",
    "done": "the goal is visibly complete",
    "blocked": "the goal cannot proceed without a human decision",
}


def build_request(state: DecisionState, *, model: str = "jev-latest",
                  include_target: bool = True) -> dict[str, Any]:
    """The exact request body sent to the decision model."""
    questions = build_questions(state, include_target=include_target)
    check_budget(render_state_text(state), questions)
    return {"model": model, "state": render_state_text(state), "questions": questions}


def estimate_tokens(value: Any) -> int:
    """Rough ASCII-token estimate; used only as a fail-closed pre-flight guard."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=True, sort_keys=True)
    return max(1, len(text) // 4)


def check_budget(state_text: str, questions: dict[str, Any], *,
                 state_limit: int = DEFAULT_STATE_LIMIT, total_limit: int = DEFAULT_TOTAL_LIMIT) -> None:
    state_tokens = estimate_tokens(state_text)
    longest = max((estimate_tokens(question) for question in questions.values()), default=0)
    total = estimate_tokens({"state": state_text, "questions": questions})
    if state_tokens + longest > state_limit:
        raise SelectorError("state_plus_longest_question_over_budget")
    if total > total_limit:
        raise SelectorError("request_over_budget")


def validate_answer(answers: Any, *, options: tuple[DecisionOption, ...],
                    allow_targetless: bool = True) -> SelectedDecision:
    """Accept only an enumerated action and an enumerated target index."""
    if not isinstance(answers, dict):
        raise SelectorError("answers_not_an_object")
    action_answer = answers.get("action")
    if not isinstance(action_answer, dict) or action_answer.get("type") != "choice":
        raise SelectorError("action_answer_invalid")
    action = action_answer.get("choice")
    if action not in ALLOWED_ACTIONS:
        raise SelectorError("unenumerated_action")
    confidence = _confidence(action_answer.get("confidence"))
    probabilities = _probabilities(action_answer.get("probabilities"))

    if action in TARGETLESS_ACTIONS:
        if not allow_targetless:
            raise SelectorError("targetless_action_not_allowed")
        return SelectedDecision(action=action, target_ref=None, confidence=confidence,
                                probabilities=probabilities)

    target_answer = answers.get("target")
    if not isinstance(target_answer, dict):
        raise SelectorError("target_answer_missing")
    target = target_answer.get("choice")
    index = _option_index(target)
    if index is None or not 1 <= index <= len(options):
        raise SelectorError("unenumerated_target")
    return SelectedDecision(action=action, target_ref=options[index - 1].ref, confidence=confidence,
                            probabilities=probabilities)


def _option_index(target: Any) -> int | None:
    if isinstance(target, bool):
        return None
    if isinstance(target, int):
        return target
    if isinstance(target, str) and target.isdigit():
        return int(target)
    return None


def _confidence(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if 0.0 <= value <= 1.0 else None


def _probabilities(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    return {str(key): float(item) for key, item in value.items()
            if isinstance(item, (int, float)) and not isinstance(item, bool)}


class Selector(Protocol):
    """A selector answers one bounded question set. It performs no action."""

    def select(self, request: dict[str, Any], *, options: tuple[DecisionOption, ...]) -> SelectedDecision: ...


class RecordedSelector:
    """Replay recorded answers. Used for offline tests and `--dry-run` demos.

    A dict replays the same answer for every request; a list replays one answer
    per request, which is what an offline multi-step loop needs.
    """

    def __init__(self, answers: dict[str, Any] | list[dict[str, Any]], *, model: str | None = None):
        self.answers = answers
        self.model = model
        self.calls = 0

    def _next_answer(self) -> dict[str, Any]:
        if isinstance(self.answers, list):
            if self.calls >= len(self.answers):
                raise SelectorError("recorded_answers_exhausted")
            answer = self.answers[self.calls]
        else:
            answer = self.answers
        self.calls += 1
        return answer

    def select(self, request: dict[str, Any], *, options: tuple[DecisionOption, ...]) -> SelectedDecision:
        decision = validate_answer(self._next_answer(), options=options)
        return SelectedDecision(action=decision.action, target_ref=decision.target_ref,
                                confidence=decision.confidence, probabilities=decision.probabilities,
                                model=self.model or str(request.get("model") or ""), usage={})


@dataclass(frozen=True)
class JevConfig:
    base_url: str = "https://api.typesafe.ai/v1/systemone"
    model: str = "jev-latest"
    key_env: str = "JEV_API_KEY"
    timeout_seconds: float = 10.0
    max_calls: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.key_env, str) or not self.key_env:
            raise ValueError("key_env is required")
        if self.max_calls < 1:
            raise ValueError("max_calls must be positive")


class HttpSelector:
    """One request, no retries, hard call cap. The key is read from the environment."""

    def __init__(self, config: JevConfig, *, client: Any = None, environ: dict[str, str] | None = None):
        self.config = config
        self._client = client
        self._environ = environ if environ is not None else os.environ
        self.calls = 0

    def _post(self, request: dict[str, Any]) -> Any:
        import httpx

        key = self._environ.get(self.config.key_env)
        if not key:
            raise SelectorError("selector_key_not_configured")
        if self.calls >= self.config.max_calls:
            raise SelectorError("selector_call_cap_reached")
        self.calls += 1
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        if self._client is not None:
            return self._client.post(self.config.base_url, json=request, headers=headers)
        timeout = httpx.Timeout(self.config.timeout_seconds)
        return httpx.post(self.config.base_url, json=request, headers=headers, timeout=timeout)

    def select(self, request: dict[str, Any], *, options: tuple[DecisionOption, ...]) -> SelectedDecision:
        try:
            response = self._post(request)
        except SelectorError:
            raise
        except Exception as exc:  # transport failures stay safe and typed
            raise SelectorError("selector_transport_failed") from exc
        status = getattr(response, "status_code", None)
        if status != 200:
            raise SelectorError(f"selector_http_{status}" if isinstance(status, int) else "selector_http_error")
        try:
            payload = response.json()
        except Exception as exc:
            raise SelectorError("selector_response_not_json") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("answers"), dict):
            raise SelectorError("selector_response_invalid")
        decision = validate_answer(payload["answers"], options=options)
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        model = payload.get("model") if isinstance(payload.get("model"), str) else None
        return SelectedDecision(action=decision.action, target_ref=decision.target_ref,
                                confidence=decision.confidence, probabilities=decision.probabilities,
                                model=model, usage=usage, raw=payload)
