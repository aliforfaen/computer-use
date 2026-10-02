# 20 — Codex screenshot handover workflow

## Purpose

Check an application state in Firefox, then write and revise a short handover
in Kate. Reuses `kwin-mcp`, the existing virtual fixtures and local MCP owner.
This is a useful bounded integration workflow, not general web browsing.

## Run

Start the allowlisted owner (no reader required):

```bash
uv run jev-desktop daemon --foreground --allow-app firefox --allow-app kate
```

Ask a vision-capable Codex agent to:

1. Read MCP capabilities/status; refuse to interrupt an existing session.
2. Start virtual Firefox in `yolo`. Request an app screenshot with
   `output: "image"`; describe initial state and disabled controls.
3. Read fresh candidates. Click `Advance state`, verify `fixture_state` with
   expected `State: complete`. Inspect the final screenshot and controls.
4. Stop Firefox and require confirmed cleanup.
5. Prepare a handover and short appended section, at most 230 characters
   combined, before starting Kate.
6. Start Kate. Inspect its screenshot, read fresh editor candidates, type the
   initial handover with `target_text` and exact expected text.
7. Refresh candidates, append the second section with expected text equal to
   the complete note. Inspect the final screenshot and verify its contents.
8. Stop Kate; confirm cleanup and final status with no active session.

Use only MCP for app observation/input. Calls are sequential. Each session
has 90 seconds, eight action attempts and 16 reads. One restart per phase is
allowed; report failures explicitly. No project reader/provider calls.

## Evidence and comparison

Save a report under ignored `run/real-agent-trial/` with exact note, capture
IDs/hashes, verifier outcomes, action/read/tool counts, phase wall times,
restarts and cleanup. Image inspection must happen in the agent context.
Agent inference usage may be unavailable; say so. Wall time includes agent
reasoning and tool/setup work and does not isolate vision latency.

The first run uses Luna High in Codex. A later DeepSeek comparison should use
this same assignment and count both agent and backend requests; it requires
fresh authorization for project provider calls. Do not infer relative speed
from the earlier OpenCode task, which included different preparation/recovery.

## First Codex run — 2026-10-02

Luna High completed both phases through the installed MCP: one Firefox click
and two Kate edits, all effect verifications passed, four screenshots inspected
inline, no restarts, both session cleanups confirmed. Firefox took about 27 s;
Kate about 42 s including cleanup. Final note: 144 characters. Agent usage and
isolated vision latency were unavailable. Screenshot bytes were not exported;
capture hashes and observed results are recorded in the local report.

Evidence: `run/real-agent-trial/codex-luna-20261002T132006Z/report.md`;
coordinator audit: `run/codex-luna-workflow/audit.jsonl`.
The coordinator confirmed idle status and shut down its daemon afterward.

Codex omitted `XDG_RUNTIME_DIR` from the MCP process. Its global MCP config
now explicitly sets `JEV_DESKTOP_SOCKET=/run/user/1000/jev-desktop/owner.sock`.
Current processes use a temporary alias at `/tmp/jev-desktop-1000/owner.sock`;
new launches use the explicit path. When changing `--run-dir`, also supply
`--socket /run/user/1000/jev-desktop/owner.sock` so the owner matches Codex.
