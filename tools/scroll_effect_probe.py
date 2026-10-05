"""One bounded scroll before/after probe against a local loopback fixture.

Virtual session only, no reader/provider calls. Run from the repo root:

    uv run python -m tools.scroll_effect_probe

Writes `report.json` under `run/scroll-<date>/` and stops/cleans the session it
started. The probe is reusable; only its output is local evidence.
"""

from __future__ import annotations

import functools
import http.server
import json
import socketserver
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from jevdesktop.desktop_service import DesktopService
from jevdesktop.transactions import _scroll_viewport, _scroll_witnesses

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "run" / f"scroll-{datetime.now(timezone.utc):%Y-%m-%d}"
CTX = {"transport": "local-host-probe", "caller_node": "local"}
REPORT: dict = {"started_at": datetime.now(timezone.utc).isoformat(), "provider_calls": 0, "steps": []}


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # keep probe output clean
        return


def serve_fixtures() -> tuple[socketserver.TCPServer, str]:
    handler = functools.partial(_Quiet, directory=str(ROOT / "benchmark_fixtures"))
    server = socketserver.TCPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}/scroll.html"


def dump_candidate(candidate) -> dict:
    return {
        "ref": candidate.ref,
        "role": candidate.role,
        "label": candidate.label,
        "bounds": vars(candidate.bounds),
        "value_number": candidate.value_number,
        "value_max": candidate.value_max,
        "states": sorted(candidate.states),
        "actions": list(candidate.actions),
        "unavailable_reason": candidate.unavailable_reason,
    }


