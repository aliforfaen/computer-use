# 25 — Next phases

## Small local follow-ups

1. Fix scroll effect grounding with one concrete before/after example.
2. Diagnose the wait reader's invalid enum response with one retained, bounded
   example when useful. Keep ordinary screenshots as the default meanwhile.
3. Use targeted checks; avoid another broad test matrix for this personal tool.

## Local live support — implemented; owner-watched validation pending

The local owner and MCP facade support explicit live sessions, one task-owned
allowlisted app per session, PID/window ownership checks, per-action focus
snapshot and exact restoration, cleanup and journal-based failure recovery.
Live start requires both `owner_present_override` and `temporary_a11y`; the
owner restores the original accessibility flags at cleanup. The read-only
`GetSessionIdleTime()` call is unsupported on this Wayland host, so there is no
physical-input gate and no idle threshold claim. Four targeted owner tests and
eleven earlier focused service/live tests passed; none launched a live app or
sent input. See [capability findings and the watched acceptance plan](26-live-owner.md).

## Owner-watched phase — last

The implementation has been reviewed; the next validation is a short visible
workflow watched by the owner. Use one app per live session, ensure no window
for the selected app is already open, and run Firefox then Kate as separate
sequential sessions. The owner watches the physical monitor while the agent
reads, acts, saves and closes each task-owned app. Check that each call restores
its own pre-call focus, then verify focus at stop and cleanup. This plan alone
does not authorize the visible input. The earlier fixed KCalc smoke test
remains the only completed live-input check for this phase.

## Afterward

Tailnet transport needs a real remote peer and explicit approval to change
Serve routes. Reuse the official MCP transport, bind loopback and preserve
existing routes. General server-side planning and Jev workflow replay follow
useful, reliable primitives; ADR-011 remains unresolved.
