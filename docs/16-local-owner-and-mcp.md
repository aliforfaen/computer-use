# 16 — Local owner, CLI and MCP surface

**2026-10-01 · M3 local surface implementation; updated 2026-10-02 for live opt-in.**
The checkout has one `jev-desktop` owner process, a thin CLI client, and an MCP
stdio client. Both clients use the same private Unix socket and the same
`DesktopService` state.
Virtual remains the default. A local live mode is now available only with
explicit owner-present and temporary AT-SPI opt-ins. Physical-input detection
is unavailable on this host, so the owner-present override is required every
time. No HTTP listener, Tailscale Serve route or planner is implemented here.

## Runtime and ownership

The daemon starts with `--foreground` and requires one or more explicit
allowlist entries from `--allow-app`, `JEV_DESKTOP_ALLOW_APPS` or
`allowed_apps` in its config file. The current virtual fixture registry is Kate,
Firefox and KCalc; the service rejects any configured app outside that set. It owns
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

After an owner-watched workflow is explicitly ready, a live session can be
started with both safeguards:

```bash
uv run jev-desktop session start firefox --live --owner-present-override --temporary-a11y
```

Live sessions launch one newly owned allowlisted app and refuse an existing
window for that app. Candidate, observation, wait and action calls snapshot
current focus, then focus the task app for accurate accessibility state and
visible captures. Actions verify their effect; every call restores exact prior
focus before returning. Stop snapshots current focus when possible and restores it
after closing the tracked app; if the owned app held focus, stop restores the
session baseline instead. Stop also restores original accessibility flags.
Failure recovery uses the task journal to terminate only the recorded app PID
and independently retries focus and accessibility restoration. The owner-watched
Kate workflow and app screenshot passed on 2026-10-03; Firefox and physical
failure/recovery remain unvalidated. See [doc 26](26-live-owner.md).

Observation defaults to metadata for the app window. `--output image` and
`--output both` explicitly request PNG data; `--output data` requests configured
reader interpretation. `--scope full` and `--scope crop --crop '{...}'` are
explicit. The crop JSON has integer `x`, `y`, `width` and `height` fields. The
`both` output means image plus reader interpretation: it requires an enabled
reader and otherwise returns `reader_unavailable`. Use `image` for screenshots
without a reader; it also returns capture metadata and the image hash.
Issue session calls sequentially, including `observe` and `candidates`;
concurrent stateful requests can return `session_busy`.

The KCalc `display_text` example verifies the fixture's blank-to-`1` transition;
it does not generalize to arbitrary application clicks. The capture ID, image
hash and crop mapping stay together. Candidate
responses omit editable contents. Typing takes `--text-file PATH` or stdin
with `--text-file -`; the CLI does not place typed text in shell arguments.
Actions require a fresh candidate reference and a supported code-owned
verification (`target_focused`, `target_text`, `fixture_state`, `display_text`,
`document_saved`, `navigation_url`, `window_title`, `visible_text` or
`scroll_changed`). There is no arbitrary coordinate or verifier callback input.
The initial effect verifiers are fixture-specific: KCalc permits `One` from a
blank editable display to `1`; Firefox permits `Advance state` from idle to
complete; Kate verifies a nonempty edit against exact changed editor text.
Focus verification only establishes focus. It does not establish task completion.
The owner now defaults to a 180-second inactivity timeout, a 30-minute
maximum session lifetime, 64 actions and 256 reads. Successful observation,
candidate and action calls refresh activity; status polling and rejected
calls do not. A watchdog stops the exact expired session even if its caller
never returns. `--idle-timeout`, `--max-session-lifetime`, `--max-actions` and
`--max-observations` configure the limits. Stop/status remain available.
Cleanup uses the same normal stop and crash-recovery path; unconfirmed cleanup
remains visible as broken state rather than being reported as success.

The stdio server is a client, not another owner. Configure an MCP host to run
`uv run jev-desktop-mcp` as a local stdio process. It exposes
`desktop_capabilities`, `desktop_status`, `desktop_session_start`,
`desktop_session_stop`, `desktop_candidates`, `desktop_observe`, `desktop_act`,
`desktop_wait`, `desktop_cancel` and `desktop_stop_all`. Images become MCP image content only
when `desktop_observe` explicitly uses `output: "image"` or `"both"`; the
payload is the same PNG represented by the observation hash. MCP tool errors
preserve the owner's `isError` result. Client disconnect does not implicitly
cancel an owner operation; use `desktop_cancel`, `desktop_session_stop` or
`desktop_stop_all` explicitly.

`desktop_wait` is a bounded read-only screenshot heartbeat and is
**experimental**: both recorded trials returned `invalid_judgment` while ordinary
polling completed the same tasks, so `capabilities` reports
`wait.status = "experimental"` and the CLI/MCP descriptions recommend polling
`candidates`/`observe` instead ([doc 24](24-primary-agent-wait-comparison.md),
[doc 29](29-verification-runbook.md)). It takes a
caller-authored visual condition and a deadline up to 120 seconds. The owner
captures the complete app window with at least a 500 ms interval, coalesces
while one reader judgment is in flight, and requires the same positive
judgment on two fresh frames before it wakes. It returns the final image with
the exact capture ID, metadata and hash used for that frame. The configured
reader can return only
`wait`, `wake`, `error` or `unexpected`; it cannot choose targets or perform
actions. The primary agent must inspect the returned image and verify state.

