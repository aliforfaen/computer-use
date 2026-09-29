# 05 — Open questions

Status: ADR-001…010 are locked (ADR-008 confirmed in ADR-009). P0 measurement is
underway; the owner preferences below remain open.

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

## Confirmed during P0

- [x] **ADR-008** — the loop runs on `cachy`; remote callers submit one task call.
      Jev still selects one step at a time (ADR-009).

## P0 — measure, don't debate

Unblocks P1. Probes only, nothing committed to `main`.

**Platform**
- [x] **KWin + EIS**: disposable `kwin-mcp==0.10.0` reported KWin EIS in
      virtual and live sessions. Live `focus_window('kcalc')` followed by
      `keyboard_key('1')` changed the KCalc window crop; the exact original
      focused window was restored. KWin 6.7.5, libei 1.6.0.
- [x] **AT-SPI reality check**: measured Kate, Firefox, and isolated Brave
      Origin before/after enabling `org.a11y.Status` (`IsEnabled` and
      `ScreenReaderEnabled`). See counts and limits below.
- [ ] **ydotool fallback**: is `/dev/uinput` ACL'd for `messhias`; does `ydotoold` run as a user
      service; does physical-rule focus transfer behave as reported?
- [ ] **Screenshot path**: exercise `kwin-mcp screenshot` in live and virtual
      sessions (KWin ScreenShot2 → Spectacle fallback); measure latency and
      whether either path prompts. Investigate the portal only as an optional fallback.
- [x] **Virtual session sanity**: disposable `kwin-mcp==0.10.0` started
      `kcalc` with `isolate_home=true`, reported KWin EIS, clicked the One
      button and captured a changed screenshot; see observation below.

**Tailnet (new — required by the endgoal)**
- [ ] **Serve works and binds correctly**: existing `/` route serves another
      local application. Preserve it and add `/mcp` → `127.0.0.1:7810` only once
      a Jev endpoint exists. Confirm LAN isolation and tailnet HTTPS.
- [ ] **Funnel is off**, and stays off. Verify, don't assume.
- [ ] **Path quality**: `tailscale ping cachy` from each remote node — direct WireGuard vs
      DERP-relayed, and the RTT. Decide whether per-step loops are even tolerable.
- [ ] **ACL reach**: which devices can actually reach the Serve URL? Write down the answer. Under
      ADR-007 this *is* the authorization policy.
- [ ] **Tagged vs user devices**: confirm which calling nodes are tagged (no identity headers) so
      the audit log's caller identification is designed for reality.
- [ ] **Backend caller identity**: use a disposable header-echo endpoint from a
      user-owned and a tagged peer; record headers and backend socket peer. Do
      not assume the Serve reverse proxy preserves the caller's IP.
- [ ] **Hermes client**: point a Hermes `mcp_servers` entry at the URL, list tools, call one.

**Models / keys**
- [x] **Jev access**: the owner supplied separate hosted and TypeSafe direct
      keys for the paired test. Both endpoints succeeded with pinned
      `jev-1.13.0`; the hosted key was then removed. The ignored `.env` now
      holds only the TypeSafe direct key as `JEV_API_KEY` (ADR-010).
- [ ] **Small text LLM**: which fast model writes `TYPE_TEXT` payloads (Mercury-class) and is it
      already reachable with an existing key?

## P0 observations — 2026-09-29

- `cachy` is in a KDE Wayland session; KWin/Plasma **6.7.5**, libei **1.6.0**.
  The live KWin exposes `org.kde.KWin.EIS.RemoteDesktop.connectToEIS`, but
  the live interface has not been exercised.
  `kwin-mcp` was not installed at the start of the probe.
- A disposable `/tmp` environment ran `kwin-mcp==0.10.0` in a virtual session
  with `session_start(app_command='kcalc', isolate_home=true)`. It reported
  `Input backend: KWin EIS`. AT-SPI located KCalc's One button; an EIS click
  was followed by a ScreenShot2 capture with a different hash and 781 changed
  pixels. The button did not show AT-SPI focus after clicking, so focused state
  alone was not a valid verifier for this widget. The virtual session stopped
  cleanly and the temporary environment was removed. The driver emitted a
  `failed to send message: Broken pipe` warning during runs despite successful
  operations; watch for recurrence in P1. Live behavior was measured separately below.
- A second disposable `kwin-mcp==0.10.0` run connected to the live session and
  reported KWin EIS. It launched a temporary KCalc, focused its exact window,
  sent the `1` key, and observed a **4×9 pixel changed region inside KCalc's
  window crop**. The KCalc AT-SPI tree did not change. The original focused
  window was restored and verified by exact KWin ID; temporary app and probe
  environment were removed. This supports focused keyboard delivery and
  screenshot verification on this KWin version. It does not establish that
  every widget exposes a semantic postcondition. The same benign `Broken pipe`
  warning appeared.
- AT-SPI bus and registry are running. Both `org.a11y.Status` properties are
  currently false. The actual property name is `ScreenReaderEnabled` (querying
  `IsScreenReaderEnabled` fails). Seven apps were registered; Kate and Firefox
  were not running, and a running Electron app was absent from the tree.
