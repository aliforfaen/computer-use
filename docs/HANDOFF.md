# Handoff — start here

Updated **2026-10-06**. Checkout: `/home/messhias/lamasync/projects/computer-use`.
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
| Owner surfaces | JSON config (CLI > env > file > default), `systemd --user` unit via `service install|status|uninstall`, and an AppIndicator tray with status, virtual/physical preference, session cleanup, settings and the kill switch. The Jev icon is the app, tray and menu icon. |
| Audit retention | Durable audit at `~/.config/jev-desktop/audit.jsonl`, size-rotated (4 MiB, 3 backups) and age-pruned (30 days); stale session journals swept at startup. |
| Jev selector seam | `src/jevdesktop/jev_selector.py` builds an enumerated decision request from candidate output and validates the typed answer in code. Offline probe, not wired into the loop. |
| Jev driver loop | `tools/jev_loop_probe.py` closes select → act → verify → re-enumerate against the owner socket (unwired). Virtual KCalc: `done_verified` on 1-2-3 with every click checked on the display; `wrong_choice` caught when the model pressed `Equals` first for 2+2; `verification_unavailable` when no postcondition exists for an operator click. [doc 31](31-jev-control-plan.md), `run/jev-loop-2026-10-04/`. |

Useful agent acceptance: [Codex Luna workflow](20-codex-luna-workflow.md),
[document workflow](21-kate-acceptance-workflow.md),
[public webpage → Kate](23-public-browser-kate-agent-task.md).

## Latest work — 2026-10-06

**Jev opt-in and perception follow-up (2026-10-06).** Jev network calls now
require `JEV_ENABLED=true`; the default is disabled, and both paid probes stop
before loading the key if disabled. This does not connect Jev to the daemon.
`.env.example` lists the app's current provider and daemon variables with no
secrets. The OCR study is CPU-only now: the temporary CUDA harness and GPU
environment were removed after the owner chose cross-host simplicity. Qwen-OCR
is recorded as a future API comparison interest; no API request or image upload
was made.

**Package and icon.** The flat root modules are now the installed
`src/jevdesktop/` package (ADR-028): entry points are `jevdesktop.desktop_cli`,
`jevdesktop.desktop_mcp` and `jevdesktop.desktop_tray`, worker children start via
`python -m jevdesktop.<worker>`, and `jevdesktop.paths` is the single place that
resolves the checkout, fixtures and `run/`. Standalone benchmarks moved to
`scripts/`; verification probes stay in `tools/`. The owner's `assets/jev-icon.svg`
is the app icon in the README, the tray (rendered to `~/.cache/jev-desktop/icons`
with a stock-icon fallback) and the menu: `service install` writes
`~/.local/share/icons/hicolor/scalable/apps/jev-icon.svg` and
`applications/jev-desktop.desktop`, and `service uninstall` removes both
(ADR-029). Presentation only. `uv run python -m tools.check` is the 101-case core
set; the full offline suite is 201 cases.

**Tine perception study complete.** [Doc 32](32-tine-perception-study.md)
summarizes the virtual KCalc/Kate and synthetic-panel run. Window crops helped
synthetic noise and KCalc latency, but Kate was slightly slower and missed the
gold `Edit` label. A one-host CUDA comparison across six saved images reduced
mean warm OCR p50 from 271 ms to 197 ms with the same recognized text; its
portability/setup cost was not worth retaining. The current benchmark harness
is CPU-only.
The 25 focused fusion tests passed. Two virtual OCR-ref clicks happened during
a queued probe despite the later inspection-only boundary; both display checks
passed, stale/conflicting refs were rejected, and the reusable click path has
been removed. No live desktop or runtime integration was used. Reuse
KWin/kwin-mcp; no Plasma extension is needed at this point.

**Previous work (2026-10-04): the Jev loop runs.**

`tools/jev_loop_probe.py` drives
`candidates -> Jev select -> act + code-owned check -> re-enumerate` as a client
of the owner socket; nothing is wired into the daemon. On a virtual KCalc
session: `done_verified` for "enter 1, 2 then 3" (4 calls, each click verified on
the display, and the model used the appended history to progress); `wrong_choice`
when it pressed `Equals` first for 2+2 — the check caught it and the loop
stopped; `verification_unavailable` when it correctly chose `Add`, because KCalc
exposes no postcondition for an operator click and the loop refuses to act
unverified. Two guardrail changes: `display_text` became a general KCalc button
check with a non-vacuous precondition (ADR-025), and loop progress travels as
bounded action **labels** in the state (ADR-026). Browsers are not a Jev surface
(ADR-027) — `agent-browser` over CDP already covers Brave/Chromium.
[doc 31](31-jev-control-plan.md), `run/jev-loop-2026-10-04/`.

