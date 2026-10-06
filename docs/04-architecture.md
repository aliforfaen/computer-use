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

### ADR-016 — Inactivity cleanup and persistent task documents
**2026-10-02 · owner-approved local work.** Reuse kwin-mcp 0.10.0 and the
existing owner teardown/recovery path. Replace the 90-second task cutoff with
configurable inactivity and total-lifetime caps, plus action/read caps. A
watchdog cancels and closes the exact abandoned session; agents still stop
all sessions they open. Kate edits/saves only its exclusively created task
file; exact editor and disk verification establish completion. Saved task
files persist independently of app/profile cleanup.

The owner exposes a $1 project-provider ceiling but currently refuses paid
reader setup until pre-request dollar reservation is reliable. This produces
zero service-provider spending. Caller inference is outside MCP accounting;
do not claim that this ceiling caps Codex/Luna billing. See doc 16 and doc 21.


### ADR-017 — Explicit readers and experimental owner waits
**2026-10-02 · owner-authorized local continuation.** Supersede ADR-016's
paid-reader refusal: configure a provider and positive per-session call cap,
report returned usage and latency without claiming billing measurement, and
journal attempts. Reuse the existing reader transport inside a one-request
subprocess so owner cancellation and an outer deadline cover headers and body.
The MCP/CLI wait primitive shares session read/reader limits and returns the
same judged capture's image and identity. It is observation, not planning or
proof of task completion. Two real wait trials returned invalid judgments;
keep it experimental. Navigation verifies address entry separately from page
readiness. Host scroll verification was fixed on 2026-10-03 (see doc 27). See
docs 23–25 for the surrounding agent trials.

### ADR-018 — Local live sessions require explicit owner presence
**2026-10-02 · local implementation; owner-watched validation pending.** Reuse
kwin-mcp 0.10.0 live connection behind the existing local CLI/MCP owner. Live
mode is explicit per session, requires an owner-present override because this
Wayland host does not support `GetSessionIdleTime()`, and requires opt-in to
temporary task-scoped AT-SPI enablement with exact restoration. Each session
starts one newly launched allowlisted app and verifies PID/window ownership.
Each action snapshots the currently active exact window, focuses the task app,
verifies the action and restores that snapshot before returning. Stop and worker
recovery close only the journaled task app and restore focus/accessibility
state; failed restoration remains a cleanup failure. No remote transport is
added. Focused tests pass, but the owner-watched physical workflow remains
pending; see doc 26.

### ADR-019 — Scroll effect verification resolves the live viewport element
**2026-10-03 · supersedes ADR-017's unresolved scroll note.** Firefox exposes a
hidden second AT-SPI `scroll pane` with the same role, empty label and identical
bounds as the live one, so a `(role, label)` or bounds lookup is ambiguous. The
scroll verifier now resolves each viewport through `transactions._scroll_viewport`
(exact fingerprint match, else the unique usable candidate) and keeps the
existing conservative witness rule (unique in-viewport link/button bounds moving
in one direction, zero opposing). A failed `act` also retains the bounded
verifier evidence in the audit instead of dropping it. Host evidence and the
reusable offline fixture are in doc 27. One fixture page, one window size and
two directions are verified; nested or live scroll remains unvalidated.

### ADR-020 — Tray and systemd --user lifecycle with a private config file
**2026-10-03 · owner-facing surfaces only.** Add two clients/lifecycles around
the unchanged local owner: a KDE StatusNotifierItem tray (GTK3 +
`AyatanaAppIndicator3`, `pygobject` now explicit) and a systemd --user unit
managed by `jev-desktop service install|status|uninstall`. The daemon reads
settings from a JSON config (`allowed_apps`, limits, reader names) with
CLI > env > config > default precedence; the file holds no secrets and an empty
allowlist still fails closed. The unit keeps the daemon's existing SIGTERM
cleanup and does not change the simple kill switch, loopback-only binding,
deny-by-default allowlist or audit logging. The tray may select virtual or
physical mode as a **preference**, but a live task is still started only after a
per-task dialog confirms owner presence and temporary accessibility, preserving
ADR-018. See doc 28.

### ADR-021 — Audit retention: persistent file with bounded rotation
**2026-10-03 · supersedes the runtime-directory audit default.** The audit log is
durable state, so it defaults to
`${XDG_CONFIG_HOME:-~/.config}/jev-desktop/audit.jsonl` rather than
`$XDG_RUNTIME_DIR`, where it would not survive a logout or reboot. All owner writers
share one append helper (`audit_log.append_jsonl`): 0600, append-only, fsynced,
size-rotated to `audit.jsonl.1..N` (default 4 MiB, 3 backups) and age-pruned (default
30 days), with the same sweep applied to stale session journals at startup. Retention
is config-file tunable. The socket and live session journals stay in the private
runtime directory. Nothing about the recorded fields or the no-sampling rule changes.
See doc 28.

### ADR-022 — Tray settings are a preference file, never authorization
**2026-10-03 · extends ADR-020.** Tray behaviour lives in
`~/.config/jev-desktop/tray.json` (0600): desktop-mode preference, autonomy mode for
tray-started tasks, status refresh interval, and whether cleanup and the kill switch
ask for confirmation. Invalid values fall back to defaults. The live-mode gate is
deliberately **not** configurable: a physical task still requires the per-task dialog
confirming owner presence and temporary accessibility. `service install --tray` may
add a second `graphical-session.target` unit for the indicator; it is never enabled
automatically. See doc 28.

