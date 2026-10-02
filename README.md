# Computer Use

Screenshot-driven computer use for agents on **CachyOS, KDE Plasma and Wayland**.
Built on [kwin-mcp](https://github.com/isac322/kwin-mcp), with a local CLI and MCP
client. Jev is an optional future accelerator.

## Where things stand

- **Working:** disposable virtual Kate, Firefox and KCalc sessions; app screenshots;
  verified actions; one shared local owner; cancellation and crash recovery.
- **Local wait baseline (M4a):** 10/10 fixture outcomes passed with cleanup, one
  trial per case/arm. This is a local pixel-difference test, not evidence of model
  or agent speed gains. Full suite is now 97 tests, all passing.
- **Vision heartbeat (M4b):** the timeout/journal/interrupt changes were reviewed,
  fixed and tested on 2026-10-02. The single configured DeepSeek follow-up returned
  one valid structured judgment in 1.41 s (821 prompt / 8 completion tokens,
  cleanup passed, $0.0002559 peak-upper). The earlier interrupted run's charge is
  still unknown; the conservative whole-session reserve is $0.0229632.
- **Later:** general planning, live desktop operation, tailnet deployment and Jev
  acceleration. These are not implemented in this release.

Start with [the handoff](docs/HANDOFF.md). Detailed results and limits:
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
records stay in ignored `run/`. Neither is published. The single M4b follow-up
request has been made and its cap is consumed; do not make further provider
requests without fresh authorization.
