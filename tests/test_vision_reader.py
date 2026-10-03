from __future__ import annotations

import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from vision_reader import (BoundedReader, IsolatedVisionReader, ReaderConfig, ReaderResult,
                           StreamParseError, VisionReader, build_request_body, parse_stream)


class _SseTestServer:
    """Minimal loopback SSE server used to exercise real transport timeouts."""

    def __init__(self, script):
        self._script = script
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

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
                finally:
                    # The client closes as soon as it has what it needs; do not
                    # try to read a second keep-alive request from a dead socket.
                    self.close_connection = True

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
            def iter_bytes(self):
                yield b"data: {}\n"
                now[0] = 2.0
                yield b"data: {}\n"

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

    def test_bounded_reader_stops_at_call_cap_and_sums_reported_usage(self):
        class Fake:
            def __init__(self): self.calls = 0
            def capabilities(self): return {"provider": "deepseek", "model": "deepseek-flash"}
            def interpret(self, _image, _questions):
                self.calls += 1
                return ReaderResult("ok", data={"value": "x"}, provider="deepseek", model="deepseek-flash",
                                    usage={"prompt_tokens": 7, "completion_tokens": 2}, latency_ms=11.0,
                                    owner_elapsed_ms=13.5)

        fake = Fake()
        reader = BoundedReader(fake, 1)
        self.assertEqual(reader.interpret(b"image", self.questions).status, "ok")
        denied = reader.interpret(b"image", self.questions)
        self.assertEqual((denied.status, denied.error), ("error", "reader_call_budget_exceeded"))
        self.assertEqual(fake.calls, 1)
        self.assertEqual(reader.summary()["reported_usage"], {"prompt_tokens": 7, "completion_tokens": 2})
        self.assertEqual(reader.summary()["latency_total_ms"], 11.0)
        self.assertEqual(reader.summary()["provider_latency_total_ms"], 11.0)
        self.assertEqual(reader.summary()["owner_elapsed_total_ms"], 13.5)

    def test_bounded_reader_latches_429_without_retrying_or_spending_more_calls(self):
        class Fake:
            def __init__(self): self.calls = 0
            def capabilities(self): return {"provider": "deepseek", "model": "deepseek-flash"}
            def interpret(self, _image, _questions):
                self.calls += 1
                return ReaderResult("error", error="http_status_429", provider="deepseek", model="deepseek-flash",
                                    latency_ms=3.0)

        fake = Fake()
        reader = BoundedReader(fake, 5)
        first = reader.interpret(b"image", self.questions)
        second = reader.interpret(b"image", self.questions)
        self.assertEqual(first.error, "http_status_429")
        self.assertEqual(second.error, "provider_rate_limited")
        self.assertEqual(fake.calls, 1)
        self.assertEqual(reader.summary()["calls_used"], 1)
        self.assertTrue(reader.summary()["rate_limited"])

    def test_isolated_reader_completes_normal_local_response_and_reaps_worker(self):
        server = _SseTestServer(lambda handler: (handler.send_response(200),
            handler.send_header("Content-Type", "text/event-stream"), handler.end_headers(),
            handler.wfile.write(sse_response('{"value":"ready"}').content), handler.wfile.flush()))
        reader = IsolatedVisionReader(ReaderConfig(base_url=server.base_url, key_env="TEST_VISION_KEY",
            timeout_seconds=0.5, total_timeout_seconds=0.5), hard_timeout_seconds=2.0)
        try:
            result = reader.interpret(b"image", self.questions)
        finally:
            server.close()
        self.assertEqual((result.status, result.data), ("ok", {"value": "ready"}))
        self.assertIsNotNone(reader.last_child_pid)
        self.assertEqual(reader.last_child_exitcode, 0)
        self.assertIsNotNone(result.owner_elapsed_ms)
        self.assertGreaterEqual(result.owner_elapsed_ms, result.latency_ms)

    def test_isolated_reader_hard_deadline_covers_slow_dripped_headers(self):
        stop = threading.Event()

        def script(handler):
            handler.wfile.write(b"HTTP/1.1 200 OK\r\n")
            handler.wfile.flush()
            for byte in b"Content-Type: text/event-stream\r\nContent-Length: 100\r\n\r\n":
                if stop.is_set():
                    return
                handler.wfile.write(bytes([byte]))
                handler.wfile.flush()
                time.sleep(0.03)

        server = _SseTestServer(script)
        reader = IsolatedVisionReader(ReaderConfig(base_url=server.base_url, key_env="TEST_VISION_KEY",
            timeout_seconds=0.5, total_timeout_seconds=0.5), hard_timeout_seconds=0.35, poll_seconds=0.02)
        started = time.monotonic()
        try:
            result = reader.interpret(b"image", self.questions)
        finally:
            stop.set()
            server.close()
        elapsed = time.monotonic() - started
        self.assertEqual(result.error, "request_deadline_exceeded")
        self.assertLess(elapsed, 1.5)
        self.assertIsNotNone(result.owner_elapsed_ms)
        self.assertIsNotNone(reader.last_child_pid)
        self.assertIsNotNone(reader.last_child_exitcode)

    def test_isolated_reader_cancellation_kills_and_joins_in_flight_request(self):
        entered = threading.Event()
        stop = threading.Event()

        def script(handler):
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.end_headers()
            entered.set()
            while not stop.is_set():
                handler.wfile.write(b": keep-alive\n\n")
                handler.wfile.flush()
                time.sleep(0.03)

        server = _SseTestServer(script)
        cancel = threading.Event()
        isolated = IsolatedVisionReader(ReaderConfig(base_url=server.base_url, key_env="TEST_VISION_KEY",
            timeout_seconds=1.0, total_timeout_seconds=10.0))
        reader = BoundedReader(isolated, 1, cancel_event=cancel)
        result_box = []
        thread = threading.Thread(target=lambda: result_box.append(reader.interpret(b"image", self.questions)))
        thread.start()
        try:
            self.assertTrue(entered.wait(2.0))
            cancel.set()
            thread.join(2.0)
        finally:
            stop.set()
            server.close()
        self.assertFalse(thread.is_alive())
        self.assertEqual(result_box[0].error, "cancelled")
        self.assertIsNotNone(isolated.last_child_pid)
        self.assertIsNotNone(isolated.last_child_exitcode)
        self.assertEqual(reader.summary()["calls_used"], 1)

    def test_concurrent_interpretation_waits_for_429_latch_before_any_second_request(self):
        entered = threading.Event()
        second_started = threading.Event()
        release = threading.Event()

        class Fake:
            def __init__(self): self.calls = 0
            def capabilities(self): return {"provider": "deepseek", "model": "deepseek-flash"}
            def interpret(self, _image, _questions):
                self.calls += 1
                entered.set()
                release.wait(1)
                return ReaderResult("error", error="http_status_429", provider="deepseek", model="deepseek-flash")

        fake = Fake()
        reader = BoundedReader(fake, 5)
        results = []
        first = threading.Thread(target=lambda: results.append(reader.interpret(b"image", self.questions)))
        def call_second():
            second_started.set()
            results.append(reader.interpret(b"image", self.questions))
        second = threading.Thread(target=call_second)
        first.start()
        self.assertTrue(entered.wait(0.5))
        second.start()
        self.assertTrue(second_started.wait(0.5))
        time.sleep(0.02)
        release.set()
        first.join(1)
        second.join(1)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(fake.calls, 1)
        self.assertCountEqual([result.error for result in results], ["http_status_429", "provider_rate_limited"])

    def test_shared_stream_size_limit_is_reported(self):
        reader, client = self.reader(sse_response('{"value":"long response"}'))
        reader.config = ReaderConfig(key_env="TEST_VISION_KEY", max_response_chars=8)
        try:
            result = reader.interpret(b"image", self.questions)
        finally:
            client.close()
        self.assertEqual(result.error, "response_too_large")

    def test_partial_line_bytes_hit_size_cap_before_any_newline(self):
        class TrickleResponse:
            def iter_bytes(self):
                yield b":" * 64
                yield b":" * 64

        with self.assertRaisesRegex(StreamParseError, "response_too_large"):
            parse_stream(TrickleResponse(), max_response_chars=32)

    def test_response_without_chunk_transport_is_rejected(self):
        class LegacyResponse:
            def iter_lines(self):
                return iter([])

        with self.assertRaisesRegex(StreamParseError, "unsupported_stream_transport"):
            parse_stream(LegacyResponse())

    def test_split_utf8_and_missing_final_newline_parse_correctly(self):
        event = json.dumps({"choices": [{"delta": {"content": "café"}, "finish_reason": "stop"}]},
                           ensure_ascii=False)
        raw = f"data: {event}\n".encode("utf-8") + b"data: [DONE]"  # no trailing newline
        split_at = raw.index("é".encode("utf-8")) + 1  # inside the two-byte sequence

        class Chunked:
            def iter_bytes(self):
                yield raw[:split_at]
                yield raw[split_at:split_at + 5]
                yield raw[split_at + 5:]

        first = []
        content, usage, finish, served_model = parse_stream(Chunked(), lambda: first.append(True))
        self.assertEqual(content, "café")
        self.assertEqual(finish, "stop")
        self.assertEqual(first, [True])

    def _sse_reader(self, base_url, *, timeout_seconds, total_timeout_seconds):
        # trust_env=False keeps the loopback test hermetic; the per-request
        # timeout override makes the configured read bound authoritative.
        client = httpx.Client(timeout=httpx.Timeout(timeout_seconds), trust_env=False)
        reader = VisionReader(ReaderConfig(base_url=base_url, key_env="TEST_VISION_KEY",
                                           timeout_seconds=timeout_seconds,
                                           total_timeout_seconds=total_timeout_seconds), client=client)
        return reader, client

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

    def test_newline_free_byte_trickle_is_cut_by_total_deadline(self):
        # Regression for the iter_lines flaw: 20 bytes are flushed 50 ms apart
        # with no newline until the end. Frequent bytes keep the idle read
        # timeout from firing, but the chunk-level absolute deadline must cut
        # the stream near total_timeout_seconds, not when the newline arrives
        # (the old parser needed ~1 s here).
        def script(handler):
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.end_headers()
            for _ in range(20):
                handler.wfile.write(b":")
                handler.wfile.flush()
                time.sleep(0.05)
            handler.wfile.write(b"\n")
            handler.wfile.flush()

        server = _SseTestServer(script)
        reader, client = self._sse_reader(server.base_url, timeout_seconds=0.15, total_timeout_seconds=0.2)
        try:
            result = reader.interpret(b"image", self.questions)
        finally:
            client.close()
            server.close()
        self.assertEqual(result.error, "stream_total_timeout")
        self.assertLess(result.latency_ms, 700.0)

    def test_delayed_headers_are_bounded_by_read_timeout(self):
        # The body deadline only starts being enforced once stream() returns.
        # A peer that delays headers entirely is still bounded by the read/idle
        # timeout; a peer that trickles header bytes is a documented residual
        # limitation (httpx exposes no total header deadline to the caller).
        def script(handler):
            time.sleep(0.6)
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.end_headers()
            handler.wfile.write(b"data: [DONE]\n\n")
            handler.wfile.flush()

        server = _SseTestServer(script)
        reader, client = self._sse_reader(server.base_url, timeout_seconds=0.2, total_timeout_seconds=5.0)
        try:
            result = reader.interpret(b"image", self.questions)
        finally:
            client.close()
            server.close()
        self.assertEqual(result.error, "request_timeout")
        self.assertLess(result.latency_ms, 1500.0)


if __name__ == "__main__":
    unittest.main()