### ADR-023 — Jev selector seam is offline-first and unwired
**2026-10-03 · ADR-011 remains proposed.** Add `jev_selector.py` as a pure selector:
it builds an indexed state from existing candidate output (roles, labels, states,
action names — no coordinates, values, selectors or paths), builds `action` and
`target` choice heads, enforces the documented token budget, and validates the typed
answer so a target must be an index we created. `HttpSelector` performs one request
with a hard call cap and no retries; `RecordedSelector` replays a saved answer for
free. Nothing is wired into the owner: execution, verification and policy stay in
`transactions.py`, and the existing `authorizer` seam remains the only place a selector
could advise. Experiment stages and what would not count as evidence are in doc 30.

### ADR-024 — Verification is a runbook, not a matrix
**2026-10-03 · process decision.** A small edit gets the module that owns it; the fast
core set is `uv run python -m tools.check` (~4 s); full discovery is for cross-module,
packaging or contract changes. The suite was reduced from 190 to 177 cases by
collapsing exhaustive per-failure-point and historical harness matrices while keeping
shipped-surface and safety-invariant coverage; the one-off wait-comparison fixture was
removed. The reader-wait heartbeat is reported as `experimental` in `capabilities`,
the CLI help and the MCP tool description, with polling named as the recommended loop.
See docs 29 and 24.

### ADR-025 — The KCalc display check is general, with a non-vacuous precondition
**2026-10-04 · supersedes the single-case rule from doc 08.** `display_text` was
hard-wired to one proof: KCalc, a click on the button labelled `One`, expecting the
display to read `1`, from an empty display. A driver loop cannot use a check that
covers one button, so the rule is now: any click on a KCalc **button** may be verified
by the display showing the caller-supplied expected string (still KCalc-only, still
click-only, still role `button`, expectation still from policy or the command line,
never from the model). The precondition adds the safety property the old rule got for
free: the display must be uniquely readable **and must not already read the expected
value**, so a no-op click can never "verify". The original proof case still passes
end to end (`tests/test_local_service_probe.py`). Nothing else about the verifier set
changed, and no other app gained a display check.

### ADR-026 — A driver loop's progress lives in the state as action labels
**2026-10-04 · selector seam, still unwired (ADR-023).** A request is stateless and the
state carries no values, so an option list is identical at every step and a model would
repeat its first answer forever. `state_from_candidates` therefore takes an optional
`history`: the loop's own record of completed actions, rendered between the goal and
the options, capped at 20 entries and 80 characters, whitespace-collapsed, **labels
only** — no values, no typed text, no results, no coordinates. `RecordedSelector` also
accepts a list now, replaying one answer per request, which is what an offline
multi-step test needs. See doc 31 for the loop and its results.

### ADR-027 — Browsers are not a Jev surface
**2026-10-04 · reuse decision.** Web automation stays with the existing driver
(`agent-browser`, already installed, driving Chromium/Brave over CDP, with element
refs and its own postcondition checks); Orca's embedded browser stays with `orca-cli`;
this repo keeps Firefox only as the *native* AT-SPI fixture target for verification
tests. Rationale: page automation already has semantic selection, so a decision model
adds a paid round trip and a new failure mode without adding capability. Jev's value is
on surfaces with no DOM — Qt dialogs, native editors. Named in doc 31 §4.

### ADR-028 — Runtime lives in an installable `src/jevdesktop` package
**2026-10-05 · supersedes the flat-module layout in ADR-015.** Every runtime module
moved from the repository root into `src/jevdesktop/` and is installed as a real
package; the console entry points are `jevdesktop.desktop_cli:main`,
`jevdesktop.desktop_mcp:main` and `jevdesktop.desktop_tray:main`. Worker children
start with `python -m jevdesktop.<worker>` instead of a path, so the package stays
importable when installed rather than used in place. Standalone benchmarks that no
runtime code imports moved to `scripts/` (not installed); verification probes stay in
`tools/`; `benchmark_fixtures/` and the repo-root `run/` directory stay where the
checks expect them, resolved through `jevdesktop.paths`. A small `paths` module is the
single place that walks up to the checkout so assets, fixtures and run output do not
depend on `__file__` pointing at a root-level module. No behaviour changed; the full
offline suite and the fast core set pass unchanged.

### ADR-029 — The Jev cursor icon is the app, tray and menu icon
**2026-10-05 · owner surfaces (ADR-020).** `assets/jev-icon.svg` is the canonical
app icon (shown in the README). Three surfaces use the same file: the tray renders it
into a small PNG pair under `~/.cache/jev-desktop/icons` and points the
StatusNotifierItem at that path (falling back to a stock theme icon when librsvg is
unavailable, so the tray always starts); `service install` copies the SVG to
`~/.local/share/icons/hicolor/scalable/apps/jev-icon.svg` and installs
`assets/jev-desktop.desktop` (with `Icon=jev-icon`) to `~/.local/share/applications`,
removing both again on `service uninstall`. The package ships the icon and entry
through symlinks into `assets/`, so the checkout file is the only source of truth.
This changes presentation only: no allowlist, focus, audit or authorization behaviour
is touched.

### ADR-030 — Jev provider requests are opt-in by environment
**2026-10-06 · accepted.** HTTP selector requests, including the standalone paid
probes, require `JEV_ENABLED=true`; missing, false or unrecognized values disable
the request. The `HttpSelector` enforces this immediately before reading the API
key or sending a request, and the probes report `jev_disabled` before loading the
key. Dry-run and recorded-answer modes remain offline and usable. This is a
provider-call kill switch; it does not wire Jev into the daemon or authorize any
desktop action. Credential values stay in the existing private config files.
