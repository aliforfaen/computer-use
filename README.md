# Computer Use

Screenshot-driven computer use for agents on **CachyOS, KDE Plasma and Wayland**.
Built on [kwin-mcp](https://github.com/isac322/kwin-mcp), with a local CLI and MCP
client. Jev is an optional future accelerator.

## Where things stand — 2026-10-03

- **Working:** virtual Firefox/Kate/KCalc, app screenshots, grounded navigation,
  links, text entry and scrolling, saved Kate documents, automatic cleanup,
  CLI and MCP.
- **Owner surfaces:** a private JSON config (CLI > env > file > default), a
  `systemd --user` unit with `service install|status|uninstall`, and an
  AppIndicator tray with status, a virtual/physical **preference**, session
  cleanup, settings and the kill switch. The tray never pre-authorizes live
  mode. [Tray, service and config](docs/28-tray-and-user-service.md).
- **Audit retention:** the audit log is durable at
  `~/.config/jev-desktop/audit.jsonl`, size-rotated and age-pruned; stale session
  journals are swept at startup. [ADR-021](docs/04-architecture.md).
- **Jev selector seam:** `jev_selector.py` turns candidate output into an
  enumerated decision request and validates the typed answer in code. Offline by
  default, one-call `HttpSelector`, not wired into the loop.
  [Experiment plan](docs/30-jev-selector-experiment.md).
- **Jev driver loop:** `tools/jev_loop_probe.py` closes
  select → act + code-owned check → re-enumerate as a client of the owner socket.
  Every step needs a verification the policy accepts, and `done` is only success
  if a check already passed. Virtual KCalc: verified 1-2-3, caught a wrong first
  choice, and refused an unverifiable operator click. Still unwired.
  [Plan and results](docs/31-jev-control-plan.md).
- **Scroll fixed:** the verifier now resolves the live scroll pane instead of an
  ambiguous `(role, label)` match, and failed verifications keep their evidence.
  Host-verified down and up on an offline fixture.
  [Evidence](docs/27-scroll-effect-verification.md).
- **Useful Luna task passed:** read Python.org, recover from a failed navigation,
  draft/revise/save a sourced Kate note. [Workflow](docs/23-public-browser-kate-agent-task.md).
- **DeepSeek:** explicit image interpretation is enabled with call limits and
  cancellable requests. The heartbeat is experimental: both wait trials returned
  invalid judgments; ordinary screenshot polling completed both tasks.
  [Comparison](docs/24-primary-agent-wait-comparison.md).
- **Local live mode:** implemented for one newly launched allowlisted app per
  session. It requires `--live --owner-present-override --temporary-a11y`,
  focuses the task window for candidate, screenshot, wait and action calls,
  restores exact prior focus, and closes only its owned app. The owner-watched
  Kate draft/save/revise/save and screenshot checks passed; original focus,
  AT-SPI flags and app cleanup were independently confirmed. Physical-input
  idle detection remains unavailable on this host.
  [Contract and evidence](docs/26-live-owner.md).
- **Still pending:** live Firefox, forced cancellation/crash recovery, tailnet
  deployment and Jev replay have not been validated. Nested/live scrolling,
  tray menu clicks and enabling the unit at login are untested.

Start with [the handoff](docs/HANDOFF.md). [Local commands](docs/16-local-owner-and-mcp.md)
· [next phases](docs/25-next-phases.md). A fixed physical KCalc smoke test passed
previously; [its scope](docs/22-live-desktop-smoke.md) remains separate from the
new local live-task service. The owner-watched Kate service check is recorded in
[doc 26](docs/26-live-owner.md).

## Local quick start

Requires Python 3.13+, `uv`, KDE Wayland and the system dependencies described
in [the platform notes](docs/03-wayland-constraints.md).

```bash
uv sync
uv run jev-desktop daemon --foreground --allow-app kate --allow-app firefox --allow-app kcalc
```

In another terminal:

```bash
uv run jev-desktop capabilities
uv run jev-desktop session start kcalc
uv run jev-desktop observe kcalc
uv run jev-desktop candidates kcalc
uv run jev-desktop session stop
uv run jev-desktop stop --all
uv run jev-desktop shutdown
```

For an MCP host, run `uv run jev-desktop-mcp` in this checkout. It connects to
that same local owner. Images are returned only when explicitly requested;
reader interpretation requires explicit provider configuration and a positive
per-session call limit. Use `image` for screenshots; `data` or `both` also
requires reader questions. Provider usage and caller inference billing are distinct. See [doc 16](docs/16-local-owner-and-mcp.md)
for actions, reader configuration and lifecycle limits.

As a user service plus tray (the installer seeds a private config, never enables
anything, and does not change the kill switch):

```bash
uv run jev-desktop service install --allow-app kate --allow-app firefox
systemctl --user enable --now jev-desktop.service
uv run jev-desktop tray
```

## Repository layout

| Location | Contents |
| --- | --- |
| Root Python modules | Service, clients, workers and shared capture/benchmark code. |
| `tests/` | Offline unit tests, grouped by module. |
| `tools/` | Standalone probes and the fast core check: `check`, service probe, MCP trial client, vision-wait probe, scroll-effect probe. |
| `benchmark_fixtures/` | Reusable local HTML fixtures. |
| `docs/` | Current handoff, contracts, decisions and historical results. |
| `run/` (ignored) | Captures, documents, timelines and trial evidence. |

Run a targeted check from the repo root:

```bash
uv run python -m tools.check                      # fast core set (~4 s, 96 cases)
uv run python -m unittest tests.test_live_owner   # one module
uv run python -m unittest discover -s tests       # everything (~10 s, 199 cases)
```

[Verification runbook](docs/29-verification-runbook.md) maps a change type to its
cheapest check. Probe commands can launch apps or call a provider; read their
documented limits before running them. Shared benchmark modules remain at root
because the runtime imports them.

## Benchmarks and design

- [Vision benchmark](docs/13-runnable-vision-benchmark.md): saved screenshots,
  configurable DeepSeek/MiMo readers and earlier measured results.
- [Dynamic wait baseline](docs/17-dynamic-wait-baseline.md): loading, error,
  animation, no-change and outside-region dialog cases.
- [Build plan](docs/14-app-build-plan.md): milestones and deferred decisions.
- [Architecture and ADRs](docs/04-architecture.md): decisions and proposal history.
- [Prior art](docs/02-prior-art.md): the projects we reuse.

Credentials stay in ignored `.env`; captures, results and interrupted-run
records stay in ignored `run/`. Neither is published. Small provider tests are
owner-authorized; keep explicit call limits and usage accounting. Historical single-probe caps do not describe the current reader policy.
