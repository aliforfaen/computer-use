"""Small, fail-closed AT-SPI action transactions over kwin-mcp.

This module deliberately supports only semantic buttons and editable text
fields. kwin-mcp 0.10.0 exposes AT-SPI as formatted text and coordinate input;
it does not expose an AT-SPI Action invocation method. Bounds are therefore
used only after a fresh identity/state check against one active KWin window.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Protocol


_TREE_LINE = re.compile(
    r'^\s*- \[(?P<role>[^]]+)\] "(?P<name>(?:[^"\\]|\\.)*)"'
    r' \((?P<states>[^)]*)\)(?P<tail>.*)$'
)
_BOUNDS = re.compile(r'@ screen \((?P<x>\d+), (?P<y>\d+), (?P<w>\d+)x(?P<h>\d+)\)')
_ACTIONS = re.compile(r'\[actions: (?P<actions>[^]]*)\]')
_TEXT = re.compile(r"\btext='((?:[^'\\]|\\.)*)'")
_ID = re.compile(r'^\s*id:\s*(?P<id>\S+)\s*$', re.MULTILINE)
_APP = re.compile(r'^- (?P<app>\S+) "(?P<title>(?:[^"\\]|\\.)*)"(?P<active> \[active\])?$', re.MULTILINE)
_CLIENT_RECT = re.compile(r'^\s*client:\s*\((?P<x>-?\d+), (?P<y>-?\d+), (?P<w>\d+)x(?P<h>\d+)\)\s*$', re.MULTILINE)


class TransactionError(RuntimeError):
    """Safe public error; details are intentionally not copied from the driver."""


class AutonomyMode(str, Enum):
    SUPERVISED = "supervised"
    GUARDED = "guarded"
    YOLO = "yolo"


@dataclass(frozen=True)
class Bounds:
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class Candidate:
    ref: str
    role: str
    label: str = field(repr=False)
    states: frozenset[str]
    actions: tuple[str, ...]
    bounds: Bounds
    value: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Snapshot:
    session_id: str
    app: str
    generation: str
    window_id: str
    window_app: str
    window_bounds: Bounds
    active: bool
    candidates: tuple[Candidate, ...]
    observed_at: float

    def candidate(self, ref: str) -> Candidate | None:
        return next((item for item in self.candidates if item.ref == ref), None)


@dataclass(frozen=True)
class Action:
    kind: str
    target_ref: str
    text: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Verification:
    passed: bool
    evidence: Any = field(default=None, repr=False)
    reason: str = "verified"


@dataclass(frozen=True)
class ActionResult:
    status: str
    verification: str
    reason: str
    before: Snapshot | None = None
    after: Snapshot | None = None
    evidence: Any = field(default=None, repr=False)


@dataclass(frozen=True)
class Policy:
    allowed_apps: frozenset[str] = frozenset()
    max_actions: int = 24
    max_observations: int = 64
    max_duration_seconds: float = 120.0
    max_text_chars: int = 20_000
    confirmation_words: tuple[str, ...] = ("delete", "remove", "submit", "send", "purchase", "format")

    def __post_init__(self) -> None:
        if not math.isfinite(self.max_duration_seconds) or self.max_duration_seconds <= 0:
            raise ValueError("max_duration_seconds must be finite and positive")
        if self.max_actions <= 0 or self.max_observations < 2 or self.max_text_chars <= 0:
            raise ValueError("policy budgets must be positive and reserve verification")


class Backend(Protocol):
    def accessibility_tree(self, app: str) -> str: ...
    def window_geometry(self, app: str) -> str: ...
    def active_window(self) -> str: ...
    def mouse_click(self, x: int, y: int, button: str = "left") -> Any: ...
    def keyboard_type(self, text: str) -> Any: ...


class KwinMcpBackend:
    """Adapter for the installed kwin-mcp AutomationEngine public methods."""

    def __init__(self, engine: Any):
        self.engine = engine

    def _require_virtual(self) -> None:
        session = self.engine._get_session()
        info = getattr(session, "info", None)
        session_type = getattr(info, "session_type", None)
        if getattr(session_type, "value", None) != "virtual":
            raise TransactionError("virtual_session_required")

    def accessibility_tree(self, app: str) -> str:
        self._require_virtual()
        return self.engine.accessibility_tree(app_name=app, max_depth=20)

    def accessibility_elements(self, app: str) -> list[dict[str, Any]]:
        # kwin-mcp 0.10.0 has no public typed tree API. This private seam returns
        # the driver's own ElementInfo dataclass as dictionaries, including
        # active states, mapped bounds, and the capped Text-interface value.
        self._require_virtual()
        response = self.engine._run_atspi("find", query="", app_name=app)
        if not isinstance(response, dict) or response.get("ok") is not True:
            raise TransactionError("atspi_query_failed")
        elements = response.get("result")
        if not isinstance(elements, list) or not all(isinstance(row, dict) for row in elements):
            raise TransactionError("atspi_response_invalid")
        return elements

    def window_geometry(self, app: str) -> str:
        self._require_virtual()
        return self.engine.window_geometry(app_name=app)

    def active_window(self) -> str:
        self._require_virtual()
        return self.engine.active_window()

    def mouse_click(self, x: int, y: int, button: str = "left") -> Any:
        self._require_virtual()
        return self.engine.mouse_click(x, y, button=button)

    def keyboard_type(self, text: str) -> Any:
        self._require_virtual()
        return self.engine.keyboard_type_unicode(text) if not text.isascii() else self.engine.keyboard_type(text)


class AuditLog:
    """Append-only JSONL audit writer with a deliberately closed field schema."""

    def __init__(self, path: str | os.PathLike[str], *, caller_node: str = "local", transport: str = "local"):
        self.path = Path(path)
        self.caller_node = _safe_context(caller_node)
        self.transport = _safe_context(transport)
        self._lock = threading.Lock()

    def write(self, *, tool: str, app: str, mode: AutonomyMode, event: str, status: str, reason: str) -> None:
        event = event if event in {"observe", "click", "type_text", "action"} else "action"
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "caller_node": self.caller_node,
            "transport": self.transport,
            "tool": tool,
            "app": _safe_context(app),
            "autonomy_mode": mode.value,
            "event": event,
            "status": status,
            "jev_answers": None,
            "action": event if event in {"click", "type_text"} else None,
            "verification": status if event in {"click", "type_text"} else None,
            "reason": reason,
        }
        encoded = (json.dumps(record, separators=(",", ":"), ensure_ascii=True) + "\n").encode("utf-8")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, encoded)
            finally:
                os.close(fd)


def _safe_context(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9_.:@/-]{1,128}", value) else "unknown"


def _decode(value: str) -> str:
    try:
        return json.loads('"' + value + '"')
    except (json.JSONDecodeError, TypeError):
        return value


def _parse_window_identity(geometry: str, active: str) -> tuple[str, str, bool]:
    ids = _ID.findall(geometry)
    app_rows = list(_APP.finditer(geometry))
    active_ids = _ID.findall(active)
    active_rows = list(_APP.finditer(active))
    if len(ids) != 1 or len(app_rows) != 1 or len(active_ids) != 1 or len(active_rows) != 1:
        raise TransactionError("window_identity_ambiguous")
    app_name = app_rows[0].group("app")
    # AT-SPI app names commonly differ from the KWin desktop-file app id. Require
    # the accessibility tree's app text and KWin identity to share a basename.
    window_app = app_name.lower()
    active_ok = ids[0] == active_ids[0] and active_rows[0].group("active") is not None
    client = _CLIENT_RECT.search(geometry)
    if client is None:
        raise TransactionError("window_identity_ambiguous")
    rect = Bounds(*(int(client.group(key)) for key in ("x", "y", "w", "h")))
    return ids[0], window_app, active_ok, rect


def _parse_candidates(tree: str, generation: str) -> tuple[Candidate, ...]:
    parsed: list[tuple[str, str, frozenset[str], tuple[str, ...], Bounds, str | None]] = []
    for line in tree.splitlines():
        match = _TREE_LINE.match(line)
        if not match:
            continue
        role, label = match.group("role"), _decode(match.group("name"))
        states = frozenset(part.strip().lower() for part in match.group("states").split(",") if part.strip())
        bounds_match, actions_match = _BOUNDS.search(match.group("tail")), _ACTIONS.search(match.group("tail"))
        if not bounds_match:
            continue
        x, y, width, height = (int(bounds_match.group(key)) for key in ("x", "y", "w", "h"))
        if width <= 0 or height <= 0:
            continue
        actions = tuple(
            part.strip() for part in actions_match.group("actions").split(",") if part.strip()
        ) if actions_match else ()
        role_lower = role.lower()
        if role_lower == "button":
            supported = ("click",)
        elif role_lower in {"text", "text entry", "entry"} and "editable" in states:
            supported = ("click", "type_text")
        else:
            continue
        value_match = _TEXT.search(match.group("tail"))
        value = _decode(value_match.group(1)) if value_match else None
        parsed.append((role_lower, label, states, supported, Bounds(x, y, width, height), value))

    # Duplicate accessible identities do not get addressable references.
    identities: dict[tuple[str, str], int] = {}
    for role, label, *_ in parsed:
        key = (role, label)
        identities[key] = identities.get(key, 0) + 1
    result = []
    for role, label, states, supported, bounds, value in parsed:
        if identities[(role, label)] != 1:
            continue
        result.append(Candidate(
            ref=f"cand_{generation}_{uuid.uuid4().hex}", role=role, label=label,
            states=states, actions=supported, bounds=bounds, value=value,
        ))
    return tuple(result)


def _typed_candidates(elements: list[dict[str, Any]], generation: str) -> tuple[Candidate, ...]:
    parsed: list[tuple[str, str, frozenset[str], tuple[str, ...], Bounds, str | None]] = []
    for element in elements:
        role = str(element.get("role", "")).lower()
        label = str(element.get("name", ""))
        states = frozenset(str(item).lower() for item in element.get("states", []) if isinstance(item, str))
        actions = tuple(str(item) for item in element.get("actions", []) if isinstance(item, str))
        if not element.get("mapped"):
            continue
        try:
            bounds = Bounds(*(int(element[key]) for key in ("x", "y", "width", "height")))
        except (KeyError, TypeError, ValueError):
            continue
        if bounds.width <= 0 or bounds.height <= 0:
            continue
        if role == "button":
            supported = ("click",)
        elif role in {"text", "text entry", "entry"} and "editable" in states:
            supported = ("click", "type_text")
        else:
            continue
        value = element.get("text")
        if value is not None and not isinstance(value, str):
            value = None
        parsed.append((role, label, states, supported, bounds, value))

    identities: dict[tuple[str, str], int] = {}
    for role, label, *_ in parsed:
        key = (role, label)
        identities[key] = identities.get(key, 0) + 1
    return tuple(
        Candidate(
            ref=f"cand_{generation}_{uuid.uuid4().hex}", role=role, label=label,
            states=states, actions=supported, bounds=bounds, value=value,
        )
        for role, label, states, supported, bounds, value in parsed
        if identities[(role, label)] == 1
    )


def _fingerprint(candidate: Candidate) -> tuple[Any, ...]:
    return (candidate.role, candidate.label, candidate.states, candidate.actions, candidate.bounds, candidate.value)


_SESSION_LOCKS: dict[str, threading.Lock] = {}
_SESSION_LOCKS_GUARD = threading.Lock()


def _session_lock(session_id: str) -> threading.Lock:
    with _SESSION_LOCKS_GUARD:
        return _SESSION_LOCKS.setdefault(session_id, threading.Lock())


class TransactionEngine:
    def __init__(
        self,
        backend: Backend,
        *,
        audit: AuditLog,
        policy: Policy | None = None,
        authorizer: Callable[[str, str, Action], bool] | None = None,
    ):
        self.backend = backend
        self.audit = audit
        self.policy = policy or Policy()
        self.authorizer = authorizer or (lambda _session, _app, _action: True)
        self._last: dict[tuple[str, str], Snapshot] = {}
        self._counts: dict[str, tuple[float, int, int]] = {}
        self._blocked: dict[str, str] = {}
        self._session_id: str | None = None
        self._binding_lock = threading.Lock()

    def _bind_session(self, session_id: str) -> None:
        if not isinstance(session_id, str) or not session_id:
            raise TransactionError("invalid_session_id")
        with self._binding_lock:
            if self._session_id is None:
                self._session_id = session_id
            elif self._session_id != session_id:
                raise TransactionError("session_identity_mismatch")

    def reset_session(self, session_id: str) -> None:
        """Reset a stopped transaction's latch; refuses a concurrent action."""
        self._bind_session(session_id)
        lock = _session_lock(session_id)
        if not lock.acquire(blocking=False):
            raise TransactionError("session_busy")
        try:
            self._audit("", AutonomyMode.GUARDED, "reset", "ok", "session_reset")
            self._counts.pop(session_id, None)
            self._blocked.pop(session_id, None)
            self._last.clear()
        finally:
            lock.release()

    def _audit(self, app: str, mode: AutonomyMode, event: str, status: str, reason: str) -> None:
        self.audit.write(
            tool="transaction.observe_or_act", app=app, mode=mode, event=event,
            status=status, reason=_safe_reason(reason),
        )

    def _check_budget(self, session_id: str, *, action: bool = False) -> None:
        now = time.monotonic()
        start, actions, observations = self._counts.get(session_id, (now, 0, 0))
        if now - start > self.policy.max_duration_seconds:
            raise TransactionError("task_time_budget_exceeded")
        if action and actions >= self.policy.max_actions:
            raise TransactionError("task_action_budget_exceeded")
        if action and observations + 2 > self.policy.max_observations:
            raise TransactionError("task_observation_budget_exceeded")
        if not action and observations >= self.policy.max_observations:
            raise TransactionError("task_observation_budget_exceeded")

    def _bump(self, session_id: str, *, action: bool = False) -> None:
        now = time.monotonic()
        start, actions, observations = self._counts.get(session_id, (now, 0, 0))
        self._counts[session_id] = (start, actions + int(action), observations + int(not action))

    def _snapshot(self, session_id: str, app: str) -> Snapshot:
        self._check_budget(session_id)
        geometry = self.backend.window_geometry(app)
        active = self.backend.active_window()
        window_id, window_app, is_active, window_bounds = _parse_window_identity(geometry, active)
        expected_apps = {
            "kate": {"kate", "org.kde.kate"},
            "firefox": {"firefox", "org.mozilla.firefox"},
            "kcalc": {"kcalc", "org.kde.kcalc"},
        }
        if window_app not in expected_apps.get(app, set()):
            raise TransactionError("window_app_mismatch")
        typed = getattr(self.backend, "accessibility_elements", None)
        elements = typed(app) if callable(typed) else None
        tree = None if elements is not None else self.backend.accessibility_tree(app)
        generation = uuid.uuid4().hex
        candidates = (
            _typed_candidates(elements, generation)
            if elements is not None else _parse_candidates(tree or "", generation)
        )
        candidates = tuple(
            candidate for candidate in candidates
            if candidate.bounds.x >= window_bounds.x
            and candidate.bounds.y >= window_bounds.y
            and candidate.bounds.x + candidate.bounds.width <= window_bounds.x + window_bounds.width
            and candidate.bounds.y + candidate.bounds.height <= window_bounds.y + window_bounds.height
        )
        snapshot = Snapshot(
            session_id=session_id, app=app, generation=generation, window_id=window_id,
            window_app=window_app, window_bounds=window_bounds, active=is_active,
            candidates=candidates, observed_at=time.time(),
        )
        self._bump(session_id)
        return snapshot

    def observe(self, session_id: str, app: str, *, mode: AutonomyMode | str = AutonomyMode.GUARDED) -> Snapshot:
        mode = AutonomyMode(mode)
        self._bind_session(session_id)
        lock = _session_lock(session_id)
        if not lock.acquire(blocking=False):
            self._audit(app, mode, "observe", "denied", "session_busy")
            raise TransactionError("session_busy")
        try:
            if app not in self.policy.allowed_apps:
                self._audit(app, mode, "observe", "denied", "app_not_allowed")
                raise TransactionError("app_not_allowed")
            if session_id in self._blocked:
                raise TransactionError("session_requires_reset")
            snapshot = self._snapshot(session_id, app)
            self._last[(session_id, app)] = snapshot
            self._audit(app, mode, "observe", "ok", "observed")
            return snapshot
        except TransactionError as exc:
            if str(exc) not in {"app_not_allowed", "session_busy"}:
                self._audit(app, mode, "observe", "failed", _safe_reason(str(exc)))
            raise
        except Exception:
            self._audit(app, mode, "observe", "failed", "backend_error")
            raise TransactionError("observation_failed") from None
        finally:
            lock.release()

    def act(
        self,
        session_id: str,
        app: str,
        action: Action,
        *,
        verifier: Callable[[Snapshot], Verification],
        mode: AutonomyMode | str = AutonomyMode.GUARDED,
        approve: Callable[[Action, Candidate], bool] | None = None,
        cancel: threading.Event | Callable[[], bool] | None = None,
    ) -> ActionResult:
        mode = AutonomyMode(mode)
        self._bind_session(session_id)
        lock = _session_lock(session_id)
        if not lock.acquire(blocking=False):
            self._audit(app, mode, action.kind, "denied", "session_busy")
            raise TransactionError("session_busy")
        before: Snapshot | None = None
        input_attempted = False
        try:
            if app not in self.policy.allowed_apps:
                raise TransactionError("app_not_allowed")
            if session_id in self._blocked:
                raise TransactionError("session_requires_reset")
            if action.kind not in {"click", "type_text"}:
                raise TransactionError("unsupported_action")
            if not self.authorizer(session_id, app, action):
                raise TransactionError("authorization_denied")
            self._check_budget(session_id, action=True)
            prior = self._last.get((session_id, app))
            candidate = prior.candidate(action.target_ref) if prior else None
            if candidate is None:
                raise TransactionError("target_reference_stale")
            if action.kind not in candidate.actions:
                raise TransactionError("action_not_supported_by_target")
            if action.kind == "type_text":
                if not isinstance(action.text, str) or len(action.text) > self.policy.max_text_chars:
                    raise TransactionError("text_argument_invalid")
            if _cancelled(cancel):
                raise TransactionError("cancelled")
            if mode is AutonomyMode.SUPERVISED or (
                mode is AutonomyMode.GUARDED and _requires_confirmation(action, candidate, self.policy)
            ):
                if approve is None or not approve(action, candidate):
                    raise TransactionError("approval_denied")

            # Re-resolve immediately before input. Fresh generation IDs ensure a
            # reference cannot be replayed after another observation.
            fresh = self._snapshot(session_id, app)
            before = fresh
            current = _unique_matching(prior, candidate, fresh)
            if current is None:
                raise TransactionError("target_changed_or_ambiguous")
            if not _usable(current):
                raise TransactionError("target_disabled_or_hidden")
            if fresh.window_id != prior.window_id:
                raise TransactionError("window_identity_changed")
            if not fresh.active:
                raise TransactionError("target_window_not_active")
            if _cancelled(cancel):
                raise TransactionError("cancelled")
            # Recheck after the AT-SPI resolution and immediately before any
            # focus-routed EIS input.
            active_now = self.backend.active_window()
            active_ids = _ID.findall(active_now)
            if len(active_ids) != 1 or active_ids[0] != fresh.window_id:
                raise TransactionError("target_window_not_active")
            if action.kind == "click":
                self._audit(app, mode, action.kind, "pending", "input_pending")
                input_attempted = True
                self._bump(session_id, action=True)
                self._last.pop((session_id, app), None)
                self._blocked[session_id] = "verification_pending"
                self.backend.mouse_click(
                    current.bounds.x + current.bounds.width // 2,
                    current.bounds.y + current.bounds.height // 2,
                    button="left",
                )
            else:
                if "focused" not in current.states:
                    raise TransactionError("editable_target_not_focused")
                if action.text is None:
                    raise TransactionError("text_argument_invalid")
                self._audit(app, mode, action.kind, "pending", "input_pending")
                input_attempted = True
                self._bump(session_id, action=True)
                self._last.pop((session_id, app), None)
                self._blocked[session_id] = "verification_pending"
                self.backend.keyboard_type(action.text)
            if _cancelled(cancel):
                try:
                    cancelled_snapshot = self._snapshot(session_id, app)
                    self._last[(session_id, app)] = cancelled_snapshot
                except Exception:
                    self._last.pop((session_id, app), None)
                self._blocked[session_id] = "cancelled_after_action"
                raise TransactionError("cancelled_after_action")

            after = self._snapshot(session_id, app)
            verification = verifier(after)
            if not isinstance(verification, Verification) or type(verification.passed) is not bool:
                raise TransactionError("verifier_contract_invalid")
            if _cancelled(cancel):
                self._blocked[session_id] = "cancelled_after_action"
                raise TransactionError("cancelled_after_action")
            started_at = self._counts[session_id][0]
            if time.monotonic() - started_at > self.policy.max_duration_seconds:
                raise TransactionError("task_time_budget_exceeded")
            self._last[(session_id, app)] = after
            if not verification.passed:
                self._blocked[session_id] = "verification_failed"
                raise TransactionError("verification_failed")
            result = ActionResult(
                status="ok",
                verification="passed",
                reason="verified",
                before=before, after=after,
                evidence=verification.evidence,
            )
            self._audit(app, mode, action.kind, result.status, result.reason)
            self._blocked.pop(session_id, None)
            return result
        except TransactionError as exc:
            reason = _safe_reason(str(exc))
            if input_attempted and self._blocked.get(session_id) == "verification_pending":
                try:
                    failed_snapshot = self._snapshot(session_id, app)
                    self._last[(session_id, app)] = failed_snapshot
                except Exception:
                    self._last.pop((session_id, app), None)
                self._blocked[session_id] = "unverified_input"
            self._audit(app, mode, action.kind, "failed" if input_attempted or before else "denied", reason)
            raise
        except Exception:
            if input_attempted:
                try:
                    failed_snapshot = self._snapshot(session_id, app)
                    self._last[(session_id, app)] = failed_snapshot
                except Exception:
                    self._last.pop((session_id, app), None)
                self._blocked[session_id] = "unverified_input"
            self._audit(app, mode, action.kind, "failed", "backend_or_verifier_error")
            raise TransactionError("action_failed") from None
        finally:
            lock.release()


