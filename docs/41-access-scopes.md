# 41 — Desktop access scopes

2026-10-26. Owner-authorized replacement for treating every desktop task as a
single allowlisted application. Reuses the existing kwin-mcp sessions, capture
store, input engine and ownership journal. Host acceptance is still pending.

## Three independent decisions

| Setting | Meaning |
| --- | --- |
| Access scope | Which application or desktop the session may reach |
| Autonomy mode | Which actions require approval: supervised, guarded or yolo |
| Ownership | Which processes/windows cleanup may terminate |

`yolo` does not grant a broader scope. Full-desktop scope does not prove action
effects or make an attached live app task-owned.

## Scopes

- `selected_application`: previous behavior. An explicit configured app profile
  and app allowlist entry are required. Related dialogs can be followed; other
  applications are not implicitly selected. This remains the default.
- `full_virtual_desktop`: access to the task's isolated virtual desktop. Window
  inventory/selection must stay on that session's bus. The app allowlist does
  not restrict transitions inside it. The entire isolated environment belongs
  to the task and is torn down at stop.
- `full_live_desktop`: access to the human desktop, with explicit window
  selection across applications and no per-app allowlist/profile prerequisite.
  An existing window's identity is read from the desktop rather than invented
  from an application name. User apps remain open at cleanup.

The owner enables acceptable scopes in `allowed_access_scopes` in the private
daemon config. Its default permits only `selected_application`. A session must
request an enabled scope explicitly. Full live mode still requires the owner-
present override; physical-input detection remains unavailable.

Example owner setting:

```json
{
  "allowed_access_scopes": [
    "selected_application",
    "full_virtual_desktop",
    "full_live_desktop"
  ]
}
```

Preserve other configuration fields when adding this setting. This is a
startup setting: reload the owner intentionally while no session is active.
Do not confuse a missing owner-enabled scope with a missing application profile.

## Window transitions

Within a full-desktop session, call `desktop_workspace`, explicitly select the
desired exact ID with `desktop_select_window(window_id)`, obtain a fresh image,
then act. CLI equivalents are `workspace` and `select-window WINDOW_ID`. A window switch invalidates
semantic snapshots, stored captures and both pending and armed image references.
Supplying an `app` argument alone must not redirect input or escalate scope.

Related dialogs can follow the selected window. Other applications and popups
are context until explicitly selected. Changing target never changes ownership
of a live app. Task focus is kept for work; original focus is restored at stop.

`desktop_launch_app(profile_id)` / `launch-app PROFILE_ID` launches a configured
profile inside a full virtual session. Profiles still provide owner-configured
argv for launching an app. They are
launch metadata, not per-app permission gates inside a full-desktop session.
There is no new free-form shell command API.

## Input and observation

`desktop_observe(scope="desktop", output="image")` provides an explicit full-
scope overview even with no selected application. This is an observational
capture, not an input target. Select a real window and obtain a new image before
acting. Full virtual desktop sessions currently support screenshots only. Full live
sessions support optional semantic candidates with temporary accessibility authorization. Coordinates
are caller-chosen against a recent image explicitly returned by the service,
never Jev output. Guarded/supervised approval, budgets, authorizer and audit
remain independent of access scope. Only fresh selected-window input is
accepted; a desktop overview is not permission to reuse stale window refs.

An observed result has uncertain effect. A failed input is never automatically
replayed. Reconcile and obtain fresh state before continuing after uncertainty.
Capture mapping retains its existing supported-layout restrictions.

## Watched acceptance

1. Confirm selected-application scope still denies an unallowlisted app.
2. Start full live scope, discover two apps, switch explicitly and observe each.
   Verify no profile/app allowlist edits are needed, and old refs fail after
   switching. Stop and confirm all existing apps remain open.
3. Start full virtual scope, launch configured apps inside it and switch among
   their windows. Confirm its inventory/captures never come from the host.
4. Verify a scope/desktop mismatch is refused, and autonomy mode never grants
   a disabled scope. Check status/capabilities/audit report the actual scope.
5. Exercise related-dialog following, uncertain-input recovery and stop
   restoration separately from ordinary task completion.

Offline tests establish code contracts only. No host session or input is
implied by this document. See [doc 40](40-live-workspace-and-profiles.md) for
window/input details and [HANDOFF](HANDOFF.md) for current check results.
