# HANDOFF — start here for a fresh session

**Updated 2026-10-01.** Repo: `/home/messhias/lamasync/projects/computer-use`.
Standalone Git, `main`, no remote. Latest implementation commit: `93d0351`.
Read `AGENTS.md`, this file, then [the runnable benchmark](13-runnable-vision-benchmark.md).

## Current state

We have working platform probes, one verified KCalc action, and a runnable
vision-reader benchmark. No general desktop service, MCP server, autonomous
planner, or heartbeat watcher exists yet.

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
| `benchmark_capture.py` + `benchmark_fixtures/` | Five synthetic Kate/Firefox cases, isolated sessions, AT-SPI setup verification, mapped full/app/region PNGs. |
| `vision_benchmark.py` | Saved-image evaluation, DeepSeek/MiMo/generic endpoint configuration, seeded order, bounded calls, strict scoring and JSONL results. |
| `test_vision_benchmark.py` | Six validation tests covering schemas, streams, image hashes, call caps, provider payload and expected-answer isolation. |
| `p2_kcalc.py` + `test_p2_kcalc.py` | One Jev-selected button press, fresh target check, exact blank-to-1 display verification; five tests. |

From the repo root:

```bash
# No paid calls: capture the fixture suite.
uv run --with kwin-mcp==0.10.0 --with Pillow --with httpx python vision_benchmark.py --capture-only --output run/fixtures

# DeepSeek: five app images, two repetitions, ten calls.
uv run --with httpx python vision_benchmark.py --manifest run/fixtures/manifest.json --output run/deepseek --max-calls 10

# MiMo: reuse those exact captures and questions.
uv run --with httpx python vision_benchmark.py --manifest run/fixtures/manifest.json --output run/mimo --provider mimo --base-url https://api.xiaomimimo.com/v1 --model mimo-v2.6-flash --key-env MIMO_API_KEY --max-calls 10

# Validation only.
uv run --with httpx python -m unittest -v test_vision_benchmark.py test_p2_kcalc.py
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
added. The next bounded assignment after implementation scope approval is M1.

## Recommended next work

1. **Small reusable observation adapter:** extract app-scoped capture and the
   vision request path from the benchmark. Return metadata, explicit image,
   data, or both for one capture. Preserve provider errors, uncertainty,
   freshness and mappings. Test an unfamiliar app before generalizing.
2. **Grounding and one real task:** define how caller actions resolve fresh
   targets, using AT-SPI where useful. Write/edit a Kate document or interact
   with a webpage through the chosen contract, verifying each action. Resolve
   planner placement and finalize ADR-011 before a broad execution loop.
3. **Dynamic wait fixture:** loading → ready, loading → error, no change,
   and an unexpected dialog outside a watched region, with timestamps for
   actual transitions. Existing pages are static states; they do not measure
   wakeup behavior.
4. **Heartbeat comparison:** normal agent polling vs local change watcher +
   fast vision vs local OCR + Jev. Measure success, elapsed time, primary turns
   and tokens, every backend call/cost, false wakes, missed events and detection
   delay. Install/verify OCR language data only when testing that arm.
5. **Then CLI/MCP and tailnet integration:** cancellation/stop, session controls,
   stdio, Streamable HTTP, preserved Serve routes, and a real remote caller.
   Browser-specific capture/replay accelerators follow after this foundation.

These are recommendations, not authorization for the general server/state
compiler. The owner authorized the bounded benchmark implementation and paid
DeepSeek/MiMo runs. Ask before expanding beyond that scope under `AGENTS.md`;
do not repeat permission questions for work already authorized.
