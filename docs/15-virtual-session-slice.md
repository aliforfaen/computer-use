# 15 — Observation and verified virtual transactions

**2026-10-01 · M1–M2 implementation.** This is an importable core plus a
fixed disposable integration harness. It is not the general daemon, MCP
surface, primary planner or heartbeat. Reuse `kwin-mcp==0.10.0` for KWin,
AT-SPI, screenshots and EIS; Pillow for image mapping; httpx for the reader.

## Run

```bash
# No model calls. Three disposable virtual app cases.
uv run --with kwin-mcp==0.10.0 --with Pillow --with httpx python virtual_tasks.py

# All focused validation, using synthetic data and mocked HTTP.
uv run --with Pillow --with httpx python -m unittest discover -v
```

The harness types two short edits in Kate, verifies exact accessible text after
each, clicks a local Firefox fixture button and verifies its changed text,
refuses a disabled control, then observes KCalc without input. Each session
has isolated home/bus and guaranteed cleanup attempts. Results and append-only
audit live under ignored `run/virtual-tasks/`. No provider call is required.

## Core boundaries

- `observation.py`: explicit app allowlist, mapped capture, in-memory capture
  store with TTL/count/byte bounds, and metadata/image/data/both modes. Images
  and reader data refer to the exact same hash-checked bytes. Metadata excludes
  window captions. Full and crop scopes require explicit selection.
- `vision_reader.py`: configurable DeepSeek/MiMo/generic compatible transport,
  bounded output, strict questions/response shape, provider errors and explicit
  nullable uncertainty. API keys are loaded from the configured environment
  variable; constructing the reader or requesting metadata makes no call.
- `transactions.py`: generation-scoped candidate references, fresh target and
  window/focus checks, policy/authorizer seam, session serialization, budgets,
  pre-input audit, input and read-after-action verification. No model planner.
- `virtual_tasks.py`: fixed synthetic task harness. It is not the supported
  `jev-desktop` CLI, which will be a thin client of the later single owner.

The pinned driver does not expose a semantic Action invocation method. The
wrapper uses mapped, fresh AT-SPI bounds for input; a model never supplies
coordinates. Text typing requires a focused accessible editable control.
Unmapped, ambiguous, disabled, hidden or changed targets are refused.

Postconditions are code-owned verifier callbacks. Their correctness remains
part of each task adapter: arbitrary screenshot changes do not prove success.
Failure after input blocks the session until explicitly reset and reobserved.
No Jev request is made in this slice; the selector remains optional.

## Limitations and next handoff

Only complete 1:1 virtual screenshot mapping is supported. Scaling,
multi-monitor geometry, live focus restoration and physical-input idle checks
remain later platform work. Semantic targeting requires useful AT-SPI and
unique identities; visual grounding remains open under ADR-011. Readable
accessible text is capped by the pinned driver's extraction limit; do not use
this harness to claim full-document verification.

The next service package reuses these contracts and adds one owner process,
thin CLI/MCP clients, cancellation/stop-all and driver failure handling. No
Serve route or live desktop configuration is changed by this implementation.
See [doc 14](14-app-build-plan.md) for remaining gates and worker assignments.

## Executed validation

Coordinator host run **2026-10-01, 16:32 UTC** passed all three app cases,
including both exact Kate edits and disabled-button refusal. Four executed
transactions (focus, two edits, webpage click), one refused disabled attempt,
and seven app captures. All sessions, the Kate temp document and Firefox
profile were removed. Zero paid calls. Runtime artifacts:
`run/virtual-tasks-reviewed/summary.json` and `actions.jsonl`.

Measured versions: KWin 6.7.5, kwin-mcp 0.10.0, AT-SPI 2.60.7,
Kate/KCalc 26.08.1 and Firefox 157.0. Driver `Broken pipe` warnings appeared
at teardown despite confirmed successful cleanup, as in the earlier benchmark.
The full suite has **39 passing tests**, including original benchmark/P2
checks and new observation, transport and transaction failure cases. Independent
Luna review found a cancellation-during-verification gap; it was fixed and
regression-tested before the final host run. A first integration attempt while
worker files were changing failed with TypeError; the stable and reviewed runs
passed. This is scripted task execution, not autonomous planning.

Cancellation is cooperative between synchronous driver calls and around the
verifier; it cannot interrupt an in-flight driver subprocess or a blocking
verifier callback. Task adapters must bound their callbacks. Keyword-based
confirmation is a narrow fixture policy, not a complete app-effect classifier;
expand code-owned action policies before enabling arbitrary app workflows.
