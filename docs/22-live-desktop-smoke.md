# 22 — Owner-present physical desktop smoke test

Reuses `kwin-mcp==0.10.0` live connection, tracked app launch, AT-SPI,
KWin window inventory and the existing app observation adapter.

## Fixed task

```bash
uv run python live_desktop_probe.py --execute --visible-hold --temporary-a11y
```

Start only a fresh owned KCalc, read a blank display and a fresh mapped `One`
button, focus that exact window, click once and verify `1`. Capture app metadata
and hashes before/after. Restore the original window by stable ID readback,
terminate only the launched process, verify its window is gone, then disconnect.
Temporary accessibility flags are snapshotted, enabled only for this probe and
restored exactly. The user can watch: named demonstration holds add 2 s before
input and 3 s afterward. They are not adapter latency.

This was an explicitly owner-present local test. At the time, physical-input
idle detection and live owner MCP tasks were unavailable. Current local live
support and its limits are recorded in [doc 26](26-live-owner.md). No providers
or tailnet changes were part of this probe.
The task deadline is checked between phases; synchronous driver calls retain
their own timeouts, so it is not a hard wall deadline.

## First attempt — safe refusal

`run/live-desktop-probe/kcalc-20261002T141703Z-a6bee525/report.json` records no
input, confirmed owned-process/window cleanup, exact original focus restored,
and both accessibility flags restored to false. The probe incorrectly expected
a PID in the driver's public geometry report. That report omits PID; richer
`geometry.collect_windows()` records must be cross-matched by exact window ID.
This was a probe API mismatch, not an observed process-ownership disagreement.

## Successful corrected run — 2026-10-02

Evidence: `run/live-desktop-probe/kcalc-20261002T142017Z-c0a89cea/report.json`.
KWin 6.7.5 / kwin-mcp 0.10.0. The raw KWin PID exactly matched the launched
KCalc PID. One fresh semantic click verified blank → `1`; app captures mapped
355×554 pixels within the physical 2560×1440, scale-1 display. The reported
capture backend was **Spectacle**, with full logical coverage and stable
before/after topology. This validates that fallback for this layout only.

| Step | Seconds |
| --- | ---: |
| App capture before | 0.986 |
| Deliberate pre-click viewing hold | 2.000 |
| Fresh-target click | 0.336 |
| AT-SPI effect verification | 0.176 |
| App capture after | 0.905 |
| Deliberate post-click viewing hold | 3.000 |
| Entire probe, including cleanup | 8.547 |

Exact original Codex window focus, owned process/window cleanup and session
disconnect all confirmed. Accessibility flags restored to false/false.
The coordinator independently re-read current KWin focus, checked no KCalc
window/process remained and verified both flags. Six offline live-probe tests
cover interface shapes, failure cleanup, temporary settings restoration and
caption updates. Full suite: 144 tests passing; wheel build passed.

This does not validate arbitrary live tasks, other display layouts/scales,
physical-input idle detection, or a live MCP service.
