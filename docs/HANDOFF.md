# Handoff — start here

Updated **2026-10-03**. Checkout: `/home/messhias/lamasync/projects/computer-use`.
Origin: [aliforfaen/computer-use](https://github.com/aliforfaen/computer-use).
Read `AGENTS.md`, this page, then [local CLI/MCP](16-local-owner-and-mcp.md).
Numbered docs retain earlier experiments; this page is the current summary.

## What works

| Surface | Confirmed scope |
| --- | --- |
| Virtual sessions | Firefox, Kate and KCalc; shared CLI/MCP owner; app capture, semantic actions and cleanup. |
| Browser actions | Navigation, links and text entry, including recovery from failed navigation. Scrolling now verifies in both directions on the offline fixture; nested/live scroll untested. |
| Kate documents | Draft/save/revise/save with exact editor and saved-file verification; output survives cleanup. |
| Observation | Explicit app images for vision agents; optional interpreted data from a bounded, cancellable reader. DeepSeek is provisional, MiMo configurable. |
| Lifecycle | Idle/lifetime/read/action caps, watchdog teardown, owned-app cleanup and recovery journals. |
| Local live Kate | Owner-watched document workflow and app screenshot passed; exact focus and accessibility flags restored, zero Kate windows remaining. |
| Owner surfaces | JSON config (CLI > env > file > default), `systemd --user` unit via `service install|status|uninstall`, and an AppIndicator tray with status, virtual/physical preference, session cleanup and the kill switch. |

Useful agent acceptance: [Codex Luna workflow](20-codex-luna-workflow.md),
[document workflow](21-kate-acceptance-workflow.md),
[public webpage → Kate](23-public-browser-kate-agent-task.md).

## Latest work — 2026-10-03

Scroll effect verification is fixed. Firefox exposes a hidden second `scroll
pane` with the same role/label/bounds as the live one, so the verifier's
`(role, label)` lookup was ambiguous and never reached the semantic fallback.
`_scroll_viewport` now resolves the live element and failed verifications keep
their evidence in the audit. Host probe (virtual Firefox, no provider calls):
scroll down and up both pass with `measurement: semantic_content_bounds`, 10
negative and 0 opposing witness deltas, cleanup confirmed.
[doc 27](27-scroll-effect-verification.md), `run/scroll-2026-10-03/`.

Owner surfaces were added: a private JSON config (no secret values), a
`systemd --user` unit with `service install|status|uninstall` that never
auto-enables, and an AppIndicator tray. The tray's virtual/physical selection is
a preference only — a live task still needs a per-task dialog confirming owner
presence and temporary accessibility. The kill switch, loopback-only binding,
deny-by-default allowlist and audit log are unchanged.
[doc 28](28-tray-and-user-service.md).

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

1. **Reader wait:** inspect one bounded retained invalid response. Both polling
   comparison tasks completed; both reader-wait trials returned
   `invalid_judgment`. Keep ordinary screenshots as the default and heartbeat
   waiting experimental. [Comparison](24-primary-agent-wait-comparison.md).
2. **Optional watched live checks:** Firefox and cancellation/crash cleanup,
   when the owner is present. Keep each check small and useful.
3. **Optional owner surfaces:** enable the user unit at login and click through
   the tray menu while watched; this session proved SNI registration and logic
   but did not enable or click the menu.
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

The same daemon can run as a user service and the tray as its control panel:

```bash
uv run jev-desktop service install --allow-app kate --allow-app firefox  # seeds a 0600 config, never enables
systemctl --user enable --now jev-desktop.service                       # opt in explicitly
uv run jev-desktop tray                                                 # status, mode, cleanup, kill switch
```

MCP host command: `uv run jev-desktop-mcp`; it connects to the same owner.
See [doc 16](16-local-owner-and-mcp.md) for sockets, actions, images, readers,
lifecycle and live opt-in. Changes to tool schemas require a host refresh.

Use targeted offline checks and small host probes for changed behavior.
The last broad historical run passed 172 tests and built a wheel; the
2026-10-03 surfaces received 77 focused cases across the daemon, service, tray,
service unit, transaction and service modules plus one watched host probe. Do
not rerun a large matrix solely to reproduce an old test count.

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
