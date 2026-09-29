# HANDOFF — fresh session, start here

**Repo:** `/home/messhias/lamasync/projects/computer-use` (standalone git, branch `main`,
no remote). Read `AGENTS.md` next, then run P0.

**One-line state:** ADR-001…008 are locked (ADR-008 confirmed in ADR-009);
**no project code exists yet**; P0 measurement is underway.

**Owner:** messhias. Machine `cachy` — CachyOS (Arch-based), KDE Plasma 6, Wayland.
Owner learns by doing and wants *short* prose: small runnable probes beat long documents.

---

## 1. What we are building

A computer-use layer for agents on this desktop, where the decision is made by **Jev**
(TypeSafe AI "System One": `state` + typed `questions` → typed `answers`; primitives `choice`,
`score`, `noul`; it generates no text). Execution stays in deterministic, guardrailed code.

**Endgoal:** local agents (Pi, Claude Code) *and* **remote Hermes assistants over the tailnet**
can drive this desktop. Not all callers are local.

## 2. What is already decided (do not relitigate without asking)

| ADR | Decision |
| --- | --- |
| 001 | Thin **policy layer over `isac322/kwin-mcp`**. We do not write a driver, we do not fork kwin-mcp. |
| 002 | Both session modes; **virtual (`kwin_wayland --virtual`) is the default**, live is opt-in per task. |
| 003 | Three autonomy modes: `supervised`, `guarded` (default), `yolo`. App allowlist, step caps and audit log apply in **every** mode including `yolo`. |
| 004 | One core, two surfaces: **CLI + MCP server**. |
| 005 | **Single instance on `cachy`**, MCP **stdio** for local + **Streamable HTTP** for remote via `tailscale serve` → `127.0.0.1:7810`. |
| 006 | **Remote callers have the same reach as local** — live desktop and `yolo` included. (Owner decision.) |
| 007 | **Tailnet ACLs are the only access gate.** No tokens, no OAuth. (Owner decision.) |
| 008/009 | Confirmed: run observe→decide→act→verify on `cachy`; remote callers submit a task. Jev chooses one bounded step at a time. |

Our ~30% of new code: **state compiler**, **Jev policy**, **guardrails**, **verifier**.
The other ~70% is `kwin-mcp` plus small borrowings from `browser-use/jev-ultrafast` (loop shape)
and `trycua/cua` / `agent-sh/computer-use-linux` (a11y flag flip, readiness report).

## 3. The three facts that will bite you

1. **Wayland input is focus-routed.** You cannot inject keystrokes into a background window.
   kwin-mcp **0.7.0 misroutes input; 0.8.0+ fixed it** via real activation. Keyboard follows
   focus; pointer scroll follows the pointer. Live tasks must **save focus → act → restore**
   or the owner's next keystrokes land in whatever the agent touched.
   `wtype` is unusable on KWin (no `zwp_virtual_keyboard`; KDE bug 502882).
2. **Accessibility varies by app.** Enabling `org.a11y.Status` `IsEnabled` and
   `ScreenReaderEnabled` yielded useful Kate and Firefox trees on this host,
   while isolated Brave Origin still exposed an empty root. See the P0 counts.
3. **Per-step remote loops are expensive.** A tailnet round trip plus a Jev
   call per step. Three calls through the owner's hosted gateway took
   890–1,732 ms on 2026-09-29; measure again with desktop states. Hence ADR-008.

## 4. Next action: P0 probe (half a day, measurements only)

Full checklist in **`docs/05-open-questions.md`**. The short version, in the order that
unblocks the most:

1. **KWin + EIS / virtual and live KCalc** — passed with disposable
   `kwin-mcp==0.10.0`; live focus was restored. See P0 measurements.
2. **AT-SPI tree dump** — completed for Kate, Firefox and isolated Brave.
   Kate/Firefox offer useful elements; Brave did not.
