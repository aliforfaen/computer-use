# 12 — DeepSeek screenshot speed probe

**2026-10-01, 13:26–13:28 UTC · ten paid calls, small exploratory sample.**

## Setup

Luna High ran the probe; the coordinator inspected the source image and
results. Reused `kwin-mcp==0.10.0` on KWin **6.7.5**, with a disposable,
isolated virtual KCalc session. No live-desktop content was sent.
Captured a single 1280×800 PNG, then used Pillow to crop without resizing:

| Scope | Rectangle in source (x, y, width, height) | PNG bytes |
| --- | --- | ---: |
| Full virtual session | 0, 0, 1280, 800 | 15,693 |
| KCalc window | 320, 160, 640, 480 | 10,668 |
| Display and digit keys | 326, 205, 414, 345 | 5,150 |

KWin's unfiltered window query identified `org.kde.kcalc`; filtering by
`kcalc` returned no windows. A future adapter must resolve actual window
identity instead of guessing an app-name match. The captured display was
blank; the button labeled `1` was visible, independently checked against
the image and AT-SPI state. The virtual session was stopped afterward.

Requests used `https://api.deepseek.com/chat/completions`, model
`deepseek-flash` (also returned by the endpoint), thinking disabled,
inline PNG, default image detail, streamed output and usage. The ignored
`.env` supplied `DEEPSEEK_API_KEY`. No credentials were recorded.
[Official vision format and detail behavior](https://api-docs.deepseek.com/guides/vision/).

## Timing and payload comparison

Two calls per scope, plus two no-image controls, randomized with seed
20261001. Output cap 80 tokens. Timing starts before the HTTP request and
includes connection, upload and inference; excludes capture/crop preparation.
TTFT means first nonempty content fragment. Each request used a new urllib
request rather than a persistent pooled connection.

| Scope | Median TTFT | Median completion | Observed completion range | Input tokens per call |
| --- | ---: | ---: | ---: | ---: |
| Full session | 990 ms | 1,127 ms | 1,053–1,202 ms | 735 |
| App window | 1,279 ms | 1,371 ms | 1,303–1,439 ms | 299 |
| Display/key crop | 755 ms | 938 ms | 729–1,147 ms | 287 |
| No-image control | 910 ms | 1,114 ms | 875–1,352 ms | 93 |

The app crop reduced input tokens by about **59%**, and the tighter crop by
about **61%** relative to the full session. The tight crop's median completion
was about 17% lower in this sample; the app crop was slower. Two samples
cannot establish a speed improvement or tail latency. Smaller regions
clearly reduced payload/tokens, but did not yield proportional time savings.
The provider also normalizes image dimensions internally, so progressively
smaller crops need not produce proportional token savings (see vision guide).

## Correctness check and prompt repair

The initial question used `one_button_visible`, although the visible label
was `1` and only its accessibility name was `One`. It also failed to separate
a blank display from unreadable text. Answers were inconsistent, sometimes
mistaking the `NORM` status label for the display; four of six image answers
had Markdown fences despite requesting JSON. Those calls are timing evidence,
not a valid model accuracy score. Both no-image controls declined observation.

Two additional app-window calls explicitly asked about the digit `1`, defined
empty string as a visibly blank display, excluded the status bar, and enabled
`response_format: {"type": "json_object"}`. Output cap 128 tokens. Both returned
valid, correct JSON: `{"digit_1_button_visible":true,"display_text":""}`.

| Diagnostic | TTFT | Completion | Input / output tokens |
| --- | ---: | ---: | ---: |
| 1 | 767 ms | 786 ms | 318 / 16 |
| 2 | 1,393 ms | 1,528 ms | 318 / 16 |

Total usage across all ten calls: **3,464 input and 277 output tokens**.
This is token usage, not a measured billing charge. Raw synthetic-only
results and throwaway probe are temporarily in `/tmp/jev-deepseek-speed/`;
these files are not durable project artifacts.

## Recommendation

Keep direct DeepSeek Flash as a provisional reader. Explicit visual labels,
blank-versus-unreadable handling and enforced JSON fixed this narrow task,
but two correct answers do not validate general screenshot recognition.
App-only observations and optional viewport-region crops are worthwhile
features for payload and cost reduction; speed benefits remain unproven.
Keep full-context recovery, exact crop mappings and a broader local watcher.

Next test should use the corrected prompt consistently across scopes and
include populated displays, small webpage text, loading states and errors.
Compare a persistent connection and capture-to-valid-answer time. The
heartbeat task benchmark in [doc 10](10-heartbeat-direction.md) is still
needed; these static-image calls do not measure task completion or wakeups.
