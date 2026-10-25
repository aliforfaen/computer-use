# Tray confirmation policy

> **Current follow-up (2026-10-25):** see [doc 40](40-live-workspace-and-profiles.md)
> and [HANDOFF](HANDOFF.md). The dated implementation/evidence below predates
> live attachment, foreground-between-calls policy, opt-in semantics and
> capture-grounded input. Historical statements are not the current contract.

The tray now treats routine cleanup and the stop-all/shutdown confirmation as
optional prompts, both off by default. Cleanup results remain visible in the
tray's session status, including whether cleanup was confirmed. Either prompt
can be re-enabled in Settings.

Tray-started live tasks still ask for confirmation by default. Settings has an
explicit persistent choice to stop asking for that prompt. Turning it off
records the owner's tray policy to supply the required owner-present override
and allow temporary accessibility for tray-started live tasks. Physical
presence detection is unavailable; this setting does not detect or prove that
the owner is present. The tray still sends
`owner_present_override=true` and `temporary_a11y=true` on every such start, so
the service's required live gates remain in force. The saved choice applies
only to this local tray path; CLI, MCP, and remote callers still need to supply
both fields themselves. Virtual mode remains the default.

The tray settings file remains `~/.config/jev-desktop/tray.json` with mode
0600. Older settings files load the new live prompt as enabled; missing
cleanup and kill-switch prompt settings use the new disabled defaults.

The confirmation-policy change is confined to the tray and its settings. It
does not add a daemon consent token, authorize remote live calls, alter the app
allowlist, or change focus restoration, audit, or cleanup behavior.

## Live AT-SPI status properties

Live accessibility setup now requires `org.a11y.Status.IsEnabled` and treats
`ScreenReaderEnabled` as optional when the current AT-SPI service does not
expose it. The worker changes only the properties it captured, verifies that
the property-key set is unchanged before and after the update, and restores
the captured values during cleanup. Journal recovery accepts the one-key or
two-key captured shape and uses the same availability check. This is a source
compatibility change for AT-SPI 2.62; no live host operation or test was run
for this change.
