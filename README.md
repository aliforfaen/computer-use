# Jev Computer-Use Layer (working title)

A decision-model-driven computer-use layer for agents on **CachyOS + KDE Plasma 6 Wayland**.

**Status: decisions locked (ADR-001…004), no code yet.** Next: the P0 environment probe.

Goal: let agents (Pi, Claude Code, Hermes, Codex, …) observe and operate the local KDE
desktop, where the *decision* is made by [Jev](docs/01-jev-primer.md) — a non-generative,
calibrated decision model — and all execution stays in deterministic, guardrailed code.

## Why this shape

- Jev does not generate text. It picks from bounded option sets and returns
  probabilities/confidence. That maps cleanly onto "which action, which element".
- Existing Linux computer-use drivers already solve the OS plumbing (AT-SPI2 trees,
  KWin/EIS input injection, portal screenshots). We should not rewrite them.
- What is missing in the ecosystem: a **state compiler** (UI tree → bounded, budgeted
  candidate table) and a **decision + guardrail policy** on top of an existing driver.

## Read in this order

1. [docs/01-jev-primer.md](docs/01-jev-primer.md) — what Jev is, the three primitives
2. [docs/02-prior-art.md](docs/02-prior-art.md) — projects that already do parts of this
3. [docs/03-wayland-constraints.md](docs/03-wayland-constraints.md) — what is actually possible on KDE Wayland
4. [docs/04-architecture.md](docs/04-architecture.md) — proposed layer, build-vs-reuse
5. [docs/05-open-questions.md](docs/05-open-questions.md) — P0 probe checklist + open preferences

## Locked shape (ADR-001…004)

- A thin **Jev policy layer over `kwin-mcp`** — we do not write a driver.
- **Virtual KWin session by default**, live desktop opt-in.
- Three autonomy modes: `supervised`, `guarded` (default), `yolo`.
- One core, two surfaces: **CLI + MCP server**.

## Constraints

- Target: CachyOS (Arch-based), KDE Plasma 6, Wayland, single user (messhias).
- Prefer reuse over invention; fork or wrap rather than rewrite.
- Model output must never become coordinates, selectors, shell commands or executable code.
- No credentials in the repo. `JEV_API_KEY` (`jv_live_…`) stays in env.
