#!/usr/bin/env python3
"""One-step KCalc proof using kwin-mcp and a bounded TypeSafe selector."""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MODEL = "jev-1.13.0"
API_URL = "https://api.typesafe.ai/v1/systemone"
APP = "kcalc"
TASK = "Enter digit 1 in the blank KCalc display."
MIN_CONFIDENCE = 0.80
TREE_LINE = re.compile(
    r'^\s*- \[(?P<role>[^]]+)\] "(?P<name>(?:[^"\\]|\\.)*)"'
    r' \((?P<states>[^)]*)\) @ screen '
    r'\((?P<x>\d+), (?P<y>\d+), (?P<w>\d+)x(?P<h>\d+)\)'
    r' \[actions: (?P<actions>[^]]*)\]$'
)
TEXT_LINE = re.compile(r'^\s*- \[text\] "(?P<name>(?:[^"\\]|\\.)*)" \((?P<states>[^)]*)\)(?P<tail>.*)$')
TEXT_VALUE = re.compile(r"\btext='((?:[^'\\]|\\.)*)'")


@dataclass(frozen=True)
class Button:
    target_id: str
    name: str
    x: int
    y: int
    width: int
    height: int


class ProbeError(RuntimeError):
    pass


def _unescape(value: str) -> str:
    return json.loads(f'"{value}"') if "\\" in value else value


def parse_buttons(tree: str) -> list[Button]:
    """Parse only uniquely named, actionable, visible screen buttons."""
    buttons: list[Button] = []
    seen: set[str] = set()
    for line in tree.splitlines():
        match = TREE_LINE.match(line)
        if not match or match["role"] != "button":
            continue
        states = {item.strip() for item in match["states"].split(",")}
        actions = {item.strip() for item in match["actions"].split(",")}
        name = _unescape(match["name"])
        if not {"enabled", "sensitive", "showing", "visible"}.issubset(states):
            continue
        if "Press" not in actions or not name:
            continue
        if name in seen:
            raise ProbeError(f"ambiguous duplicate button name: {name}")
        seen.add(name)
        x, y, width, height = (int(match[key]) for key in ("x", "y", "w", "h"))
        if width <= 0 or height <= 0:
            continue
        buttons.append(Button(f"button.{name}", name, x, y, width, height))
    return buttons


def parse_display(tree: str) -> str:
    """Read the single editable text field; absence of a text property means blank."""
    fields: list[str] = []
    for line in tree.splitlines():
        match = TEXT_LINE.match(line)
        if not match:
            continue
        states = {item.strip() for item in match["states"].split(",")}
        if not {"editable", "enabled", "sensitive", "showing", "visible"}.issubset(states):
            continue
        value = TEXT_VALUE.search(match["tail"])
        fields.append(_unescape(value.group(1)) if value else "")
    if len(fields) != 1:
        raise ProbeError(f"expected one visible editable display field; found {len(fields)}")
    return fields[0]


def parse_decision(payload: dict[str, Any], allowed_targets: set[str]) -> tuple[str, str, float, float]:
    """Validate Jev's two bounded choices before any desktop action."""
    answers = payload.get("answers", {})
    action = answers.get("action", {})
    target = answers.get("target", {})
    if action.get("type") != "choice" or target.get("type") != "choice":
        raise ProbeError("Jev did not return both choice answers")
    action_choice = action.get("choice")
    target_choice = target.get("choice")
    action_conf = action.get("confidence")
    target_conf = target.get("confidence")
    if not isinstance(action_conf, (int, float)) or not isinstance(target_conf, (int, float)):
        raise ProbeError("Jev confidence is missing or malformed")
    if action_conf < MIN_CONFIDENCE or target_conf < MIN_CONFIDENCE:
        raise ProbeError("Jev confidence is below the 0.80 execution threshold")
    if action_choice != "press":
        raise ProbeError("only the code-authorized press action is executable")
    if not isinstance(target_choice, str) or target_choice not in allowed_targets:
        raise ProbeError("Jev selected a target outside the observed button set")
    return action_choice, target_choice, float(action_conf), float(target_conf)


def verify_transition(before: str, after: str) -> bool:
    return before == "" and after == "1"


def _load_key() -> str:
    key = os.environ.get("JEV_API_KEY")
    if key:
        return key
    env_path = Path(__file__).resolve().parent / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ProbeError("JEV_API_KEY is not in the environment and .env could not be read") from exc
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        if name.strip() == "JEV_API_KEY":
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if value:
                return value
    raise ProbeError("JEV_API_KEY is missing from the environment and .env")


