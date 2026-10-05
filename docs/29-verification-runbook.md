# 29 — Verification runbook

**2026-10-03.** The cheapest check for a change, so a small edit never turns
into another test marathon. The suite is intentionally ~200 cases; most changes
need one or two modules, not discovery.

## Core rule

Run the module that owns the code you touched. Run discovery only when the
change crosses module boundaries, packaging, or the daemon/CLI/MCP contract.

```bash
uv run python -m unittest tests.test_desktop_service      # one module
uv run python -m tools.check                              # fast core set (~4 s)
uv run python -m unittest discover -s tests               # everything (~10 s)
```

`tools/check.py` runs the shipped-surface set: service, transactions, surfaces,
tray/service, documents, observation, daemon. It is the "did I break the
product" check. Reader/live/harness modules are deliberately excluded.

## Change → cheapest check

| Change | Check |
| --- | --- |
| `src/jevdesktop/desktop_service.py` dispatch, verifier, budgets | `tests.test_desktop_service` |
| `src/jevdesktop/transactions.py` policy, candidates, scroll, action flow | `tests.test_transactions` |
| `desktop_cli.py` / `desktop_mcp.py` / `desktop_daemon.py` | `tests.test_desktop_surfaces tests.test_desktop_daemon` |
| `desktop_service_unit.py` / `desktop_tray.py` | `tests.test_desktop_tray_and_service` |
| `desktop_worker.py` documents, window readiness | `tests.test_desktop_worker_documents` |
| `observation.py` capture store, crops | `tests.test_observation` |
| `vision_reader.py` stream bounds, call caps | `tests.test_vision_reader` |
| `wait_watcher.py` / `owner_wait.py` | `tests.test_wait_watcher` |
| `live_desktop_probe.py` / live focus restore | `tests.test_live_desktop_probe tests.test_live_owner` |
| `paths.py`, `scripts/`, `assets/`, `pyproject.toml` | discovery + `uv build` |
| Docs only | none; read the diff |

## The default agent loop

Use polling, not the heartbeat wait:

```
candidates -> pick a ref -> act (with a code-owned verification) -> re-read state
```

1. `candidates` for the app, choose one candidate by role/label.
2. `act` with the narrowest verification that proves the effect
   (`display_text`, `document_saved`, `navigation_url`, `scroll_changed`, ...).
3. Re-read (`candidates` or `observe`) before the next decision. `DONE` from a
   model is a proposal, not proof.
4. `observe --output image` only when a picture is genuinely needed, and only
   with an explicit reader config and per-session cap for interpreted data.

`wait` is **experimental**: both recorded trials returned `invalid_judgment`
while ordinary polling completed the same tasks (doc 24). `capabilities` now
reports `wait.status = "experimental"` with the recommended alternative.

## Host probes

A probe that launches a virtual app or spends provider money is an explicit
action, never part of the default loop:

- `uv run python -m tools.local_service_probe` — socket/service smoke.
- `uv run python -m tools.scroll_effect_probe` — offline scroll fixture.
- `uv run python -m tools.vision_wait_probe` — paid; read its caps first.

Keep each probe bounded, self-cleaning, and report the artifact path. Do not
re-run a probe to reproduce an old number; the recorded report is the evidence.
