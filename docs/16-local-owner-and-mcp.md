# 16 — Local owner, CLI and MCP surface

**2026-10-01 · M3 local surface implementation.** The checkout now has one
`jev-desktop` owner process, a thin CLI client, and an MCP stdio client. Both
clients use the same private Unix socket and the same `DesktopService` state.
This is a virtual-only local surface. No HTTP listener, Tailscale Serve route,
live desktop mode or planner is implemented here.

## Runtime and ownership

The daemon starts with `--foreground` and requires one or more explicit
`--allow-app` values. The current virtual fixture registry is Kate, Firefox
and KCalc; the service rejects any configured app outside that set. It owns
one active session and one [`kwin-mcp`](https://github.com/isac322/kwin-mcp)
`==0.10.0` child. `DesktopWorkerClient`
keeps one worker process alive across requests; that worker constructs one
`kwin_mcp.core.AutomationEngine` for the virtual session and forwards bounded
driver operations to it. The service does not create another driver for each
CLI or MCP request.

The default socket is `$XDG_RUNTIME_DIR/jev-desktop/owner.sock`, or
`/tmp/jev-desktop-<uid>/owner.sock` when `XDG_RUNTIME_DIR` is absent.
`JEV_DESKTOP_SOCKET` and the daemon/client `--socket` option can select another
endpoint. Its directory must be owned by the current user and private (0700);
the socket and stable flock lock file are 0600. The lock is acquired before
binding. A dead same-user socket at the configured endpoint can be recovered
while holding that lock. Shutdown removes the socket only if its device and
inode still match the socket this daemon bound. Audit output defaults to
`$XDG_RUNTIME_DIR/jev-desktop/audit.jsonl` (or the matching `/tmp` directory);
`--run-dir` and `--audit` can select local paths.

Each socket connection carries one bounded JSONL request and response. The
dispatch methods are `capabilities`, `status`, `session_start`,
`session_stop`, `candidates`, `observe`, `act`, `cancel` and `stop_all`.
The daemon's local `shutdown` control first requests `stop_all`, then exits.
Malformed requests and owner failures return safe structured error codes.
Context identifies the client as `local-cli` or `local-mcp`; it does not claim
a remote node identity.

## CLI and MCP

Install the checkout into its pinned environment with `uv sync`. Start the
daemon in one terminal:

```bash
uv run jev-desktop daemon --foreground --allow-app kate --allow-app firefox --allow-app kcalc
```

Use another local process as a client:

```bash
uv run jev-desktop capabilities
uv run jev-desktop status
uv run jev-desktop session start kcalc
uv run jev-desktop observe kcalc
uv run jev-desktop candidates kcalc
# For the KCalc fixture, use the fresh `One` candidate reference printed above.
# Replace TARGET_REF with that candidate's ref.
uv run jev-desktop act kcalc click TARGET_REF --verification display_text --expected '1'
uv run jev-desktop session stop
uv run jev-desktop stop --all
```

Observation defaults to metadata for the app window. `--output image` and
`--output both` explicitly request PNG data; `--output data` requests configured
reader interpretation. `--scope full` and `--scope crop --crop '{...}'` are
explicit. The crop JSON has integer `x`, `y`, `width` and `height` fields. The
KCalc `display_text` example verifies the fixture's blank-to-`1` transition;
it does not generalize to arbitrary application clicks. The capture ID, image
hash and crop mapping stay together. Candidate
responses omit editable contents. Typing takes `--text-file PATH` or stdin
with `--text-file -`; the CLI does not place typed text in shell arguments.
Actions require a fresh candidate reference and a supported code-owned
verification (`target_focused`, `target_text`, `fixture_state` or
`display_text`). There is no arbitrary coordinate or verifier callback input.
The initial effect verifiers are fixture-specific: KCalc permits `One` from a
blank editable display to `1`; Firefox permits `Advance state` from idle to
complete; Kate verifies a nonempty edit against exact changed editor text.
Focus verification only establishes focus. It does not establish task completion.
The owner enforces a 90-second session work window, eight input attempts,
16 explicit observations/candidate requests, and a separate transaction read
budget. Stop/status remain available after those work caps are reached.

The stdio server is a client, not another owner. Configure an MCP host to run
`uv run jev-desktop-mcp` as a local stdio process. It exposes
`desktop_capabilities`, `desktop_status`, `desktop_session_start`,
`desktop_session_stop`, `desktop_candidates`, `desktop_observe`, `desktop_act`,
`desktop_cancel` and `desktop_stop_all`. Images become MCP image content only
when `desktop_observe` explicitly uses `output: "image"` or `"both"`; the
payload is the same PNG represented by the observation hash. MCP tool errors
preserve the owner's `isError` result. Client disconnect does not implicitly
cancel an owner operation; use `desktop_cancel`, `desktop_session_stop` or
`desktop_stop_all` explicitly.

## Cancellation and cleanup limits

Sessions serialize stateful requests. `cancel` sets a cooperative event used
between driver calls and around verification; it cannot interrupt an
in-flight synchronous `AutomationEngine` call or an unbounded verifier. The
current fixture verifiers are bounded. `session_stop` and `stop_all` cancel
then wait up to the owner's stop timeout for the active request. If it does
not release the session lock, the worker is terminated and its private cleanup
journal is used to verify removal of known child processes and fixture paths.
The result reports cleanup as confirmed or unconfirmed; an unconfirmed session
stays broken for operator recovery. Daemon shutdown waits for socket handlers
and calls owner cleanup; cleanup failure produces a nonzero daemon exit.

## Optional interpreted-data reader

No reader is constructed unless `--reader-provider` is selected. DeepSeek and
MiMo defaults follow the existing local benchmark configuration; a generic
OpenAI-compatible endpoint requires explicit base URL, model and key-variable
name. Every enabled reader requires a positive `--max-reader-calls` cap, which
is enforced per session before each provider attempt. Reader credentials are
read from the named process environment variable only when `data` or `both`
is explicitly requested. The CLI accepts the variable name, never a key
value, and the daemon does not load `.env` itself.

For example, let `uv` load the ignored local `.env` into the daemon process
without putting key values in command arguments or output:

```bash
uv run --env-file .env jev-desktop daemon --foreground --allow-app kcalc \
  --reader-provider deepseek --max-reader-calls 3
```

Use `--reader-provider mimo --max-reader-calls 3` for the configured MiMo
defaults, or pass explicit `--reader-base-url`, `--reader-model` and
`--reader-key-env` values. Reader construction, capabilities and metadata
observation make no provider call. The cap bounds calls; it does not authorize
expanding the previously scoped paid benchmark.

## Pins and validation state

The project requires Python 3.13+, `kwin-mcp==0.10.0` and the official
[Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk)
`mcp==2.2.0`; these are pinned in `pyproject.toml` and `uv.lock`.
Pillow performs image mapping and httpx is used by the optional reader. The MCP
surface uses the SDK's low-level `Server` and stdio transport, not a hand-built
MCP protocol.

**Coordinator validation on cachy, 2026-10-01:**

- `uv run python -m unittest discover -q`: **59 tests passed**. Tests cover
  competing requests, cancellation during a blocked read, failed-start
  ownership, unchanged-effect refusal and replacement after worker recovery.
- `uv run python local_service_probe.py`: **passed**. Independent CLI, Unix
  client and official SDK stdio client reached one owner. MCP observed the
  session started over Unix IPC; retained metadata/image capture IDs and
  SHA-256 hashes matched. CLI verified KCalc blank→1. The reported worker was
  deliberately SIGKILLed; status became broken, cleanup was confirmed, and a
  fresh session started with a new worker. Its normal stop and daemon shutdown
  also passed. No paid calls.
- Additional direct-owner fixture checks verified exact Kate typing and the
  Firefox idle→complete effect; both sessions cleaned up, and the healthy
  worker PID remained the same across the two sessions.
- `uv build --wheel --out-dir run/wheel-check`: passed; wheel contains runtime
  modules, entry points and the HTML fixtures.

Measured versions: Python **3.13.13**, KWin **6.7.5**, kwin-mcp **0.10.0**,
MCP SDK **2.2.0**. Host evidence is local and ignored:
`run/local-service-probe-results/local-service-probe-j26e_is7/` contains summary
and audit; `run/m3-fixture-check-mv2hjhm_/` contains the separate fixture checks.
The probe is repeatable from the checkout. The competing-action and in-flight
cancellation checks use deterministic fake workers; the host cancellation
probe checks the control response, not interruption of a blocked real driver.

Review caught and fixed false unchanged-effect verification, an absent CLI
socket-default attribute, stale probe arguments, and reuse of a dead worker
after successful recovery. Failed probes retained safe summaries; their owned
sessions cleaned up. Provider-backed interpretation was not retested here.

This milestone does not add `wait`, task execution, broad visual grounding,
multi-monitor mapping, live desktop operations, a remote HTTP/MCP listener or
Tailscale Serve configuration. The planner contract and visual grounding stay
open under ADR-011.
