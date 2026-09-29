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
        │     (behind our input-adapter interface, so the backend    │
        │      can be swapped without touching the policy)          │
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

## Three scope options (A chosen — see ADR-001)

### Option A — Decision policy on top of kwin-mcp ✅ **chosen**
A thin Python process (or MCP server) that consumes kwin-mcp's tools, compiles state, calls Jev,
applies guardrails, and returns verified results. Small, testable in isolation, no platform code.
Risk: depends on kwin-mcp's private EIS path; two processes to run.

### Option B — Fork kwin-mcp and add the Jev loop in-tree _(rejected, ADR-001)_
Everything in one MCP server, direct access to AT-SPI internals, no IPC hop, can add the
ydotool backend. Risk: inherits maintenance of a large third-party codebase; upstream drift.

### Option C — Own driver from scratch _(rejected, ADR-001)_
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
- What is the input-adapter boundary exactly — one interface with `eis` / `portal` / `ydotool`
  implementations, each reporting whether it can reach a non-focused surface?

## Decisions log (ADR-style, append as we lock things)

### ADR-001 — Jev policy layer over `kwin-mcp` (not a fork, not a new driver)
**Locked 2026-09-29.** We own layers 2, 3, 4 and 6 (state compiler, Jev policy, guardrails,
verifier). Layer 0/1/5 — AT-SPI observation, KWin EIS input, virtual sessions — come from
`isac322/kwin-mcp` unchanged. Rationale: kwin-mcp already targets exactly Plasma 6 Wayland
with the least-prompting input path, is MIT, and is actively maintained. Forking inherits a
large codebase; a new driver contradicts the no-reinvention rule.

Accepted consequences: two processes to run; we depend on kwin-mcp's **private** KWin EIS
D-Bus interface, which can move between KWin releases — pin the version and record it in the
P0 probe. If EIS proves unreliable on `cachy`, the fallback is the portal RemoteDesktop path
or ydotool, behind our own input-adapter interface so the swap stays local.

### ADR-002 — Both session modes; virtual is the default
**Locked 2026-09-29.** `session_start` (isolated `kwin_wayland --virtual`) is the default
because it has no focus contention and no consent prompts. `session_connect` (live desktop)
is opt-in per task. Live mode must always run the **save focus → act → restore focus** pattern
before returning control to the user (see `docs/03-wayland-constraints.md` §1).

### ADR-003 — Three autonomy modes, `guarded` by default
**Locked 2026-09-29.** Mode is a policy-engine setting, never a model input.

| Mode | Behaviour | Intended caller |
| --- | --- | --- |
| `supervised` | Observe-only; every action waits for human approval | learning how Jev decides |
| `guarded` *(default)* | Read/reversible actions auto-execute; destructive, irreversible or external effects require approval | general use |
| `yolo` | No gates; runs to completion inside the app allowlist | trusted/smarter agents, tasks the owner doesn't want to babysit |

The **app allowlist is enforced in every mode, including `yolo`.** `yolo` removes approval
prompts, not the allowlist, step caps or the audit log. Jev's `risk` (`noul`) gate is advisory
in all modes; in `guarded` it escalates to the user, in `yolo` it is logged and may trigger a
step budget reduction, never an action.

### ADR-004 — One core, two surfaces: CLI + MCP
**Locked 2026-09-29.** The policy engine is a library. A CLI (`jev-desktop act --task …`)
wraps it for fast iteration and manual driving; an MCP stdio server exposes the same core to
Pi/Claude Code/Hermes/Codex. MCP tools carry `readOnlyHint`/`destructiveHint` annotations
(pattern from `computer-use-linux`) so hosts can surface risk before invocation.

### Next: P0 probe

Not decided yet because it is measurement, not preference — see the P0 list in
`docs/05-open-questions.md`.
