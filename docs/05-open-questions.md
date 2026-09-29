# 05 — Open questions

Blocking questions are marked **B**. Answers get recorded in the ADR log at the end of
`docs/04-architecture.md`.

## Scope

1. **B — Which option?**
   A thin Jev policy layer over kwin-mcp (recommended) · fork kwin-mcp · own driver.
2. **B — Is the deliverable a layer for agents (MCP server), or a personal tool for you to
   drive, or both?** MCP server is reusable by Pi/Claude Code/Hermes; a CLI is faster to iterate.
3. What is the learning goal here — do you want to understand the platform internals, or get a
   working thing with minimal time? (Affects how much we reuse.)

## Mode

4. **B — Primary mode: live desktop or isolated virtual KWin session?** Live is useful and
   real, but every text action steals focus and needs restore logic. Virtual is safe and
   repeatable but sees none of your real apps/data.
5. May the agent take focus during a task (serial), or must it never disturb what you're doing
   (which limits us to mouse-only + AT-SPI actions)?

## Safety

6. **B — Autonomy level.** (a) observe-only until you approve each action, (b) auto-execute
   read-only/reversible actions, confirm anything destructive, (c) free run within an
   allowlist of apps. Jev's `noul` risk gate is advisory in all cases.
7. Which apps are in scope first? Candidates: a browser, Kate/KWrite, Dolphin, System Settings,
   Konsole (explicitly out?), your own tools.
8. Hard no-go zones to encode from day one (e.g. password managers, banking, `~/.ssh`,
   anything that types into a terminal)?

## Interface

9. Do we want a visible agent cursor / overlay so you can see what it is doing (cua-driver
   paints one)? Costs an overlay library; big comprehension win while learning.
10. Where should the key live — env var for the MCP server, or a local proxy/gateway
    (Vercel AI Gateway / OpenRouter) so one key serves other tools too?
11. Which aggregator, if any — direct `jevtypesafeai.com` `/v1/decide`, Vercel AI Gateway
    `typesafe-ai/jev`, or OpenRouter?

## To verify in P0 (not questions for you, just unknowns)

- Does kwin-mcp's EIS input actually land correctly on your KWin version?
- Does Chromium/Electron AT-SPI tree appear after the `org.a11y.Status` flip?
- Is `/dev/uinput` already ACL'd for your user (ydotool fallback)?
- Jev key: do you already have one, and which endpoint/billing path?
