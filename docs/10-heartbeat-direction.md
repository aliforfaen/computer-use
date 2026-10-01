# 10 — Screenshot agent and optional heartbeat

**2026-10-01 · owner-supported direction; implementation and performance unproven.**

## Recommendation

Build a usable screenshot-driven computer-use session over `kwin-mcp`, with
virtual sessions by default, explicit live sessions, and the existing autonomy
modes. Agents connect locally or through Tailscale. The primary vision agent
owns task interpretation and recovery. Jev is an optional accelerator whose
place is decided by measurements.

Reuse `kwin-mcp` for KWin sessions, EIS input and screenshots; reuse AT-SPI
for structured targets and events where available. The [structured observation
options](09-observation-options.md) remain useful. Screenshot perception and
precise action targeting are distinct concerns: define and test the grounding
interface before broad desktop execution. Jev continues to select only
code-enumerated options; it never supplies coordinates or executable text.

## Waiting without repeated agent turns

The primary agent registers a wait: what it expects, a relevant region or
window, and a deadline. A local watcher collects fresh evidence while the agent
is suspended. It returns `ready`, `unexpected`, `timeout`, or `cancelled`,
with evidence and a current screenshot when requested. An unexpected dialog
or substantial change outside the watched region must also be able to wake it.

Prefer accessible text/events when available. Otherwise compare locally
captured regions and inspect meaningful changes with one of two backends:

- **Vision heartbeat:** a fast vision model judges the changed image against
  the registered wait condition.
- **OCR + Jev:** local OCR extracts changed text; code handles exact expected
  matches, and Jev chooses `keep_waiting`, `wake_agent`, or `unexpected_state`
  for ambiguous text evidence.

Debounce noisy OCR and animation; bound polling, model calls and wait duration.
Keep one judgment in flight and evaluate the latest evidence afterward.
A change detector only reports change, not success. OCR misses non-text
signals, and neither model can detect completion from an old observation.
Capture and judgment timing must be measured separately. Suppressing expensive
agent turns can save work even if local screenshot capture still polls.

Jev's [documented strengths](https://docs.typesafe.ai/introduction) are bounded
judgments. [Jev Ultrafast](https://github.com/browser-use/jev-ultrafast/blob/main/docs/performance.md)
already uses event-based waits before selection. These support the design
pattern; they do not establish a desktop performance improvement.

## Benchmark before selecting the accelerator

Compare the same primary agent and task across three arms:

| Arm | Wait behavior |
| --- | --- |
| Baseline | Primary agent waits, screenshots and interprets repeatedly. |
| Vision heartbeat | Local change watcher plus the owner's fast vision endpoint. |
| OCR + Jev heartbeat | Same watcher plus local OCR and TypeSafe direct. |

Use identical reset states and randomized arm order over multiple repetitions.
Start with controlled fixtures: delayed text results, a spinner disappearing,
and an unexpected error. Follow with a real app task. Record actual state
transition time so detection delay and missed events have ground truth.

Measure task success and elapsed time; primary-agent turns and tokens; all
backend calls, tokens and cost; capture/OCR/judgment latency; false wakes,
missed events and detection delay. Count retries and timeouts. Report medians
and tail latency. A faster incorrect wake is not a win.

The owner reports using a vision model described as DeepSeek v4.1 Flash at
about 200 output tokens/s. Exact provider, model ID, image support and pricing
must be confirmed before the comparison. Output throughput does not establish
image request latency. Earlier synthetic direct Jev calls had a 254 ms median;
this excludes capture/OCR and is not a heartbeat benchmark.

## Later accelerators

Recorded workflows can store recognizable states, semantic targets, expected
effects and recovery exits. Jev may select a workflow or its next branch;
the primary agent resumes when observations no longer fit. A demonstration
does not train Jev or establish reliable replay. Test workflow acceleration
after the basic session and heartbeat comparison work.

## Decisions still needed

Where the primary agent runs (on `cachy` or in the connecting client), its
model/provider, and the screenshot-to-action grounding contract remain open.
Local session ownership, locking, verification, audit and cancellation stay
on `cachy`. A client running the vision loop must explicitly request images;
the existing metadata-only remote default still applies. The local watcher
must be available independently of where the primary agent runs.

Next implementation milestone: a disposable virtual-session heartbeat fixture
and benchmark harness, followed by the minimal usable session surface. Broad
server implementation remains subject to the repository's P0 review gate.
