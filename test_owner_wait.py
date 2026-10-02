from __future__ import annotations

import hashlib
import threading
import unittest
from dataclasses import dataclass
from types import SimpleNamespace

from observation import CaptureRef, ObservationError, Observation
from owner_wait import run_owner_wait
from vision_reader import BoundedReader, ReaderResult


class _FakeReader:
    def __init__(self, judgment="wake"):
        self.judgment = judgment
        self.calls = 0

    def capabilities(self):
        return {"provider": "fake", "model": "test"}

    def interpret(self, _image, _questions):
        self.calls += 1
        return ReaderResult("ok", data={"judgment": self.judgment}, provider="fake", model="test",
                            usage={"prompt_tokens": 3, "completion_tokens": 1}, latency_ms=0.5)


class _Adapter:
    def __init__(self, reader):
        self.reader = reader
        self.sequence = 0

    def capture(self, app, *, title=None, scope="app"):
        self.sequence += 1
        image = f"frame-{self.sequence}".encode()
        return CaptureRef(str(self.sequence), "2026-10-02T00:00:00+00:00", "session", "window", app,
                          "fixture", len(image), 1, len(image), 1, hashlib.sha256(image).hexdigest(),
                          hashlib.sha256(image).hexdigest(), scope,
                          {"origin": {"x": 0, "y": 0}, "source_dimensions": {"width": len(image), "height": 1},
                           "selected_rect": {"x": 0, "y": 0, "width": len(image), "height": 1},
                           "coordinate_basis": "full"})

    def observe(self, ref, *, mode="image", questions=None):
        image = f"frame-{ref.capture_id}".encode()
        metadata = {"capture_id": ref.capture_id, "captured_at": ref.captured_at,
                    "image_sha256": ref.image_sha256, "scope": ref.scope}
        return Observation(ref, metadata, image=image)


def _session(*, judgment="wake", observation_cap=4, reader_calls=2, reader=True):
    wrapped = BoundedReader(_FakeReader(judgment), reader_calls) if reader else None
    monotonic = __import__("time").monotonic
    return SimpleNamespace(app="firefox", adapter=_Adapter(wrapped), reader_calls=0,
                           max_reader_calls=reader_calls if reader else 0, observations=0,
                           max_observations=observation_cap, monotonic=monotonic,
                           started_clock=monotonic(), max_session_lifetime=1800.0,
                           expiring=False, stopping=False, last_activity_clock=monotonic(),
                           last_activity_at="", cancel=threading.Event())


class OwnerWaitTests(unittest.TestCase):
    def test_wait_is_exposed_as_bounded_read_only_mcp_tool(self):
        from desktop_mcp import METHODS, _tools
        tool = next(item for item in _tools() if item.name == "desktop_wait")
        self.assertTrue(tool.annotations.read_only_hint)
        self.assertIn("wait", METHODS["desktop_wait"])
        self.assertEqual(tool.input_schema["properties"]["timeout_seconds"]["maximum"], 120)
        self.assertEqual(tool.input_schema["properties"]["expected"]["maxLength"], 500)

    def test_ready_wait_returns_same_frame_metadata_hash_and_debits_internal_reads(self):
        session = _session(observation_cap=2)
        result = run_owner_wait(session, "ready screen", 2)
        self.assertEqual(result["wait"]["status"], "ready")
        self.assertEqual(result["wait"]["captures"], session.observations)
        self.assertGreaterEqual(session.observations, 2)
        image = __import__("base64").b64decode(result["evidence"]["image_base64"])
        metadata = result["evidence"]["capture"]
        self.assertEqual(hashlib.sha256(image).hexdigest(), metadata["image_sha256"])
        self.assertEqual(metadata["capture_id"], result["evidence"]["capture"]["capture_id"])
        self.assertEqual(result["reader"]["session_total"]["reported_usage"],
                         {"prompt_tokens": 6, "completion_tokens": 2})

    def test_missing_reader_and_exhausted_observations_fail_before_wait(self):
        with self.assertRaisesRegex(ValueError, "reader_unavailable"):
            run_owner_wait(_session(reader=False), "ready", 1)
        session = _session(observation_cap=0)
        with self.assertRaisesRegex(ValueError, "task_observation_budget_exceeded"):
            run_owner_wait(session, "ready", 1)
        self.assertEqual(session.observations, 0)

    def test_reader_budget_is_shared_and_exhaustion_is_returned_as_error(self):
        session = _session(judgment="wait", observation_cap=10, reader_calls=1)
        result = run_owner_wait(session, "ready", 3)
        self.assertEqual((result["wait"]["status"], result["wait"]["error_code"]),
                         ("error", "reader_call_budget_exceeded"))
        self.assertEqual(result["reader"]["session_total"]["calls_used"], 1)


if __name__ == "__main__":
    unittest.main()
