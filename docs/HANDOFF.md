# Handoff — start here

Updated **2026-10-03**. Checkout: `/home/messhias/lamasync/projects/computer-use`.
Origin: [aliforfaen/computer-use](https://github.com/aliforfaen/computer-use).
Read `AGENTS.md`, this page, then [local CLI/MCP](16-local-owner-and-mcp.md).
Numbered docs retain earlier experiments; this page is the current summary.

## What works

| Surface | Confirmed scope |
| --- | --- |
| Virtual sessions | Firefox, Kate and KCalc; shared CLI/MCP owner; app capture, semantic actions and cleanup. |
| Browser actions | Navigation, links and text entry, including recovery from failed navigation. Scrolling is unresolved. |
| Kate documents | Draft/save/revise/save with exact editor and saved-file verification; output survives cleanup. |
| Observation | Explicit app images for vision agents; optional interpreted data from a bounded, cancellable reader. DeepSeek is provisional, MiMo configurable. |
| Lifecycle | Idle/lifetime/read/action caps, watchdog teardown, owned-app cleanup and recovery journals. |
| Local live Kate | Owner-watched document workflow and app screenshot passed; exact focus and accessibility flags restored, zero Kate windows remaining. |

Useful agent acceptance: [Codex Luna workflow](20-codex-luna-workflow.md),
[document workflow](21-kate-acceptance-workflow.md),
[public webpage → Kate](23-public-browser-kate-agent-task.md).

## Latest live checkpoint

Kate launch uses `--block` to preserve process/window ownership. Live
`candidates`, `observe`, `wait` and `act` briefly focus the task app and restore
the exact prior window afterward. This fixes stale background targets and
screenshots of an occluding window. Kate briefly returning to the background
is expected under this focus-restoration contract.

The watched draft/save/revise/save output matched the intended 270 bytes.
Both successful sessions independently confirmed focus, accessibility settings
and owned-app cleanup. The document session took about 75 s, including 17 s
of owner calls; the screenshot follow-up took 26 s, including 11 s of owner
calls. Gaps include agent work and orchestration. No provider calls were made.
[Contract and evidence](26-live-owner.md); local report and timeline:
`run/live-kate-2026-10-03/`. The test daemon was shut down after acceptance.

Local live sessions require explicit owner-present override and temporary
AT-SPI opt-in, and own one newly launched allowlisted app. Physical-input idle
detection is unavailable here. This Kate result does not validate live Firefox
or physical cancellation/crash recovery.

## What remains

1. **Scroll:** fix grounding/effect verification using one concrete before/after
   example. Current host verifier fails; do not claim scrolling works.
2. **Reader wait:** inspect one bounded retained invalid response. Both polling
   comparison tasks completed; both reader-wait trials returned
   `invalid_judgment`. Keep ordinary screenshots as the default and heartbeat
   waiting experimental. [Comparison](24-primary-agent-wait-comparison.md).
3. **Optional watched live checks:** Firefox and cancellation/crash cleanup,
   when the owner is present. Keep each check small and useful.
4. **Tailnet:** requires a real peer and approval to change Serve routes.
   Preserve existing routes, bind loopback, reuse official MCP transport.
5. **Planning/replay:** resolve ADR-011 and broader visual grounding before
   general execution. Jev stays a selector; OCR/Jev acceleration is unproven.

[Next phases](25-next-phases.md) gives the remaining gates. No remote HTTP
surface, tailnet deployment or general planner exists.

## Running and checking

```bash
uv sync
uv run jev-desktop daemon --foreground --allow-app kate --allow-app firefox --allow-app kcalc
# In another terminal:
uv run jev-desktop capabilities
uv run jev-desktop session start kcalc
uv run jev-desktop observe kcalc
uv run jev-desktop session stop
uv run jev-desktop shutdown
```

MCP host command: `uv run jev-desktop-mcp`; it connects to the same owner.
See [doc 16](16-local-owner-and-mcp.md) for sockets, actions, images, readers,
lifecycle and live opt-in. Changes to tool schemas require a host refresh.

Use targeted offline checks and small host probes for changed behavior.
The last broad historical run passed 172 tests and built a wheel; later live
fixes received focused checks and actual watched acceptance. Do not rerun a
large matrix solely to reproduce an old test count.

## Cleanup checkpoint

The 17 root test files now live in `tests/`. Standalone utilities moved to
`tools/`: `local_service_probe`, `mcp_trial_client`, `vision_wait_probe`.
Invoke them with `uv run python -m tools.NAME`; runtime-imported benchmark
modules remain at root. Tool roots and packaging were updated accordingly.

Verification: 176 cases discovered without running the full suite; 37 focused
tests passed (service probe, vision wait probe, live probe and KCalc proof).
All three moved tools passed `--help`. No live apps or provider calls were used.

## Repository and operational notes

- Runtime modules implement the local service; tests and experiment utilities
  have separate directories (see README for layout and invocation).
- `.env`, `run/`, `.memsearch/` and local MCP/OpenCode config stay ignored.
  Never display credential values. Local evidence is not shipped with Git.
- Preserve owner config edits. The existing `/.pi/mcp.json` ignore rule belongs
  in the repository; the cleanup does not remove local client config.
- Small reader tests are authorized with explicit limits/accounting. Older
  paid-probe caps and refusal notes in historical docs are superseded.
- Measured live host: KWin 6.7.5, kwin-mcp 0.10.0. Recheck versions before a
  new platform behavior claim; input is focus-routed.
- Owner prefers concise docs, Luna High for legwork and calm targeted testing.
  Avoid unsolicited security reviews and repeated authorization questions for
  already approved local work.
