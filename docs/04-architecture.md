# 04 — Proposed architecture

Not a spec. A shape to react to.

**2026-10-01 update:** the diagram below records the original Jev-led shape.
The owner-supported screenshot-agent direction and heartbeat benchmark are
in [doc 10](10-heartbeat-direction.md); planner placement and grounding remain open.

## The layer, in one picture

```
   local agents (stdio)          remote agents over tailnet (Streamable HTTP)
   Pi / Claude Code / Codex      Hermes @ VPS · Hermes @ GPU box · any tailnet node
        │                                   │
        └───────────────┬───────────────────┘
                        │  tailscale serve -> 127.0.0.1:7810  (see docs/06-remote-agents.md)
        ┌───────────────▼──────────────────────────────────────────┐
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
        │     gates, dry-run, save/restore focus, kill switch,      │
        │     append-only audit log (every call, both transports)   │
        │                                                          │
        │  5. EXECUTOR      AT-SPI action → EIS/libei → ydotool     │  ← reuse driver
        │     (behind our input-adapter interface, so the backend    │
        │      can be swapped without touching the policy)          │
        │     text via small LLM, JSON-parsed, never a command      │
        │                                                          │
        │  6. VERIFIER      re-read AT-SPI / focused window /       │  ← ours (thin)
        │     cropped screenshot diff; DONE is a proposal           │
        │  7. SURFACE       MCP stdio (local) + MCP Streamable HTTP │  ← ours
        │                   (remote, behind tailscale serve)        │
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
| 7 · Surface | standard MCP stdio server | ✔ tool definitions + skill + **Streamable HTTP transport** for tailnet callers |

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
- **P4 · MCP surface + skill.** Expose to Pi/Claude Code **and remote Hermes nodes**. Stdio first
  (local, no auth surface), then Streamable HTTP behind `tailscale serve`. Ship a small skill that
  teaches the agent *which tool to call when* (kwin-mcp does this well — copy the pattern).
  Details: `docs/06-remote-agents.md`.

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

### ADR-005 — Single instance on `cachy`, reached over the tailnet
**Locked 2026-09-29.** One `jev-desktop` process on `cachy`, exposed two ways: MCP **stdio** for
local agents and MCP **Streamable HTTP** via `tailscale serve` for remote ones. Rejected: one
instance per desktop node (a different product — fleet control), control plane + workers
(premature, heavy). Rationale: one desktop, one policy engine, one allowlist, one audit log.
Serve binds to `127.0.0.1` only, so no LAN exposure and no header forgery.

### ADR-006 — Remote callers have the same reach as local
**Locked 2026-09-29 (owner decision).** Remote callers may drive the live desktop and may use
`yolo`; they are not restricted to virtual sessions. Virtual stays the default for anyone who
does not ask otherwise. Live + remote + `yolo` is the highest-risk configuration in the system,
so: live must be requested explicitly per task, the audit log records it, **focus save → act →
restore is mandatory**, one live task at a time behind a lock, and the session refuses to start
if the owner's physical input was active in the last N seconds unless overridden.

### ADR-007 — Tailnet ACLs are the only access gate
**Locked 2026-09-29 (owner decision).** No per-client tokens, no OAuth, no identity checks.
Accepted consequence: any device able to reach the Serve URL controls the desktop, and Tailscale
identity headers are unavailable anyway for tagged (VPS-shaped) nodes. Required compensations
ship alongside it — localhost-only bind, deny-by-default app allowlist in **every** mode,
append-only audit log, a local kill switch, per-task step/time budgets, and no screenshot
streaming to remote callers. An `authorizer` interface stays in place so a bearer token or
Tailscale app capabilities (`--accept-app-caps`) can be added later as a config change.
Full reasoning and threat notes: `docs/06-remote-agents.md`.

### ADR-008 — Run the loop server-side; remote callers get one call per task *(PROPOSED)*
**Proposed 2026-09-29 — confirm during P0.** The observe→decide→act→verify loop executes on
`cachy`; remote callers call one high-level tool per task, with low-level primitives
(`observe`/`act`/`verify`) still exposed for agents that want to plan themselves. Rationale: a
per-step remote loop pays a tailnet round trip *plus* a Jev call per step; the Jev key stays on
`cachy`; remote callers cannot bypass guardrails by driving the executor directly. Trade-off: a
remote agent has less control over fine-grained decisions.

### Next: P0 probe

Not decided yet because it is measurement, not preference — see the P0 list in
`docs/05-open-questions.md`.

### ADR-009 — Confirm ADR-008; loop and session ownership stay on `cachy`
**Locked 2026-09-29 (owner confirmation).** The task call enters the one `jev-desktop`
process on `cachy`; that process owns observe → decide → act → verify and the task's
step/time budget. Jev still chooses one bounded next action at a time. It does not
plan the whole task. A remote caller can submit a task and receive progress/results.
Low-level tools, if exposed, must go through the same session owner and cannot
interleave actions with a running task.

Implementation seam to resolve in P0/P1: `kwin-mcp` is a stdio MCP server, so the
single `jev-desktop` process should own a persistent `kwin-mcp` child. A local
stdio-facing `jev-desktop` command must be a thin client of that process (for
example over a Unix socket), not a second policy-engine instance. The HTTP surface
is owned by `jev-desktop`, not by `kwin-mcp`. See
[`kwin-mcp`'s server](https://github.com/isac322/kwin-mcp/blob/main/src/kwin_mcp/server.py).

### ADR-010 — Use TypeSafe direct for P1
**Locked 2026-09-29 (owner decision).** Use the first-party TypeSafe endpoint
`https://api.typesafe.ai/v1/systemone` with `JEV_API_KEY` on `cachy`. The hosted
gateway was useful for P0 but its key has been removed. In 12 alternating,
matched synthetic calls with pinned `jev-1.13.0`, TypeSafe direct had a
**254 ms median** versus **839 ms** through the gateway, returned the same
action/target choices, and was faster in every pair. Its published input price
is one tenth of the gateway's. Keep endpoint selection in config for future
comparisons, but do not run a gateway fallback without a separately supplied key.
Measurements: [`docs/05-open-questions.md`](05-open-questions.md#p0-observations--2026-09-29).

### ADR-011 — Screenshot agent with optional heartbeat acceleration *(PROPOSED)*
**2026-10-01; owner supports the direction, final contract pending.** Use a primary
vision agent for task interpretation and recovery, with `kwin-mcp` handling
desktop sessions and input. Compare normal agent polling, a vision heartbeat,
and OCR + Jev before selecting an accelerator. Jev remains a bounded selector.
This would supersede ADR-001's Jev-led policy and the Jev-specific loop in
ADR-009; session ownership and controls stay on `cachy`. Planner placement and
action grounding must be resolved before locking this ADR. See
[doc 10](10-heartbeat-direction.md) for scope, metrics and remaining decisions.

### ADR-012 — Images or interpreted data for callers with different vision capabilities
**Locked 2026-10-01 (owner requirement).** Callers may explicitly request a
screenshot to inspect themselves or have a configured vision model interpret
it and return data. Support both from the same capture. Planning agents need
not have vision. Remote metadata-only defaults remain; image return and
provider interpretation are explicit requests. Jev remains a pure bounded
selector. Capture identity, interpreter provenance, failures and usage must
be represented. API details and provider selection remain open; see doc 10.

### ADR-013 — App-scoped observation by default; optional region crops
**Locked 2026-10-01 (owner requirement).** Explicit image or interpreted-data
observations default to the working app window. Full-session context and
region crops remain explicit choices. Browser-specific capture adapters can
follow later; an initial webpage capture uses the browser window. Preserve
capture identity, window identity and crop mappings. Metadata remains the
default remote response under ADR-012. Direct DeepSeek Flash is the baseline
for a runnable, provider-configurable fixture benchmark; provider selection
remains provisional. The benchmark is authorized implementation, without
authorizing the general MCP server or state compiler.


### ADR-014 — Bounded virtual semantic transaction slice
**2026-10-01; implementation scope approved by owner.** M1–M2 reuse
kwin-mcp 0.10.0 to provide mapped app observations and fresh AT-SPI candidate
transactions for disposable virtual fixtures. Input coordinates derive from
freshly resolved driver bounds, never model output. Typing requires the exact
focused editable target; every input is followed by a code-owned postcondition.
A failed or cancelled post-input transaction blocks further input until reset.

This slice does not decide primary planner placement, general visual grounding
or supersede ADR-009's server ownership. No daemon, MCP surface, live tasks or
remote deployment is included. Recorded implementation and actual host checks:
[doc 15](15-virtual-session-slice.md). ADR-011 remains proposed.

### ADR-015 — Local virtual owner with CLI and MCP stdio clients
**2026-10-01 · M3 implementation direction.** The local service is one
foreground `jev-desktop` owner with a private Unix socket. It owns one
`DesktopService`, one active virtual session and one persistent
`kwin-mcp==0.10.0` worker/`AutomationEngine` child. CLI and official Python MCP
SDK 2.2.0 stdio surfaces are thin clients of that owner; they do not construct
separate desktop engines or policy state. The app allowlist is explicit and
deny-by-default. Observation defaults to metadata; image and interpreted data
are requested explicitly, and provider use stays disabled unless configured
with a positive per-session call cap.

Cancellation is cooperative around synchronous driver calls and verifiers.
Disconnecting a CLI or MCP client does not cancel owner work; clients request
`cancel`, `session_stop` or `stop_all` explicitly. This implementation does not
add the proposed task planner, general visual grounding, live desktop support,
HTTP/Streamable HTTP, Tailscale Serve routing or remote access. Host integration
validation is recorded separately from synthetic socket/MCP tests; see
[doc 16](16-local-owner-and-mcp.md). ADR-011 remains proposed.
