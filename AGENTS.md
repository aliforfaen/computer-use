# AGENTS.md

Guidance for agents working in this repo (`computer-use`, aka the Jev computer-use layer).

## Current phase

**Latest work (2026-10-06):** Jev HTTP selector calls require
`JEV_ENABLED=true` and default to disabled, including in the standalone probes.
The study harness uses CPU-only RapidOCR; the experimental CUDA probe and
disposable GPU environment were removed for cross-host portability. Qwen-OCR is
a future API test interest recorded in [doc 32](docs/32-tine-perception-study.md).
`.env.example` lists current app-specific variables without secrets.

**Previous work (2026-10-05):** the flat root modules became the installable
`src/jevdesktop/` package; the entry points are now `jevdesktop.desktop_cli`,
`jevdesktop.desktop_mcp` and `jevdesktop.desktop_tray`, workers start via
`python -m jevdesktop.<worker>`, and `jevdesktop.paths` resolves the checkout,
fixtures and run directory (ADR-028). Standalone benchmarks moved to `scripts/`
(not installed) and the probes stay in `tools/`. The owner's `assets/jev-icon.svg`
is now the app icon everywhere: the README header, the tray (rendered to a small
PNG pair under `~/.cache/jev-desktop/icons`, with a stock-icon fallback), and the
menu entry — `service install` drops
`~/.local/share/icons/hicolor/scalable/apps/jev-icon.svg` plus
`applications/jev-desktop.desktop` and `service uninstall` removes them
(ADR-029). Presentation only; no allowlist, focus, audit or authorization change.
Full offline suite (201 cases) and `uv run python -m tools.check` (101 cases) pass.

**Tine perception study (2026-10-05):** [doc 32](docs/32-tine-perception-study.md)
and `run/tine-perception-study-2026-10-05/` record virtual-only tests of
candidate filtering, AT-SPI/OCR provenance, deduplication, disagreement,
target-window crops, and a historical CPU/GPU comparison. Reuse KWin/kwin-mcp; do not build a GNOME
or Plasma extension for this. The study added a research harness and focused
offline tests; nothing is wired into `src/jevdesktop/`. Two isolated virtual
KCalc OCR-ref clicks were executed and verified during the study; this is
historical evidence, not permission for future OCR-grounded actions. Current
and future study work stops at inspection unless separately reviewed. CUDA
showed a warm speedup on the saved images but was dropped due to portability and
setup cost; the probe and GPU environment are removed. Synthetic data only, no
live desktop captures or paid Jev/reader calls, and no host driver or runtime
dependency installation. OCR packages may be installed only in a disposable
CPU venv under ignored `tmp/`. Qwen-OCR remains future test interest only.

**Previous work (2026-10-04):** the Jev selector is exercised for real and the
driver loop exists. `tools/jev_loop_probe.py` closes
`candidates -> Jev select -> act + code-owned check -> re-enumerate` as a
*client* of the owner socket — nothing is wired into the daemon (ADR-023). On a
virtual KCalc session it produced `done_verified` (digits 1,2,3 with each click
verified on the display, 4 calls), `wrong_choice` (the model pressed `Equals`
first for 2+2 and the check caught it) and `verification_unavailable` (no AT-SPI
postcondition exists for an operator click, so the loop refuses it rather than
guessing). Two guardrail changes made this possible and are recorded:
`display_text` is now a general KCalc button check with a **non-vacuous**
precondition (ADR-025), and the loop's progress travels in the state as bounded
action **labels** only (ADR-026). Browsers are explicitly *not* a Jev surface
(ADR-027): web work stays with the installed `agent-browser` over CDP
(Chromium/Brave), Orca's embedded browser with `orca-cli`, and Firefox here only
as the native AT-SPI fixture. Plan, results and next steps:
[doc 31](docs/31-jev-control-plan.md). The Jev key lives in the private
`~/.config/jev-desktop/jev.env` (0600), separate from the reader key in
`daemon.env`; the daemon never reads it.

**Previous work (2026-10-03, later session):** the testing pit was closed and the
owner surfaces finished. The suite went 190 → 177 cases by collapsing historical
harness matrices; `uv run python -m tools.check` is the 96-case core set (~4 s)
and [doc 29](docs/29-verification-runbook.md) maps a change to its cheapest
check. The heartbeat wait is reported `experimental` in `capabilities`, the CLI
help and the MCP description, with polling named as the recommended loop. The
audit is durable at `~/.config/jev-desktop/audit.jsonl` with size/age rotation
and journal pruning (ADR-021). Tray settings live in `tray.json`
(ADR-022) and `service install --tray` can add the indicator unit without ever
enabling it. `jev_selector.py` + `tools/jev_selector_probe.py` implement the
enumerated-options Jev selector offline-first, unwired, for the owner's
decision-model experiment ([doc 30](docs/30-jev-selector-experiment.md)); ADR-011
stays proposed.

