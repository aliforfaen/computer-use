---
name: jev-desktop
description: Use the jev-desktop MCP server for desktop observation and control on CachyOS/KDE Wayland, including live window attachment, screenshot-grounded input and MCP relay monitoring. Use this when testing or operating the Jev desktop layer; Orca's own computer-use driver is a separate surface.
---

# Jev desktop

Use the `jev-desktop` MCP server (`desktop_*` tools; Pi may prefix names).
Jev model calls are deferred and are not needed to use this desktop service.

## Connect and choose the target

Read `desktop_capabilities` and `desktop_status`. Use `desktop_workspace` to
identify the active app/window and desktop, and `desktop_profiles` to see the
configured app identities and separate allowlist. `desktop_relays` reports local
app MCP routes; prefer a suitable app MCP when available. A running SSH process
or reachable TCP listener does not prove MCP readiness.

Live work requires the user's authorization and `owner_present_override=true`;
physical-input detection is unavailable. Attach an existing app with
`desktop_session_start` using its canonical `app`, `desktop_mode="live"`,
`attach=true` and exact `window_id`. Attaching never grants permission to close
that user app. A task-launched app has different cleanup ownership.

The task app/dialog is foregrounded for work. Focus is not restored between
calls; original focus is restored at session stop. Related dialogs are followed
by window/process identity. An unrelated popup is an interruption/context, not
an automatically authorized target. Read workspace/status when interrupted.

## Screenshot-first interaction

Live sessions default to `semantic_candidates=false`. Request
`desktop_observe` with `output="image"` to receive the actual current image.
Use `desktop_input` with that `capture_id` and coordinates relative to the
returned image, not the full screen. Supported operations include click,
right/double click, typing at a point, named shortcuts, scroll and drag.

Only a recent image explicitly returned to the caller can authorize input.
Every input attempt invalidates older capture references. The returned fresh
state is metadata: observe its capture ID with image output before the next
image-grounded input. Metadata-only observations do not arm input. If the
capture expires, geometry changes, or the selected window changes, obtain a
fresh image rather than guessing a transform.

Respect the selected autonomy mode. Guarded/supervised image input requires
explicit `approved=true` reflecting authorization for that action; `yolo`
suppresses prompts but retains app, ownership, budget and audit restrictions.

## Optional semantic access and uncertain results

For a semantic run, opt in at session start with `semantic_candidates=true`
and `temporary_a11y=true`. Use `desktop_candidates`, or pair a new image with
`include_candidates=true`. Refs are generation-scoped. Prefer useful refs when
available; they are optional for screenshot work.

`desktop_act` supports strong postconditions or `verification="observed"`.
Image input and observed results report uncertain effect, not task success.
Inspect the returned state/image and report only what the evidence establishes.

Never automatically replay input after timeout or disconnect. Reconnect through
status/workspace. When an uncertain-input latch is reported, use
`desktop_reconcile` with `acknowledge_uncertain=true`, then inspect fresh state.
Reconciliation does not prove the previous action succeeded.

Stop the session when finished; attached apps must remain open. Do not start or
enable relays or change services as part of an ordinary desktop task unless
requested. Do not expose clipboard/typed contents in logs or provider state.

## Testing this checkout

Use synthetic/disposable documents and the owner-watched acceptance checklist
in `docs/40-live-workspace-and-profiles.md`. The October 25 offline checks do not
establish live acceptance. Report app/window, actions, observed outcomes and
cleanup separately. Keep experiments small; no paid reader/OCR/Jev calls are
needed for the basic test.
