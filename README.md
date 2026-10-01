# Computer Use

Screenshot-driven computer use for agents on **CachyOS, KDE Plasma and Wayland**.
Built on [kwin-mcp](https://github.com/isac322/kwin-mcp), with a local CLI and MCP
client. Jev is an optional future accelerator.

## Where things stand

- **Working:** disposable virtual Kate, Firefox and KCalc sessions; app screenshots;
  verified actions; one shared local owner; cancellation and crash recovery.
- **Local wait baseline:** 10/10 fixture outcomes passed, with cleanup. The full
  suite passed 77 tests at that checkpoint. This is a local pixel-difference test,
  not evidence of model or agent speed gains.
- **Paused for tomorrow:** the DeepSeek heartbeat test stalled and was stopped.
  Its charged usage is unknown; the conservative first-batch allowance was
  about **$0.022**. Timeout, interrupt and progress-recording fixes need review.
- **Later:** general planning, live desktop operation, tailnet deployment and Jev
  acceleration. These are not implemented in this release.

Start tomorrow with [the handoff](docs/HANDOFF.md). Detailed results and limits:
[local service](docs/16-local-owner-and-mcp.md) · [wait baseline](docs/17-dynamic-wait-baseline.md).

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
model interpretation is disabled by default. See [doc 16](docs/16-local-owner-and-mcp.md)
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
records stay in ignored `run/`. Neither is published. Testing is paused for
this checkpoint; resume from the handoff before running the paid probe.
