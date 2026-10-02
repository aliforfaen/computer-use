# 17 — Local dynamic-wait baseline

**2026-10-01 · M4a implementation and virtual host baseline.** This records
the local screenshot-change wait primitive and its synthetic Firefox fixture.
It is not a vision-provider, Jev, OCR or primary-agent comparison.

## Implementation and test scope

`wait_watcher.py` reuses `ObservationAdapter` for full app images and the
existing `Reader` interface for an optional structured judgment. It allows
one judgment in flight, coalesces to the latest frame, debounces positive
judgments, and observes deadlines and cancellation between callbacks. A
region in `WaitSpec` is context only: `AdapterFrameSource` still captures the
whole app so an unrelated dialog remains visible. Capture/reader callbacks
must provide their own finite timeouts; Python cannot preempt a blocked call.

`wait_benchmark.py` runs each case in a fresh Firefox virtual session and
private profile. The page remains pending until the host captures and checks
the loading baseline, then arms its transition timer. The fixture reports
loading→ready, loading→error, no change, animation noise and an unexpected
dialog outside the watched area. A loopback event endpoint records event
receipts independently of the visual judge. The visual judge is a local
pixel-difference classifier shared by both arms. The arms are fixed screenshot
polling and the bounded watcher; there is no model in either arm.

Full validation:

```bash
uv run python -m unittest discover -v
```

At the M4a local-validation checkpoint, before the later paid-probe code and
test additions, the full suite passed 77 tests. M4a tests cover supported
reader schema, wake debounce, serialization/coalescing, deadlines/cancellation,
whole-app capture, fixture events, the loading/ready/error palette and
screenshot diff threshold at fixture geometry. The paid-probe changes were
reviewed, fixed and tested on 2026-10-02; the suite is now **111 tests** after a
second-pass review (see the M4b section below).

Run the local no-provider comparison with:

```bash
uv run python wait_benchmark.py --seed 17 --repetitions 1 --max-trials 10
```

## Host run

The corrected run used seed 17, one repetition and all ten case/arm
combinations in randomized order. It ran on cachy with `kwin-mcp 0.10.0`,
KWin 6.7.5, Firefox 157.0 and AT-SPI 2.60.7. The persisted JSONL is
`run/wait-benchmark/manifest.jsonl` (ignored local output).

| Arm | Correct classifications | False wakes | Missed deadlines | Captures / local judgments | Median event-receipt→decision delay* | Median wait elapsed |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Screenshot polling | 5/5 | 0 | 0 | 28 / 23 | 280 ms (4 transitions) | 1,475 ms |
| Local watcher | 5/5 | 0 | 0 | 34 / 29 | 787 ms (4 transitions) | 2,145 ms |

Each arm had one trial per fixture case. Both no-change trials correctly
returned timeout at the 5-second deadline; the polling trial returned at
5,283 ms because its in-progress capture completed after the deadline. Those
trials have no transition delay. All ten virtual sessions and temporary
profiles reported successful cleanup. The initial full run classified the
ready, dialog, animation and no-change cases correctly but missed both error
states (8/10). Host inspection measured the loading-to-error grayscale change
at 24, below the then-threshold of 32. Lowering the threshold to 10 and adding
a fixture-geometry regression fixed it. A separate error-only recheck passed
both arms (polling 342 ms, watcher 460 ms; ignored local artifact
`run/wait-error-recheck/summary.jsonl`); then the corrected randomized run
passed all ten cases. The recheck and full run are distinct; it is not extra
replication of every case.

*The page posts a status event after updating the DOM and two animation-frame
callbacks. The server's monotonic HTTP receipt is independent of the image
judge, but it is a timing bound, not an exact display-paint timestamp. Delay
is measured from that receipt to the arm's decision time, not from a proven
first visible pixel.

## Limits and next comparison

This is one randomized repetition on a local deterministic image classifier.
It does not establish a performance advantage: in this run, the watcher had
longer event-receipt-to-decision delay than polling. Primary-agent turns and
tokens are null/not measured; paid backend requests were zero. The fixture
does not use OCR, Jev, a vision provider, or a real task-running agent, and
does not evaluate end-to-end task success. Color and geometry thresholds are
fixture-specific, not general app grounding.

## DeepSeek heartbeat probe status

The owner authorized one bounded DeepSeek heartbeat probe under $1. The first
run produced no usable result: an SSE response kept delivering comment
keepalives for more than four minutes, while the configured 40-second HTTP
timeout limited only idle reads. SIGINT cleanup was requested; the runner had
caught `BaseException`, so it advanced into another fixture case before the
remaining owned process was stopped and cleanup verified. The old runner had
no flushed progress journal, so exact provider attempt count and token usage
were lost. Do not infer a successful heartbeat or treat its output as a
validated result.

At peak cache-miss prices and the local conservative per-attempt ceiling of
$0.0017664, reserve all 12 possible attempts from that run ($0.0211968) as
potentially charged; the actual charge is unknown. This is a budget reserve,
not an observed bill. The coordinator configured at most one additional
provider request, giving a conservative 13-attempt total reserve of
$0.0229632. Any later usage report shows actual returned token usage when
available and keeps the interrupted request's charge explicitly unknown.