def scroll_diagnostics(before, after, viewport_before, viewport_after) -> dict:
    before_positions = _scroll_witnesses(before, viewport_before)
    after_positions = _scroll_witnesses(after, viewport_after)
    common = set(before_positions) & set(after_positions)
    deltas = {f"{key[0]}:{key[1]}": after_positions[key].y - before_positions[key].y for key in common}
    return {
        "before_witnesses": len(before_positions),
        "after_witnesses": len(after_positions),
        "common_witnesses": len(common),
        "delta_signs": {"negative": sum(d < 0 for d in deltas.values()),
                        "zero": sum(d == 0 for d in deltas.values()),
                        "positive": sum(d > 0 for d in deltas.values())},
        "sample_deltas": dict(list(sorted(deltas.items()))[:8]),
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    server, url = serve_fixtures()
    service = DesktopService(OUT / "audit.jsonl", allowed_apps={"firefox"}, request_timeout=20)
    REPORT["url"] = url
    session_id = None
    try:
        started = service.dispatch("session_start", {"app": "firefox"}, CTX)
        REPORT["start"] = {"ok": started.get("ok"), "error": started.get("error")}
        if not started.get("ok"):
            return
        session_id = started["session"]["session_id"]
        REPORT["session_id"] = session_id

        deadline = time.monotonic() + 15
        candidates = service.dispatch("candidates", {"app": "firefox"}, CTX)
        while (not candidates.get("ok") and time.monotonic() < deadline
               and candidates.get("error", {}).get("code") in {"window_not_found", "window_identity_ambiguous"}):
            time.sleep(0.3)
            candidates = service.dispatch("candidates", {"app": "firefox"}, CTX)
        REPORT["candidates_ready"] = candidates.get("ok")
        address = next((item for item in candidates.get("candidates", []) if "navigate_url" in item["actions"]), None)
        if address is None:
            REPORT["address_candidate_error"] = "not_exposed"
            return
        if "focused" not in address["states"]:
            focus = service.dispatch("act", {"app": "firefox", "action": "click", "target_ref": address["ref"],
                                             "verification": "target_focused"}, CTX)
            REPORT["steps"].append({"step": "focus_address", "ok": focus.get("ok"), "error": focus.get("error")})
            candidates = service.dispatch("candidates", {"app": "firefox"}, CTX)
            address = next((item for item in candidates.get("candidates", []) if "navigate_url" in item["actions"]), None)
            if address is None:
                REPORT["address_candidate_error"] = "not_exposed_after_focus"
                return
        nav = service.dispatch("act", {"app": "firefox", "action": "navigate_url", "target_ref": address["ref"],
                                       "verification": "navigation_url", "expected": url, "text": url}, CTX)
        REPORT["steps"].append({"step": "navigate_url", "ok": nav.get("ok"), "error": nav.get("error"),
                                "evidence": nav.get("evidence")})
        if not nav.get("ok"):
            return
        time.sleep(3)

        session = service._session
        candidates = service.dispatch("candidates", {"app": "firefox"}, CTX)
        raw_before = session.tx._last[(session_id, "firefox")]
        targets = [raw_before.candidate(item["ref"]) for item in candidates.get("candidates", [])
                   if item["role"] in {"document frame", "scroll pane", "web area"}
                   and "scroll" in item["actions"] and not item.get("unavailable_reason")
                   and {"enabled", "sensitive", "showing", "visible"}.issubset(set(item["states"]))]
        REPORT["scroll_targets"] = [dump_candidate(c) for c in targets if c is not None]
        REPORT["before"] = [dump_candidate(c) for c in raw_before.candidates
                            if c.role in {"scroll bar", "scrollbar", "scroll pane", "document frame", "web area"}]
        if len(targets) != 1 or targets[0] is None:
            REPORT["target_error"] = f"expected one visible scroll target, saw {len(targets)}"
            return
        target = targets[0]
        REPORT["target_ref"] = target.ref

        result = service.dispatch("act", {"app": "firefox", "action": "scroll", "target_ref": target.ref,
                                          "verification": "scroll_changed", "direction": "down", "steps": 2}, CTX)
        REPORT["steps"].append({"step": "scroll_down", "ok": result.get("ok"), "error": result.get("error"),
                                "evidence": result.get("evidence")})

        # The failed-verification latch blocks a second read; clear it only to
        # collect the after-state diagnostics, never for the app under test.
        session.tx._blocked.pop(session_id, None)
        service.dispatch("candidates", {"app": "firefox"}, CTX)
        raw_after = session.tx._last[(session_id, "firefox")]
        REPORT["tx_failure_evidence"] = session.tx.last_verification_evidence
        REPORT["after"] = [dump_candidate(c) for c in raw_after.candidates
                           if c.role in {"scroll bar", "scrollbar", "scroll pane", "document frame", "web area"}]
        before_viewport = _scroll_viewport(raw_before.candidates, target)
        after_viewport = _scroll_viewport(raw_after.candidates, target)
        REPORT["viewport_match"] = {
            "role_label_bounds_matches_before": sum(
                (c.role, c.label, c.bounds) == (target.role, target.label, target.bounds)
                for c in raw_before.candidates),
            "role_label_matches_after": sum((c.role, c.label) == (target.role, target.label)
                                            for c in raw_after.candidates),
            "resolved_before": before_viewport is not None,
            "resolved_after": after_viewport is not None,
        }
        if before_viewport is not None and after_viewport is not None:
            REPORT["diagnostics"] = scroll_diagnostics(raw_before, raw_after, before_viewport, after_viewport)

        # Confirm the opposite direction still resolves and reports the sign.
        if result.get("ok"):
            up_candidates = service.dispatch("candidates", {"app": "firefox"}, CTX)
            up_ref = next((item["ref"] for item in up_candidates.get("candidates", [])
                           if item["role"] in {"document frame", "scroll pane", "web area"}
                           and "scroll" in item["actions"] and not item.get("unavailable_reason")
                           and {"enabled", "sensitive", "showing", "visible"}.issubset(set(item["states"]))), None)
            if up_ref is not None:
                up = service.dispatch("act", {"app": "firefox", "action": "scroll", "target_ref": up_ref,
                                               "verification": "scroll_changed", "direction": "up", "steps": 2}, CTX)
                REPORT["steps"].append({"step": "scroll_up", "ok": up.get("ok"), "error": up.get("error"),
                                        "evidence": up.get("evidence")})
    finally:
        try:
            REPORT["cleanup"] = service.dispatch("stop_all", {}, CTX)
            REPORT["status_after_stop"] = service.dispatch("status").get("session")
        except Exception as exc:  # keep the report even on cleanup failure
            REPORT["cleanup_error"] = type(exc).__name__
        try:
            service.close()
        except Exception as exc:
            REPORT["close_error"] = type(exc).__name__
        server.shutdown()
        server.server_close()
        REPORT["finished_at"] = datetime.now(timezone.utc).isoformat()
        (OUT / "report.json").write_text(json.dumps(REPORT, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps({key: REPORT.get(key) for key in
                          ("steps", "viewport_match", "diagnostics", "scroll_targets", "target_error",
                           "tx_failure_evidence", "cleanup", "status_after_stop", "cleanup_error", "close_error")},
                         indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
