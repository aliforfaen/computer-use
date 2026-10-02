from __future__ import annotations

import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from vision_reader import ReaderConfig, StreamParseError, VisionReader, build_request_body, parse_stream
import vision_benchmark as vb


class _SseTestServer:
    """Minimal loopback SSE server used to exercise real transport timeouts."""

    def __init__(self, script):
        self._script = script
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                if length:
                    self.rfile.read(length)
                try:
                    outer._script(self)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.block_on_close = False
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="sse-test-http", daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)


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
            ReaderConfig(total_timeout_seconds=0)
        with self.assertRaises(ValueError):
            ReaderConfig(max_tokens=257)

    def test_stream_absolute_deadline_stops_even_when_lines_keep_arriving(self):
        now = [0.0]

        class SlowResponse:
            def iter_lines(self):
                yield "data: {}"
                now[0] = 2.0
                yield "data: {}"

        with self.assertRaisesRegex(Exception, "stream_total_timeout"):
            parse_stream(SlowResponse(), deadline_monotonic=1.0, clock=lambda: now[0])

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

    def test_comment_only_lines_with_fake_clock_exceed_total_deadline(self):
        now = [0.0]

        class CommentResponse:
            def iter_lines(self):
                yield ": keep-alive"
                now[0] = 5.0
                yield ": keep-alive"

        with self.assertRaisesRegex(StreamParseError, "stream_total_timeout"):
            parse_stream(CommentResponse(), deadline_monotonic=1.0, clock=lambda: now[0])

    def _sse_reader(self, base_url, *, timeout_seconds, total_timeout_seconds):
        # trust_env=False keeps the loopback test hermetic; the per-request
        # timeout override makes the configured read bound authoritative.
        client = httpx.Client(timeout=httpx.Timeout(timeout_seconds), trust_env=False)
        reader = VisionReader(ReaderConfig(base_url=base_url, key_env="TEST_VISION_KEY",
                                           timeout_seconds=timeout_seconds,
                                           total_timeout_seconds=total_timeout_seconds), client=client)
        return reader, client

    def test_comment_only_keepalive_is_cut_by_total_deadline(self):
        stop = threading.Event()

        def script(handler):
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.end_headers()
            while not stop.is_set():
                handler.wfile.write(b": keep-alive\n\n")
                handler.wfile.flush()
                time.sleep(0.03)

        server = _SseTestServer(script)
        reader, client = self._sse_reader(server.base_url, timeout_seconds=1.0, total_timeout_seconds=0.4)
        try:
            result = reader.interpret(b"image", self.questions)
        finally:
            stop.set()
            client.close()
            server.close()
        self.assertEqual(result.error, "stream_total_timeout")
        # Total deadline plus at most one bounded read, with scheduling margin.
        self.assertLess(result.latency_ms, 1800.0)

    def test_stalled_transport_read_is_bounded_by_read_timeout(self):
        def script(handler):
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.end_headers()
            handler.wfile.write(b": keep-alive\n\n")
            handler.wfile.flush()
            time.sleep(2.0)

        server = _SseTestServer(script)
        reader, client = self._sse_reader(server.base_url, timeout_seconds=0.3, total_timeout_seconds=5.0)
        try:
            result = reader.interpret(b"image", self.questions)
        finally:
            client.close()
            server.close()
        self.assertEqual(result.error, "request_timeout")
        # Bounded by the configured read timeout, not the stall length.
        self.assertLess(result.latency_ms, 1500.0)

    def test_content_arriving_after_total_deadline_is_rejected(self):
        def script(handler):
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.end_headers()
            handler.wfile.write(b": keep-alive\n\n")
            handler.wfile.flush()
            time.sleep(0.6)
            handler.wfile.write(b'data: {"model":"late","choices":[{"delta":{"content":"{\\"value\\":\\"1\\"}"},"finish_reason":"stop"}]}\n\n')
            handler.wfile.write(b"data: [DONE]\n\n")
            handler.wfile.flush()

        server = _SseTestServer(script)
        reader, client = self._sse_reader(server.base_url, timeout_seconds=5.0, total_timeout_seconds=0.3)
        try:
            result = reader.interpret(b"image", self.questions)
        finally:
            client.close()
            server.close()
        self.assertEqual(result.error, "stream_total_timeout")
        self.assertLess(result.latency_ms, 1800.0)


if __name__ == "__main__":
    unittest.main()
