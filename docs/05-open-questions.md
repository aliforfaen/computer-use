# 05 — Open questions

Status: the blockers are **answered** (ADR-001…007). What remains is measurement (P0), one
proposal to confirm (ADR-008), and a few owner preferences.

## Answered

| # | Question | Answer |
| --- | --- | --- |
| 1 | Build shape | Policy layer over `kwin-mcp` — ADR-001 |
| 2 | Deliverable | One core, two surfaces: CLI + MCP — ADR-004 |
| 3 | Session mode | Both; virtual default, live opt-in — ADR-002 |
| 4 | Autonomy | Three modes, `guarded` default, plus `yolo`; allowlist applies in all modes — ADR-003 |
| 5 | Where it runs | Single instance on `cachy`, stdio + Streamable HTTP over the tailnet — ADR-005 |
| 6 | Remote reach | Remote sees everything, including live desktop and `yolo` — ADR-006 |
| 7 | Remote auth | Tailnet ACLs only; compensations required — ADR-007 |

## Confirm in P0

- [ ] **ADR-008** — run the whole loop server-side, remote callers get one call per task. Confirm
      or reject; it is the only remaining architecture-level choice.

## P0 — measure, don't debate

Unblocks P1. Probes only, nothing committed to `main`.

**Platform**
- [ ] **KWin + EIS**: does `kwin-mcp session_connect` report "Input backend: KWin EIS" on `cachy`,
      and does `focus_window` + `keyboard_key` actually land in the target? (kwin-mcp **≥0.8.0**;
      0.7.0 misroutes input.) Record the KWin version — the EIS D-Bus interface is private.
- [ ] **AT-SPI reality check**: dump the tree for Kate (Qt), Firefox (Gecko) and one
      Electron/Chromium app *before* and *after* flipping `org.a11y.Status`
      (`IsEnabled` / `IsScreenReaderEnabled`). How many useful named elements appear?
- [ ] **ydotool fallback**: is `/dev/uinput` ACL'd for `messhias`; does `ydotoold` run as a user
      service; does physical-rule focus transfer behave as reported?
- [ ] **Screenshot path**: portal `ScreenShot` vs `spectacle -b -n -o`. Which is prompt-free after
      a restore token, and how slow is a capture? (This is the verification budget.)
- [ ] **Virtual session sanity**: launch `kcalc` inside `session_start` and complete one action
      end to end.

**Tailnet (new — required by the endgoal)**
- [ ] **Serve works and binds correctly**: `tailscale serve` maps
      `https://cachy.<tailnet>.ts.net/mcp` → `127.0.0.1:7810`; confirm the service is *not*
      reachable from the LAN and that HTTPS certs are enabled for the tailnet.
- [ ] **Funnel is off**, and stays off. Verify, don't assume.
- [ ] **Path quality**: `tailscale ping cachy` from each remote node — direct WireGuard vs
      DERP-relayed, and the RTT. Decide whether per-step loops are even tolerable.
- [ ] **ACL reach**: which devices can actually reach the Serve URL? Write down the answer. Under
      ADR-007 this *is* the authorization policy.
- [ ] **Tagged vs user devices**: confirm which calling nodes are tagged (no identity headers) so
      the audit log's caller identification is designed for reality.
- [ ] **Hermes client**: point a Hermes `mcp_servers` entry at the URL, list tools, call one.

**Models / keys**
- [ ] **Jev access**: key availability, chosen endpoint (direct `jevtypesafeai.com/api/v1/decide`
      vs Vercel AI Gateway `typesafe-ai/jev` vs OpenRouter), and measured latency from `cachy`.
- [ ] **Small text LLM**: which fast model writes `TYPE_TEXT` payloads (Mercury-class) and is it
      already reachable with an existing key?

## Still open — owner preferences

1. **App scope for v1.** Candidates: Firefox, Kate, Dolphin, System Settings, KCalc. Konsole
   excluded, or allowed with a typed-command confirmation gate?
2. **Hard no-go zones** to encode from day one — password managers, banking, `~/.ssh`, anything
   that types into a shell. Default proposal: deny-list, plus "never read clipboard while a
   password manager is focused".
3. **Which apps may remote callers touch?** Currently the same allowlist as local (ADR-006/007).
   A narrower remote allowlist is cheap to add.
4. **Notification on remote live-desktop task start** (ntfy/Telegram)? Cheapest honest safeguard
   given ADR-007 — recommended yes.
5. **Visible agent cursor / overlay.** cua-driver paints one. Costs an overlay library; large
   comprehension win while learning, and useful for `yolo` transparency.
6. **Key location.** Env var per-process on `cachy`, or one local gateway so other tools share it?
7. **Learning goal.** Understand platform internals vs ship fast. Affects how much P0 is done by
   hand instead of through kwin-mcp's tooling.
8. **Repo home.** This folder is a standalone git repo with no remote. Stays here, gets a GitHub
   remote, or becomes a Multica project card?
