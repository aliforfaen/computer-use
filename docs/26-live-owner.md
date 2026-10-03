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
- Before each live `candidates`, `observe`, `wait`, or `act` call, capture the
  currently active exact window ID and focus the task app. Keep it focused for
  AT-SPI snapshots and desktop captures, then restore and verify the captured
  ID before returning. Actions also verify their requested effect. This applies
  on success, failure and cancellation.
- Session stop snapshots current focus when the task app is not active; after
  closing the app, it confirms the task window/process are gone and restores
  the surviving focus snapshot, falling back to the original baseline when the
  task app held focus. It also restores the original accessibility flags.
- If the worker dies, recovery avoids terminating the compositor/session
  process group, uses the journaled app PID, reconnects, and independently
  retries focus and accessibility restoration. Unconfirmed cleanup stays
  failed/broken.

Four targeted `test_live_owner` tests passed, as did eleven earlier focused
live/service checks. The owner-watched Kate check passed on 2026-10-03 after
two issues were corrected: Kate detached by default, so live launch now uses
`kate --new --block <ownedpath>` to keep the tracked process attached to its
window; and live candidates were originally collected while Kate was unfocused,
so their `focused` state no longer matched the later action snapshot. Live
observation also captured the desktop while Kate was behind the restored
Codex window. Candidate, observe, wait and act calls now focus Kate for the
entire call and restore the exact prior focus afterward. The `--block` option
is documented in [Kate's upstream source](https://github.com/KDE/kate/blob/master/apps/kate/main.cpp).

The first successful workflow drafted, saved, revised and saved a note with
four verified actions. Exact output: 270 bytes at
`run/documents/handover-20591b45062d5e1e144ea06e.txt`, SHA-256
`7d8415967f06e34ffd35239a594c2a57047a3e65c79a605fe703864f4c40b594`.
A follow-up session returned a screenshot with Kate text and the Save control
visible in the 1318×810 capture (capture ID
`f69e3b4f2c0149208150788bc2088758`, SHA-256
`94d07b2eda288fdc2392e81b72dba2dcf1300f81fd3cf708328913acb3814142`). For
both sessions, independent post-checks confirmed exact original Codex focus,
AT-SPI flags restored to `false/false`, and zero Kate windows after cleanup.
Evidence: `run/live-kate-2026-10-03/`.
The successful document session took about 75 s elapsed (16.916 s in owner
calls); the screenshot follow-up took about 26 s elapsed (10.997 s in owner
calls), 27.915 s combined owner-call time. The coordinator independently
confirmed saved bytes exactly matched the intended file. See the [report](../run/live-kate-2026-10-03/report.md)
and [call timeline](../run/live-kate-2026-10-03/timeline.md).

This validates an owner-watched local Kate task and screenshot behavior. It
does not validate Firefox, forced cancellation/crash recovery, tailnet access,
physical-input idle detection or general planning.

The prior fixed KCalc smoke test remains separate evidence ([doc 22](22-live-desktop-smoke.md));
it did not validate general live tasks or physical-input idle detection.
