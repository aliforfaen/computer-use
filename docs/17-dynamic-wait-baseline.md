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
screenshot diff threshold at fixture geometry. Paid-probe changes are pending
the next review; do not treat 77 as their validation count.

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
not an observed bill. Further provider work is blocked until elapsed-stream
timeouts, per-attempt progress journaling and interrupt propagation are tested.
The coordinator configured at most one additional provider request, giving
a conservative 13-attempt total reserve of $0.0229632. Any later usage report
must show actual returned token usage when available and keep the interrupted
request's charge explicitly unknown. The owner paused work for the evening;
do not run tests, host probes, code reviews or provider calls until the owner
resumes. The frozen runner has a 5-second idle timeout, 30-second total stream
deadline and 40-second watch limit, plus fsynced pre-attempt/lifecycle
journaling and KeyboardInterrupt propagation. These changes are not reviewed
or retested; they are not cleared for the one follow-up request.

Pricing and image-size assumptions are based on the [DeepSeek Vision guide](https://api-docs.deepseek.com/guides/vision/)
and [official Models & Pricing page](https://api-docs.deepseek.com/quick_start/pricing/).
Prices may change; the provider's returned usage is the source for observed
token counts. This probe remains separate from the no-provider pixel-diff
baseline. OCR+Jev and an end-to-end primary-agent comparison remain future
work.