## M4b review, fixes and single follow-up (2026-10-02)

The owner resumed on 2026-10-02 and the frozen runner was independently
reviewed, tested and fixed:

- **Bug fixed:** `run()` referenced an undefined
  `PRIOR_INTERRUPTED_RUN_RESERVE_USD` and raised `NameError` at the end of every
  non-blocked run. It is now a frozen constant derived from the 12-attempt
  ceiling, so the prior-run reserve and the follow-up cap stay consistent.
- **Bound initially overstated (later corrected):** the first pass passed the
  read/idle timeout per request, but it still parsed with
  `response.iter_lines()`. `iter_lines` buffers bytes until a newline, so a
  peer that trickles bytes without newlines keeps the idle read timeout from
  firing while the parser never regains control. The claimed strict 30 s + 5 s
  bound was therefore false for newline-free trickles; see the second-pass fix
  below.
- **Interrupt path:** the reader and case runner catch `Exception`, not
  `BaseException`, so `KeyboardInterrupt` propagates after the case's
  session/profile cleanup. The pre-attempt record is fsynced before the provider
  call and survives an interrupted attempt whose usage remains unknown.

### Second-pass review and fix (2026-10-02)

Codex reviewed the first-pass claim and reproduced the flaw with a loopback
server: 20 `:` bytes flushed 50 ms apart with the newline only at the end
(`timeout_seconds=0.15`, `total_timeout_seconds=0.2`) produced
`stream_total_timeout` at 1,002.98 ms instead of ~350 ms. An unbounded byte
trickle could likewise grow the parser buffer past `max_response_chars` before
any newline arrived.

The parser now consumes raw `response.iter_bytes()` chunks and enforces the
absolute deadline and the byte cap on every chunk, including partial lines,
with incremental UTF-8 decoding. Body-phase bound: `total_timeout_seconds`
checked between chunks, plus at most one blocked read bounded by
`timeout_seconds`. It does **not** claim a strict wall-clock bound across the
header phase: `httpx` returns from `stream()` only once headers are complete,
and the read/idle timeout bounds idle gaps but not a peer that trickles header
bytes. A framing layer that buffers an incomplete HTTP transfer chunk can also
delay delivery; there the idle read timeout, not the per-chunk deadline, is
the bound.

Fourteen offline tests were added across the two passes, and the current suite
is **111 tests, all passing**. Reader coverage now includes comment-only SSE
total deadline, newline-free byte trickle cut at the total deadline,
partial-line bytes hitting the size cap before any newline, split multi-byte
UTF-8 with a missing final newline, a stalled loopback read bounded by the read
timeout, content arriving after the total deadline, delayed-header idle
timeout, unsupported `iter_lines`-only transport, interrupt propagation with no
second provider request, fsynced pre-call journaling observed during the call,
interrupted-run partial persistence, and owned-session/profile cleanup
including engine-construction failure, partial `session_start`, and journal
failure. The probe also refuses to reuse a non-empty output directory, so an
existing paid result (including `run/vision-wait-probe-followup`) cannot be
truncated or overwritten, and setup failures now close an already-created
reader or server.

The single configured follow-up request (made before the second-pass parser
fix) ran with `max_calls = 1` on the `ready` fixture on cachy (KWin 6.7.5,
`kwin-mcp` 0.10.0, Firefox 157.0, AT-SPI 2.60.7). It returned on a normal
newline-terminated stream, so its result is valid; it did not exercise the
newline-free trickle path, so the strengthened bound rests on the offline
regressions rather than on a paid reproduction. The judge returned `ready`,
matching the independent fixture event.

| Field | Result |
| --- | --- |
| Status | `completed`; `acceptance_passed` true |
| Provider / served model | deepseek / `deepseek-flash` |
| Latency | 1,414 ms |
| Returned usage | 821 prompt (0 cache-hit, 821 cache-miss) + 8 completion = 829 tokens |
| Observed upper cost (peak cache-miss) | $0.0002559 |
| Rate limited | No |
| Session/profile cleanup | Passed (known driver broken-pipe warning only) |
| Primary-agent turns/tokens | null / not measured |

Cost accounting: the follow-up's $0.0002559 is a peak cache-miss upper bound
from returned usage; it is the only observed provider charge in this
checkpoint. The earlier interrupted run's charge is still unknown, so the
whole-session reserve remains $0.0229632 (12 prior attempts at $0.0017664 each
plus this one at $0.0017664). The follow-up cap is now consumed; no further
provider request may be made without fresh authorization. The single
successful judgment validates the heartbeat/transport path; it is not an
acceleration or primary-agent comparison.

Pricing and image-size assumptions are based on the [DeepSeek Vision guide](https://api-docs.deepseek.com/guides/vision/)
and [official Models & Pricing page](https://api-docs.deepseek.com/quick_start/pricing/).
Rechecked 2026-10-02: `deepseek-flash` cache-miss peak is $0.30/M input and
$1.20/M output, and the image upper bound is 1,024 tokens, matching the frozen
reserve. Prices may change; the provider's returned usage is the source for
observed token counts. This probe remains separate from the no-provider
pixel-diff baseline. OCR+Jev and an end-to-end primary-agent comparison remain
future work.
