# Jev Computer-Use Layer (working title)

A decision-model-driven computer-use layer for agents on **CachyOS + KDE Plasma 6 Wayland**,
reachable by **remote agents over the tailnet** as well as local ones.

**Status: decisions locked (ADR-001…008; ADR-008 confirmed in ADR-009), P0 in progress, no project code yet.**
Next step: the P0 probe checklist in [docs/05-open-questions.md](docs/05-open-questions.md).
Picking this up cold? Read [docs/HANDOFF.md](docs/HANDOFF.md).

## Endgoal

Your Hermes assistants — and other agents that do **not** run on this machine — connect over the
tailnet and operate the desktop. The decision is made by [Jev](docs/01-jev-primer.md), a
non-generative calibrated decision model; execution stays in deterministic, guardrailed code.

```
   local agents (stdio)          remote agents over tailnet (Streamable HTTP)
   Pi / Claude Code / Codex      Hermes @ VPS · Hermes @ GPU box · any tailnet node
        └───────────────┬───────────────────┘
                        │  tailscale serve -> 127.0.0.1:7810
               cachy · jev-desktop  (single instance)
               state compiler · Jev policy · guardrails · audit log · verifier
                        │
                 kwin-mcp  (AT-SPI trees, KWin EIS input, virtual sessions)
```

## Why this shape

- Jev does not generate text. It picks from bounded option sets and returns
  probabilities/confidence — a clean mapping onto "which action, which element".
- Existing Linux computer-use drivers already solve much of the OS plumbing (AT-SPI2 trees,
  KWin/EIS input injection, KWin ScreenShot2 with Spectacle fallback). We do not rewrite them.
- Missing in the ecosystem and therefore ours: a **state compiler** (UI tree → bounded, budgeted
  candidate table), a **decision + guardrail policy**, and a **remote surface** that keeps
  guardrails non-bypassable.

## Read in this order

1. [docs/HANDOFF.md](docs/HANDOFF.md) — start here for a fresh session
2. [docs/01-jev-primer.md](docs/01-jev-primer.md) — what Jev is, the three primitives
3. [docs/02-prior-art.md](docs/02-prior-art.md) — projects that already do parts of this
4. [docs/03-wayland-constraints.md](docs/03-wayland-constraints.md) — what is actually possible on KDE Wayland
5. [docs/04-architecture.md](docs/04-architecture.md) — the layer, build-vs-reuse, ADR log
6. [docs/05-open-questions.md](docs/05-open-questions.md) — P0 probe checklist + open preferences
7. [docs/06-remote-agents.md](docs/06-remote-agents.md) — tailnet topology, access model, Hermes config

## Locked shape (ADR-001…007)

- A thin **Jev policy layer over `kwin-mcp`** — we do not write a driver.
- **One instance on `cachy`**; MCP **stdio** locally, **Streamable HTTP** remotely via `tailscale serve`.
- **Virtual KWin session by default**, live desktop opt-in per task.
- Three autonomy modes: `supervised`, `guarded` (default), `yolo`.
- **Remote callers have the same reach as local** (owner decision) and **tailnet ACLs are the only
  access gate** (owner decision) — which is why the compensating controls in
  `docs/06-remote-agents.md` are mandatory, not optional.
- One core, two surfaces: **CLI + MCP server**.

## Constraints

- Target: CachyOS (Arch-based), KDE Plasma 6, Wayland, single user (messhias).
- Prefer reuse over invention; fork or wrap rather than rewrite.
- Model output must never become coordinates, selectors, shell commands or executable code.
- No credentials in the repo. `JEV_API_KEY` (`jv_live_…`) stays on `cachy`, in env.
- Never expose this over `tailscale funnel` (public, no identity headers).
