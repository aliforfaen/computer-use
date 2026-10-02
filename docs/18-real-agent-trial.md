# 18 — Real agent trial: local virtual desktop

**2026-10-02 · Runnable brief for an external agent.**

## Assignment

You are the task-running agent. Use this project's public MCP tools to observe
and operate disposable virtual applications on `cachy`. Complete the three
tasks below, choosing targets from fresh observations, and report evidence.
This tests whether a real agent can use the service without the scripted host
harness. It does not test a general planner, arbitrary visual coordinates,
Jev acceleration or remote deployment.

Read [AGENTS.md](../AGENTS.md) and [the local service guide](16-local-owner-and-mcp.md).
Reuse the existing **kwin-mcp** owner and official Python MCP SDK facade.
Do not modify implementation files or run the scripted task/benchmark harness
as a substitute for completing these tasks through the public tools.

## Setup

Run on `cachy` as the desktop owner, in:

```text
/home/messhias/lamasync/projects/computer-use
```

A remote agent must have its tool process running on this host. There is no
project HTTP endpoint or tailnet MCP listener yet. Report a setup blocker if
your client cannot launch a local stdio MCP process here.

Use a vision-capable agent for this first trial. The project reader is disabled:
there are **zero project-provider calls**. Your primary agent's own inference
is separate; record its usage/cost if your client exposes it. A non-vision
client may run the semantic subset, but must mark screenshot interpretation
untested rather than claim the complete trial passed.

1. Run `uv sync` from the checkout.
2. Check `uv run jev-desktop status`. If an owner exists, inspect it; do not
   stop another active task. An idle compatible owner can be reused.
3. If no owner exists, start this in a separate persistent terminal:

   ```bash
   uv run jev-desktop daemon --foreground --allow-app kate --allow-app firefox --allow-app kcalc
   ```

4. Configure your client's stdio MCP server. A typical configuration is:

   ```json
   {
     "mcpServers": {
       "jev-desktop": {
         "command": "uv",
         "args": ["--directory", "/home/messhias/lamasync/projects/computer-use", "run", "jev-desktop-mcp"]
       }
     }
   }
   ```

   Adapt the enclosing configuration to your client. Discover the live tool
   schemas rather than assuming this document is the protocol specification.
5. Call `desktop_capabilities` and `desktop_status`. Record the Git commit,
   agent model/client, vision support, KWin and kwin-mcp versions. If the tools
   are unavailable, report that blocker before attempting actions.

CLI fallback is allowed when MCP setup fails; follow doc 16 and label the run
**CLI-only**. It cannot establish MCP integration success.

## Rules during the trial

- One virtual session at a time; use `mode: "yolo"` for this bounded fixture
  trial. App allowlists, action caps and audit still apply.
- Request screenshots explicitly with `desktop_observe` using
  `output: "image", scope: "app"`. Metadata alone is not a viewed screenshot.
  `both` means image plus reader data and fails when the reader is disabled.
- Issue session tool calls sequentially. Parallel `observe` and `candidates`
  calls can return `session_busy`.
- Use `desktop_candidates` immediately before each action. Never invent or
  reuse refs across candidate refreshes, actions or sessions. Candidate labels
  and states ground actions; screenshots supply visual context.
- Supported actions are `click` and `type_text`. No coordinates, browser DOM
  automation, direct AT-SPI calls, terminal input or filesystem edits as task
  shortcuts. Shell use is for setup, version checks and the result report.
- Inspect every action's verification result, then request a fresh screenshot.
  A success message from yourself is not evidence of the app effect.
- Each session has a 90-second work window, eight input attempts and 16
  explicit observation/candidate requests. Prepare your next call promptly.
  Avoid sleep/screenshot loops; this MCP surface has no `wait` tool yet.
- On a refusal, preserve the error and inspect status. At most one clean
  restart/retry per task. Never bypass a refusal. If cleanup is unconfirmed,
  stop the trial and report it.

## Task A — KCalc: observe, select and verify

Goal: change a fresh blank calculator display to **1**.

Start `desktop_session_start` with `app: "kcalc", mode: "yolo"`. View an app
screenshot and identify the starting display. Get fresh candidates and choose
the enabled button corresponding to the visible digit 1 (its semantic label
is `One`). Call `desktop_act` with:

```json
{"app":"kcalc","action":"click","target_ref":"REPLACE_WITH_FRESH_REF","verification":"display_text","expected":"1"}
```

Require successful effect verification and a fresh screenshot showing 1.
Stop the session and check status before Task B. This fixture supports this
specific transition; do not attempt arbitrary arithmetic in this trial.

## Task B — Kate: compose and type

Goal: write an original three-line poem about a virtual desktop, totaling
**at most 200 characters**, into the fresh editor. Keep the exact poem in your
working context for verification; no file saving is required.

Start a Kate session and view its screenshot. Identify the editable text
candidate. If needed, click that editor with `verification: "target_focused"`;
focus verification alone does not complete this task. Refresh candidates
before typing. Call `desktop_act` with `action: "type_text"`, the fresh editor
ref, `verification: "target_text"`, and the exact same poem string in both
`text` and `expected` (newlines included).

