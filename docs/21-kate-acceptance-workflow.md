# 21 — Luna local-to-Kate acceptance workflow

**2026-10-02 · local virtual acceptance.** This is a bounded primary-agent
workflow for the updated lifecycle and Kate document surfaces. It starts from
the local briefing below, asks Luna to produce a useful handoff note, saves
that note from Kate, and records an end-to-end call timeline. It is not a live
desktop test and does not use a vision provider.

## Acceptance assignment

Use the briefing shown in the existing Firefox `action.html` local fixture and
give Luna this task:

> Read the synthetic shift handoff briefing in Firefox. Write a concise
> handoff note in Kate that preserves confirmed facts, gives the next operator
> clear actions, and calls out the unresolved question. Save the note. Do not
> invent causes, owners, or completion claims. Report the saved document path
> and the final note text.

Use only the local desktop owner and existing CLI/MCP surface. Start virtual
Firefox and read the local briefing fixture. Stop that session and start Kate;
inspect the document candidate, put the note in the owner-generated task
document, then save and verify it. `replace_document` edits the current focused
Kate editor in the owner-generated task document and verifies exact fresh
AT-SPI text; it can revise the first draft in the same document. `save_document`
issues Ctrl+S and verifies editor text against the bytes on the owner-created
document path. The path is generated and returned by the owner; the task does
not choose a path. The supported save verifier is `document_saved`.

## Timeline evidence

Record each primary-agent tool call with its start and end timestamps, tool
name, and outcome. Also record each gap from one tool-call completion to the
next call start, and the total elapsed time from task delivery through verified
save. Keep **tool-call duration** (owner/MCP call wall time) separate from
**between-call gap** (agent reasoning, scheduling, or other delay). Report
vision-reader latency as not applicable: this workflow makes no provider call.
Do not label a screenshot/capture duration or a single observation duration as
the task's end-to-end performance.

Summarize calls, total call time, total between-call gap, and end-to-end time.
Include the raw timestamped timeline so another run can be compared. Preserve
the distinction between measured values and any attributed cause for a gap.
Generate the owner-call report with:

```bash
uv run python workflow_timeline.py run/acceptance/audit.jsonl --output run/acceptance/metrics
```

The output directory contains `timeline.json`, `timeline.md`, and
`timeline.html`. This audit report covers completed `local-mcp` owner calls;
record task-delivery and verified-save timestamps separately when available,
since the owner-call span alone is not prompt-to-save latency.

## Pass conditions and cleanup

- The note is useful to the next operator, contains only facts present in the
  briefing, lists concrete next actions, and preserves the open question.
- Kate reports the exact final editor text and `document_saved` succeeds for
  the owner-generated file.
- The run records successful session cleanup and confirms the Kate process and
  temporary session resources are gone. The saved note remains under the
  ignored run directory as acceptance evidence.
- Report the active MCP tool schema revision. If the connected host still has
  the old `desktop_act` enum, refresh tool discovery/reconnect before sending
  the action; do not infer that schema changes are active from source files.
  For a temporary fresh-schema acceptance call through the official stdio
  server, use `uv run python -m tools.mcp_trial_client TOOL --params-file FILE
  --output-dir DIR`. It initializes, discovers and validates the live server
  schema, saves image blocks and structured output under `DIR`, and prints
  paths/status only. It exercises the official local MCP server and owner;
  restart the host later to refresh its cached schema.

Record the session lifecycle stop reason, action/observation counts and any
configured idle or lifetime limit. The acceptance run should stop normally
after saving so that automatic inactivity cleanup is not mistaken for a
successful task stop. A separate controlled short-idle run may verify timeout
cleanup without a provider call.

## Later live desktop phase

This acceptance covers only the isolated virtual session. The pinned
`kwin-mcp==0.10.0` source has `AutomationEngine.session_connect()`,
`active_window()`, `window_geometry()`, `focus_window()` and `screenshot()` for
a later explicit live probe. `LiveSession.launch_app()` returns a tracked PID;
`session_stop()` terminates only apps launched through that live session and
disconnects without stopping the user's compositor. A minimal live smoke test
can therefore launch an owned KCalc process, verify its blank-to-1 display
transition, disconnect to terminate that process, and verify cleanup. Save
the prior active window ID and app identity first; the installed
`focus_window()` reactivates by app-name substring, so only proceed if that
identity resolves to one window, then compare the final active ID with the
saved ID. If focus cannot be restored exactly, stop and report the recovery
state. The standalone local smoke test is an explicitly owner-present task, with
physical-input idle detection recorded as unavailable. This does not implement
the remote live-task idle gate. A read-only connection/capture/active-window
probe can establish capture support first. No live input is part of the
virtual acceptance assignment above.

## Completed virtual acceptance — 2026-10-02

Luna High read the briefing from an MCP screenshot, verified the Firefox
transition, then wrote/saved a 424-character Kate draft and revised/saved
593 characters. Exact editor and disk checks passed; the persisted file has
594 characters because Kate adds a terminal newline. Coordinator image and
byte review confirmed every other byte matches. Both cleanups passed.

Evidence: `run/kate-acceptance-corrected/report.md`, screenshots under its
`agent/` directory, and its `metrics/timeline.html`/`.md`/`.json`.
The 21-call span through cleanup was 264.557 s, including 47.480 s of owner
work. First call through final save was 228.711 s. Between-call gaps include
agent work and fresh MCP-client startup; they do not isolate image reading.
ASCII character typing took 14.973 s and 20.239 s; saves took about 1.9 s each.
These measurements precede the subsequent bulk-text change.

The first attempt is preserved under `run/kate-acceptance/`: Kate failed at
startup because its title was checked before the window appeared. A bounded
readiness check fixed the reproduced problem. No input occurred in that failed
phase; cleanup completed, and exact empty failed-start files were removed.

A separate KCalc watchdog probe waited seven seconds without calls with a
five-second idle cap. Automatic cleanup removed the app, session group,
journal and temporary home. Evidence:
`run/host-watchdog-probe/kcalc-final-20261002T141210Z/summary.json`.

## Bulk text check

After retaining that baseline, Kate input switched to kwin-mcp's existing
Unicode/bulk-text path, including its temporary clipboard transfer and restore
fallback. A separate 1447-character host note verified exact editor text,
saved bytes and persistence after cleanup. Replacement took 2.066 s, save
1.864 s, and the short scripted probe took 6.312 s. This is a targeted input
measurement, not a rerun or speedup claim for Luna's entire acceptance task.
Evidence: `run/kate-bulk-owner/report.json` and its audit.
