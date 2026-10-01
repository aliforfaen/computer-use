"""Focused validation tests for the reusable vision benchmark."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import httpx

import vision_benchmark as vb


class FakeResponse:
    def __init__(self, lines):
        self.lines = lines

    def iter_lines(self):
        return iter(self.lines)


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

    def test_stream_requires_done_and_stop_finish_reason(self):
        seen = []
        good = FakeResponse([
            "data: " + json.dumps({"model": "fake-served-model", "choices": [{"delta": {"content": '{"x":'}}]}),
            "data: " + json.dumps({"choices": [{"delta": {"content": "1}"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 2}}),
            "data: [DONE]",
        ])
        content, usage, finish, served_model = vb._sse_content(good, lambda: seen.append(True))
        self.assertEqual(content, '{"x":1}')
        self.assertEqual(usage["prompt_tokens"], 10)
        self.assertEqual(finish, "stop")
        self.assertEqual(served_model, "fake-served-model")
        self.assertEqual(len(seen), 1)

        with self.assertRaisesRegex(vb.BenchmarkError, "stream_missing_done"):
            vb._sse_content(FakeResponse(["data: " + json.dumps({"choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}]})]), lambda: None)
        with self.assertRaisesRegex(vb.BenchmarkError, "stream_not_completed"):
            vb._sse_content(FakeResponse(["data: " + json.dumps({"choices": [{"delta": {"content": "x"}, "finish_reason": "length"}]}), "data: [DONE]"]), lambda: None)
        with self.assertRaisesRegex(vb.BenchmarkError, "malformed_stream_event"):
            vb._sse_content(FakeResponse(["data: []", "data: [DONE]"]), lambda: None)

    def test_fixture_hash_is_checked_and_scope_skips_are_reportable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = b"synthetic fixture bytes"
            (root / "a.png").write_bytes(image)
            manifest = {
                "cases": [
                    {"case_id": "app-case", "questions": [{"field": "ok", "type": "boolean", "description": "visible"}],
                     "expected_facts": {"ok": True}, "source_provenance": {"kind": "synthetic"},
                     "images": [{"path": "a.png", "scope": "app", "sha256": hashlib.sha256(image).hexdigest()}]},
                    {"case_id": "full-only", "questions": [{"field": "ok", "type": "boolean", "description": "visible"}],
                     "expected_facts": {"ok": True}, "images": [{"path": "a.png", "scope": "full", "sha256": hashlib.sha256(image).hexdigest()}]},
                ]
            }
            path = root / "manifest.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            chosen, skipped = vb._select_requests(manifest, path, "app")
            self.assertEqual([entry["case_id"] for entry in chosen], ["app-case"])
            self.assertEqual(skipped, ["full-only"])
            manifest["cases"][0]["images"][0]["sha256"] = "wrong"
            with self.assertRaisesRegex(vb.BenchmarkError, "fixture_image_hash_mismatch"):
                vb._select_requests(manifest, path, "app")

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

    def test_summary_tracks_accuracy_and_malformed_answers_separately(self):
        summary = vb._summary([
            {"status": "ok", "valid_shape": True, "valid_json": True, "accuracy": {"correct": 1, "total": 2}, "ttft_ms": 10, "attempt_ms": 20},
            {"status": "invalid_response", "valid_shape": False, "error": "wrong_field_type", "accuracy": {"correct": 0, "total": 1}},
            {"status": "error", "error": "http_status_503", "accuracy": {"correct": 0, "total": 1}},
        ])
        self.assertEqual(summary["macro_mean_field_accuracy"], 0.1667)
        self.assertEqual(summary["overall_field_accuracy"], {"correct": 1, "total": 4, "ratio": 0.25})
        self.assertEqual(summary["malformed_count"], 1)
        self.assertEqual(summary["error_count"], 1)
        self.assertEqual(summary["attempt_completion_ms"]["p95"], 20)
        self.assertEqual(summary["all_facts_correct_count"], 0)


if __name__ == "__main__":
    unittest.main()
