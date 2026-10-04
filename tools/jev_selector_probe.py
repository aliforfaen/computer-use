#!/usr/bin/env python3
"""Bounded selector probe: build a decision request from saved candidates.

Offline by default. Nothing is sent unless `--call` is passed, and `--call`
spends at most `--max-calls` (default 1, hard cap 3) provider requests.

    # 1. capture candidates from a running owner (free)
    uv run jev-desktop candidates kcalc > /tmp/kcalc.json
    # 2. inspect the exact request and its token estimate (free)
    uv run python -m tools.jev_selector_probe --candidates-json /tmp/kcalc.json --goal "enter 1"
    # 3. replay a recorded answer to test the validation rules (free)
    uv run python -m tools.jev_selector_probe --candidates-json /tmp/kcalc.json --goal "enter 1" \
        --recorded /tmp/answer.json
    # 4. one real call, explicitly (paid, key from JEV_API_KEY)
    uv run python -m tools.jev_selector_probe --candidates-json /tmp/kcalc.json --goal "enter 1" --call

The probe selects only. It never performs an action and never sends typed text.
The key comes from `JEV_API_KEY` in the environment, falling back to the private
`~/.config/jev-desktop/jev.env` (0600). It is never placed in the request state,
never echoed, and never written to a report.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from desktop_daemon import _load_dotenv_key, default_config_dir
from jev_selector import (HttpSelector, JevConfig, MAX_OPTIONS, RecordedSelector, SelectorError,
                          build_request, estimate_tokens, render_state_text, state_from_candidates)

MAX_PROBE_CALLS = 3
KEY_NAME = "JEV_API_KEY"


def _load_api_key(dotenv: Path | None, key_name: str = KEY_NAME) -> bool:
    """Load the Jev key from the private dotenv file without printing it.

    The environment wins, so an exported key still works. Reuses the daemon's
    single dotenv loader rather than growing a second implementation.
    """
    if dotenv is not None and not os.environ.get(key_name):
        _load_dotenv_key(dotenv, key_name)
    return bool(os.environ.get(key_name))


def _load_candidates(path: Path) -> tuple[str, str, list[dict]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and isinstance(payload.get("candidates"), list):
        return (str(payload.get("app") or ""), str(payload.get("window_title") or ""), payload["candidates"])
    if isinstance(payload, list):
        return ("", "", payload)
    raise SystemExit("candidates json must be a candidates response or a candidate list")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jev-selector-probe", description=__doc__.splitlines()[0])
    parser.add_argument("--candidates-json", type=Path, required=True,
                        help="saved `jev-desktop candidates` output")
    parser.add_argument("--goal", required=True, help="the bounded goal for this state")
    parser.add_argument("--app", default=None, help="override the app name")
    parser.add_argument("--window-title", default=None, help="override the window title")
    parser.add_argument("--model", default="jev-latest", help="pinned model alias")
    parser.add_argument("--limit", type=int, default=MAX_OPTIONS, help="maximum enumerated options")
    parser.add_argument("--recorded", type=Path, default=None,
                        help="replay a recorded answers JSON instead of calling the provider")
    parser.add_argument("--call", action="store_true", help="make one real provider request")
    parser.add_argument("--max-calls", type=int, default=1, help=f"provider call cap (maximum {MAX_PROBE_CALLS})")
    parser.add_argument("--save-request", type=Path, default=None, help="write the exact request body")
    parser.add_argument("--dotenv", type=Path, default=default_config_dir() / "jev.env",
                        help="private dotenv holding JEV_API_KEY; values are never printed")
    args = parser.parse_args(argv)

    if not 1 <= args.max_calls <= MAX_PROBE_CALLS:
        print(json.dumps({"ok": False, "error": "max_calls_out_of_range", "hard_cap": MAX_PROBE_CALLS}))
        return 2
    api_key_present: bool | None = None
    if args.call:
        api_key_present = _load_api_key(args.dotenv)
        if not api_key_present:
            print(json.dumps({"ok": False, "error": "jev_api_key_missing", "dotenv": str(args.dotenv),
                              "hint": f"export {KEY_NAME} or put it in that private file (0600); nothing was sent"}))
            return 2
    try:
        app, title, candidates = _load_candidates(args.candidates_json)
        state = state_from_candidates(args.app or app or "unknown", args.window_title or title,
                                      args.goal, candidates, limit=args.limit)
        request = build_request(state, model=args.model)
    except (SelectorError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)[:80]}))
        return 2

    report: dict = {
        "ok": True,
        "mode": "call" if args.call else "recorded" if args.recorded else "dry_run",
        "provider_calls": 0,
        "options": len(state.options),
        "state_tokens_estimate": estimate_tokens(request["state"]),
        "request_tokens_estimate": estimate_tokens(request),
        "targetless_actions": ["wait", "done", "blocked"],
        "dotenv": str(args.dotenv),
        "api_key_present": api_key_present,
    }
    if args.save_request:
        args.save_request.write_text(json.dumps(request, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        report["request_path"] = str(args.save_request)
    if not args.call and not args.recorded:
        report["state_text"] = render_state_text(state)
        report["note"] = "dry run: nothing was sent; pass --call to spend one provider request"
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0

    selector: object | None = None
    try:
        if args.recorded:
            recorded = json.loads(args.recorded.read_text(encoding="utf-8"))
            answers = recorded.get("answers") if isinstance(recorded, dict) and "answers" in recorded else recorded
            selector = RecordedSelector(answers, model="recorded")
        else:
            selector = HttpSelector(JevConfig(model=args.model, max_calls=args.max_calls))
        decision = selector.select(request, options=state.options)
    except SelectorError as exc:
        report.update(ok=False, error=str(exc)[:80], provider_calls=getattr(selector, "calls", 0) if selector else 0)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 2
    report.update(provider_calls=getattr(selector, "calls", 0),
                  decision={"action": decision.action, "target_ref": decision.target_ref,
                            "confidence": decision.confidence, "probabilities": decision.probabilities,
                            "model": decision.model, "usage": decision.usage})
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
