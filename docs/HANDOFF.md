# Handoff — current state

Updated **2026-10-26**. Repository: `aliforfaen/computer-use`, branch `main`.
Start with AGENTS.md, [access scopes](41-access-scopes.md), then [current live/profile contract](40-live-workspace-and-profiles.md),
[CLI/MCP](16-local-owner-and-mcp.md), and [verification runbook](29-verification-runbook.md).
Earlier handoff detail is preserved in [the archive](archive/handoff-before-2026-10-25.md).

## Direction

Useful broad desktop assistance for the calling agent. Jev is disabled by
default, unwired and deferred. Qwen-OCR and GPU work are deferred. Reuse
kwin-mcp, the existing capture/transaction layers, OpenSSH and systemd.

## Implemented — pending watched live acceptance

- **Access scopes:** selected application, full virtual desktop, or full live
  desktop, enabled by owner config and requested explicitly. Full scopes allow
  exact window switching without per-app allowlist/profile requirements.
  Switching invalidates every prior semantic/image reference.
- **Profiles:** built-in Kate/Firefox/KCalc plus validated custom argv and
  identity profiles in private `app_profiles.json`. Profiles provide launch
  metadata. App allowlists restrict selected-application scope, not full-desktop
  transitions. Scope does not change cleanup ownership or autonomy.
- **Workspace:** `desktop_workspace` discovers current windows, active ID,
  geometry and available output metadata without taking focus. Captions are
  limited to allowlisted apps in selected-application scope; full live scope
  explicitly includes workspace-wide context. `desktop_profiles` reports profile availability.
- **Live ownership:** launch a task app or attach one exact existing window.
  Foreground the task app/dialog for work; do not restore focus between calls.
  Restore baseline focus at stop. Attached apps are preserved; only apps
  launched by the task may be closed during cleanup.
- **Dialogs/interruption:** transient/PID metadata identifies related dialogs.
  Unrelated popups/background windows are reported, not silently acted on.
  Disappeared windows never fall back to cached target geometry.
- **Optional semantics:** live defaults to screenshots without AT-SPI. Opt in
  through `semantic_candidates=true` plus `temporary_a11y=true`. Captured
  accessibility properties are restored exactly, including IsEnabled-only hosts.
- **Grounding:** short generation-scoped semantic refs, useful names and
  screen/image bounds. Same-label controls with distinct geometry can resolve.
  `include_candidates=true` pairs fresh images with sequential semantic reads.
- **Image input:** `desktop_input` supports clicks, right/double clicks, typing,
  named shortcuts, bounded scrolling and in-window drag against a recent,
  single-use capture whose image was explicitly returned. Guarded/supervised image input requires explicit approval.
  Geometry/active-window checks precede delivery; fresh state follows.
- **Truthful outcomes:** `verification=observed` and image inputs report effect
  uncertain. Strong verifiers remain. `desktop_reconcile` acknowledges uncertain
  input, reads state and resumes without replaying actions.
- **Lifecycle:** status/watchdog detect dead workers and invoke owned-resource
  cleanup. IPC reconnects can inspect the existing session; client disconnect
  does not replay work. Idle/lifetime caps remain in force.
- **Relays:** named OpenSSH local/reverse forwards, explicit tray lifecycle
  controls, systemd reconnect supervision and five-second tray monitoring.
  `desktop_relays` reports timestamps, errors, restart/exit data and listener
  reachability; remote MCP readiness is explicitly untested.
- **Owner UI:** quiet cleanup, configurable repeated-live prompts, optional
  live semantics and exact-window attachment. No live service or relay was
  started/restarted by this implementation session.

## Validation and limits

Checks on 2026-10-26: `tools.check` passed **175 tests in 13 modules**;
44 targeted native-window/live-owner/harness tests passed; wheel build passed.
These are offline/mocked checks, not a host acceptance result. A connecting
agent's live test is deliberately deferred to the owner-watched checklist in doc 40.

Earlier evidence: owner-watched Kate draft/save/revise/save passed October 3
(doc 26). It does not validate current attachment, dialog handling, capture
input, general profiles or relay behavior.

Known limits: KDE/Wayland and pinned private kwin-mcp 0.10.0 seams; capture
mapping supports complete 1:1 logical sources only; no physical-input detection;
foreign-process/identity dialogs may require separate explicit attachment;
no automatic task success inference or action retries. Desktop HTTP/tailnet
transport and general planning are still deferred. No new OCR action path.

## Pi is configured for the watched test

Checked 2026-10-26: Pi has a dedicated `jev-desktop` skill and direct MCP tools,
with global and project-local configuration. The owner socket is explicit so
limited child environments cannot select the wrong fallback. All 17 tools
discover successfully; read-only status/capability calls succeed through MCP.
The idle owner was restarted to load the scope implementation and enable all three
access scopes in its private config. No desktop session or relay was started. Start a fresh Pi session to
load the setup. See [Pi harness notes](16-local-owner-and-mcp.md#pi-harness--checked-2026-10-26).
This is harness readiness, not live desktop acceptance.

## Next work

1. Watched agent connection: workspace discovery → attach existing test app →
   inspect image → input → related dialog → stop, preserving the original app.
2. Separate launched-app cleanup, popup interruption, uncertain-input recovery
   and worker disconnect checks. Keep all data synthetic and checks small.
3. One named SSH forward, verifying endpoint readiness independently of process
   status. No installed/started relay is implied by code being present.
4. Improve app/capture compatibility only from concrete owner task failures;
   avoid another broad OCR/model benchmark before a demonstrated need.

## Running

```bash
uv sync
uv run jev-desktop daemon --foreground --allow-app kate --allow-app firefox --allow-app kcalc
uv run jev-desktop capabilities
uv run jev-desktop profiles
uv run jev-desktop workspace
uv run jev-desktop relays
```

MCP host command: `uv run jev-desktop-mcp`. Refresh tools/restart intentionally
after updating; this session did not restart a running owner or client.
Private config and credentials remain outside Git; output in `run/`, disposable
environments/checkouts in `tmp/`, both ignored. User authorized commit/push of
this session. See the final session report for the published commit.
