"""Safety tests for the reusable vision benchmark harness.

The stream parser, fixture-hash and summary-shape checks were dropped once the
measured results in doc 13 were recorded. What remains protects the two things
a future re-run must never get wrong: no provider call before the call cap is
checked, and ground-truth facts never enter the request.
"""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import httpx

import vision_benchmark as vb


class VisionBenchmarkTests(unittest.TestCase):
    def test_answer_shape_and_type_validation_is_separate_from_accuracy(self):
        questions = [
            {"field": "visible", "type": "boolean", "description": "Is it visible?"},
            {"field": "label", "type": "string", "description": "Visible label"},
        ]
        answer, error = vb.parse_answer('{"visible":true,"label":"1"}', questions)
        self.assertIsNone(error)
        self.assertEqual(answer["label"], "1")
        self.assertEqual(vb.parse_answer('{"visible":1,"label":"1"}', questions)[1], "wrong_field_type")
        self.assertEqual(vb.parse_answer('{"visible":true}', questions)[1], "unexpected_or_missing_fields")
        self.assertEqual(vb.parse_answer('{"visible":true,"label":"1","extra":0}', questions)[1], "unexpected_or_missing_fields")
        self.assertEqual(vb.parse_answer('not json', questions)[1], "invalid_json")

    def test_call_cap_is_checked_before_credentials_or_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "fixture.png").write_bytes(b"bytes")
            image = (root / "fixture.png").read_bytes()
            case = {"case_id": "one", "questions": [{"field": "ok", "type": "boolean", "description": "visible"}],
                    "expected_facts": {"ok": True},
                    "images": [{"path": "fixture.png", "scope": "app", "sha256": hashlib.sha256(image).hexdigest()}]}
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps({"cases": [case]}), encoding="utf-8")
            with self.assertRaisesRegex(vb.BenchmarkError, "planned_calls_2_exceed_max_calls_1"):
                vb.run_benchmark(manifest_path=manifest_path, output_dir=root / "out", base_url="http://127.0.0.1:1",
                                 model="fake", key_env="MISSING_TEST_KEY", provider="generic", repetitions=2,
                                 scope="app", max_calls=1, seed=1)

    def test_mimo_request_keeps_expected_facts_local_and_records_served_model(self):
        expected = "ground-truth-never-in-the-prompt"
        observed_bodies = []

        def handle(request):
            body = json.loads(request.content)
            observed_bodies.append(body)
            self.assertNotIn(expected, request.content.decode())
            self.assertEqual(body["thinking"], {"type": "disabled"})
            self.assertEqual(body["max_completion_tokens"], 128)
            self.assertNotIn("max_tokens", body)
            event = {"model": "served-fixture-model", "choices": [
                {"delta": {"content": json.dumps({"marker": expected})}, "finish_reason": "stop"}
            ], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
            return httpx.Response(200, text="data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n")

        fixture = {"image_data": b"synthetic-png", "questions": [
            {"field": "marker", "type": "string", "description": "Transcribe the visible marker."}
        ], "expected_facts": {"marker": expected}}
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            result = vb._request_one(client, fixture, base_url="https://fixture.invalid/v1",
                                     model="requested-fixture-model", provider="mimo",
                                     api_key="synthetic-test-key", max_tokens=128)
        self.assertEqual(len(observed_bodies), 1)
        self.assertEqual(result["served_model"], "served-fixture-model")
        self.assertEqual(result["accuracy"]["correct"], 1)
        self.assertTrue(result["valid_shape"])


if __name__ == "__main__":
    unittest.main()
