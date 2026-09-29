# 08 — Executed KCalc proof

**2026-09-29 · one virtual-session action.** The local `p2_kcalc.py` proof ran
with `kwin-mcp==0.10.0`, KWin 6.7.5, and the direct TypeSafe
`jev-1.13.0` endpoint. The initial KCalc editable display was blank. Jev
selected `press` and the enumerated `button.One`; code checked the answer,
re-read the target and display, then used one KWin EIS click in the isolated
session. A subsequent AT-SPI read returned display text `1`. The script
reported `P2 proof passed: virtual KCalc display verified empty → 1` and
stopped the session. No virtual KWin process remained.

Five focused parser, selector-policy, and verifier tests passed. The proof is
restricted to this fixed task and app. It uses `AutomationEngine` directly;
`kwin-mcp` 0.10.0 exposes AT-SPI observations as formatted text and has no
semantic button-press API, so the proof parses that text and clicks validated
fresh bounds. That text format is a driver compatibility risk for subsequent
work. The result establishes a single verified action, not a general task
loop or remote service.
