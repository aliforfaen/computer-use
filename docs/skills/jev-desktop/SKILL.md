---
name: jev-desktop
description: Use the jev-desktop MCP server for desktop observation and control on CachyOS/KDE Wayland, including live window attachment, screenshot-grounded input and MCP relay monitoring. Use this when testing or operating the Jev desktop layer; Orca's own computer-use driver is a separate surface.
---

# Jev desktop

Use the `jev-desktop` MCP server (`desktop_*` tools; Pi may prefix names).
Jev model calls are deferred and are not needed to use this desktop service.

## Connect, choose scope and select the target

Read `desktop_capabilities` and `desktop_status`. Access scope is separate from
supervised/guarded/yolo approval and from cleanup ownership. Check the owner's
`allowed_access_scopes`; do not infer full access from autonomy mode alone.

For an authorized task spanning the human desktop, request
`desktop_session_start` with `access_scope="full_live_desktop"` and
`owner_present_override=true`. No `app`, app profile or per-app allowlist change
is needed. Use `desktop_workspace` and `desktop_select_window(window_id)` to
switch explicitly between fresh observed window IDs inside the same session.
Do not repeatedly ask the user to add apps once full-desktop scope is enabled
and the task is authorized. Supplying an `app` name alone is not a window switch.

For isolated broad desktop work, request `access_scope="full_virtual_desktop"`.
Its workspace is the isolated desktop, never the human host. Launch a configured
profile using `desktop_launch_app(profile_id)`, then select/observe its window.
Profiles describe launch argv; they do not restrict which windows can be used
inside a full-desktop scope. No arbitrary shell command API is added.

For deliberately narrow work, use `access_scope="selected_application"` (the
backward-compatible default). This requires the configured app and app
allowlist. Attaching uses its canonical `app`, `desktop_mode="live"`,
`attach=true` and exact `window_id`. Only this scope requires per-app access.

Live work needs the user's authorization; physical-input detection is
unavailable. Full live access preserves preexisting apps at stop. An isolated
virtual environment belongs to its task. Neither scope nor window selection
turns a preexisting live app into a task-owned process.

`desktop_workspace` reports window/desktop context; use `desktop_observe` with
`scope="desktop", output="image"` for a full-scope desktop overview without
requiring a selected app. Overview captures are observational: select a real
window and obtain a fresh image before input.

`desktop_relays` reports local app MCP routes. Prefer a suitable app MCP when
available; a running SSH process or TCP listener does not prove MCP readiness.
The selected task window/dialog is foregrounded for work; original focus is
restored at stop. Related dialogs may follow it. Other popups remain context
until explicitly selected in a full scope.

A window switch invalidates all earlier semantic/image refs. Always observe
after selection. A switch does not acknowledge or replay uncertain prior input.

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
suppresses prompts but retains the chosen scope, ownership, budget and audit restrictions.

## Optional semantic access and uncertain results

Full virtual desktop sessions currently support screenshots only; omit semantic
opt-in there. For a live semantic run, opt in at session start with `semantic_candidates=true`
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
