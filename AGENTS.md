# AGENTS.md

Guidance for agents working in this repo (`computer-use`, aka the Jev computer-use layer).

## Current phase

**Latest checkpoint (2026-10-02):** owner authorized broader virtual actions,
DeepSeek interpretation and a small primary-agent wait comparison. Navigation,
links and text entry passed host checks; Luna completed public webpage → Kate
save/revise/save with exact file verification and cleanup (doc 23). Scrolling
still fails host effect verification; do not claim it is host-validated. The
supported owner now permits explicit readers with per-session call limits,
numeric usage accounting and killable request processes. This authorization
supersedes the older consumed paid-probe cap and dollar-reservation refusal.
The four-trial comparison (doc 24) found polling completed both tasks while
both wait trials returned `invalid_judgment`; keep heartbeat waiting experimental.
No general live MCP support or tailnet deployment was added. See doc 25 for
remaining work and the owner-watched phase last.

**Testing preference:** this is the owner's small personal tool. Use targeted
checks for changed behavior and small useful host probes. Avoid repeated full
suites and large matrices unless a concrete failure or broad change requires
one. The owner reduced this session's comparison from eight to four trials.

Historical checkpoint below:

**2026-10-02: M1–M4a are implemented and host-validated; the M4b DeepSeek
heartbeat was reviewed, fixed, tested and completed one bounded follow-up
request.** CLI/MCP share a virtual session; KCalc, Kate and Firefox effects and
cleanup passed. The no-provider M4a baseline classified ten polling/watcher
fixture outcomes with cleanup; it does not establish acceleration. The M4b
follow-up returned one `deepseek-flash` structured judgment in 1.41 s with
reported usage, no rate limit and confirmed session/profile cleanup. There is
no remote HTTP surface, live desktop mode or general planner. Start with
`docs/HANDOFF.md`, `docs/17-dynamic-wait-baseline.md`,
`docs/15-virtual-session-slice.md` and `docs/16-local-owner-and-mcp.md`.

The current owner-supported direction is screenshot-driven computer use over
`kwin-mcp`, with Jev as an optional accelerator. ADR-011 remains proposed;
ADR-012 requires images or interpreted data, and ADR-013 makes app capture
the default. DeepSeek Flash is the provisional reader; MiMo is configurable.

The owner authorized benchmark implementation, the M1–M2 observation and
verified virtual-task slice, the M3 local owner/CLI/MCP implementation, and one
bounded DeepSeek heartbeat probe under $1 (all 2026-10-01). Those local virtual
slices may proceed without further approval. On 2026-10-02 the owner resumed
work; the frozen M4b timeout/journal/interrupt changes were independently
reviewed, and a second-pass review corrected the deadline guarantee (time and
size are now bounded per raw response chunk, including partial lines; no bound
is claimed across header reception). Fourteen new offline tests cover these
paths (111 total, all passing), and the probe refuses to overwrite existing
evidence. The probe's single configured follow-up request was then made and
completed. That
historical follow-up cap was consumed; the newer explicit owner authorization
above permits the current bounded reader/comparison work. General planning, tailnet deployment and live
tasks remain later gates; do not repeat authorization questions for M1–M3.
Remaining P0 measurements are in `docs/05-open-questions.md`, not the entire
next-work plan.

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
- **Never write secrets to tracked files or logs.** The ignored `.env` holds
  `JEV_API_KEY` for TypeSafe direct. Load it into the process environment for
  `https://api.typesafe.ai/v1/systemone`; portal tokens stay private.
- **Document platform constraints, don't paper over them.** Wayland input is focus-routed.
  If something cannot work, say so in `docs/03-wayland-constraints.md` instead of degrading silently.
- **Ask before destructive things.** Anything that types into a terminal, clicks a
  destructive dialog, or accesses `~` broadly needs user confirmation (see owner profile).

## Layout

```
README.md            project overview, endgoal, reading order
AGENTS.md            this file
HANDOFF.md           (in docs/) start here for a fresh session
docs/01-jev-primer.md
docs/02-prior-art.md
docs/03-wayland-constraints.md
docs/04-architecture.md       append-only ADR-001..015 log
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
benchmark_capture.py        isolated fixture capture; benchmark_fixtures/
vision_benchmark.py         configurable image-reader benchmark
```

## Conventions

- Docs, benchmarks and the authorized M1–M4a virtual wait baseline are deliverables.
  One idea per doc, short sections, link out to sources.
- Cite external claims with a URL. Mark vendor-reported numbers as vendor-reported.
- Record decisions as short ADR-style entries in the ADR log at the end of
  `docs/04-architecture.md` (append-only; supersede rather than rewrite).
- GitHub/origin is `https://github.com/aliforfaen/computer-use.git`; `main`
  contains the published checkpoint. Keep the working tree clean after commits.
