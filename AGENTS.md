# AGENTS.md

Guidance for agents working in this repo (`computer-use`, aka the Jev computer-use layer).

## Current phase

**Discussion / research only.** Do not start implementing a driver, daemon, or MCP server
until the open decisions in `docs/05-open-questions.md` are answered by the owner.
Writing docs, probes, and spikes is fine — ask first.

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
- Record decisions as short ADR-style entries at the bottom of `docs/04-architecture.md` once made.
- Keep `main` clean; this is a standalone git repo (no remote yet).
