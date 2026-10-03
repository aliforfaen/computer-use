# 25 — Next phases

Updated 2026-10-03 after the scroll fix, owner surfaces, test-suite shrink and
the Jev selector seam. See [the handoff](HANDOFF.md) for current commands.

## Done 2026-10-03

1. Scroll effect grounding: fixed and host-verified in both directions with a
   reusable offline fixture. [doc 27](27-scroll-effect-verification.md).
2. Owner surfaces: JSON config with CLI/env precedence, `systemd --user` unit
   and `service install|status|uninstall`, AppIndicator tray with a settings
   file and optional tray unit. Live mode stays behind a per-task confirmation.
   [doc 28](28-tray-and-user-service.md).
3. Durable audit with size/age rotation and session-journal pruning (ADR-021).
4. Testing pit closed: 190 → 177 cases, `tools.check` core set, and a
   change-to-check runbook. [doc 29](29-verification-runbook.md).
5. Jev selector seam implemented offline-first and left unwired, with an
   experiment plan. [doc 30](30-jev-selector-experiment.md).

## Small local follow-ups

1. Owner: enable the user unit at login (optionally with `--tray`) and click
   through the tray menu and Settings dialog once, watched. Neither was done by
   this session; SNI registration and logic were verified instead.
2. Optional: one bounded look at the reader wait's `invalid_judgment`, or leave
   it experimental. Polling is the documented default either way.
3. Optional watched live checks (Firefox, cancellation/crash). Not important
   enough to block anything; the vision path is already demonstrated.
4. Jev: run Stage 0 (free) then Stage 1 (one call) from doc 30 and decide
   whether Stage 2 is worth it.
5. Later, only if a task needs it: nested/horizontal scroll regions.

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
