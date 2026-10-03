# 25 — Next phases

Updated 2026-10-03 after the scroll fix and the tray/user-service work.
See [the handoff](HANDOFF.md) for the current working surfaces and commands.

## Done 2026-10-03

1. Scroll effect grounding: fixed and host-verified in both directions with a
   reusable offline fixture. [doc 27](27-scroll-effect-verification.md).
2. Owner surfaces: JSON config with CLI/env precedence, `systemd --user` unit
   and `service install|status|uninstall`, and an AppIndicator tray whose live
   mode stays behind a per-task confirmation. [doc 28](28-tray-and-user-service.md).

## Small local follow-ups

1. Diagnose the wait reader's invalid enum response with one retained, bounded
   example when useful. Keep ordinary screenshots as the default meanwhile.
2. Use targeted checks; avoid another broad test matrix for this personal tool.
3. Optional: enable the user unit at login and click through the tray menu with
the owner watching; neither was done by this session.

## Local live support — Kate host check passed

The local owner and MCP facade support explicit live sessions, one task-owned
allowlisted app per session, PID/window ownership checks, per-action focus
snapshot and exact restoration, cleanup and journal-based failure recovery.
Live start requires both `owner_present_override` and `temporary_a11y`; the
owner restores the original accessibility flags at cleanup. The read-only
`GetSessionIdleTime()` call is unsupported on this Wayland host, so there is no
physical-input gate and no idle threshold claim. Four targeted owner tests and
eleven earlier focused service/live tests passed; none launched a live app or
sent input. See [capability findings and the watched acceptance plan](26-live-owner.md).

## Owner-watched phase — Kate completed 2026-10-03

Visible Kate draft/save/revise/save and corrected app screenshot capture passed.
Independent checks confirmed exact focus, accessibility settings and app cleanup.
See [doc 26](26-live-owner.md). Firefox and physical cancellation/crash checks
remain optional follow-ups; keep them small and owner-watched. Use one app per
session and ensure the selected app has no existing window before starting.

## Afterward

Tailnet transport needs a real remote peer and explicit approval to change
Serve routes. Reuse the official MCP transport, bind loopback and preserve
existing routes. General server-side planning and Jev workflow replay follow
useful, reliable primitives; ADR-011 remains unresolved.
