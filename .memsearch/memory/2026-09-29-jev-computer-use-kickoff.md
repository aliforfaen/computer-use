# Jev computer-use layer — kickoff (2026-09-29)

Project: `/home/messhias/lamasync/projects/computer-use` (own git repo, no remote).
Goal: a Jev-powered computer-use layer for agents on CachyOS + KDE Plasma 6 Wayland.
**Endgoal: remote Hermes assistants connect over the tailnet too — not all callers are local.**

## Decisions locked (ADR-001…007; 008 proposed)

1. **Policy layer over `isac322/kwin-mcp`** — we own state compiler, Jev policy, guardrails,
   verifier. kwin-mcp provides AT-SPI observation, KWin private EIS input, virtual sessions.
   Rejected: forking kwin-mcp, writing our own driver.
2. **Both session modes; virtual (`session_start`) is default**, live (`session_connect`) opt-in.
   Live mode must always save focus → act → restore focus.
3. **Three autonomy modes**: `supervised` (approve every action), `guarded` (default:
   read/reversible auto, confirm destructive), `yolo` (no prompts, for trusted agents).
   App allowlist, step caps and audit log apply in *every* mode including `yolo`.
4. **One core, two surfaces: CLI + MCP server.**
5. **Single instance on `cachy`**: MCP **stdio** for local agents, MCP **Streamable HTTP** for
   remote ones, via `tailscale serve` → `127.0.0.1:7810`. One policy engine, one allowlist,
   one audit log.
6. **Remote callers have the same reach as local** (owner decision) — live desktop and `yolo`
   included. Virtual stays the default unless live is requested explicitly.
7. **Tailnet ACLs are the only access gate** (owner decision) — no tokens, no OAuth. Accepted
   consequence: any tailnet device that can reach the Serve URL controls the desktop; identity
   headers aren't available for tagged (VPS-shaped) nodes anyway. Mandatory compensations:
   localhost-only bind, deny-by-default app allowlist in all modes, append-only audit log,
   local kill switch, per-task step/time budgets, no screenshot streaming to remote callers,
   and an `authorizer` seam so a bearer token or Tailscale app capabilities
   (`--accept-app-caps`) can be added later as config.
8. *PROPOSED:* run the whole loop server-side; remote callers get one high-level call per task
   (low-level primitives still exposed). Rationale: per-step remote loops pay tailnet RTT + Jev
   latency per step; the Jev key then stays on `cachy`; guardrails can't be bypassed.

## Key facts worth remembering

- Jev (TypeSafe AI "System One", early access 2026-09-15) is **not an LLM** — `state` +
  typed `questions` → typed `answers`. Primitives: `choice` (≤255 options + probabilities),
  `score` (2–10 ordered levels), `noul` (calibrated yes/no). One call mixes all three.
  `POST /api/v1/decide`, `jev-latest` / `jev-1.13.0`, ~$0.42/1M input tokens, 70–500 ms claimed.
  Budgets: 64k combined, 32k for state + longest question. Vendor says multi-step reasoning is weak.
- Reference loop: `browser-use/jev-ultrafast` — indexed element table → one request with
  `action` + `target` heads → code executes only the matching head. Model output never becomes
  coordinates/selectors/commands. Text via a small LLM, JSON-parsed.
- **Wayland input is focus-routed** (kwin-mcp issue #33). Cannot inject keystrokes into a
  background window. kwin-mcp 0.7.0 misrouted; **0.8.0+ fixed** via real activation. Scroll follows
  the pointer, keyboard follows focus. ydotool/uinput is still only a roadmap item (M11).
  KWin does **not** implement `zwp_virtual_keyboard` (wtype unusable; KDE bug 502882).
- Chromium/Electron build no AT-SPI tree until `org.a11y.Status` (`IsEnabled`,
  `IsScreenReaderEnabled`) is set — then they build it retroactively. Trick from `trycua/cua`.
- Remote MCP = **Streamable HTTP** (spec rev 2026-07-28), one URL, POST per message,
  `Accept: application/json, text/event-stream`. **Hermes supports `url:`+`headers:` natively** —
  no stdio→HTTP bridge needed.
- `tailscale serve` proxies tailnet traffic to a localhost service with a tailnet TLS cert and
  adds `Tailscale-User-Login`/`-Name` (strips forged copies) — but **not for tagged devices**,
  and Funnel traffic gets none. Backend must listen on localhost only.
- Other reusable prior art: `agent-sh/computer-use-linux` (Rust, `doctor` readiness report,
  multi-compositor window registry), `BeckhamLabsLLC/linux-desktop-mcp` (ref_N semantic refs),
  `trycua/cua` (per-toolkit focus-free write paths), OmniParser (visual fallback, keep off hot path).

## Next step

P0 probe checklist in `docs/05-open-questions.md` (see `docs/HANDOFF.md` for the ordered shortlist):
confirm ADR-008, verify KWin EIS on this box, dump AT-SPI trees before/after the a11y flag flip,
virtual-session action end to end, `/dev/uinput` + ydotoold, prompt-free screenshot path,
`tailscale serve` binding + Funnel-off + `tailscale ping` path quality + which devices can reach
the URL, Hermes client lists tools, Jev key + measured latency, pick the small text LLM.
