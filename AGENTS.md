# AGENTS.md

Guidance for agents working in this repo (`computer-use`, aka the Jev computer-use layer).

## Current phase

**Decisions locked (ADR-001…008; ADR-008 confirmed in ADR-009), no implementation yet.**
P0 measurement is underway. The shape: a Jev policy layer over `kwin-mcp`, one instance
on `cachy`, virtual sessions by default, CLI + MCP surfaces, three autonomy modes, reachable by
remote agents over the tailnet.

Next step is the **P0 probe checklist** in `docs/05-open-questions.md`. Do not start the state
compiler or MCP server until P0 measurements are in — and ask before adding code to `main`
beyond throwaway probe scripts.

## Owner context

- Owner: messhias. Machine: `cachy` (CachyOS, Arch-based), KDE Plasma 6, Wayland session.
- Tone: concise and direct. Discussion is text-first and short.
- Owner thinks best by building; prefer small runnable probes over long prose.

## Rules for this project

- **Reuse before writing.** A driver already exists for almost every layer here
  (`docs/02-prior-art.md`). Name the project being reused in the design before proposing new code.
- **The model is a pure selector.** Jev output selects from options we enumerated in code.
  It must never produce coordinates, CSS selectors, shell commands, file paths or code.
  Only a text-argument helper (a small LLM) may produce free text, and only for typing.
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
- **Never write secrets.** `JEV_API_KEY` / `jv_live_…` and any portal tokens stay in env vars.
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
docs/04-architecture.md       ADR-001..008 log
docs/05-open-questions.md     P0 checklist
docs/06-remote-agents.md      tailnet topology + access model
```

## Conventions

- Docs are the deliverable in this phase. One idea per doc, short sections, link out to sources.
- Cite external claims with a URL. Mark vendor-reported numbers as vendor-reported.
- Record decisions as short ADR-style entries in the ADR log at the end of
  `docs/04-architecture.md` (append-only; supersede rather than rewrite).
- Keep `main` clean; this is a standalone git repo (no remote yet).
