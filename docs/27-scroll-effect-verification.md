# 27 — Scroll effect verification

**2026-10-03.** Fixes the scroll item that had failed host effect verification
since [doc 24](24-primary-wait-comparison.md). Supersedes the "scrolling is
unresolved" note in [doc 16](16-local-owner-and-mcp.md) and ADR-017.

## Root cause

Firefox exposes **two** AT-SPI `scroll pane` candidates with the same role,
empty label and identical bounds: the live pane and a hidden one
(`showing` absent, `target_disabled_or_hidden`). The scroll verifier resolved
its before/after viewports by `(role, label)` and required exactly one match, so
the semantic fallback never ran. The verifier returned
`measurement: unavailable` and every scroll failed as `verification_failed`.

The precondition had passed (`_scroll_witnesses` was non-empty), so the input
was attempted and only the effect check failed. The failure audit also dropped
the verifier evidence, which is why the earlier diagnosis had no reason.

## Fix

- `transactions._scroll_viewport(candidates, target)` resolves the live
  viewport: an exact fingerprint match first (states, actions, bounds, values),
  otherwise the unique *usable* candidate. `desktop_service._verifier` uses it
  for both the before and after viewport instead of a `(role, label)` lookup.
- `TransactionEngine.last_verification_evidence` retains the bounded verifier
  evidence, and a failed `act` now records it in the audit `verification` field.

`_scroll_witnesses` still requires in-viewport `link`/`button` elements whose
unique labels move in one direction, so the measurement stays conservative.

## Host evidence — 2026-10-03

Probe: `uv run python -m tools.scroll_effect_probe` (virtual Firefox, no provider
calls, self-cleaning). It serves new fixture `benchmark_fixtures/scroll.html`
over loopback so the case is offline and repeatable, navigates to it, then
scrolls down 2 steps and up 2 steps. Latest report: `run/scroll-2026-10-03/`.

```
focus_address   ok
navigate_url    ok   address_bar_destination_verified
scroll_down     ok   {"measurement": "semantic_content_bounds",
                      "scroll_position_changed_in_requested_direction": true,
                      "verification_reads": 1}
scroll_up       ok   {"measurement": "semantic_content_bounds", ...}
cleanup         ok   confirmed
```

Before/after witness measurement for the down scroll: 39 → 45 in-viewport
witnesses, 31 common, **10 negative y deltas, 0 opposing, 21 zero** (fixed
browser chrome). The zero deltas are Firefox toolbar buttons and are counted
neither for nor against the required `agreeing >= 2 and opposing == 0`.

Offline checks: `tests.test_desktop_service` and `tests.test_transactions`
(55 cases) cover the existing scroll tests plus two new ones — the
duplicate-hidden-pane regression and the failed-verification audit evidence.
`tests.test_wait_benchmark` also had its fixture path corrected to the repo
root, a pre-existing breakage from the tests/ move.

## Not claimed

This validates one fixture page, one window size and two scroll steps in a
virtual session. Nested scroll regions, horizontal scroll, scrollbar-drag
grounding and live-desktop scrolling stay unvalidated.
