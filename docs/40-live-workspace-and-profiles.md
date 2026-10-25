# Live workspace and application profiles

Updated 2026-10-25. This documents the current source contract for configured
applications and live desktop sessions. The recent live path has not yet had a
watched host acceptance run; the checklist below is still pending.

## Application profiles and permission

Profiles describe how to identify and launch an application. They do not grant
permission to use it. The explicit app allowlist remains the authorization
boundary in every mode, including `yolo`.

Custom profiles are read from
`${XDG_CONFIG_HOME:-~/.config}/jev-desktop/app_profiles.json`. The file has a
`profiles` array. For example:

```json
{
  "profiles": [
    {
      "id": "dolphin",
      "command": ["dolphin"],
      "window_app_ids": ["dolphin"],
      "atspi_app": "dolphin"
    }
  ]
}
```

`command` is an argv array (never a shell command); `window_app_ids` are the
window identities accepted for this profile; `atspi_app` selects the semantic
application; and optional `aliases` map caller names to the profile. The
runtime loads built-ins plus validated custom entries. A custom profile must
also be added explicitly to the service allowlist before it can be used. Check
the actual window and AT-SPI identities on the target desktop when configuring
a profile; the example values are not host-verified. See
[the profile implementation](../src/jevdesktop/app_profiles.py).

## Live session behavior

Live sessions require the explicit owner-present override because physical
input activity detection is unavailable. They may launch a configured app or
attach to one exact existing window ID. Attach checks the selected window's app
identity against the profile. It does not choose a window by title or switch to
another matching app window.

The task app stays in the foreground while a live session is active. Session
operations re-check and focus the task window when needed; the originally
active window is restored when the session stops. Stopping a launched session
closes only the task-owned process/window. Stopping an attached session leaves
the application open. Cleanup also attempts to restore temporary accessibility
flags. These are source-level guarantees awaiting watched acceptance.

Task-related transient/dialog windows may join the selected task when KWin
reports transient ancestry with an accepted app identity, or the window belongs
to the selected process and is marked dialog, popup, or transient. An unrelated
popup is reported separately and is never adopted as the task target. If such a
window is active, operations may report an interruption instead of switching
through it. This classification depends on KWin's window metadata and needs
acceptance with real application dialogs.

AT-SPI semantic candidates are off by default for live sessions. Opting in
requires `semantic_candidates=true` and `temporary_a11y=true`; the service
records/restores accessibility state. Screenshot observation and image-grounded
input do not require semantic candidates. Virtual sessions keep semantic
candidates enabled by default.

## Image-grounded input and uncertainty

Image input must name a fresh capture returned for the current session. The
service checks capture age, selected window identity, unchanged geometry, and
that each supplied image point falls inside both the image and selected window.
The caller supplies any image-relative point based on that fresh observation;
Jev output is never accepted as coordinates. Image and semantic capture are
sequential, not atomic. Current coordinate mapping accepts only complete 1:1
logical-screen coverage; scaled and multi-monitor layouts are rejected when
mapping cannot be proven. See [grounding details](39-grounding.md).

After sending input, the service captures fresh state. A result can report that
input was delivered while leaving its effect explicitly uncertain; this is not
proof that the intended task state changed. If delivery or the follow-up capture
fails, the session is latched uncertain. Call reconcile to inspect fresh state
and explicitly acknowledge uncertainty. Reconcile never replays the input.
Strong code-owned verifiers remain available for actions that have them.

## Relay status

`desktop_relays` reports configured SSH forward and local process/listener
status. A live SSH process or reachable local TCP listener does not prove the
ultimate MCP endpoint is healthy; endpoint and MCP health remain
`not_checked`. See [SSH forward status](38-ssh-forwards.md).

## Watched acceptance checklist

Run only with the owner watching the desktop and explicitly authorizing the
live session. Record the KWin/kwin-mcp versions and evidence for each result.

- Add a custom profile and separately allowlist its canonical app ID; verify
  that profile presence alone does not authorize a session.
- Attach by exact window ID when two same-app windows are open; verify only the
  selected window is used and that stopping preserves both windows.
- Launch a task-owned app, exercise one harmless action, then stop; verify only
  the launched app closes and the original focus is restored at stop.
- Open a task-owned dialog and an unrelated popup; verify the dialog is treated
  as related, the popup remains external, and it is not acted on.
- Start with semantic candidates disabled and verify no AT-SPI inventory is
  returned. Repeat with explicit semantic opt-in and temporary accessibility;
  then verify the original accessibility properties are restored.
- Capture, perform one harmless fresh-image input, inspect the returned state,
  and verify uncertainty is explicit. Force a safe capture/reconnect failure,
  reconcile, and confirm no input is replayed.
- Inspect relay status with the endpoint unavailable; verify process/listener
  status is not presented as MCP endpoint health.
- Verify unsupported scaled or multi-monitor mapping is refused instead of
  producing a coordinate transform.

### Capture eligibility

Only an image explicitly returned by `desktop_observe` (`output=image` or
`both`) can authorize coordinate input. Metadata and reader-only outputs do
not arm input. Post-input and reconciliation captures return metadata; request
their current capture ID with image output before the next input. Any input
attempt invalidates older capture eligibility. Observing an older stored image
does not rearm it after an intervening input.

Workspace metadata uses the documented [KWin scripting API](https://develop.kde.org/docs/plasma/kwin/api/)
for current desktop/activity, window membership, output, stacking order and
transient relationships. Optional unavailable fields are null/empty. These
metadata paths still need the planned host acceptance.
