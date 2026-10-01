# Jev Computer-Use Layer (working title)

A screenshot-driven computer-use layer for agents on **CachyOS + KDE Plasma 6 Wayland**,
reachable by **remote agents over the tailnet** as well as local ones.

**Status, 2026-10-01: runnable virtual Kate/Firefox vision benchmark, plus a
verified one-action KCalc proof.** App-window capture is the default; DeepSeek
Flash is the provisional reader, with MiMo and optional crops configurable.
The app baseline measured 28/30 exact facts at 1.24 s median for DeepSeek,
27/30 at 5.45 s for MiMo. Both missed disabled-button state.

The owner-supported direction is a primary screenshot agent with optional
heartbeat acceleration. Explicit images and interpreted data for text-only
callers are required. No general service or heartbeat exists yet; planner
placement and action grounding remain open under proposed ADR-011.
Picking this up cold? Start with [docs/HANDOFF.md](docs/HANDOFF.md), then
[the runnable benchmark and measurements](docs/13-runnable-vision-benchmark.md).

## Endgoal

Your Hermes assistants — and other agents that do **not** run on this machine — connect over the
tailnet and operate the desktop. A primary vision agent interprets the task;
[Jev](docs/01-jev-primer.md) is a candidate accelerator for waits and familiar
workflows. Execution stays in deterministic, guardrailed code. The original
Jev-led architecture below is retained as historical context pending
ADR-011's final contract; it is not a diagram of a deployed service.

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

## Reading guide

For current work, read the handoff, doc 13's runnable suite, and doc 10's
observation/heartbeat direction first. The numbered documents below retain
the design and measurement history.

1. [docs/HANDOFF.md](docs/HANDOFF.md) — start here for a fresh session
2. [docs/01-jev-primer.md](docs/01-jev-primer.md) — what Jev is, the three primitives
3. [docs/02-prior-art.md](docs/02-prior-art.md) — projects that already do parts of this
4. [docs/03-wayland-constraints.md](docs/03-wayland-constraints.md) — what is actually possible on KDE Wayland
5. [docs/04-architecture.md](docs/04-architecture.md) — the layer, build-vs-reuse, ADR log
6. [docs/05-open-questions.md](docs/05-open-questions.md) — P0 probe checklist + open preferences
7. [docs/06-remote-agents.md](docs/06-remote-agents.md) — tailnet topology, access model, Hermes config
8. [docs/07-p1-selector-probe.md](docs/07-p1-selector-probe.md) — virtual KCalc observations and bounded Jev choices
9. [docs/08-p2-kcalc-proof.md](docs/08-p2-kcalc-proof.md) — one executed and verified action
10. [docs/09-observation-options.md](docs/09-observation-options.md) — structured observation alternatives
11. [docs/10-heartbeat-direction.md](docs/10-heartbeat-direction.md) — current direction and benchmark
12. [docs/11-vision-model-shortlist.md](docs/11-vision-model-shortlist.md) — provider/model candidates
13. [docs/12-deepseek-speed-probe.md](docs/12-deepseek-speed-probe.md) — measured image latency and crop comparison
14. [docs/13-runnable-vision-benchmark.md](docs/13-runnable-vision-benchmark.md) — runnable Kate/webpage suite and DeepSeek/MiMo comparison

## Run the vision benchmark

With `DEEPSEEK_API_KEY` in ignored `.env`, capture five disposable virtual
Kate/Firefox fixtures and run ten bounded image-reading requests:

```bash
uv run --with kwin-mcp==0.10.0 --with Pillow --with httpx python vision_benchmark.py --output run/baseline --max-calls 10
```

App-window images are the default. See [the benchmark guide](docs/13-runnable-vision-benchmark.md)
for capture-only mode, reusing images with MiMo or another compatible provider,
optional crops and results. This suite scripts the fixture actions; general
agent planning and the desktop server are still pending.

## Original decisions (ADR-001…010)

- A thin **Jev policy layer over `kwin-mcp`** — we do not write a driver.
- **One instance on `cachy`**; MCP **stdio** locally, **Streamable HTTP** remotely via `tailscale serve`.
- **Virtual KWin session by default**, live desktop opt-in per task.
- Three autonomy modes: `supervised`, `guarded` (default), `yolo`.
- **Remote callers have the same reach as local** (owner decision) and **tailnet ACLs are the only
  access gate** (owner decision) — which is why the compensating controls in
  `docs/06-remote-agents.md` are mandatory, not optional.
- One core, two surfaces: **CLI + MCP server**.
- The loop runs on `cachy`, with Jev choosing one step at a time (ADR-008/009).
- TypeSafe direct is the selected Jev provider for P1 (ADR-010).

Later requirements: image or interpreted-data observations (ADR-012),
app-window scope by default (ADR-013). Screenshot-led planning and optional
Jev acceleration remain proposed in ADR-011; see the handoff for next work.

## Constraints

- Target: CachyOS (Arch-based), KDE Plasma 6, Wayland, single user (messhias).
- Prefer reuse over invention; fork or wrap rather than rewrite.
- Model output must never become coordinates, selectors, shell commands or executable code.
- No tracked credentials. An ignored `.env` on `cachy` holds `JEV_API_KEY` for
  TypeSafe direct, selected for P1 after the [P0 comparison](docs/05-open-questions.md#p0-observations--2026-09-29).
- Never expose this over `tailscale funnel` (public, no identity headers).

## Narrow P2 proof

`p2_kcalc.py` runs the fixed task “enter digit 1” in an isolated KCalc session, asks the
TypeSafe direct selector to choose from observed visible buttons, validates its answer in code,
clicks the freshly re-read `One` button through `kwin-mcp`, and verifies the editable display
changed exactly from blank to `1`. It is a one-action proof, not the state compiler or server.
The [executed result and limits](docs/08-p2-kcalc-proof.md) are recorded separately.

Run it on the KDE host with `JEV_API_KEY` exported or present in the ignored local `.env`:

```sh
uv run --with 'kwin-mcp==0.10.0' python p2_kcalc.py
```

The append-only audit goes to `${XDG_STATE_HOME:-~/.local/state}/jev-desktop/audit.jsonl`.
Focused parser and policy checks run with `python -m unittest test_p2_kcalc.py`.
