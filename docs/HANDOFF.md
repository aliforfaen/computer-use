# HANDOFF — start here for a fresh session

**Updated 2026-10-02.** Repo: `/home/messhias/lamasync/projects/computer-use`.
Branch: `main`; this checkpoint commits on top of `6e23448`. GitHub/origin
[aliforfaen/computer-use](https://github.com/aliforfaen/computer-use) is
connected and pushed; no push or deploy is pending. Owner work resumed on
2026-10-02. The M4b timeout/journal/interrupt changes were reviewed, fixed and
covered by offline tests, then the probe's single configured follow-up request
completed. Read `AGENTS.md`, this file, [the virtual slice](15-virtual-session-slice.md),
then [the local owner surface](16-local-owner-and-mcp.md).

## Current state

M1–M2 provide reusable app observation, semantic action transactions and a
disposable virtual Kate/Firefox/KCalc task harness. M3 adds the single-owner
virtual daemon, thin CLI and official MCP SDK stdio facade. The disposable host
integration probe passed; CLI and MCP observe the same session, CLI verifies a
KCalc transition and worker-death recovery confirms cleanup. There is no
remote HTTP service, live desktop mode or general planner. M4a adds a bounded
local screenshot-change watcher and synthetic Firefox fixture; its corrected
seed-17 host baseline classified all ten polling/watcher outcomes and cleaned
up all sessions. This local pixel-diff run does not use a vision reader, Jev,
OCR or a primary task agent, and does not demonstrate acceleration. See
[doc 17](17-dynamic-wait-baseline.md).

**Latest M1–M2 validation:** 39 tests pass. The reviewed host harness passed
two exact Kate edits, Firefox button transition and disabled refusal, plus
KCalc app observation; all three sessions and temporary files cleaned up.
No paid calls. See [doc 15](15-virtual-session-slice.md) for results and limits.

**Latest M3 validation:** 59 tests pass. Real owner checks verified Kate typing,
Firefox idle→complete and KCalc blank→1; normal sessions reuse the same worker.
Crash recovery and cleanup passed. Zero paid calls; [doc 16](16-local-owner-and-mcp.md)
records evidence, commands and limits.

**Latest M4a validation:** `uv run python -m unittest discover -v` passed 77
tests. The corrected local seed-17 wait baseline recognized 10/10 fixture
outcomes across five states and two arms; all ten sessions and temporary
profiles cleaned up. Initial classifier testing missed both loading→error
trials; a grayscale threshold correction fixed the issue, and a separate
two-arm recheck plus corrected full run passed. This is local pixel-diff only,
not a model or primary-agent comparison. See [doc 17](17-dynamic-wait-baseline.md).

**M4b paid probe status:** the first DeepSeek heartbeat run (2026-10-01) was
interrupted by a comment-only SSE stream that exceeded four minutes; its old
runner swallowed the first interrupt and lost exact attempt/usage counts.
Cleanup was confirmed. Reserve the full 12-attempt worst-case estimate
($0.0211968) as possibly charged; that amount is not observed billing. On
2026-10-02 the frozen deadlines, fsynced pre-attempt/lifecycle journal and
interrupt path were independently reviewed and fixed (an undefined prior-run
reserve constant made `run()` raise `NameError`). A follow-up review then found
the stated deadline bound still overstated: `iter_lines` buffered partial
lines, so the parser now enforces the deadline and byte cap on every raw
response chunk (including partial lines) and no longer claims a bound across
header reception. Fourteen new offline tests cover these paths, and the suite
is **111 tests, all passing**. The one configured follow-up request then
completed: one `deepseek-flash` judgment, returned usage
821 prompt / 8 completion tokens, 1.41 s, no rate limit, cleanup passed, and a
peak cache-miss upper bound of $0.0002559. That paid result predates the
second-pass parser fix but used a normal newline-terminated stream, so it
remains valid; the strengthened bound rests on offline regressions. The
follow-up cap is now consumed;
do not make another provider request without fresh authorization. See doc 17
for detail and evidence limits.

## Current work and next steps

Completed 2026-10-02:

1. Reviewed the frozen stream timeout, progress journal and interrupt path;
   fixed the undefined prior-run reserve constant. A second-pass review found
   the deadline claim still overstated for newline-free byte trickles; the
   parser now bounds time and size per raw chunk (partial lines included) and
   documents the header-phase limitation. The offline suite is **111 tests,
   all passing**, and the probe refuses to overwrite existing evidence.
2. Ran the single configured DeepSeek follow-up request; it returned one valid
   structured judgment and confirmed cleanup. The paid cap is consumed.
3. Corrected the stale HANDOFF claim that the initial remote connection/push
   was pending (it was already done).

Next work remains gated:

1. **Planner contract / action grounding:** resolve ADR-011 and visual
   grounding before broad task execution. Current transactions support unique
   mapped semantic AT-SPI targets, not arbitrary screenshot coordinates.
2. **Live and tailnet (M5/M6):** idle/focus measurements, explicit live tasks,
   cancellation/error restoration, preserved Serve routes and real remote
   caller tests. Do not change Serve configuration without approval.
3. **OCR+Jev and end-to-end primary-agent comparison:** later arms; they need
   fresh authorization and must count primary turns/tokens and every backend
   request. Do not infer acceleration from the M4a pixel-diff baseline or the
   single M4b heartbeat. The chunk-level parser fix is offline-tested; no paid
   request has exercised the newline-free trickle path.

The owner-supported direction is a screenshot-driven computer-use terminal
for local and tailnet agents, using `kwin-mcp` for sessions, capture and input.
Virtual sessions are the default; real desktop and `yolo` remain intended
features. The primary agent interprets tasks and handles recovery. Jev is an
optional accelerator for bounded judgments and familiar workflows.

**Observation requirements:** app-window capture by default; explicit images
for callers with vision, interpreted data for callers without it; both may
refer to the same capture. Full-session context and region crops are optional.
Browser-specific capture adapters come later. DeepSeek Flash is the provisional
reader, with provider/model switching supported by the benchmark.

## What exists and how to run it

| File | Purpose |
| --- | --- |
| `observation.py` + `vision_reader.py` | Allowlisted mapped captures, bounded TTL store, same-capture metadata/image/data and shared reader transport. |
| `transactions.py` | Fresh AT-SPI target refs, virtual-only input, policy/caps/audit, read-after-action verification and failure latch. |
| `virtual_tasks.py` + `benchmark_fixtures/action.html` | Disposable Kate edits, Firefox action/disabled refusal and KCalc capture; no paid calls. |
| `benchmark_capture.py` + `benchmark_fixtures/` | Five synthetic Kate/Firefox cases, isolated sessions, AT-SPI setup verification, mapped full/app/region PNGs. |
| `vision_benchmark.py` | Saved-image evaluation, DeepSeek/MiMo/generic endpoint configuration, seeded order, bounded calls, strict scoring and JSONL results. |
| `test_vision_benchmark.py` | Six validation tests covering schemas, streams, image hashes, call caps, provider payload and expected-answer isolation. |
| `p2_kcalc.py` + `test_p2_kcalc.py` | One Jev-selected button press, fresh target check, exact blank-to-1 display verification; five tests. |
| `desktop_service.py` + `desktop_worker.py` | One virtual session owner and one persistent `AutomationEngine` worker child. |
| `desktop_daemon.py` + `desktop_cli.py` + `desktop_mcp.py` | Private Unix socket owner, thin CLI and official MCP SDK stdio client. |
| `test_desktop_surfaces.py` + `local_service_probe.py` | Synthetic socket/MCP checks and the passing separate host integration probe. |
| `wait_watcher.py` | Bounded full-app capture wait primitive with debounce, latest-frame coalescing, cancellation/deadline checks and optional existing Reader seam. |
| `wait_benchmark.py` + `benchmark_fixtures/wait.html` | No-provider local polling/watcher fixture comparison; five controlled states and independent loopback event receipts. |
| `test_wait_watcher.py` + `test_wait_benchmark.py` | Wait API, fixture, schema, cancellation, deadline and pixel-diff threshold tests. |

From the repo root:

```bash
# No paid calls: verified virtual task slice.
uv run --with kwin-mcp==0.10.0 --with Pillow --with httpx python virtual_tasks.py

# No paid calls: capture the fixture suite.
uv run --with kwin-mcp==0.10.0 --with Pillow --with httpx python vision_benchmark.py --capture-only --output run/fixtures

# DeepSeek: five app images, two repetitions, ten calls.
uv run --with httpx python vision_benchmark.py --manifest run/fixtures/manifest.json --output run/deepseek --max-calls 10

# MiMo: reuse those exact captures and questions.
uv run --with httpx python vision_benchmark.py --manifest run/fixtures/manifest.json --output run/mimo --provider mimo --base-url https://api.xiaomimimo.com/v1 --model mimo-v2.6-flash --key-env MIMO_API_KEY --max-calls 10

# Validation only.
uv run --with Pillow --with httpx python -m unittest discover -v

# M3 local surface: start an explicitly allowlisted virtual owner.
uv run jev-desktop daemon --foreground --allow-app kate --allow-app firefox --allow-app kcalc

# In another local terminal: inspect and control the owner.
uv run jev-desktop capabilities
uv run jev-desktop session start kcalc
uv run jev-desktop observe kcalc
uv run jev-desktop session stop
uv run jev-desktop stop --all

# Local MCP stdio client for an MCP host; it connects to the same Unix owner.
uv run jev-desktop-mcp

# No paid calls: five local fixture cases, polling vs. local screenshot watcher.
uv run python wait_benchmark.py --seed 17 --repetitions 1 --max-trials 10
```

Keys stay in ignored `.env`: `JEV_API_KEY`, `DEEPSEEK_API_KEY`, `MIMO_API_KEY`.
Read names only when checking configuration. Never display values. Results
and captures are under ignored `run/`; they are local artifacts, not committed
fixtures. The suite can regenerate them. The earlier doc-12 probe lives in
`/tmp/jev-deepseek-speed/` and may disappear.

## What we learned

The runnable suite types an original three-line poem into Kate, reads a local
webpage table and small text, checks a disabled button, and distinguishes
loading, ready and error pages. Actions are scripted; models read screenshots.
These are static perception tests, not autonomous task-completion tests.

| Run | Calls | Exact facts | All-fact passes | Median completion |
| --- | ---: | ---: | ---: | ---: |
| DeepSeek Flash, app | 10 | 28/30 | 8/10 | 1.24 s |
| MiMo V2.6 Flash, app | 10 | 27/30 | 7/10 | 5.45 s |
| DeepSeek Flash, region | 5 | 11/15 | 3/5 | 1.08 s |
| MiMo V2.6 Flash, region | 5 | 15/15 | 5/5 | 9.38 s |

- Both models read the poem correctly in both app runs. Both missed the
  disabled-button state twice. Prefer AT-SPI properties for control state.
- MiMo's other app mismatch was a trailing period on a reference code.
  DeepSeek's region run included one JSON object with entirely wrong fields.
  Strict JSON syntax alone does not establish a usable observation.
- Crops reduced average input tokens by about 47% for DeepSeek and 58% for
  MiMo. Speed and correctness gains were inconsistent. Keep crops optional.
- Source captures took 135–141 ms. Provider timings include request work but
  exclude app setup/capture. Service load may affect the sequential provider
  comparison; small samples do not establish stable tail latency.
- Use exact visible labels in questions: KCalc prints `1`, while AT-SPI calls
  it `One`. Distinguish blank text from unreadable text; enforce response shape.

Full methodology, usage totals and limitations: [doc 13](13-runnable-vision-benchmark.md).

## Platform facts and constraints

Measured on `cachy`: KWin **6.7.5**, `kwin-mcp` **0.10.0**, AT-SPI **2.60.7**,
Kate **26.08.1**, Firefox **157.0** for the latest fixture run. Recheck versions
before new behavior claims; the KWin EIS interface is private.

- Virtual KWin/EIS and ScreenShot2 work. A prior live KCalc probe also worked
  and restored focus. Wayland input is focus-routed; live tasks must restore it.
- Kate/Firefox accessibility requires both `org.a11y.Status` `IsEnabled` and
  `ScreenReaderEnabled`. The benchmark enables these only on each isolated bus.
  Earlier isolated Brave exposed no useful tree.
- Resolve actual KWin window identities: filtering for `kcalc` missed
  `org.kde.kcalc`. Require known screenshot origin/size/full coverage before
  applying geometry. Keep capture identity, hashes and crop mapping together.
- Firefox is maximized in these fixtures, so app and full images coincide.
  The optional region image is a real smaller crop. Fixed fixture rectangles
  are not a general webpage-region detector.
- Fresh Firefox profiles suppress first-run pages. All virtual sessions were
  stopped. Driver `Broken pipe` warnings occurred despite successful capture
  and teardown; do not treat the warning alone as task failure.
- Tesseract 5.5.3 was installed, but English trained data was missing in the
  earlier OCR probe. OCR + Jev has not been benchmarked successfully.
- ydotool input fallback and screenshot fallbacks remain unmeasured. Existing
  Tailscale Serve `/` targets localhost:3080; preserve it. No project `/mcp`
  endpoint is deployed, and no Hermes client round trip has been completed.

## Decisions and remaining questions

ADR-001…010 record the original Jev-led design. ADR-011 proposes screenshot-led
planning and optional acceleration; it remains proposed. ADR-012 locks image
and interpreted-data support. ADR-013 locks app observation as default.
Read the append-only log in [doc 04](04-architecture.md); older Jev-loop diagrams
are historical proposals where the newer direction differs.

Session ownership, locking, execution verification and cancellation stay on
`cachy`. Placement of the primary planner and screenshot-to-action grounding
remain unresolved. Jev stays a selector over code-enumerated choices. Vision
interpretations are observations, not executable targets or proof of success.

Owner preferences: concise updates, small runnable steps, reuse existing
drivers. Luna High subagents are authorized. Trusted tailnet and high access
are intentional; avoid unsolicited security reviews. Follow existing project
controls without reopening settled remote-access decisions.

## App plan and worker handoffs

The owner requested an app build plan on 2026-10-01. Three Luna High reviews
were consolidated into [doc 14](14-app-build-plan.md): reusable observation,
verified virtual actions/CLI, single-owner service/local MCP, measured waits,
then live and tailnet support. It includes concrete worker assignments,
acceptance gates and owner-dependent deferrals. The owner approved M1–M2, then
M3 local owner/CLI/MCP. Those surfaces are implemented and host-validated.
Grounding currently covers unique mapped
AT-SPI buttons/editors; general visual grounding and ADR-011 remain open. See
[doc 16](16-local-owner-and-mcp.md) for commands, IPC ownership and lifecycle
limits.

## Recommended next work

1. **M4 reader heartbeat (done for this checkpoint):** the frozen stream
   deadline, flushed progress journal and interrupt handling were reviewed,
   tested and used for the single configured follow-up request, which returned
   one valid judgment. Its cap is consumed. Keep provider measurements separate
   from the completed local pixel-diff baseline. OCR+Jev and end-to-end
   primary-agent comparisons remain later work and need fresh authorization.
2. **Planner contract:** resolve ADR-011 and visual grounding before broad task
   execution. Current transactions support unique mapped semantic targets,
   not arbitrary screenshot coordinates from a model. Task-specific verifiers
   and guarded effect policies need expansion beyond the synthetic fixtures.
3. **Live and tailnet (M5/M6):** idle/focus measurements, explicit live tasks,
   cancellation/error restoration, preserved Serve routes and actual remote
   caller tests. These do not block a useful virtual local service.
4. **Workflow acceleration later:** semantic replay prerequisites, expected
   effects and recovery exits, with Jev as an optional bounded selector.

M1–M3 local virtual implementation was authorized. Do not ask for that
approval again. The M3 integration probe passed. M4b was reviewed, fixed,
tested and its single follow-up request completed on 2026-10-02; that paid cap
is consumed. Planner, live tasks and remote deployment remain later gates in
doc 14; no Serve configuration was changed. No paid provider calls are needed
for the local host probes.
