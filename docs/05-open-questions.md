# 05 — Open questions

Status: the four blocking questions are **answered** (see ADR-001…004 in
`docs/04-architecture.md`). What remains is measurement (P0) and a few owner preferences.

## Answered

| # | Question | Answer |
| --- | --- | --- |
| 1 | Build shape | Policy layer over `kwin-mcp` — ADR-001 |
| 2 | Deliverable | One core, two surfaces: CLI + MCP — ADR-004 |
| 3 | Session mode | Both; virtual default, live opt-in — ADR-002 |
| 4 | Autonomy | Three modes, `guarded` default, plus `yolo` for trustworthy agents — ADR-003 |

## P0 — measure, don't debate

These unblock P1. All are probes, no code committed to `main`.

- [ ] **KWin + EIS**: does `kwin-mcp session_connect` report "Input backend: KWin EIS" on
      `cachy`, and does `focus_window` + `keyboard_key` actually land in the target? (kwin-mcp
      0.8.0+ required — 0.7.0 misroutes input.) Record KWin version.
- [ ] **AT-SPI reality check**: dump the tree for Kate (Qt), Firefox (Gecko) and one
      Electron/Chromium app *before* and *after* flipping `org.a11y.Status`
      (`IsEnabled` / `IsScreenReaderEnabled`). How many useful named elements appear?
- [ ] **ydotool fallback**: is `/dev/uinput` already ACL'd for `messhias`; does
      `ydotoold` run as a user service; does physical-rule focus transfer behave as reported?
- [ ] **Screenshot path**: portal `ScreenShot` vs `spectacle -b -n -o`. Which one is
      prompt-free after a restore token, and how slow is a capture? (Verification budget.)
- [ ] **Jev access**: key availability, chosen endpoint (direct `jevtypesafeai.com/api/v1/decide`
      vs Vercel AI Gateway `typesafe-ai/jev` vs OpenRouter), and measured latency from here.
- [ ] **Small text LLM**: which fast model writes `TYPE_TEXT` payloads (Mercury-class) and is it
      already reachable with an existing key?
- [ ] **Virtual session sanity**: launch `kcalc` inside a `session_start` sandbox and complete
      one action end to end.

## Still open — owner preferences

1. **App scope for v1.** Candidates: Firefox, Kate, Dolphin, System Settings, KCalc. Konsole
   explicitly excluded, or allowed with a typed-command confirmation gate?
2. **Hard no-go zones** to encode from day one — password managers, banking, `~/.ssh` and other
   secret stores, anything that types into a shell. Default proposal: deny-list plus a
   "never read clipboard when a password manager is focused" rule.
3. **Visible agent cursor / overlay.** cua-driver paints one. Costs an overlay library; large
   comprehension win while learning, and useful for `yolo` transparency.
4. **Key location.** Env var per-process, or one local gateway/proxy so other tools share it?
5. **Learning goal.** Understand platform internals vs ship fast. Affects how much of P0 we do
   by hand instead of through kwin-mcp's own tooling.
6. **Repo home.** This folder is a standalone git repo with no remote. Does it live here, get a
   GitHub remote, or become a Multica project card?