**Previous work (2026-10-03):** scrolling is fixed and host-verified in both
directions. Firefox exposes a hidden second `scroll pane` with the same
role/label/bounds as the live one; the verifier now resolves the live element
and failed verifications keep their evidence in the audit. Owner surfaces were
added: a private JSON config (CLI > env > file > default, never holding secret
values), a `systemd --user` unit with `service install|status|uninstall` that
never auto-enables, and an AppIndicator tray. The tray's virtual/physical
selection is a **preference only**: a live task still opens a per-task dialog
confirming owner presence and temporary accessibility, so the local-live gate is
preserved, not relaxed. The kill switch, loopback-only binding,
deny-by-default allowlist and audit logging are unchanged. Evidence:
[doc 27](docs/27-scroll-effect-verification.md),
[doc 28](docs/28-tray-and-user-service.md), `run/scroll-2026-10-03/`.

**Latest live checkpoint (2026-10-03):** the owner-watched local live Kate workflow
passed draft/save/revise/save with four verified actions, plus a follow-up
screenshot visibly showing Kate text and its Save control. Both sessions
independently confirmed original Codex focus and AT-SPI flags restored and zero
Kate windows after cleanup. Evidence and timing: [doc 26](docs/26-live-owner.md)
and `run/live-kate-2026-10-03/`. Kate live launch now uses `--block` to retain
process/window ownership. Live `candidates`, `observe`, `wait` and `act` calls
focus the task app for matching AT-SPI state and correct screenshots, then
restore exact prior focus. The daemon is shut down. Physical-input idle
detection remains unavailable; live Firefox, cancellation/crash recovery,
tailnet deployment and general planning remain unvalidated or out of
scope. Do not call the Kate check general live-task validation.

Virtual navigation, links and text entry passed host checks; Luna completed
public webpage → Kate save/revise/save with exact file verification and cleanup
(doc 23). Scrolling now verifies in a virtual session on the offline scroll
fixture (doc 27); nested scroll regions and live-desktop scrolling remain
untested. The four-trial
comparison (doc 24) found polling completed both tasks while both wait trials
returned `invalid_judgment`; keep heartbeat waiting experimental. Explicit
readers retain per-session call limits, numeric usage accounting and killable
request processes. The current reader authorization superseded the older
consumed paid-probe cap and dollar-reservation refusal.

**Testing preference:** this is the owner's small personal tool. Use targeted
checks for changed behavior and small useful host probes; `tools.check` first.
Avoid repeated full suites and large matrices unless a concrete failure or broad
change requires one. The owner reduced this session's comparison from eight to
four trials.

## Start here

Read `docs/HANDOFF.md` for current status and next work, then
`docs/16-local-owner-and-mcp.md` for supported commands and
`docs/26-live-owner.md` for the live contract and host evidence.
Earlier milestone/test counts in numbered docs are historical checkpoints.
Current reader authorization supersedes the consumed one-request probe cap;
keep explicit per-session limits and accounting. M1–M3 local work is already
authorized. Tailnet deployment and general planning remain later gates.

Screenshot-driven computer use uses `kwin-mcp`; Jev is an optional accelerator.
ADR-011 remains proposed; ADR-012 requires images or interpreted data and
ADR-013 makes app capture the default. DeepSeek Flash is the provisional
reader; MiMo is configurable.

## Owner context

- Owner: messhias. Machine: `cachy` (CachyOS, Arch-based), KDE Plasma 6, Wayland session.
- Tone: concise and direct. Discussion is text-first and short.
- Owner thinks best by building; prefer small runnable probes over long prose.

## Rules for this project

- **Reuse before writing.** A driver already exists for almost every layer here
  (`docs/02-prior-art.md`). Name the project being reused in the design before proposing new code.
- **Jev is a pure selector.** Jev output selects from options we enumerated in code.
  It must never produce coordinates, CSS selectors, shell commands, file paths or code.
  Only a text-argument helper (a small LLM) may produce free text, and only for typing.
  The separate vision reader may report observed text/data under ADR-012;
  its descriptions are not executable targets. Action grounding remains open.
- Jev provider calls require `JEV_ENABLED=true`; default is disabled. This
  environment gate does not wire Jev into the daemon.
- **Every action is verified.** After execution, re-read state (AT-SPI property or
  screenshot diff) before the next decision. `DONE` from the model is a proposal, not proof.
- **Guardrails live outside the model.** Allow/deny lists, confirmation gates, step and
  budget caps, and rollback are plain code. Jev may advise via `noul`; it does not authorize.
- **Autonomy mode is policy, not model input.** `supervised` (approve every action),
  `guarded` (default: read/reversible auto, confirm destructive) and `yolo` (no prompts).
  The **app allowlist, step caps and audit log apply in every mode, including `yolo`.**
- **Live mode always restores focus.** save focused window → focus target → act → restore.
  Never leave the user's keystrokes landing in a window the agent touched.
- **Pin and record versions.** kwin-mcp `>=0.8.0` is required (0.7.0 misroutes input) and its
  KWin EIS D-Bus interface is private. Record the KWin version alongside any behaviour claim.

