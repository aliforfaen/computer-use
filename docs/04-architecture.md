# 04 — Proposed architecture

Not a spec. A shape to react to.

## The layer, in one picture

```
                     agent (Pi / Claude Code / Hermes / Codex)
                                     │ MCP
        ┌────────────────────────────▼─────────────────────────────┐
        │  jev-desktop  (the thing we would own)                   │
        │                                                          │
        │  1. OBSERVER      AT-SPI tree + window list + a11y flags  │  ← reuse driver
        │                   optional screenshot (verification only) │
        │                                                          │
        │  2. STATE COMPILER                                       │  ← OUR REAL IP
        │     tree → dedup → filter → index → text table            │
        │     hard token budget, stable refs, app profiles          │
        │                                                          │
        │  3. POLICY        ONE Jev /decide call per cycle          │  ← ours (pattern from
        │     action  : choice  {click,type,key,scroll,activate,    │     jev-ultrafast)
        │                         wait,done,blocked}                │
        │     target  : choice  (candidates valid for the action)   │
        │     need_text: noul   (does this step need an LLM?)       │
        │     risk     : noul   (destructive / irreversible?)       │
        │     progress : score  (did the last action help?)         │
        │                                                          │
        │  4. GUARDRAILS    code only: allowlists, caps, confirm    │  ← ours
        │     gates, dry-run, save/restore focus, kill switch       │
        │                                                          │
        │  5. EXECUTOR      AT-SPI action → EIS/libei → ydotool     │  ← reuse driver
        │     text via small LLM, JSON-parsed, never a command      │
        │                                                          │
        │  6. VERIFIER      re-read AT-SPI / focused window /       │  ← ours (thin)
        │     cropped screenshot diff; DONE is a proposal           │
        └────────────────────────────┬─────────────────────────────┘
                                     │
        kwin-mcp (KWin EIS, AT-SPI, virtual sessions)  ← layer 0, reuse as-is
```

Two decisions per cycle in one round trip, exactly like `jev-ultrafast`. The model only ever
picks from indices we created. Typed answers branch in plain code.

## Build vs reuse

| Layer | Reuse | Build |
| --- | --- | --- |
| 0 · OS driver | **kwin-mcp** (KDE/EIS/AT-SPI/virtual sessions, MIT, 33 tools) — primary. **computer-use-linux** for `doctor`-style readiness probing. | nothing |
| 1 · Observer | driver's AT-SPI + window tools; a11y flag flip from cua-driver | thin normalization wrapper |
| 2 · State compiler | ref/GC idea from `linux-desktop-mcp` | ✔ genuinely new; where accuracy is won |
| 3 · Policy | question shapes from `jev-ultrafast` | ✔ new question set for desktop actions |
| 4 · Guardrails | MCP tool annotations (`readOnlyHint`/`destructiveHint`) from computer-use-linux | ✔ policy engine + focus arbiter |
| 5 · Executor | driver tools; Mercury-class small LLM for text | thin dispatch + validation |
| 6 · Verifier | driver screenshot/AT-SPI reads | ✔ post-condition checks |
| 7 · Surface | standard MCP stdio server | ✔ tool definitions + skills doc |

Rough split: **~70% reuse, ~30% new code**, and the new code is the interesting part.

## Three scope options

### Option A — Decision policy on top of kwin-mcp *(recommended)*
A thin Python process (or MCP server) that consumes kwin-mcp's tools, compiles state, calls Jev,
applies guardrails, and returns verified results. Small, testable in isolation, no platform code.
Risk: depends on kwin-mcp's private EIS path; two processes to run.

### Option B — Fork kwin-mcp and add the Jev loop in-tree
Everything in one MCP server, direct access to AT-SPI internals, no IPC hop, can add the
ydotool backend. Risk: inherits maintenance of a large third-party codebase; upstream drift.

### Option C — Own driver from scratch
Only justified if the goal is learning the platform itself. Highest cost, lowest leverage;
contradicts "don't reinvent the wheel".

## Suggested phases (each ends in something runnable)

- **P0 · Probe (half a day).** Answer the environment checklist in
  `docs/03-wayland-constraints.md`. Run kwin-mcp live + virtual. Capture a real AT-SPI tree dump
  from 3 apps (a Qt app, Kate, and a browser) and look at what the compiler has to work with.
- **P1 · Single-app decision.** State compiler + one Jev call for one app, no execution —
  just print the chosen action/target and confidence. Judge whether Jev is picking sensibly.
- **P2 · Closed loop.** Execute + verify on one narrow task (e.g. "open Preferences and toggle
  X"), with focus save/restore. Measure steps, latency, cost, failure modes.
- **P3 · Guardrails.** Destructive-action gate, confirmation UX, allowlists, step/budget caps.
- **P4 · MCP surface + skill.** Expose to Pi/Claude Code, ship a small skill that teaches the
  agent *which tool to call when* (kwin-mcp does this well — copy the pattern).

## Open technical questions

- One Jev call per action vs per task? (Per-action is proven; per-task needs multi-step
  reasoning, which the vendor says Jev is weaker at. Hybrid: Jev picks, code plans.)
- Where does short-term memory live — the driver's tree, or an append-only step log fed back
  into `state` within the token budget?
- Do we need OmniParser-style visual grounding at all, or is AT-SPI text enough for the target
  app set? (Probably: keep it behind a flag.)
- How do we express "the agent may take focus now" to the user without a prompt each time?
  Probably a session grant with a visible indicator, negotiated at task start.

## Decisions log (ADR-style, append as we lock things)

_Nothing locked yet — see `docs/05-open-questions.md`._