3. **ydotool fallback** — `/dev/uinput` ACL and `ydotoold` service passed;
   input behavior remains untested. KWin EIS currently works.
4. **Screenshot path** — ScreenShot2 worked in virtual and live sessions;
   capture latency and fallback behavior remain unmeasured.
5. **Tailnet** — preserve existing Serve `/` route; add `/mcp` → `127.0.0.1:7810`
   after the endpoint exists. Confirm not LAN-reachable; confirm
   **Funnel off**; `tailscale ping cachy` from a remote node (direct vs DERP, RTT); list which
   devices can reach the URL and which are tagged.
6. **Hermes client** — add an `mcp_servers` entry with `url:`, list tools, call one.
7. **Jev** — hosted endpoint and key work; three synthetic calls were slow.
   Re-measure with desktop states, and choose the small text LLM.

Read `docs/05-open-questions.md` for measurements already made on 2026-09-29 and
the remaining process/caller-identity seams.

### Probe hygiene

- Throwaway scripts only; do **not** commit implementation to `main` until P0 is reviewed.
- Record every measurement in the doc it belongs to (KWin version next to the EIS claim, RTT next
  to the tailnet section). Claims without a recorded version/timestamp rot fast here.
- Nothing in a probe should type into a terminal, touch a password manager, or run unattended.

## 5. Open owner preferences (ask, don't assume)

App scope for v1 · hard no-go zones · whether remote gets a narrower allowlist than local ·
notification on remote live-desktop task start (recommended) · visible agent cursor/overlay ·
where the Jev key lives · whether the goal is learning internals or shipping fast ·
repo home (stays here / GitHub remote / Multica card).

## 6. After P0 — the shape of P1…P4

- **P1 · Single-app decision.** State compiler + one Jev call for one app, **no execution** —
  print the chosen action/target/confidence and judge whether Jev is picking sensibly.
- **P2 · Closed loop.** Execute + verify one narrow task, with focus save/restore,
  narrow app scope, step cap, stop control and audit from the first executable probe.
  Measure steps, latency, cost and failure modes.
- **P3 · Guardrails.** Generalize the probe controls into the full policy:
  destructive-action gate, confirmation UX, allowlist and budgets.
- **P4 · Surface.** MCP stdio first, then Streamable HTTP behind `tailscale serve`. Ship a skill
  that teaches agents which tool to call when — copy kwin-mcp's plugin pattern.

## 7. Repo contents

```
README.md                        overview + endgoal + locked shape
AGENTS.md                        rules for agents in this repo (incl. remote-caller rules)
docs/HANDOFF.md                  this file
docs/01-jev-primer.md            Jev: primitives, API, cost, why it fits computer use
docs/02-prior-art.md             what to reuse (kwin-mcp, jev-ultrafast, cua, …) and what's missing
docs/03-wayland-constraints.md   the honest platform limits, incl. remote amplification of them
docs/04-architecture.md          diagram, build-vs-reuse table, options, phasing, ADR log
docs/05-open-questions.md        answered questions, ADR-008, P0 checklist, open preferences
docs/06-remote-agents.md         tailnet topology, access model + mandatory compensations, Hermes config
.memsearch/memory/               kickoff note (indexed in the workspace memory store)
```

## 8. Gotchas / anti-patterns to avoid

- Do not "improve" the auth situation by adding a half-built token scheme. ADR-007 stands; the
  `authorizer` seam exists so a bearer token or Tailscale app capabilities can be added
  deliberately later.
- Do not let the model produce coordinates, selectors, paths or commands. It chooses **indices we
  created**; a small LLM may write only the text for a `TYPE_TEXT` action, JSON-parsed.
- Do not treat the model's `DONE` as proof. Re-read state; verify.
- Do not stream screenshots to remote callers by default.
- Do not assume kwin-mcp's KWin EIS interface is stable across Plasma releases — pin the version.
- Do not use `tailscale funnel`, ever, for this.
