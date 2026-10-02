# 24 — Primary-agent wait comparison

## Question and method

Does the owner-side screenshot heartbeat reduce agent screenshot polling on
real virtual computer-use work? The earlier pixel-diff baseline had no provider
or task agent, so it does not answer this question
([doc 17](17-dynamic-wait-baseline.md)). The agent still inspects screenshots,
chooses actions and verifies results; the reader only judges a visual wait
condition. A reader judgment is never task proof.

The comparison used the normal local owner/MCP service, Firefox app capture,
DeepSeek `deepseek-flash`, kwin-mcp `0.10.0`, and the local `wait.html`
fixture. Both arms used an 8-second transition after a successful screenshot
arms each stage:

- **Polling:** Luna chooses when to request fresh image screenshots; it does
  not call `desktop_wait`.
- **Wait:** Luna calls `desktop_wait` once for “Forecast ready is visible, or
  page shows a forecast loading error,” then inspects its returned image.

After the first ready trial, the owner asked to keep testing short. That
supersedes the earlier eight-run matrix: this feasibility sample is **one
matched pair for loading→ready and one for loading→error→fresh-ready**, four
valid runs total, all sequential and wait→polling. It is not a randomized
performance estimate. The first ready wait run reused a Luna worker context
after three excluded setup mistakes; the other primary-agent trials used fresh
Luna High contexts. Setup mistakes are included in each report but excluded
from the valid-task counts.

## Results

| Scenario / arm | Observed outcome | Wait/read work | Cleanup |
|---|---|---|---|
| Ready / owner wait | `invalid_judgment` after 1.282 s; returned image still showed Loading forecast. No readiness verified. | 1 provider call; 2 internal captures; 800 reported tokens; 937 ms provider latency, 1,002 ms owner elapsed. | Confirmed; final status had no session. |
| Ready / polling | Reached Forecast ready / Station: North Pier. First post-navigation screenshot to ready screenshot: 12.539 s. | 3 image screenshots; 0 reader calls. | Confirmed; final status had no session. |
| Error recovery / owner wait | `invalid_judgment` after 1.405 s; returned image still showed Loading forecast. Agent correctly did not claim an error or navigate to recovery. | 1 provider call; 2 internal captures; 800 reported tokens; 1,052 ms provider latency, 1,119 ms owner elapsed. | Confirmed; final status had no session. |
| Error recovery / polling | Visibly observed Unable to load forecast / Reference E-17, navigated to the fresh ready URL, then observed Forecast ready / Station: North Pier. | 7 image screenshots; 0 reader calls; 4 actions. | Confirmed; final status had no session. |

Both wait judgments failed closed with `invalid_judgment`; the precise rejected
reader value was not retained, so the evidence does not identify the cause.
Wait returned before the fixture's 8-second transition in both cases and
saved no primary-agent screenshot polling. This sample shows no acceleration
or completion benefit. Keep the reader-backed wait experimental and opt-in;
do not treat these two failures as a general estimate of provider quality.
The error-recovery wait arm never reached its second page, so that matched pair
does not provide a same-task elapsed-time comparison.

The reported usage totals 1,600 input/output tokens across two provider calls.
The owner does not measure provider billing, and Luna inference tokens were not
exposed. No other provider requests were made for this comparison. MCP tool
calls are listed per run in the reports; they are not model-turn counts.

## Evidence

Each owner timeline is rendered from the local append-only audit. Timeline
gaps combine caller reasoning, image interpretation and transport; they do not
isolate model inference. Available fixture receipts separately record when
their pages loaded, were armed and changed state. The first ready-wait
controller was interrupted before the fixture-receipt shutdown fix, so that
run has no persisted receipt.

Evidence is retained locally under ignored `run/agent-wait/`:

- `ready-wait1/agent-report.md`, `owner-audit.jsonl`, `timeline.json`
- `ready-poll1/agent-report.md`, `fixture.json`, `owner-audit.jsonl`, `timeline.json`
- `recovery-wait1/agent-report.md`, `fixture.json`, `owner-audit.jsonl`, `timeline.json`
- `recovery-poll1/agent-report.md`, `fixture.json`, `owner-audit.jsonl`, `timeline.json`

The fixture controller is only a deterministic loopback stimulus. Agents used
MCP for every desktop observation and action. Do not use browser APIs, HTTP,
page source or direct driver calls to do the task. In Firefox, focus the fresh
address-field candidate with a verified click when needed, then refresh
candidates. For `navigate_url`, pass the same exact full URL including scheme
as both `text` and `expected`.

## Boundaries and next work

Wait captures debit the normal observation cap and reader requests debit the
same per-session cap as `desktop_observe(data|both)`. The wait is bounded by
session lifetime and cancels with the session. Provider usage is not billing
data. Codex/Luna turn, token and cost data are unavailable unless their host
exposes exact measurements.

General live MCP support first needs implementation of focus restoration,
stop/cancel behavior, task-owned cleanup and the physical-input activity gate.
[Doc 25](25-next-phases.md) puts the owner-watched monitor check last. No
live-desktop input or tailnet deployment was part of this comparison.
