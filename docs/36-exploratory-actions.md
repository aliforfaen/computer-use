# Exploratory action outcomes

> **Current follow-up (2026-10-25):** see [doc 40](40-live-workspace-and-profiles.md)
> and [HANDOFF](HANDOFF.md). The dated implementation/evidence below predates
> live attachment, foreground-between-calls policy, opt-in semantics and
> capture-grounded input. Historical statements are not the current contract.

Updated 2026-10-24. This contract separates delivery of an allowed input from
proof that the app produced the requested effect.

## Request

`act` accepts `verification: "observed"` when the caller has no strong
code-owned postcondition. `expected` must be omitted. Existing verifier names
continue to require their existing app, action, target, and precondition checks.

This mode does not weaken policy: the app allowlist, action budget, autonomy
approval rules, target-generation freshness, unique identity, visible/enabled
state, target window identity, active focus, pre-input re-resolution, and audit
remain in force. For scroll, lack of a measurable scroll position does not
block delivery in `observed` mode.

## Outcome

After the backend input call returns, the transaction takes a fresh snapshot
and stores it as the current inventory. The result reports `status: "observed"`,
`verification: "unavailable"`, `verified: false`, and
`reason: "effect_uncertain"`. The observation confirms only that the input call
returned and a post-action snapshot was collected; it does not prove the
requested effect. Strong verifiers retain their existing pass/fail behavior.

No action is retried automatically. If input delivery raises or the fresh
post-action snapshot cannot be collected, the result remains a failure/uncertain
input path and the session stays latched for recovery; the service does not
claim an observed completion. A later action requires a new caller decision
and fresh target inventory. Successful actions include their fresh post-action
grounding in the response, so the caller can inspect the current candidate set
without another observation call.

## Scope

This is an outcome-reporting change to the existing bounded transaction path.
It adds a bounded `keyboard_shortcut` action for a fixed set of named key
combinations. The target must be a fresh, uniquely resolved control that is
focused both before and immediately before input; the request requires observed
outcome mode, and no arbitrary key names, shell commands, or executable text are
accepted. The fixed key set is `ctrl+a`, `ctrl+s`, `ctrl+z`, `ctrl+shift+z`, `ctrl+f`,
`ctrl+tab`, `alt+left`, `alt+right`, `Escape`, `Tab`, `shift+Tab`, and
`Return`. Candidate references
now use a short generation prefix plus an inventory index, and AT-SPI screen
bounds accept negative origins for multi-monitor layouts. Existing button/link
click support remains; added menu, checkbox, radio, tab, tree, and list roles
require advertised click, press, activate, select, or toggle actions. Duplicate
accessible identities remain ambiguous and unaddressable. Generic `visible_text`
and `window_title` verifiers can now be used on other supported apps where the
action and target roles satisfy their existing policy checks.