- The app-specific AT-SPI probe used isolated profiles and a local test page.
  With both status flags false, the target Kate, Firefox and Brave windows
  exposed no useful tree. With both true: Kate **620 nodes / 289 named useful**
  elements (231 menu items); Firefox **247 / 79** shortly after launch, growing
  to **955 / 523** as its own pages loaded. The local fixture page contributed
  seven named items. Isolated Brave Origin stayed at **1 root / 0 useful**
  elements, even with `--force-renderer-accessibility`. Versions: Kate
  26.08.1, Firefox 156.0.1, Brave Origin 154.1.96.59. The two flags, linked
  settings, launched apps and original focus were restored after the probe.
- `/dev/uinput` grants `messhias` read/write through an ACL; the `ydotoold`
  user service is active. Input behavior remains untested.
- Tailscale **1.102.4** is running. HTTPS Serve already maps `/` to
  `127.0.0.1:3080`; the new MCP route must coexist with it. A direct peer
  (`valhalla`) answered `tailscale ping` at about **45 ms**. This is a network
  observation, not an MCP round-trip measurement. Funnel-off status and
  localhost backend caller identity remain unverified.
- Initially the current agent environment had no `JEV_API_KEY` and no Jev
  SDK/CLI. The owner then added the hosted key to an ignored `.env` file.
  An early synthetic request was mistakenly sent to the official direct
  endpoint and returned HTTP 401; no real desktop state was sent. This is a
  provider mismatch, not evidence that the hosted key is invalid.
  Three subsequent synthetic requests to the **hosted** endpoint all returned
  HTTP 200 with model `jev-1.13.0`, a typed `choice` answer and a typed `noul`
  answer. Each reported 351 input tokens, 48 output tokens and $0.000148 cost.
  End-to-end times were **1,273 ms, 890 ms, 1,732 ms** (median **1,273 ms**).
  This tiny sample is slower than the gateway's vendor-reported 70–500 ms;
  measure again with actual bounded desktop states before setting time budgets.
  A second **10-call** serial sample with a synthetic KCalc-like state and
  pinned `jev-1.13.0` returned HTTP 200 every time. Call times were **768–1,060 ms**
  (median **810 ms**); total reported cost was **$0.00189**. All five
  pre-action cases chose `press`/`one`, and all five completed cases chose
  `done`. The independent `target` head still answered `one` when action was
  `done`; the executor must ignore targets for targetless actions. This is a
  simple fixture, not a measure of desktop-task accuracy or direct-API speed.
  Ollama responds locally and has Qwen/Gemma models installed, but no model
  was loaded during the check. Use these only for separate baselines or text
  helpers; they do not establish Jev selector quality.
- After the owner added a separate first-party key, a **12-pair** test alternated
  TypeSafe direct and the gateway from `cachy`, using the same pinned
  `jev-1.13.0` model and synthetic KCalc states of **1,445–1,453 input tokens**.
  Both had **12/12 HTTP 200** responses, resolved to `jev-1.13.0`, chose
  `press`/One in six pre-action cases and `done` in six completed cases, and
  returned near-identical `noul` probabilities. Direct latency was **230–384 ms**
  (median **254 ms**, p90 **296 ms**); gateway latency was **785–996 ms**
  (median **839 ms**, p90 **919 ms**). Direct was faster in all 12 matched pairs,
  with a median per-pair advantage of **579 ms**. The gateway reported
  **$0.007308** total cost; direct returned token counts but no cost field.
  At the [published direct price](https://docs.typesafe.ai/models), its input
  estimate is about **$0.00073** for the batch. This supports direct as the
  P1 default on latency and price; it does not prove higher model accuracy.
- The owner selected TypeSafe direct for P1. The hosted key was removed from
  ignored `.env`, and the direct key renamed `JEV_API_KEY`. A synthetic request
  using that name returned HTTP 200 from `api.typesafe.ai` with model
  `jev-1.13.0` in **350 ms**. The file is ignored by Git and mode `0600`.

## Implementation seams to resolve after P0

1. One policy engine process owns the task/session and a persistent upstream
   stdio child. Local stdio is a thin client; otherwise each local caller would
   create a competing policy engine (ADR-009).
2. Task goals and stopping conditions need a small code-owned loop contract.
   Jev selects each action, while the caller/task definition supplies the goal.
3. The action and target questions are evaluated independently. P1 must test
   whether speculative targets produce valid pairs; revalidate the selected
   pair and fall back to a second target decision when necessary. Ignore the
   target answer for `done`, `wait`, and other targetless actions.
4. Keep the provider URL configurable, with TypeSafe direct as the selected
   P1 endpoint (ADR-010). The gateway remains documented as a measured
   comparison, without a stored key or configured fallback. Published input price is $0.42/M
   at the [hosted gateway](https://jevtypesafeai.com/jev/api) and $0.042/M
   at [TypeSafe direct](https://docs.typesafe.ai/models), as of 2026-09-29.

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