Internal wait captures debit the same session observation cap as ordinary
reads. DeepSeek interpretations debit the same per-session reader cap as
`desktop_observe(data|both)`. Attempts, numeric usage, provider-reported
latency and reader subprocess wall time appear separately in the wait result
and session status. Attempts are not retried, and rate limiting latches the
reader closed for the rest of the session. Provider usage is not a billing
receipt. Each reader call runs in a reaped subprocess under a hard wall
deadline that includes response headers and body; stream parsing also keeps
the response-body size limit. The wait is shortened to the remaining session
lifetime. Cancellation and session teardown stop the reader process, wait and
capture producer.

## Config, service and tray (added 2026-10-03)

Settings resolve with CLI > `JEV_DESKTOP_*` env > config file > built-in
default. The config path is `--config`, else `$JEV_DESKTOP_CONFIG`, else
`${XDG_CONFIG_HOME:-~/.config}/jev-desktop/config.json`; unknown keys and wrong
types are rejected, and the file never holds secret values (a reader key stays
in the environment or `dotenv`). Besides `allowed_apps`, limits and reader
names, the config tunes audit retention (`audit_max_bytes`, `audit_backups`,
`audit_max_age_days`). The audit itself is durable at
`~/.config/jev-desktop/audit.jsonl`, size-rotated and age-pruned, with stale
session journals swept at startup; the socket and live session journals stay in
the private runtime directory. See [doc 28](28-tray-and-user-service.md) and
ADR-021.

`jev-desktop service install|status|uninstall` renders and manages the
`systemd --user` unit around `daemon --foreground --config <config>`.
`--tray` also writes `jev-desktop-tray.service`. The installer seeds a `0600`
starter config only when none exists, never enables a unit, and `uninstall`
refuses an active unit without `--stop`. Stopping a unit uses the daemon's
existing SIGTERM cleanup, so focus/accessibility restoration and owned-app
cleanup still run; the simple kill switch is unchanged.

`jev-desktop tray` runs a KDE StatusNotifierItem client of the same socket with
status lines, a virtual/physical **preference**, per-app task start, session
cleanup (with the cleanup confirmation surfaced), service start/stop/enable and
the stop-all-and-shutdown kill switch. Tray behaviour is configurable in
`~/.config/jev-desktop/tray.json` (desktop mode, autonomy mode, poll interval,
confirmation toggles) through a Settings dialog. The mode preference is not
authorization: a live start always opens a per-task dialog confirming owner
presence and temporary accessibility, so ADR-018 is preserved.

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

## Budget and interpreted-data reader

The local daemon can opt into DeepSeek, MiMo or a compatible reader by setting
a provider and a positive per-session request cap. For the local `.env` key,
for example:

```bash
uv run jev-desktop daemon --foreground --allow-app firefox --allow-app kate \
  --reader-provider deepseek --reader-key-env DEEPSEEK_API_KEY \
  --max-reader-calls 8 --reader-timeout 10 --reader-total-timeout 15 --dotenv .env
```

The dotenv loader reads only the named key into the daemon environment and
never prints its value. `--reader-timeout` bounds connect/idle reads;
`--reader-total-timeout` bounds response-body parsing by time and size, inside
an outer killable subprocess deadline that also covers slow response headers.
Every attempted request is fsynced to the append-only audit before network I/O
and completed requests record provider/model, provider latency, subprocess
wall time, numeric returned usage and outcome. Requests are not retried; HTTP
429 latches the per-session reader closed. These usage fields are provider
reports, not billing data or a hard dollar cap.

The wait command needs a running Firefox session and an enabled reader; do not
mix it into the KCalc session above. For example, with the reader-configured
daemon running:

```bash
uv run jev-desktop session start firefox --mode yolo
uv run jev-desktop wait firefox --expected 'Forecast ready is visible, or page shows a forecast loading error' --timeout 20
uv run jev-desktop session stop
```

Codex/Luna inference happens outside this owner. Its cost is unavailable to
MCP, so session reader call limits do not cap the caller's own inference.
Caller turns, tokens and cost can be reported only if the model host exposes
them.

`output: "image"` returns screenshots for vision-capable callers; `data` and
`both` request the configured reader, with the image and interpretation tied
to the same capture. Reader output is observed text/data, not executable
targets. ADR-012 remains the grounding requirement.

## Persistent Kate documents

Kate starts with an exclusively created task-owned file under `run/documents/`.
Its path is returned in session start/status. It survives session teardown;
temporary profiles, virtual apps and compositor resources are cleaned up.
The caller cannot choose an arbitrary file or overwrite an existing document.

Fresh focused editor candidates support `replace_document` and `save_document`.
Replacement selects all through Kate, types the replacement and verifies exact
fresh editor text. Save sends Ctrl+S and verifies the actual file bytes against
fresh editor text using `document_saved`. The bounded AT-SPI wrapper reuses
kwin-mcp's worker and expands its 200-character text cap for task documents;
the supported text limit is 4096 characters. No model supplies key combinations.

See [the useful acceptance task](21-kate-acceptance-workflow.md). Owner audit
records include per-request duration and an ID for timing reports, without
recording typed text.

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
- `uv run python -m tools.local_service_probe`: **passed**. Independent CLI, Unix
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
