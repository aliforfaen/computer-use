# 19 — Cross-app task: write a verified handover note

**2026-10-02 · External agent assignment.** Use the same local MCP setup as
[trial 18](18-real-agent-trial.md). The owner authorized this next trial.

## Goal

Use Firefox to complete the local application check, then compose and revise
a short handover note in Kate using facts you actually observed. This tests
cross-app context retention, multiple editor actions and recovery from a
stale target reference. It uses the current fixture-limited action contract;
general browsing, file saving and arbitrary visual actions are still absent.

## Scope

- Use public `jev-desktop_desktop_*` MCP tools for application observation and
  input; virtual sessions, `yolo`, one session at a time.
- Read fresh capabilities/status. Start an allowlisted owner only if absent;
  shut it down at the end only if you started it. Do not stop unrelated work.
- No project reader/provider calls, source edits, external websites or direct
  driver/DOM/shell shortcuts for completing the task.
- Session calls are sequential. Request `output: "image"`, not `both`.
- Keep each session under its 90-second / eight-action / 16-read caps. Prepare
  text before starting Kate. At most one clean session restart per phase.
- Retain all evidence locally in a new `run/real-agent-trial/` subdirectory.
  Existing evidence and ignored OpenCode config must remain intact.

## Phase 1 — Check Firefox

1. Start Firefox, inspect the screenshot and fresh candidates.
2. Identify the initially disabled control and visible starting state.
3. Click the enabled `Advance state` with `fixture_state` verification and
   expected `State: complete`.
4. Inspect the resulting screenshot and fresh candidates. Record the final
   visible state and whether `Advance state` is enabled or disabled.
5. Stop Firefox and require confirmed cleanup before starting Kate.

## Phase 2 — Produce and revise a note in Kate

Compose an initial note in your own words from Phase 1 observations. Include
a heading and the observed before/after state. Keep it below 150 characters.
Prepare a second section naming the two final button states. Initial note
plus appended section must total at most **230 characters**, newlines included.

1. Start Kate, inspect its screenshot, and obtain editor candidates.
2. **Controlled stale-reference check:** retain the editor ref from this read,
   request candidates again, then make exactly one `type_text` call using the
   older ref with `target_text` verification and the planned initial note in
   both `text` and `expected`. Require a refusal before input. This is an
   intentional freshness check, not an application failure. If it unexpectedly
   succeeds, stop and report the discrepancy; do not duplicate the text.
3. On the expected refusal, inspect status and screenshot. Confirm no note was
   inserted. Refresh candidates and retry using the new editor ref. Require
   exact-text verification and inspect the screenshot.
4. Refresh candidates again, append only the second section via `type_text`.
   Set `expected` to the complete combined note, not just the appended text.
   Require exact-text verification and a screenshot showing the complete note.
   If typing inserts/replaces unexpectedly, report the actual behavior; do not
   bypass verification or repeatedly retry.
5. Stop Kate and require confirmed cleanup. End with no active session.

Do not save the note through shell or filesystem edits as a substitute for an
app operation. The useful deliverable is the verified note in the editor,
retained as screenshot and exact text in the local report; general Save As
support is outside the current service. Export evidence before teardown if
your client exposes screenshot bytes; otherwise retain the OpenCode session
ID so the coordinator can extract its embedded screenshots later.

## Report

Record Git commit, model/client, versions, session IDs, capture IDs/hashes,
exact final note, verifier responses and the stale-reference refusal code.
List every action attempt and whether input occurred. Separate intentional
refusal recovery from unexpected failures. Report phase time including final
checks and stop, tool-call count, session restarts, cleanup and measured agent
usage (or unavailable). Do not equate tool calls with inference turns.

Pass requires: Firefox effect verified; note grounded in observed facts;
stale ref rejected without input; fresh-ref retry succeeds; append preserves
the first section and exact final text; screenshots delivered to and inspected
by the agent; all owned sessions cleaned up. A partial result is useful—name
the exact blocker. No implementation changes, commits, push or deployment.

## Completed trial — 2026-10-02

OpenCode / DeepSeek V4.1 Flash completed this task on `d0d5e6e` through MCP.
Evidence: `run/real-agent-trial/20261002T141956-ses_f039a5d99ffeywaNjWkZjGJ12w/`.
Coordinator review of the exported session confirms Firefox effect verification,
the stale-reference refusal, `actions: 0` afterward, two successful exact-text
Kate verifications, two cleanup-confirmed session stops and final no-session
status. Six extracted screenshot hashes match capture metadata. Independent
inspection confirms the editor was empty after the refusal and the final note
preserved the initial section and appended the observed button states.

Firefox took approximately 24 seconds and Kate 85 seconds including session
cleanup. Kate came close to the 90-second session limit; avoid generalizing
this limit to longer useful tasks without addressing session lifetime. The
agent also spent several minutes preparing between phases, including reading
implementation code: this was a working integration trial, not a test of
whether tool descriptions alone suffice or a pure execution-speed measurement.
No project-provider calls or implementation changes were made.

The report's cumulative assistant-turn counts are inconsistent with the
earlier report (45 versus 48); do not use them as comparable measurements.
Reported cost delta of about $0.0823 also includes intervening setup/config
work. Recovery demonstrated fresh-target acquisition after a controlled
refusal, not recovery from an application crash or failed effect.
