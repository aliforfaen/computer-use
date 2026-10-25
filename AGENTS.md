# AGENTS.md

Guidance for `computer-use` / the Jev desktop layer. Owner: messhias, CachyOS,
KDE Plasma, Wayland. Communicate concisely; prefer small runnable slices.

## Start here

Read [docs/HANDOFF.md](docs/HANDOFF.md), then
[the current live/profile contract](docs/40-live-workspace-and-profiles.md) and
[CLI/MCP reference](docs/16-local-owner-and-mcp.md). Numbered experiment reports
are historical evidence, not the active backlog. Earlier guidance is archived
under `docs/archive/`.

## Current direction — 2026-10-25

Broad desktop assistance for the calling agent. Configured app profiles replace
the three-app launch restriction. Live sessions can launch or attach to an exact
window; related dialogs are followed by KWin identity. Screenshot-grounded input
and optional AT-SPI candidates coexist. Named SSH forwards reuse OpenSSH/systemd
and expose monitoring through MCP. Jev experiments and Qwen-OCR remain deferred.

The October 24–25 implementation has offline checks; the new live contract still
needs owner-watched acceptance with a connecting agent. Do not present earlier
Kate acceptance as validation of this broader surface. No physical-input idle
detector is available. No host service or relay activation is implied by coding.

## Runtime rules

- Reuse before writing: name the reused project in designs. KWin/kwin-mcp is
  the desktop driver; do not build a GNOME or Plasma extension for this.
- App profiles specify launch argv, KWin identities and AT-SPI name. A profile
  never grants permission: an explicit deny-by-default app allowlist applies in
  every autonomy mode, including `yolo`. Preserve the authorizer seam.
- Autonomy is code policy: supervised approves every action; guarded approves
  destructive semantic actions and image inputs; yolo suppresses prompts.
  Budgets, audit, focus and ownership checks apply in every mode.
- **Live focus (owner change Oct 25):** bring the selected task window/dialog
  foreground for work and do not restore focus between calls. Restore the
  original window at stop. This supersedes older per-call restoration guidance.
  Unrelated popups are reported, never silently promoted to task targets.
- **Ownership:** never close/kill an attached user app. Only a launched task app
  may be cleaned up as owned. Journal recovery must retain that distinction.
- **Observation after input:** strong verifiers prove explicit postconditions;
  `observed` and image inputs report uncertain effect with fresh state. Model
  DONE and returned input calls are not proof of task completion.
- Never automatically replay input after timeout/disconnect. Reconnect through
  status/workspace. Explicit reconciliation refreshes state and acknowledges
  uncertainty; it cannot prove the previous effect.
- Live semantic candidates are opt-in and require temporary accessibility
  authorization. Restore exactly the captured properties; discover optional
  `ScreenReaderEnabled` instead of assuming it exists.
- Image inputs consume fresh session/window-scoped captures and validate their
  coordinate mapping. Returned geometry is for the calling agent, not Jev.
- Jev is a disabled-by-default pure selector (`JEV_ENABLED=true` required). It
  chooses enumerated options only; never produces coordinates, code, paths or
  commands. Only a text helper may generate free typing text. Jev is unwired.
- OCR study/fusion remains inspection-only: local CPU RapidOCR, synthetic or
  virtual data, no live captures/paid provider calls/host driver installs.
  Disposable OCR environments belong under ignored `tmp/`. No OCR-grounded
  action or Qwen API comparison without separate authorization.
- Ask before destructive actions, typing into a terminal, or broad home-directory
  access. Honor already supplied authorization and explicit owner policy.
- Record KWin and kwin-mcp versions with host claims; kwin-mcp >=0.8.0 is needed
  for input routing and the current runtime pins 0.10.0/private KWin seams.

## Remote and privacy rules

- Tailnet ACLs are the chosen access gate and remote reach equals local. Bind
  the owner HTTP transport to 127.0.0.1 only if implemented; Tailscale Serve
  proxies it. Never expose via Funnel or bind LAN/tailnet interfaces. Owner
  HTTP/tailnet deployment is still deferred; SSH relays are separate.
- Remote live tasks require an explicit per-task request, audit, single-session
  lock and owner-present override while input-idle detection is unavailable.
- Keep the local kill switch simple: `jev-desktop stop --all`;
  `tailscale serve off` remains the hard stop for any future Serve deployment.
- Audit every owner call: caller, transport, tool, mode, outcome, verification,
  timestamp; no sampling. Keep executable actions separate from observations.
- Never write secrets or typed/clipboard contents to state, provider requests,
  tracked files or logs. Editable values are omitted from candidate output;
  unallowlisted workspace captions are redacted.
- Screenshots are returned only on explicit image/both requests. Metadata/hash
  is the default; no screenshot/video stream to remote callers by default.
- Private reader key: `~/.config/jev-desktop/daemon.env` (0600). Private Jev key:
  `~/.config/jev-desktop/jev.env` (0600); the daemon never reads the latter.
- Relays manage named loopback TCP forwards, not MCP authorization/protocol.
  Process or listener status never proves endpoint/MCP health.
- Document platform constraints honestly in docs/03-wayland-constraints.md.

## Layout and verification

Runtime → `src/jevdesktop/`; tests → `tests/`; standalone probes → `tools/`;
benchmarks/research harnesses → `scripts/`; docs → `docs/`; icons/desktop files →
`assets/`; HTML fixtures → `benchmark_fixtures/`; ignored captures/output →
`run/`; disposable checkouts/environments → ignored `tmp/`. No new root modules.
Resolve checkout fixtures/output through `jevdesktop.paths`.

Use `uv run python -m tools.check` first and targeted checks for changed behavior.
Keep host probes small and owner-watched; do not rerun matrices for old counts.
See docs/29-verification-runbook.md. Explain what was actually checked and its
limits; no live acceptance or provider billing claim from offline mocks.

Record decisions by appending ADRs in docs/04-architecture.md; supersede instead
of rewriting evidence. Cite external claims with URLs, label vendor numbers.
Origin is https://github.com/aliforfaen/computer-use.git, branch main. Check
working-tree state before edits, preserve owner changes, and leave it clean
after authorized commits. Current user authorized a push after this session.
