<p align="center">
  <img src="assets/jev-icon.svg" width="96" height="96" alt="Jev cursor icon: a light-blue mouse pointer with a curious eye">
</p>

# Computer Use

Grounded desktop observation and control for KDE Plasma on Wayland. The local
owner service uses [kwin-mcp](https://github.com/isac322/kwin-mcp); CLI and MCP
clients share its policy, allowlist, audit and session state. Jev remains an
optional selector experiment and is not wired into the runtime.

## Current contract — 2026-10-25

- **Observation and input:** fresh app captures and semantic candidates are
  available. Strong code-owned verifiers remain available for supported actions.
  `verification=observed` and image-grounded input return fresh state while
  reporting effect as uncertain; they never prove task completion. Uncertain
  input is reconciled by observing again, with no automatic replay.
- **Live desktop:** requires an explicit owner-present override. Sessions can
  launch an allowlisted configured app or attach to one exact window ID. The
  task app stays foreground until stop; stop restores original focus, closes
  only an app launched by the task, and preserves attached apps. Physical input
  idle detection is unavailable.
- **Semantic access:** AT-SPI candidates are disabled by default in live mode.
  Enabling them requires `--semantic-candidates --temporary-a11y`; original
  accessibility properties are restored at cleanup. Virtual mode enables
  semantic candidates by default.
- **Profiles and authorization:** built-in profiles cover Kate, Firefox and
  KCalc. Custom profiles are read from
  `${XDG_CONFIG_HOME:-~/.config}/jev-desktop/app_profiles.json`; a profile does
  not authorize its app. Add the canonical app separately to the explicit
  allowlist. [Profile and live contract](docs/40-live-workspace-and-profiles.md).
- **Workspace and relays:** `workspace`, `profiles` and `relays` report window,
  profile and SSH-forward state. Relay process/listener state does not guarantee
  remote MCP endpoint health. Capture mapping currently rejects unsupported
  scaled and multi-monitor layouts.
- **Validation:** current implementation is source-reviewed and covered by
  offline checks. The newer live workspace/profile contract still needs an
  owner-watched host acceptance run; see the checklist in doc 40.

Start with [the handoff](docs/HANDOFF.md), [local commands and MCP](docs/16-local-owner-and-mcp.md),
[verification guidance](docs/29-verification-runbook.md), and the
[live workspace acceptance checklist](docs/40-live-workspace-and-profiles.md).

## Quick start

Requires Python 3.13+, `uv`, KDE Wayland and the system dependencies in
[platform notes](docs/03-wayland-constraints.md).

```bash
uv sync
uv run jev-desktop daemon --foreground --allow-app kate --allow-app firefox --allow-app kcalc
```

In another terminal:

```bash
uv run jev-desktop capabilities
uv run jev-desktop profiles
uv run jev-desktop session start kcalc
uv run jev-desktop observe kcalc
uv run jev-desktop candidates kcalc
uv run jev-desktop session stop
uv run jev-desktop stop --all
uv run jev-desktop shutdown
```

For a watched live task, first ensure its app is explicitly allowlisted. Launch
the app with screenshots only by default, or attach to an exact discovered
window ID:

```bash
uv run jev-desktop workspace
uv run jev-desktop session start dolphin --live --owner-present-override --no-semantic-candidates
# Or attach to an exact existing window instead of launching it:
uv run jev-desktop session start dolphin --live --owner-present-override --attach-window WINDOW_ID
```

The Dolphin example requires a configured Dolphin profile and a separate
`--allow-app dolphin` daemon setting. Semantic opt-in is explicit and requires
temporary accessibility consent:

```bash
uv run jev-desktop session start dolphin --live --owner-present-override --semantic-candidates --temporary-a11y
```

For an MCP host, run `uv run jev-desktop-mcp` as a local stdio client of the
same owner. Images are returned only when requested. Reader interpretation
requires explicit provider configuration and a positive per-session cap. Relay
and reader status do not claim provider/endpoint billing or health guarantees.

## Repository and checks

Runtime modules live in `src/jevdesktop/`; tests in `tests/`; standalone probes
in `tools/`; development harnesses in `scripts/`; docs in `docs/`; local
captures and reports in ignored `run/`.

```bash
uv run python -m tools.check
uv run python -m unittest tests.test_transactions
```

Use the [verification runbook](docs/29-verification-runbook.md) to choose a
targeted check. Live actions and provider probes are separate owner-authorized
acceptance work, not part of offline checks.

## Design and evidence

- [Grounding and capture mapping](docs/39-grounding.md)
- [SSH forward configuration and status](docs/38-ssh-forwards.md)
- [Tray, user service and configuration](docs/28-tray-and-user-service.md)
- [Architecture and append-only ADR history](docs/04-architecture.md)
- [Prior art and reused drivers](docs/02-prior-art.md)
- [Historical live Kate evidence](docs/26-live-owner.md)

Credentials belong in private files or the invoking process environment, never
tracked files or audit logs. `.env.example` contains names and path guidance
only; provider calls remain opt-in.
