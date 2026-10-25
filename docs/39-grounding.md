# 39 — Agent grounding

> **Current follow-up (2026-10-25):** see [doc 40](40-live-workspace-and-profiles.md)
> and [HANDOFF](HANDOFF.md). The dated implementation/evidence below predates
> live attachment, foreground-between-calls policy, opt-in semantics and
> capture-grounded input. Historical statements are not the current contract.

2026-10-24. Reuses kwin-mcp AT-SPI geometry and the existing ObservationAdapter.
No OCR, provider call or coordinate-input API is added.

## Semantic inventory

`desktop_candidates` now returns window identity/client bounds, generation,
observation timestamp and candidate screen-logical bounds. Refs are short
generation-scoped identifiers; re-enumerating invalidates old refs. Names identify
controls. Editable values remain omitted; names containing the known current
nonempty editable value are suppressed, as are protected-field names. This is
not a general secret detector for arbitrary accessible names. Names/bounds are
returned to the caller, not added to audit or Jev requests.

## Image pairing

Request `desktop_observe` with `include_candidates: true` (CLI:
`observe kate --output image --include-candidates`). This brackets a new capture
with semantic observations and returns `observation.grounding` alongside it.
It costs three observation slots in total. Retained capture IDs cannot be paired
with new refs. Existing capture mapping supports complete 1:1 logical sources.

When window identity and client geometry remain stable, each candidate receives
`image_bounds`, `inside_image`, and a shared translation from screen coordinates
to the selected image rectangle. A moved/changed window disables image mapping.
Capture and semantics are sequential, not atomic: animated or changing content
can still disagree. No actionable OCR target is inferred from an image.

Successful actions return fresh `grounding` inline. Reuse those refs or enumerate
again; do not reuse the pre-action inventory. No numbered overlay is generated.

## Limits and review

This does not enlarge the launch registry beyond Kate, Firefox and KCalc, or
remove ambiguous-target refusal. Newly exposed native control roles require an
advertised AT-SPI action; existing buttons/links retain their previous behavior.
Negative AT-SPI screen origins now parse, but capture mapping still rejects
unsupported multi-monitor/scaled layouts rather than claiming correct mapping.

Implementation was reviewed in source with syntax/whitespace checks only. No
new host or regression-suite result is claimed. Refresh MCP schemas/restart the
owner intentionally before use; no running owner was restarted by this work.
