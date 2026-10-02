# 26 — Live desktop capability and owner-watched check

## Capability findings — 2026-10-02

Read-only checks on `cachy` report KWin **6.7.5** and the checkout's pinned
`kwin-mcp` **0.10.0** (`.venv`). The live driver connects to the existing KWin
D-Bus and Wayland socket. The driver can report KWin EIS or fall back to
ydotool; the owner currently fails closed unless KWin EIS is available. The
existing bounded KCalc probe used EIS and verified a physical effect.

`org.freedesktop.ScreenSaver` exposes `GetSessionIdleTime()` in introspection,
but calling it on this Wayland session returns `NotSupported`. KF6IdleTime
headers and a runtime library are installed; the header describes `idleTime()`
in milliseconds, but the runtime query could not be built because Qt/KF6
development metadata is unavailable. Its behavior on this host, and whether
any candidate excludes EIS or other synthetic input, are unverified. The
service must report physical-input detection as unavailable; it must not
claim an idle threshold is enforced. Live start therefore requires an
explicit owner-present override.

For focus, kwin-mcp 0.10.0 activates by a substring match over the window's
resource class and caption. The existing probe's safe restore resolves the
captured KWin window ID in a fresh inventory, requires a unique app+caption
match, activates that match, then accepts success only after reading back the
same exact KWin ID. The live owner uses this contract for task focus and
restoration; an activation response alone is not verification.

## Implemented local contract

- Live is explicit per session and requires both `owner_present_override` and
  `temporary_a11y`. The service records physical-input detection as unavailable.
- A session starts one newly launched allowlisted app. It refuses a pre-existing
  window for that selected app and verifies the new window's PID against the
  tracked launch. A session owns only that app.
- Before each action, capture the currently active exact window ID; refocus the
  task app, run and verify the action, then restore and verify that captured ID
  before returning. This applies on success, failure and cancellation.
- Session stop snapshots current focus when the task app is not active; after
  closing the app, it confirms the task window/process are gone and restores
  the surviving focus snapshot, falling back to the original baseline when the
  task app held focus. It also restores the original accessibility flags.
- If the worker dies, recovery avoids terminating the compositor/session
  process group, uses the journaled app PID, reconnects, and independently
  retries focus and accessibility restoration. Unconfirmed cleanup stays
  failed/broken.

Four targeted `test_live_owner` tests passed, as did eleven earlier focused
live/service checks. These were code-level checks only: no live app or physical
input was used. The owner-watched host workflow remains pending.

## Owner-watched acceptance — pending

Implementation and independent review are complete. No owner-watched task or
additional physical input has been run for this phase. The next check is a
short visible Firefox-to-Kate workflow on the physical monitor, using separate
sequential sessions because each session owns one app. The selected app must
not already have a window open. Before starting, show that live mode needs the
explicit owner-present override and temporary AT-SPI opt-in and that
physical-input detection is unavailable.

Acceptance observations:

- Capture the baseline focused KWin window ID; refuse ambiguous or pre-existing
  windows for the selected app; start only that task-owned allowlisted app.
- Before each input action, snapshot current focus. Verify the requested action
  through fresh accessibility state or saved file bytes, then restore that
  per-call exact focus before returning.
- Stop on any focus mismatch, failed verification, cancellation, or uncertain
  ownership. Cleanup only task-owned apps and confirm their windows/processes
  are gone; restore original focus again after cleanup.
- After success and after a forced safe failure/cancellation, independently
  confirm per-call focus, baseline focus, original accessibility flags and app
  cleanup. Keep the visible workflow owner-watched; this plan itself does not
  authorize its input.

The prior fixed KCalc smoke test remains separate evidence ([doc 22](22-live-desktop-smoke.md));
it did not validate general live tasks or physical-input idle detection.