def _ask_jev(key: str, state: dict[str, Any], target_ids: list[str]) -> dict[str, Any]:
    question = {
        "action": {
            "type": "choice",
            "instructions": "For the fixed task, choose whether to press the relevant button or report done.",
            "criteria": {"press": "press the button needed for the task", "done": "the requested display value is already present"},
        },
        "target": {
            "type": "choice",
            "instructions": "Choose the observed button that enters digit 1.",
            "criteria": {target_id: target_id for target_id in target_ids},
        },
    }
    body = json.dumps({"model": MODEL, "state": state, "questions": question}).encode("utf-8")
    request = urllib.request.Request(
        API_URL,
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ProbeError(f"TypeSafe request failed ({type(exc).__name__})") from None
    if not isinstance(result, dict):
        raise ProbeError("TypeSafe response was not a JSON object")
    return result


def _audit(event: dict[str, Any]) -> None:
    path = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "jev-desktop" / "audit.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "caller_node": "local",
        "transport": "cli",
        "tool": "kcalc.enter_digit_1",
        "app": APP,
        "autonomy_mode": "guarded",
        **event,
    }
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, separators=(",", ":")) + "\n")


def run() -> int:
    if APP not in {"kcalc"}:  # fixed deny-by-default proof allowlist
        raise ProbeError("application is outside the proof allowlist")
    key = _load_key()
    try:
        from kwin_mcp.core import AutomationEngine
    except ImportError as exc:
        raise ProbeError("install pinned dependency with: uv run --with kwin-mcp==0.10.0 python p2_kcalc.py") from exc

    engine = AutomationEngine()
    started = False
    try:
        started = True  # session_stop is attempted even if startup partially fails
        start_result = engine.session_start(
            app_command="kcalc", screen_width=1280, screen_height=800, isolate_home=True
        )
        if "Input backend: KWin EIS" not in start_result:
            raise ProbeError("virtual session did not report the expected KWin EIS input backend")

        engine.wait_for_element("One", app_name=APP, timeout_ms=10000, expected_states=["enabled", "visible"])
        initial_tree = engine.accessibility_tree(app_name=APP, max_depth=15)
        before = parse_display(initial_tree)
        if before != "":
            raise ProbeError("code-owned task requires the display to be blank")
        buttons = parse_buttons(initial_tree)
        targets = {button.target_id: button for button in buttons}
        if "button.One" not in targets:
            raise ProbeError("the observed visible/enabled button set does not contain button.One")

        state = {
            "app": "KCalc",
            "task": TASK,
            "display": "blank",
            "buttons": [{"id": button.target_id, "label": button.name} for button in buttons],
        }
        result = _ask_jev(key, state, [button.target_id for button in buttons])
        action, target_id, action_conf, target_conf = parse_decision(result, set(targets))
        if target_id != "button.One":
            raise ProbeError("selector did not choose the code-required One button")

        # Re-read immediately before acting; only fresh, uniquely resolved bounds are used.
        fresh_tree = engine.accessibility_tree(app_name=APP, max_depth=15)
        fresh_display = parse_display(fresh_tree)
        fresh_buttons = {button.target_id: button for button in parse_buttons(fresh_tree)}
        button = fresh_buttons.get(target_id)
        if fresh_display != "" or button is None:
            raise ProbeError("pre-action target or blank-display state changed")

        _audit({
            "event": "decision",
            "jev_model": result.get("model", MODEL),
            "answers": {"action": action, "action_confidence": action_conf, "target": target_id, "target_confidence": target_conf},
            "verification": "pending",
        })
        engine.mouse_click(button.x + button.width // 2, button.y + button.height // 2, button="left")

        # The next observation is the verifier; the model never decides success.
        deadline = time.monotonic() + 5
        after = ""
        while time.monotonic() < deadline:
            after = parse_display(engine.accessibility_tree(app_name=APP, max_depth=15))
            if after == "1":
                break
            time.sleep(0.1)
        verified = verify_transition(before, after)
        _audit({"event": "verification", "action": action, "target": target_id, "verification": "passed" if verified else "failed"})
        if not verified:
            raise ProbeError("KCalc display did not verify the exact empty-to-1 transition")
        print("P2 proof passed: virtual KCalc display verified empty → 1")
        return 0
    except Exception as exc:
        if isinstance(exc, ProbeError):
            print(f"P2 proof stopped: {exc}", file=sys.stderr)
        else:
            print(f"P2 proof stopped: {type(exc).__name__}", file=sys.stderr)
        try:
            _audit({"event": "failure", "verification": "failed", "error_type": type(exc).__name__})
        except Exception:
            print("Audit append failed", file=sys.stderr)
        return 1
    finally:
        if started:
            try:
                engine.session_stop()
            except Exception as exc:
                print(f"Virtual session cleanup failed ({type(exc).__name__})", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(run())
