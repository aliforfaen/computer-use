# AGENTS.md

Guidance for agents working in this repo (`computer-use`, aka the Jev computer-use layer).

## Current phase

**Decisions locked, no implementation yet.** ADR-001…004 in `docs/04-architecture.md` fix the
shape: a Jev policy layer over `kwin-mcp`, virtual sessions by default, CLI + MCP surfaces,
three autonomy modes. Next step is the **P0 probe checklist** in `docs/05-open-questions.md`.
Do not start the state compiler or MCP server until P0 measurements are in — and ask before
adding code to `main` beyond throwaway probe scripts.

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
- **Never write secrets.** `JEV_API_KEY` / `jv_live_…` and any portal tokens stay in env vars.
- **Document platform constraints, don't paper over them.** Wayland input is focus-routed.
  If something cannot work, say so in `docs/03-wayland-constraints.md` instead of degrading silently.
- **Ask before destructive things.** Anything that types into a terminal, clicks a
  destructive dialog, or accesses `~` broadly needs user confirmation (see owner profile).

## Layout

```
README.md            project overview, reading order
AGENTS.md            this file
docs/01-jev-primer.md
docs/02-prior-art.md
docs/03-wayland-constraints.md
docs/04-architecture.md
docs/05-open-questions.md
```

## Conventions

- Docs are the deliverable in this phase. One idea per doc, short sections, link out to sources.
- Cite external claims with a URL. Mark vendor-reported numbers as vendor-reported.
- Record decisions as short ADR-style entries in the ADR log at the end of
  `docs/04-architecture.md` (append-only; supersede rather than rewrite).
- Keep `main` clean; this is a standalone git repo (no remote yet).
