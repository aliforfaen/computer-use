# 13 — Runnable vision benchmark

## What it measures

Reuse `kwin-mcp==0.10.0` for isolated virtual Kate/Firefox sessions, KWin
geometry and screenshots; Pillow for mapped crops; httpx for a persistent
OpenAI-compatible HTTP connection. App-window images are the default.
The fixture driver performs fixed actions and verifies state with AT-SPI;
the model reads the captured image. This is a perception benchmark, not yet
an autonomous task or heartbeat benchmark.

Five cases cover an original three-line poem typed into Kate, a webpage
table with small text and a disabled button, and loading/ready/error webpage
states. The pages are local reproducible fixtures, with no external browsing.
Expected answers remain in the local scorer and are not sent to the model.
All desktop input stays in disposable virtual sessions.

## Run

From the repository root, with provider keys in the ignored `.env`:

```bash
# Capture once, with no paid model calls.
uv run --with kwin-mcp==0.10.0 --with Pillow --with httpx python vision_benchmark.py --capture-only --output run/fixtures

# DeepSeek baseline: five cases, two repetitions, ten calls.
uv run --with httpx python vision_benchmark.py --manifest run/fixtures/manifest.json --output run/deepseek --repetitions 2 --max-calls 10

# MiMo: exactly the same saved captures and questions.
uv run --with httpx python vision_benchmark.py --manifest run/fixtures/manifest.json --output run/mimo --provider mimo --base-url https://api.xiaomimimo.com/v1 --model mimo-v2.6-flash --key-env MIMO_API_KEY --repetitions 2 --max-calls 10
```

Both first-party presets disable thinking and request JSON. MiMo uses its
`max_completion_tokens` parameter; DeepSeek uses `max_tokens`. Configuration
is explicit: provider, base URL, model and credential variable. Selecting a
provider does not automatically replace the other flags. Other compatible
endpoints can use `--provider generic` with their own URL/model/key variable;
they must accept image input, JSON output and streamed Chat Completions.
[DeepSeek vision](https://api-docs.deepseek.com/guides/vision/),
[MiMo direct API example](https://mimo.mi.com/models/en-US/mimo-v2.6-flash).

The default is two repetitions in a seeded randomized order. `--max-calls`
refuses an oversized run before a paid request. There are no automatic
retries. Use `--scope full` for the source virtual session or `--scope crop`
for the fixture's optional region image; compare separately against the same
manifest. Preserve app scope as the baseline unless a crop earns its place.
The optional regions are fixture-specific: 520×160 around Kate's poem and
800×450 around the local webpage content. They are not a general browser
layout detector. Firefox is maximized in these fixtures, so its app image
equals the full virtual frame; the selected scope still resolves the app
window rather than arbitrarily capturing another surface.

## Results and limits

Each provider run writes append-only request JSONL and a summary JSON under
its output directory. Capture writes the image/manifest set and an event log.
The manifest records fresh window identity, source mapping, hashes,
versions, setup verification and capture time. Each evaluation reports served
model, usage, first-content latency, completion time, strict JSON/field shape,
and exact expected-fact checks. Failures and malformed/truncated streams
remain failed attempts. Model confidence does not determine success.

Request timings exclude fixture setup and capture; these are reported
separately. Reusing saved frames is useful for provider comparisons, but
does not measure capture-to-action task time. Small sample p95 values are
descriptive, not reliable production tail estimates. Token usage is not a
measured billing charge. No browser-specific DOM capture adapter is included.

Outputs contain only the known synthetic fixtures and their model answers;
keep them under ignored `run/`. Provider credentials and HTTP bodies are not
written to results. Do not use this fixture harness to capture arbitrary
live content. Ctrl+C cancels a run; capture sessions are stopped in `finally`.

Validation without model calls:

```bash
uv run --with httpx python -m unittest -v test_vision_benchmark.py
```

## First baseline and MiMo comparison

**2026-10-01, 13:51–13:54 UTC.** Thirty paid requests: two repetitions of
each app image per provider, then one repetition of each crop per provider.
DeepSeek completed before MiMo, with randomized case order within each run.
Both endpoints returned their requested model IDs. No HTTP/transport errors;
all outputs were syntactically valid JSON. One cropped DeepSeek reply had
the wrong fields. Provider runs were sequential, so changing service load
is a possible influence; this is a small initial sample, not a speed guarantee.

| Provider / scope | Calls | Exact facts | All-fact passes | Median first content | Median attempt completion | Maximum attempt |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| DeepSeek Flash / app | 10 | 28/30 | 8/10 | 1,113 ms | 1,241 ms | 1,565 ms |
| MiMo V2.6 Flash / app | 10 | 27/30 | 7/10 | 4,470 ms | 5,451 ms | 13,694 ms |
| DeepSeek Flash / crop | 5 | 11/15 | 3/5 | 837 ms | 1,078 ms | 1,263 ms |
| MiMo V2.6 Flash / crop | 5 | 15/15 | 5/5 | 8,772 ms | 9,384 ms | 20,548 ms |

First-content medians use schema-valid replies; attempt completion includes
every attempt, including the wrong-shape DeepSeek crop response.

Both providers correctly transcribed Kate's poem on both app repetitions.
Both incorrectly called the disabled Save button enabled on both app
repetitions. MiMo's remaining app mismatch was `E-17.` rather than `E-17`:
a punctuation mismatch under exact scoring, not a different reference.
DeepSeek's crop errors were the disabled button and a loading-page reply
of `{"type":"json_object"}` instead of answering the requested questions.
The scorer rejected that shape; it did not count valid JSON as success.

| Provider / scope | Total input tokens | Total output tokens | Mean input tokens per call |
| --- | ---: | ---: | ---: |
| DeepSeek / app | 6,928 | 246 | 693 |
| MiMo / app | 9,802 | 291 | 980 |
| DeepSeek / crop | 1,824 | 113 | 365 |
| MiMo / crop | 2,081 | 146 | 416 |

Cropping reduced mean input tokens by about 47% for DeepSeek and 58% for
MiMo. It did not consistently improve latency or recognition. Keep app
capture as the default and region capture as an optional capability.
Use AT-SPI properties for enabled/disabled state where available; image-only
judgment failed this subtle visual distinction in both baseline runs.

The five source captures took 135–141 ms each (median 137 ms), separately
from app launch/setup and image preparation. Measured software: KWin 6.7.5,
kwin-mcp 0.10.0, AT-SPI 2.60.7, Kate 26.08.1, Firefox 157.0. Each fixture
enabled accessibility only on its isolated bus; fresh Firefox profiles
suppressed first-run pages. All virtual sessions were stopped.

Baseline files are retained under ignored `run/fixtures`, `run/deepseek`,
`run/mimo`, `run/deepseek-crop` and `run/mimo-crop`. Correctness failures
produce exit code 1 while retaining the complete results; setup/configuration
errors produce exit code 2. DeepSeek remains the provisional baseline.
