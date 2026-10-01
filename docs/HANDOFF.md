# HANDOFF — start here for a fresh session

**Updated 2026-10-01.** Repo: `/home/messhias/lamasync/projects/computer-use`.
Standalone Git, `main`, no remote.
Read `AGENTS.md`, this file, then [the virtual slice](15-virtual-session-slice.md).

## Current state

We have reusable app observation, semantic action transactions and a
disposable virtual Kate/Firefox/KCalc task harness, alongside the platform
probes and vision benchmark. No general desktop service, MCP server,
autonomous planner or heartbeat watcher exists yet.

**Latest M1–M2 validation:** 39 tests pass. The reviewed host harness passed
two exact Kate edits, Firefox button transition and disabled refusal, plus
KCalc app observation; all three sessions and temporary files cleaned up.
No paid calls. See [doc 15](15-virtual-session-slice.md) for results and limits.

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
| `observation.py` + `vision_reader.py` | Allowlisted mapped captures, bounded TTL store, same-capture metadata/image/data and shared reader transport. |
| `transactions.py` | Fresh AT-SPI target refs, virtual-only input, policy/caps/audit, read-after-action verification and failure latch. |
| `virtual_tasks.py` + `benchmark_fixtures/action.html` | Disposable Kate edits, Firefox action/disabled refusal and KCalc capture; no paid calls. |
| --- | --- |
| `benchmark_capture.py` + `benchmark_fixtures/` | Five synthetic Kate/Firefox cases, isolated sessions, AT-SPI setup verification, mapped full/app/region PNGs. |
| `vision_benchmark.py` | Saved-image evaluation, DeepSeek/MiMo/generic endpoint configuration, seeded order, bounded calls, strict scoring and JSONL results. |
| `test_vision_benchmark.py` | Six validation tests covering schemas, streams, image hashes, call caps, provider payload and expected-answer isolation. |
| `p2_kcalc.py` + `test_p2_kcalc.py` | One Jev-selected button press, fresh target check, exact blank-to-1 display verification; five tests. |

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
acceptance gates and owner-dependent deferrals. No general service code was
added at planning time. The owner then approved M1–M2 launch; both are now
implemented and validated. The next service handoff is Worker C/M3, after
explicit service scope approval. Grounding currently covers unique mapped
AT-SPI buttons/editors; general visual grounding and ADR-011 remain open.

## Recommended next work

1. **Single-owner service and local surfaces (M3):** persistent kwin-mcp child,
   local IPC, thin CLI/MCP clients, capabilities/session status, cancellation,
   stop-all and driver failure handling. Reuse the validated core; see Worker C
   in doc 14. No public/server planner contract has been finalized.
2. **Dynamic wait fixture (M4):** loading→ready/error, no change, animation noise
   and an unexpected dialog outside the watched region. Record actual
   transition timestamps before comparing polling and accelerated waits.
3. **Planner contract:** resolve ADR-011 and visual grounding before broad task
   execution. Current transactions support unique mapped semantic targets,
   not arbitrary screenshot coordinates from a model. Task-specific verifiers
   and guarded effect policies need expansion beyond the synthetic fixtures.
4. **Live and tailnet (M5/M6):** idle/focus measurements, explicit live tasks,
   cancellation/error restoration, preserved Serve routes and actual remote
   caller tests. These do not block a useful virtual local service.
5. **Workflow acceleration later:** semantic replay prerequisites, expected
   effects and recovery exits, with Jev as an optional bounded selector.

M1–M2 implementation was authorized and completed. Do not ask for that
approval again. General service/planner work, live tasks and remote deployment
remain later gates in doc 14; no Serve configuration was changed. Paid tests
in this slice were unnecessary. Worker handoffs are complete in doc 14.
