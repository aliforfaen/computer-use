# Computer Use

Screenshot-driven computer use for agents on **CachyOS, KDE Plasma and Wayland**.
Built on [kwin-mcp](https://github.com/isac322/kwin-mcp), with a local CLI and MCP
client. Jev is an optional future accelerator.

## Where things stand — 2026-10-03

- **Working:** virtual Firefox/Kate/KCalc, app screenshots, grounded navigation,
  links and text entry, saved Kate documents, automatic cleanup, CLI and MCP.
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
- **Still pending:** scrolling fails host effect verification. Live Firefox,
  forced cancellation/crash recovery, tailnet deployment and Jev replay have
  not been validated.

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

## Benchmarks and design

- [Vision benchmark](docs/13-runnable-vision-benchmark.md): saved screenshots,
  configurable DeepSeek/MiMo readers and earlier measured results.
- [Dynamic wait baseline](docs/17-dynamic-wait-baseline.md): loading, error,
  animation, no-change and outside-region dialog cases.
- [Build plan](docs/14-app-build-plan.md): milestones and deferred decisions.
- [Architecture and ADRs](docs/04-architecture.md): decisions and proposal history.
- [Prior art](docs/02-prior-art.md): the projects we reuse.

Credentials stay in ignored `.env`; captures, results and interrupted-run
records stay in ignored `run/`. Neither is published. The single M4b follow-up
request has been made and its cap is consumed; do not make further provider
requests without fresh authorization.