**Keys.** The Jev key lives in `~/.config/jev-desktop/jev.env` (0600), separate
from the reader key in `daemon.env`; `--dotenv` and the loop probe read it, the
daemon never does. The reader is armed (DeepSeek Flash, 8 calls/session) and
verified end to end on a virtual app.

**Next, in order:** a Kate verified loop (`replace_document` + `document_saved`);
close the modal guardrail gaps (destructive risk words, a modal postcondition);
the modal experiment; ambiguity runs. See doc 31 §6.

## Latest work — 2026-10-03 (later session)

**Testing pit closed.** The suite went from 190 to 177 cases by collapsing
exhaustive per-failure-point and historical harness matrices; the one-off wait
comparison fixture was deleted. `uv run python -m tools.check` runs the 96-case
core set in ~4 s, and [doc 29](29-verification-runbook.md) maps a change to its
cheapest check. The heartbeat wait is now reported `experimental` in
`capabilities`, the CLI help and the MCP description, with polling named as the
recommended loop.

**Audit retention.** The audit moved to the durable config directory with
size/age rotation and session-journal pruning (ADR-021).

**Tray settings.** `~/.config/jev-desktop/tray.json` now holds desktop-mode
preference, autonomy mode, poll interval and the two confirmation toggles, with
a Settings dialog and an Open-config-folder item. `service install --tray` can
add the indicator unit; it is still never enabled automatically (ADR-022).

**Jev path.** `jev_selector.py` + `tools/jev_selector_probe.py` + doc 30: an
offline-first, enumerated-options-only selector with a one-call `HttpSelector`,
ready for the owner to test the decision-model idea cheaply. ADR-011 stays
proposed; nothing is wired into the running owner.

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
6. **Future OCR API comparison:** Qwen-OCR is a candidate in [doc 32](32-tine-perception-study.md).
   Compare only after choosing synthetic/virtual crops and reviewing the external
   upload, latency, output quality and cost. Do not wire it into the runtime yet.

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

Use targeted offline checks and small host probes for changed behavior:
`uv run python -m tools.check` is the 101-case core set (~4 s), and
[doc 29](29-verification-runbook.md) maps a change type to its cheapest check.
The suite is 201 cases after collapsing the historical harness matrices; full
discovery is ~10 s but only needed for cross-module, packaging or contract
changes. Do not rerun a large matrix solely to reproduce an old test count.

## Cleanup checkpoint

The 17 test files live in `tests/`. The installed runtime lives in
`src/jevdesktop/` (entry points `jevdesktop.desktop_cli`, `jevdesktop.desktop_mcp`,
`jevdesktop.desktop_tray`; workers via `python -m jevdesktop.<worker>`). Standalone
benchmarks moved to `scripts/` (`python -m scripts.NAME`); standalone probes stay in
`tools/` (`python -m tools.NAME`: `check`, `local_service_probe`, `mcp_trial_client`,
`scroll_effect_probe`, `vision_wait_probe`). The app icon and `.desktop` entry live in
`assets/` and are installed by `service install`. `benchmark_fixtures/` stays at the
root and is resolved by `jevdesktop.paths`; packaging was updated to a `src/` layout
and a clean wheel ships only `jevdesktop/`.

Verification: the full offline suite (201 cases) and the fast core set (101 cases)
pass; the tray icon render and the `service install`/`uninstall` desktop assets were
checked on the host into a temporary XDG data home. No live apps or provider calls
were used.

## Repository and operational notes

- New files go by kind: runtime module → `src/jevdesktop/`, standalone
  benchmark/harness → `scripts/`, probe → `tools/`, test → `tests/`, icon/image/
  `.desktop` asset → `assets/`, HTML fixture → `benchmark_fixtures/`, docs → `docs/`,
  captures and trial output → `run/` (ignored). Nothing new belongs in the root.
  See the layout sections in README and AGENTS.
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
