"""Serve a deterministic local page transition for real MCP-agent wait trials.

The fixture arms only after the configured virtual owner's audit records the
agent's first screenshot after each page load. The controller never performs
desktop actions or decides what the screenshot means.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import signal
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from wait_benchmark import FixtureServer


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AgentWaitFixture:
    def __init__(self, audit_path: Path, scenario: str, output: Path, delay_ms: int = 8000):
        if scenario not in {"ready", "error_recovery"}:
            raise ValueError("scenario must be ready or error_recovery")
        if not 250 <= delay_ms <= 15000:
            raise ValueError("delay_ms must be 250..15000")
        self.audit_path = audit_path
        self.output = output
        self.server = FixtureServer()
        self.stages = ["ready"] if scenario == "ready" else ["error", "ready"]
        self.delay_ms = delay_ms
        self.stage_ids = [uuid.uuid4().hex for _ in self.stages]
        self.armed: set[str] = set()
        self.observe_count = 0
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._monitor, name="agent-wait-fixture-controller", daemon=True)
        self.started_at = utc_now()

    def start(self) -> dict[str, Any]:
        self.server.start()
        self.thread.start()
        base = f"http://127.0.0.1:{self.server.server_port}/wait.html"
        stages = []
        for ident, state in zip(self.stage_ids, self.stages, strict=True):
            url = base + "?" + urlencode({"trial": ident, "state": state, "delay": self.delay_ms,
                                          "dialog": 0, "noise": 0})
            stages.append({"state": state, "trial_id": ident, "url": url})
        return {"scenario": "ready" if self.stages == ["ready"] else "error_recovery",
                "stages": stages, "delay_ms": self.delay_ms,
                "arm_rule": "after a loaded event and the next successful virtual MCP desktop_observe",
                "audit_path": str(self.audit_path), "server_started_at": self.started_at}

    def _completed_observations(self) -> int:
        try:
            lines = self.audit_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return 0
        count = 0
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (record.get("tool") == "observe" and record.get("status") == "ok"
                    and record.get("transport") == "local-mcp" and record.get("app") == "firefox"):
                count += 1
        return count

    def _monitor(self) -> None:
        last_observe = self._completed_observations()
        while not self.stop_event.wait(0.05):
            current = self._completed_observations()
            if current > last_observe:
                available = current - last_observe
                for ident in self.stage_ids:
                    if available <= 0 or ident in self.armed:
                        continue
                    events = self.server.snapshot(ident)
                    if not any(event.get("kind") == "loaded" for event in events):
                        continue
                    try:
                        request = Request(f"http://127.0.0.1:{self.server.server_port}/arm?trial={ident}",
                                          data=b"", method="POST")
                        with urlopen(request, timeout=2) as response:
                            if response.status == 204:
                                self.armed.add(ident)
                                available -= 1
                    except Exception:
                        pass
                last_observe = current

    def stop(self) -> dict[str, Any]:
        self.stop_event.set()
        self.thread.join(timeout=2)
        self.server.shutdown()
        self.server.server_close()
        self.server.thread.join(timeout=2)
        events = {ident: self.server.snapshot(ident) for ident in self.stage_ids}
        report = {"scenario": "ready" if self.stages == ["ready"] else "error_recovery",
                  "stages": [{"trial_id": ident, "expected_state": state, "armed": ident in self.armed,
                              "events": events[ident]}
                             for ident, state in zip(self.stage_ids, self.stages, strict=True)],
                  "server_started_at": self.started_at, "server_stopped_at": utc_now(),
                  "successful_firefox_observations_seen": self._completed_observations()}
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", required=True, type=Path, help="isolated owner's local-mcp audit.jsonl")
    parser.add_argument("--output", required=True, type=Path, help="ignored output path for fixture event report")
    parser.add_argument("--scenario", choices=("ready", "error_recovery"), required=True)
    parser.add_argument("--delay-ms", type=int, default=8000)
    args = parser.parse_args(argv)
    fixture = AgentWaitFixture(args.audit, args.scenario, args.output, args.delay_ms)
    print(json.dumps(fixture.start(), indent=2), flush=True)
    stop_requested = threading.Event()
    previous_sigint = signal.signal(signal.SIGINT, lambda _signum, _frame: stop_requested.set())
    try:
        stop_requested.wait()
    finally:
        # The desktop exec PTY may deliver repeated Ctrl-C while shutdown joins
        # its helper threads. Ignore further interrupts until the receipt is
        # durably written.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        report = fixture.stop()
        print(json.dumps({"report": str(args.output), "armed_stages": sum(1 for s in report["stages"] if s["armed"])}))
        signal.signal(signal.SIGINT, previous_sigint)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
