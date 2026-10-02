# 23 — Public webpage to saved Kate handover

## Purpose

Validate ordinary browser navigation, screenshot reading, bounded page
readiness, recovery and a persistent Kate document in one Luna MCP task. This
extends [the local Kate workflow](21-kate-acceptance-workflow.md), which only
uses fixed fixtures. It remains an isolated virtual desktop test; it does not
touch the owner's browser or physical desktop.

## Assignment

Using the local MCP owner, open the public Python.org About page at
`https://www.python.org/about/`. Read it from fresh app screenshots and, when
useful, the configured DeepSeek reader's interpretation of the same capture.
Use only the returned MCP candidate/action surfaces for browser interaction.
Do not use HTTP clients, page source, DOM, browser debugging interfaces, shell
commands, or outside summaries to answer the task.

Create a **120–180 word** Kate handover for a reader unfamiliar with Python.
Summarize three useful facts that are actually visible on the page, include the
source URL, and separate the page's claims from your own short takeaway. Save
the initial version, check its wording against the source, revise it once, save
again, and report the exact final text and owner-generated document path. Do
not infer current release/version facts unless the page visibly provides them.

After starting the navigation, use the bounded owner wait once with a plain
condition such as “the Python.org About page heading and article text are
visible.” Inspect the final returned screenshot yourself before extracting
facts. The wait's interpreted judgment can indicate `ready`, `unexpected`,
`timeout` or `error`; it is not proof that a particular fact is correct.
Re-read the image or request a structured interpretation from that same
capture. Keep capture IDs and hashes with the evidence.

In Firefox, the address field may not start focused. If so, use its fresh
candidate with `click` and `target_focused`, then request candidates again
before `navigate_url`. Supply the identical complete HTTP(S) URL, including
scheme, as both the action's `text` and `expected`; the verifier checks the
observed address-bar host and path after navigation.

If the first navigation visibly fails, record the error state, navigate back
to the exact Python.org URL, wait for the page condition again, and continue
only after inspecting a fresh screenshot. If the navigation succeeds on the
first attempt, make one explicit bounded check of recovery by navigating to a
reserved `.invalid` host, inspect the visible browser failure, then return to
Python.org and re-check the page. Never put a failure-page claim in the saved
handover. If the browser does not expose an actionable URL field, stop and
report the exact candidate/action gap; do not use keyboard shortcuts,
coordinates, driver calls, or browser APIs as a workaround.

## Limits and pass conditions

- Allow only Firefox and Kate; one virtual session at a time, sequential MCP
  calls, `yolo` permitted under the normal code-owned app/action/call budgets.
- Use fresh candidates immediately before each action. Require its coded
  verifier and inspect a fresh screenshot after each navigation and document
  save/revision. Never treat the model's `DONE` as evidence.
- Use the reader only through the configured owner. Report its provider/model,
  call count, returned usage and latency. Agent-side token totals may be
  unavailable; mark them unavailable instead of estimating.
- Keep a raw MCP-call timeline and task prompt/start/save/cleanup timestamps.
  Separate owner time, reader latency, between-call gaps and end-to-end time.
- Save after the first draft and after the revision; compare final editor text
  with actual document bytes. Stop both owned sessions and confirm no active
  session, Firefox/Kate process, virtual profile, or cleanup journal remains.
- Do not open or mutate the live desktop, Tailscale routes, unrelated owners,
  or user documents.

Pass means the agent personally inspected current screenshots, used the
wait/readiness path, recovered from a visible navigation failure, wrote three
source-supported facts with the URL, saved and revised the actual Kate
document, and confirmed cleanup. A failure that safely stops at an unsupported
target or verification boundary is a useful partial result; record the
evidence and blocker.

## Evidence to retain

Place each attempt in a new ignored `run/public-kate-agent/<trial-id>/`
directory. Keep the assignment/model/client and MCP schema revision, host and
package versions, randomization seed if comparing arms, all call timestamps,
outcomes and gaps, capture IDs/hashes and images inspected by Luna, reader
metadata, exact saved text/path, document byte verification, recovery path,
session IDs, action/read/reader counts, and cleanup checks. No credentials,
clipboard contents or reader prompt bodies belong in audit logs.

The task itself is a primary-agent acceptance run, not a scripted driver test.
An independent coordinator checks the persisted report, timeline, saved bytes
and teardown before claiming it passed.


## Completed acceptance — 2026-10-02

Luna completed the public About-page read, visible `.invalid` failure/recovery,
and Kate draft/save/revise/save through MCP. Both sessions cleaned up.
Coordinator confirmed the reported final text exactly matches the saved
866-byte file. Evidence: `run/public-kate-agent/2026-10-02-luna-acceptance-01/`;
saved document: `run/documents/handover-54b05810723ab10aff229f0c.txt`.
The reconstructed owner timeline covers 36 calls, 293.795 s through final
status and 28.343 s of tool work. Images were inspected in the agent transcript;
image files were not retained. The later controlled wait comparison is in doc 24.
