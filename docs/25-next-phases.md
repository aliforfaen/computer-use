# 25 — Next phases

## Small local follow-ups

1. Fix scroll effect grounding with one concrete before/after example.
2. Diagnose the wait reader's invalid enum response with one retained, bounded
   example when useful. Keep ordinary screenshots as the default meanwhile.
3. Use targeted checks; avoid another broad test matrix for this personal tool.

## Live support — implement before asking the owner to watch

Reuse kwin-mcp's live connection and the existing physical probe. Add explicit
per-task live mode to the owner, one task at a time. Save and restore the exact
original focused window on success, failure and cancellation. Track only apps
opened by the task and close them. Measure available KDE/Wayland physical-input
activity first; define the idle threshold and explicit owner-present override
from actual capabilities. Report unsupported detection honestly.

## Owner-watched phase — last

Once those contracts work, notify the owner that a short visible Firefox/Kate
workflow is ready. The owner watches the physical monitor while the agent reads,
acts, saves and closes its app; check original focus and cleanup. No visible
input is authorized merely by this plan. The earlier fixed KCalc smoke test
remains the only completed live-input check.

## Afterward

Tailnet transport needs a real remote peer and explicit approval to change
Serve routes. Reuse the official MCP transport, bind loopback and preserve
existing routes. General server-side planning and Jev workflow replay follow
useful, reliable primitives; ADR-011 remains unresolved.
