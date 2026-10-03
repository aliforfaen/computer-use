from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

from agent_wait_trial_fixture import AgentWaitFixture


def post(url: str, body: bytes = b"") -> int:
    with urlopen(Request(url, data=body, method="POST", headers={"content-type": "application/json"}),
                 timeout=2) as response:
        return response.status


def wait_armed(fixture: AgentWaitFixture, trial: str) -> bool:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if any(event.get("kind") == "arm" for event in fixture.server.snapshot(trial)):
            return True
        time.sleep(0.02)
    return False


class AgentWaitFixtureTests(unittest.TestCase):
    def test_error_recovery_stages_arm_only_after_loaded_page_and_mcp_screenshot(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            audit = root / "audit.jsonl"
            output = root / "fixture.json"
            fixture = AgentWaitFixture(audit, "error_recovery", output)
            config = fixture.start()
            base = f"http://127.0.0.1:{fixture.server.server_port}"
            try:
                first, second = config["stages"]
                self.assertEqual([first["state"], second["state"]], ["error", "ready"])
                post(f"{base}/event", json.dumps({"trial": first["trial_id"], "kind": "loaded"}).encode())
                time.sleep(0.05)
                self.assertFalse(fixture.armed)
                with audit.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"tool": "observe", "status": "ok", "transport": "local-mcp",
                                             "app": "firefox"}) + "\n")
                self.assertTrue(wait_armed(fixture, first["trial_id"]))
                post(f"{base}/event", json.dumps({"trial": second["trial_id"], "kind": "loaded"}).encode())
                with audit.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"tool": "observe", "status": "ok", "transport": "local-mcp",
                                             "app": "firefox"}) + "\n")
                self.assertTrue(wait_armed(fixture, second["trial_id"]))
            finally:
                report = fixture.stop()
            self.assertTrue(all(stage["armed"] for stage in report["stages"]))
            self.assertTrue(output.is_file())


if __name__ == "__main__":
    unittest.main()
