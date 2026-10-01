# 14 — App build plan and worker handoffs

**2026-10-01 · implementation sequence and worker handoffs.** Prepared with
Luna High reviews of the core, acceleration and CLI/remote surface. The owner
approved the bounded M1–M2 slice, then M3 local owner/CLI/MCP work. Current
status is recorded in the milestone table; detailed M1–M2 and M3 evidence
boundaries are in docs 15 and 16. Tailnet service deployment, broad planning
and live-desktop tasks remain later gates.

## Recommendation

Build a usable virtual computer-use session first, then measure acceleration.
Reuse [kwin-mcp](https://github.com/isac322/kwin-mcp) for sessions, windows,
AT-SPI, screenshots and EIS input; retain the tested 0.10.0 pin initially.
Reuse Pillow for image mapping/crops and httpx for the configurable reader.
Use the [official Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk)
for the eventual transports. Avoid a new desktop driver or a compulsory Jev
planner. DeepSeek stays the provisional reader; MiMo remains selectable.

The existing suite demonstrates perception on five controlled fixtures.
It does not establish action grounding, task completion or wakeup performance.
The first useful deliverable is a virtual session that lets an agent observe,
perform one grounded action and receive verified results.

## Proposed contract

These interfaces are recommendations; they do not lock ADR-011.

- `CaptureRef`: ID, time, session/window identity, dimensions, source hash,
  scope, crop/resize mapping and a server-owned image handle.
- `Observation`: that capture reference, metadata, explicit image and/or
  interpreted data, provider/model, usage, latency and structured errors.
  Image and interpretation use the same bytes. Expired captures fail explicitly.
  Remote defaults are metadata/hash only; images and provider interpretation
  each require an explicit request.
- `Candidate`: code-created reference, role, label, supported actions,
  relevant AT-SPI properties and observation generation.
- `Action`: enumerated operation, candidate reference and validated arguments.
  Typing accepts caller-provided text; text is not included in audit/state.
- `ActionResult`: execution status, separate verification status, before/after
  observation references and a machine-readable failure reason.
- `WaitResult`: `ready`, `unexpected`, `timeout` or `cancelled`, with fresh
  evidence references and timings. A pixel change alone never means ready.

Start grounding with fresh AT-SPI candidates. Reject absent, ambiguous,
disabled and stale targets. Vision descriptions are observations, and Jev
only selects code-enumerated choices. Apps without useful AT-SPI will require
a separately agreed visual grounding path; do not pretend semantic targeting
covers every desktop app.

Keep primitive operations behind one session owner on cachy. Recommend first
using an external agent to exercise those primitives, then implementing the
server task runner through the same operations. This is an implementation
sequence, not a decision to move the locked server-side loop to clients.
Planner placement and any change to ADR-009 require a concrete ADR-011 decision.
Never interleave primitive actions with a running task.

## Milestones and ready-to-assign handoffs

Each worker reads AGENTS.md, HANDOFF.md and this document. Implementation
workers begin only after the scope gate is cleared. A worker reports changed
files, actual commands/results, remaining failures and teardown status;
coordinator independently reviews before integration. Keep desktop tests in
virtual sessions and paid requests bounded and explicitly scoped.

| Stage | Deliverable | Dependencies | Acceptance |
| --- | --- | --- | --- |
| M1 (implemented) | Reusable observation adapter | Authorized and validated; doc 15 | Same-capture image/data; app scope default; explicit full/crop; errors, hashes and mappings preserved |
| M2 (implemented, virtual fixtures) | Grounded action transaction + disposable harness | Authorized and validated; doc 15 | A real Kate edit and local webpage action verified after every action; stale/disabled targets refused |
| M3 (implemented and host-validated) | Single-owner virtual service + local CLI/MCP | M2 | CLI and stdio share the owner; verified fixture effects, competing-call refusal, cancellation and child-death cleanup checked; doc 16 |
| M4a (implemented, local baseline) | Synthetic full-app wait fixture; screenshot polling vs. local pixel-diff watcher | M1–M2; [doc 17](17-dynamic-wait-baseline.md) | Corrected seed-17 run classified 10/10 fixture states; cleanup passed. No provider, Jev, OCR, primary agent or acceleration claim. |
| M4b (paused; first run invalid) | Bounded DeepSeek heartbeat over real fixture captures | M4a; explicit under-$1 owner authorization on 2026-10-01 | Frozen code adds idle/total/watch deadlines, fsynced progress journaling and interrupt propagation. Independently review and test after owner resumes, before the probe's one configured follow-up request; doc 17 records unknown prior usage. No tests, probes or calls during the pause. |
| M5 | Live desktop support | M3; idle/focus probes; explicit owner live task | Explicit audited per-task live request; focus restored on success/error/cancel; recent physical input blocks entry; one live task |
| M6 | Tailnet surface + real remote client | M3; deployment approval and peer availability | Loopback HTTP, preserved Serve routes, remote virtual task, truthful caller provenance and stop controls |

M4 can proceed alongside service integration after the transaction works.
Live mode and remote deployment do not block a useful virtual local release.

### Worker A — Observation adapter (M1)

Extract capture/mapping logic from `benchmark_capture.py` and provider transport
from `vision_benchmark.py`, leaving fixture setup and expected-answer scoring
in the benchmark. Introduce a small importable core and explicit configuration;
keep the existing benchmark CLI working. Use in-memory or bounded ephemeral
capture storage; define expiry and cleanup before arbitrary app observation.

Deliver metadata/image/data/both for one capture, reader capability reporting,
timeouts and explicit malformed/uncertain results. Do not silently retry or
switch providers. Start with the proven virtual 1:1 mapping; reject unsupported
mapping rather than generalizing fixed fixture geometry. Later scaling and
multi-monitor support need measured examples.

Verify capture hash consistency, crop bounds, expired IDs, provider failure,
wrong response shape and an unfamiliar virtual app. Preserve the static
benchmark's expected-answer isolation. Do not log raw screenshots, extracted
text, HTTP bodies or credentials in the general adapter.

### Worker B — Transactions and virtual tasks (M2)

Define fresh candidate enumeration and `act` through kwin-mcp semantic targets.
Re-resolve immediately before execution. Add one transaction at a time per
session, action/time/call caps, cancellation and plain-code policy. Initial
fixture allowlist is explicit Kate, Firefox and KCalc; production allowlist
remains owner configuration. Modes retain their existing meanings and limits.

M2 uses a disposable test harness, not a supported CLI or competing daemon.
The supported CLI is a thin client added with the owner in M3.

First task: type/edit an original short document in a disposable Kate session
and verify exact editor content through AT-SPI. Second: interact with a local
page button and verify the expected changed state. Save only inside a declared
disposable workspace if file-save testing is included. Include changed target,
disabled control, timeout and verification-failure cases. Report `unknown` if
only an unexplained screenshot diff exists; do not claim task success.

Audit every call/action with the existing required fields, omitting typed
text and sensitive content. Stop and approval paths must exist before surfaces
expose actions. Jev integration is optional and cannot authorize execution.

### Worker C — Owner process and surfaces (M3, then M6)

Implement one long-lived owner of the pinned kwin-mcp child, sessions, locks,
policy and audit. Choose/document local IPC; CLI and MCP stdio are thin clients,
not independent owners. Use MCP SDK primitives rather than custom protocol code.

Expose capabilities, session start/status/stop, observe, act, wait/cancel and
`stop --all`; add task execution after planner contract approval. Define client
disconnect behavior and task ownership explicitly. Start with session-level
serialization; defer parallel session optimization until driver isolation is
verified. Verify two clients, driver death, cancellation and teardown.

For M6 bind HTTP only to 127.0.0.1, preserve existing Serve `/`→3080, and add the
project route deliberately. Verify ACL reach, loopback binding and Funnel off;
validate `tailscale serve off` as the hard stop. Keep authorizer seam and append-only audit. Tagged
caller identity may be unavailable: record unknown and provenance rather than
inventing a node. Require an actual remote list-tools and virtual task round
trip before calling remote support complete. Do not deploy during planning.

### Worker D — Wait fixtures and accelerators (M4)

M4a's local watcher and polling baseline are implemented. See [doc 17](17-dynamic-wait-baseline.md)
for host evidence, the corrected error-color classifier and limits. It is a
local pixel-difference primitive comparison, not evidence of saved model calls
or faster end-to-end agent tasks. The first authorized DeepSeek heartbeat
probe produced no usable result and lost exact attempt/usage counts after a
comment-keepalive SSE stream and interrupt-handling failure. Fix and review its
absolute deadline, flushed attempt journal and interrupt cleanup before the
owner's one allowed follow-up request. OCR+Jev and primary-agent comparisons
remain later arms.

For M4b, after the owner resumes, reuse the controlled page and full-app capture
path. The owner authorized one bounded DeepSeek heartbeat probe under $1.
Record the real image bytes, request/response status, latency, usage and cleanup; count only calls
actually sent, stop at the approved cap or on rate limits, and do not retry.
Keep provider call cost separate from any estimated cost. No primary-agent
turns or tokens are part of this probe. Do not report it as end-to-end speedup.

OCR+Jev and the primary-agent polling/watcher comparison remain later arms.
For those, randomize repeated runs on a common task and count primary
turns/tokens, every backend request, capture/OCR/judgment time, end-to-end
success/elapsed time, false wakes, missed events, retries/timeouts and measured
or explicitly estimated cost. Keep dynamic manifests separate from static
perception results, and never declare an accelerator selected from the static
perception results.

## Owner intervention — deferred gates

No answers are needed to review this plan. Bring these back when their
preceding work produces concrete options:

1. **Implementation scope:** M1–M2 approved by the owner on 2026-10-01
   ("You are go for launch"). No further scope question is needed for that slice.
   M3 local owner/CLI/MCP continuation was subsequently authorized.
2. **Planner and grounding:** before a broad task runner, settle ADR-011 with
   a tested primitive contract and example failed grounding cases. Recommend
   semantic targets first, with unsupported-app failure explicit.
3. **Live operation:** after idle/focus measurements, choose the inactivity
   threshold/override behavior and request the exact live task. Broad home
   access, terminal typing and destructive operations retain confirmation.
4. **Remote deployment:** need an available remote client/peer and approval
   to change Serve configuration; preserve the existing route. No additional
   provider accounts are needed for the local baseline.
5. **Paid comparison:** the owner authorized one DeepSeek heartbeat probe under
   $1 on 2026-10-01. Keep that probe within its explicit cap; ask before any
   broader provider comparison, OCR+Jev arm or end-to-end paid task comparison.
   Frozen runner changes are not reviewed or retested. Work is paused now: do
   not run tests, probes or provider calls until the owner resumes; the single
   follow-up request is not currently cleared.

## Concerns and practical limits

- Capture code currently assumes fixed virtual geometry and parses driver text.
  Extraction needs an adapter boundary and explicit unsupported-mapping errors.
- Both readers missed disabled controls; semantic properties matter for action
  validity. An image reader is not a reliable permission or completion check.
- Verification needs task-specific postconditions. A screenshot diff can show
  animation rather than the requested effect.
- A watcher may save primary turns while still polling screenshots locally.
  Count local work and all calls when reporting speed/cost improvement.
- OCR can miss non-text changes; narrow crops can hide dialogs. Keep broader
  context detection and recovery requests.
- Workflow replay comes after reliable transactions and waits. Store semantic
  targets, prerequisites, expected effects and recovery exits; Jev can select
  a recorded workflow/branch, but demonstration alone does not make it reliable.

## Fresh-session routing

Read HANDOFF.md for current status, doc 13 for perception measurements, doc 15
for M1–M2 evidence, [doc 16](16-local-owner-and-mcp.md) for the M3 local
owner/CLI/MCP lifecycle, and [doc 17](17-dynamic-wait-baseline.md) for M4a.
M3 host integration and the corrected M4a local baseline passed. M4b is paused
until the owner resumes; do not run tests, host probes or provider calls during
the pause. Planner/visual grounding, live desktop and tailnet deployment remain
separate later gates; do not infer their support from the local virtual service.