## Remote-caller rules (tailnet)

The owner chose **tailnet ACLs as the only access gate** and **remote reach equal to local**
(ADR-006/007). That makes the following mandatory rather than optional — never quietly relax them:

- **Bind `127.0.0.1` only.** `tailscale serve` proxies to it. Never bind a tailnet-visible or
  LAN-visible interface, and never expose this over `tailscale funnel` (public, and it strips
  identity headers).
- **Deny-by-default app allowlist applies in every mode, including `yolo`.** This is now the main
  blast-radius limiter for remote callers; it is explicit config, not default-open.
- **Audit every call** to an append-only JSONL log: caller node, transport, tool, autonomy mode,
  Jev answers with confidences, action, verification outcome, timestamp. No sampling.
- **A local kill switch must exist and stay simple** (`jev-desktop stop --all`, plus
  `tailscale serve off` as the hard stop).
- **Never put secrets in state.** No clipboard contents, no secret-store window titles, no typed
  text in the state sent to Jev or written to logs.
- **Remote live-desktop tasks**: explicit per-task request, recorded in the audit log, one at a
  time behind a lock, mandatory focus save→act→restore, and refuse to start while the owner's
  physical input has been active recently unless overridden.
- Keep the `authorizer` seam in place even though it currently allows everything — a bearer token
  or Tailscale app capabilities must be addable later as config, not a rewrite.
- Do not stream screenshots or video to remote callers by default; metadata and a hash, and a
  downscaled JPEG only on explicit request.
- **Never write secrets to tracked files or logs.** Private keys live outside the
  checkout: `~/.config/jev-desktop/daemon.env` (0600) holds `DEEPSEEK_API_KEY` for the
  reader, and `~/.config/jev-desktop/jev.env` (0600) holds `JEV_API_KEY` for the Jev
  selector probe. The daemon never reads `jev.env`. Keep secrets out of state,
  requests, reports and the audit.
- **Document platform constraints, don't paper over them.** Wayland input is focus-routed.
  If something cannot work, say so in `docs/03-wayland-constraints.md` instead of degrading silently.
- **Ask before destructive things.** Anything that types into a terminal, clicks a
  destructive dialog, or accesses `~` broadly needs user confirmation (see owner profile).

## Layout

```
README.md            project overview, endgoal, reading order
AGENTS.md            this file
.env.example         current app-specific environment variables; placeholders only
HANDOFF.md           (in docs/) start here for a fresh session
docs/01-jev-primer.md
docs/02-prior-art.md
docs/03-wayland-constraints.md
docs/04-architecture.md       append-only ADR-001..029 log
docs/05-open-questions.md     P0 checklist
docs/06-remote-agents.md      tailnet topology + access model
docs/07-p1-selector-probe.md  KCalc observation + decision-only Jev probe
docs/08-p2-kcalc-proof.md     one executed and verified action
docs/09-observation-options.md
docs/10-heartbeat-direction.md
docs/11-vision-model-shortlist.md
docs/12-deepseek-speed-probe.md
docs/13-runnable-vision-benchmark.md
docs/14-app-build-plan.md       staged app plan and worker handoffs
docs/15-virtual-session-slice.md reusable observation and verified actions
docs/16-local-owner-and-mcp.md local owner, CLI/MCP and lifecycle limits
docs/17-dynamic-wait-baseline.md local M4a wait implementation, host baseline and limits
tests/                      offline tests; run as tests.test_MODULE
tools/                      standalone probes; run as python -m tools.MODULE
src/jevdesktop/             installed runtime package (service, CLI, MCP, tray, workers)
scripts/                    standalone benchmarks/harnesses; run as python -m scripts.MODULE
assets/                     app icon (jev-icon.svg) + jev-desktop.desktop launcher entry
benchmark_fixtures/         local HTML fixtures, resolved by jevdesktop.paths
```

**Where new files go.** Runtime module → `src/jevdesktop/`. Standalone benchmark or
one-off harness → `scripts/`. Verification or owner probe → `tools/`. Unit test →
`tests/`. Icon, image or `.desktop` asset → `assets/`. HTML fixture →
`benchmark_fixtures/`. Documentation → `docs/`. Captures and trial output → `run/`
(ignored). Tine study prototypes belong in `scripts/`, their deterministic fusion
tests in `tests/`, and study-only captures/reports in
`run/tine-perception-study-YYYY-MM-DD/`. Nothing new belongs in the repository
root or `src/jevdesktop/` until a reviewed result authorizes runtime work.

## Conventions

- Docs, benchmarks and the authorized M1–M4a virtual wait baseline are deliverables.
  One idea per doc, short sections, link out to sources.
- Cite external claims with a URL. Mark vendor-reported numbers as vendor-reported.
- Record decisions as short ADR-style entries in the ADR log at the end of
  `docs/04-architecture.md` (append-only; supersede rather than rewrite).
- GitHub/origin is `https://github.com/aliforfaen/computer-use.git`; `main`
  contains the published checkpoint. Keep the working tree clean after commits.