Require successful exact-text verification and inspect a fresh screenshot for
all three lines. Explain any visual/semantic disagreement. Stop the session.
The service currently limits `expected` to 256 characters even though the
MCP schema advertises a larger field; the 200-character task avoids that gap.

## Task C — Firefox: inspect controls and transition state

Goal: change the built-in local action fixture from **State: idle** to
**State: complete**.

Start a Firefox session. The service launches its own local fixture; no URL
or internet browsing is needed. View the app screenshot and get candidates.
Report which control is disabled using the semantic states. Do not try to
click `Disabled control`: it is an observation check, not a destructive or
negative-action test. Choose the enabled `Advance state` candidate and call:

```json
{"app":"firefox","action":"click","target_ref":"REPLACE_WITH_FRESH_REF","verification":"fixture_state","expected":"State: complete"}
```

Require successful verification, a fresh screenshot showing the completed
state, and fresh candidates showing `Advance state` is now disabled. Stop
the session and confirm the owner reports no active session.

## Recovery and cleanup

If a task fails, record the failing tool/error and distinguish setup failure,
target ambiguity, policy refusal, verification failure and app failure. Stop
your session, confirm cleanup, then retry once from a fresh session if useful.
Recovery is **not exercised** when all tasks pass first try; do not fabricate
a failure to claim coverage.

Always stop your session at the end, including after failure. Use
`desktop_stop_all` only if all owner work belongs to this trial. If you started
the daemon, finish with `uv run jev-desktop shutdown`; leave a pre-existing
owner running. Record confirmed versus unconfirmed cleanup. MCP disconnection
alone does not stop a session.

## Evidence and report

Create a fresh ignored directory under `run/real-agent-trial/` for your report;
do not overwrite existing evidence. Keep screenshots local. Record tool names,
relevant safe responses, capture IDs/hashes, verification outcomes and errors.
Do not dump base64 images, environment contents or unrelated session logs.

Use this report structure:

```markdown
# Real agent trial
Date / host / Git commit:
Agent client / model / vision support:
Transport: MCP stdio | CLI-only
KWin / kwin-mcp versions:
Owner: reused | started by trial

| Task | Pass / fail / blocked | Action verification | Screenshot evidence | Elapsed | Tool calls | Restarts |
| --- | --- | --- | --- | --- | --- | --- |
| KCalc | | | | | | |
| Kate | | | | | | |
| Firefox | | | | | | |

Cleanup: session stop result, final owner status, daemon shutdown if owned.
Project-provider requests: 0.
Primary-agent turns / input tokens / output tokens / cost: measured or unavailable.
Observed friction and exact errors:
Recovery: observed details or not exercised.
Recommended next change:
```

A full pass requires all three verified effects, actual screenshot inspection
by the agent, MCP use and confirmed cleanup. Report partial success honestly.
Elapsed time includes task tool calls and reasoning; separate daemon setup.
Use your client's actual inference-turn count when available: tool-call count
is not a substitute. This is one integration trial, not an acceleration A/B
test or evidence of broad autonomous desktop capability.

## First completed trial — OpenCode, 2026-10-02

OpenCode with `deepseek-v4.1-flash` (high) ran the three tasks through MCP
stdio on commit `d0d5e6e`. Retained local evidence:
`run/real-agent-trial/20261002T134050/{report.md,audit.jsonl}`.

The audit corroborates one verified effect per app (`display_text`,
`target_text`, `fixture_state`) and three successful session stops. Coordinator
review subsequently exported OpenCode v2.0.22 session
`ses_f039a5d99ffeywaNjWkZjGJ12w` into `opencode-session.json` in the same ignored
directory. Its six embedded screenshots were extracted to capture-ID PNGs;
all six SHA-256 hashes match the observation metadata. Independent inspection
of the final images confirms KCalc displays 1, Kate contains the exact
three-line poem, and Firefox displays `State: complete` with disabled controls.
The transcript confirms image content delivery through MCP, successful action
verification, cleanup-confirmed stop responses and final `session: null`.
Subsequent CLI output confirms the owner socket was removed. This is a
successful integration trial with independently inspected visual evidence;
it was not rerun by the coordinator.

Two observation calls failed safely (`session_busy`, `reader_unavailable`)
and were corrected without restarting a session. App-failure recovery was not
exercised. OpenCode reportedly needed a relaunch to load its new MCP config;
its local `opencode.json`/`opencode.jsonc` remain ignored. The trial brief did
not instruct parallel session calls; the explicit sequential rule above now
makes that constraint clearer.

The report's approximately 22/24/14-second task timings end at the post-action
screenshot. They exclude later checks and session cleanup; Firefox's final
candidate check occurred another 15 seconds later. Do not treat these as full
task-and-cleanup timings. Reported primary-agent session totals include setup:
48 assistant turns and $0.0826843, not a per-task cost or acceleration measure.
No project-provider calls were reported; the service reader was disabled.
