from __future__ import annotations

import json
import os
import unittest

import httpx

from vision_reader import ReaderConfig, VisionReader, build_request_body, parse_stream
import vision_benchmark as vb


def sse_response(content: str, *, finish_reason="stop", done=True):
    events = [
        {"model": "served-test", "choices": [{"delta": {"content": content}, "finish_reason": finish_reason}]},
        {"usage": {"prompt_tokens": 4, "completion_tokens": 2, "secret": "do not expose"}, "choices": []},
    ]
    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
    if done:
        body += "data: [DONE]\n\n"
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


class VisionReaderTests(unittest.TestCase):
    def setUp(self):
        self.env_before = os.environ.get("TEST_VISION_KEY")
        os.environ["TEST_VISION_KEY"] = "secret-key"
        self.questions = [{"field": "value", "type": "string", "description": "Read the visible value."}]

    def tearDown(self):
        if self.env_before is None:
            os.environ.pop("TEST_VISION_KEY", None)
        else:
            os.environ["TEST_VISION_KEY"] = self.env_before

    def reader(self, response):
        def handler(request):
            self.assertEqual(request.headers["authorization"], "Bearer secret-key")
            payload = json.loads(request.content)
            self.assertTrue(payload["stream"])
            self.assertEqual(payload["messages"][0]["content"][1]["image_url"]["url"], "data:image/png;base64,aW1hZ2U=")
            return response
        client = httpx.Client(transport=httpx.MockTransport(handler))
        return VisionReader(ReaderConfig(key_env="TEST_VISION_KEY"), client=client), client

    def test_valid_stream_with_usage(self):
        reader, client = self.reader(sse_response('{"value":"1"}'))
        try:
            result = reader.interpret(b"image", self.questions)
        finally:
            client.close()
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.data, {"value": "1"})
        self.assertEqual(result.usage, {"prompt_tokens": 4, "completion_tokens": 2})
        self.assertEqual(result.served_model, "served-test")

    def test_nullable_is_explicit_uncertainty(self):
        reader, client = self.reader(sse_response('{"value":null}'))
        try:
            result = reader.interpret(b"image", [{**self.questions[0], "nullable": True}])
        finally:
            client.close()
        self.assertEqual(result.status, "uncertain")
        self.assertEqual(result.uncertainty, "reader_returned_nullable_value")

    def test_invalid_shape_and_duplicate_questions(self):
        reader, client = self.reader(sse_response('{"other":"1"}'))
        try:
            result = reader.interpret(b"image", self.questions)
            duplicate = reader.interpret(b"image", [self.questions[0], self.questions[0]])
        finally:
            client.close()
        self.assertEqual(result.status, "invalid_response")
        self.assertEqual(result.error, "unexpected_or_missing_fields")
        self.assertEqual(duplicate.error, "invalid_questions")

    def test_bad_stream_is_safe_and_explicit(self):
        reader, client = self.reader(sse_response('{"value":"1"}', done=False))
        try:
            result = reader.interpret(b"image", self.questions)
        finally:
            client.close()
        self.assertEqual(result.error, "stream_missing_done")
        self.assertNotIn("secret-key", repr(result))

    def test_configuration_bounds(self):
        with self.assertRaises(ValueError):
            ReaderConfig(timeout_seconds=float("inf"))
        with self.assertRaises(ValueError):
            ReaderConfig(max_tokens=257)

    def test_shared_body_builder_matches_provider_payload_rules(self):
        deepseek = build_request_body(b"image", "read it", provider="deepseek", model="d", max_tokens=32)
        mimo = build_request_body(b"image", "read it", provider="mimo", model="m", max_tokens=32)
        generic = build_request_body(b"image", "read it", provider="generic", model="g", max_tokens=32)
        self.assertEqual(deepseek["thinking"], {"type": "disabled"})
        self.assertEqual(mimo["max_completion_tokens"], 32)
        self.assertNotIn("max_tokens", mimo)
        self.assertNotIn("thinking", generic)
        self.assertEqual(deepseek["messages"][0]["content"][1], mimo["messages"][0]["content"][1])

    def test_benchmark_wrapper_and_reader_share_sse_parser(self):
        lines = [
            'data: {"model":"shared","choices":[{"delta":{"content":"{\\"value\\":1}"},"finish_reason":"stop"}]}',
            'data: {"choices":[],"usage":{"prompt_tokens":3,"private":"discard"}}',
            "data: [DONE]",
        ]

        class Response:
            def iter_lines(self):
                return iter(lines)

        first = []
        parsed = parse_stream(Response(), lambda: first.append(True))
        second_first = []
        benchmark = vb._sse_content(Response(), lambda: second_first.append(True))
        self.assertEqual(parsed, benchmark)
        self.assertEqual(parsed[0], '{"value":1}')
        self.assertEqual(parsed[1], {"prompt_tokens": 3})
        self.assertEqual(first, [True])
        self.assertEqual(second_first, [True])

    def test_shared_stream_size_limit_is_reported(self):
        reader, client = self.reader(sse_response('{"value":"long response"}'))
        reader.config = ReaderConfig(key_env="TEST_VISION_KEY", max_response_chars=8)
        try:
            result = reader.interpret(b"image", self.questions)
        finally:
            client.close()
        self.assertEqual(result.error, "response_too_large")


if __name__ == "__main__":
    unittest.main()