def _cancelled(cancel: threading.Event | Callable[[], bool] | None) -> bool:
    if cancel is None:
        return False
    return cancel.is_set() if isinstance(cancel, threading.Event) else bool(cancel())


def _usable(candidate: Candidate) -> bool:
    return {"enabled", "sensitive", "showing", "visible"}.issubset(candidate.states)


def _unique_matching(prior: Snapshot, candidate: Candidate, fresh: Snapshot) -> Candidate | None:
    if prior.generation not in candidate.ref:
        return None
    matches = [item for item in fresh.candidates if (item.role, item.label) == (candidate.role, candidate.label)]
    if len(matches) != 1 or _fingerprint(matches[0]) != _fingerprint(candidate):
        return None
    return matches[0]


def _requires_confirmation(action: Action, candidate: Candidate, policy: Policy) -> bool:
    if action.kind != "click":
        return False
    label = candidate.label.casefold()
    return any(word in label for word in policy.confirmation_words)


_AUDIT_REASONS = frozenset({
    "observed", "app_not_allowed", "session_busy", "session_requires_reset",
    "observation_failed", "backend_error", "authorization_denied", "unsupported_action",
    "task_time_budget_exceeded", "task_action_budget_exceeded", "task_observation_budget_exceeded",
    "target_reference_stale", "action_not_supported_by_target", "text_argument_invalid", "cancelled",
    "approval_denied", "target_changed_or_ambiguous", "target_disabled_or_hidden",
    "window_identity_changed", "window_app_mismatch", "window_identity_ambiguous",
    "target_window_not_active", "editable_target_not_focused", "input_pending", "cancelled_after_action",
    "verification_failed", "verifier_contract_invalid", "backend_or_verifier_error", "action_failed",
    "verified",
})


def _safe_reason(value: str) -> str:
    return value if value in _AUDIT_REASONS else "operation_failed"
