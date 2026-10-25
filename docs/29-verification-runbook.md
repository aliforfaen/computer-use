# 29 — Verification runbook

**Updated 2026-10-25.** Choose the smallest check that covers the changed
contract. Current counts change with the checkout; do not reuse old counts as
fresh evidence. The `tools.check` module list is authoritative for the current
core set.

## Core rule

Run the module that owns the code you touched. Run discovery only when the
change crosses module boundaries, packaging, or the daemon/CLI/MCP contract.

```bash
uv run python -m unittest tests.test_desktop_service      # one module
uv run python -m tools.check                              # fast core set (~4 s)
uv run python -m unittest discover -s tests               # everything (~10 s)
```

`tools/check.py` runs 12 focused offline modules covering service,
transactions, CLI/MCP/daemon surfaces, tray/service, worker documents,
observation, AT-SPI startup, app profiles, live-session API contracts,
image-grounded input, and SSH forwards. It does not validate host desktop
behavior, provider calls, or all reader and live-worker paths. Run only when
requested or when the change warrants that broader offline check.

## Change → cheapest check

| Change | Check |
| --- | --- |
| `src/jevdesktop/desktop_service.py` dispatch, verifier, budgets | `tests.test_desktop_service` |
| `src/jevdesktop/transactions.py` policy, candidates, scroll, action flow | `tests.test_transactions` |
| `desktop_cli.py` / `desktop_mcp.py` / `desktop_daemon.py` | `tests.test_desktop_surfaces tests.test_desktop_daemon` |
| `desktop_service_unit.py` / `desktop_tray.py` | `tests.test_desktop_tray_and_service` |
| `desktop_worker.py` documents, window readiness | `tests.test_desktop_worker_documents` |
| `observation.py` capture store, crops | `tests.test_observation` |
| `app_profiles.py` profile parsing and aliases | `tests.test_app_profiles` |
| `image_input.py` capture-bound input and uncertainty | `tests.test_image_input` |
| `ssh_forwards.py` relay configuration/status | `tests.test_ssh_forwards` |
| live service API/session start options | `tests.test_live_session_api` |
| `vision_reader.py` stream bounds, call caps | `tests.test_vision_reader` |
| `wait_watcher.py` / `owner_wait.py` | `tests.test_wait_watcher` |
| `desktop_worker.py` live ownership, window selection, focus/cleanup | `tests.test_live_window_worker tests.test_live_owner` |
| `native_windows.py` window inventory and identity | `tests.test_native_windows` |
| `live_desktop_probe.py` / AT-SPI state restoration | `tests.test_live_desktop_probe tests.test_live_owner` |
| `benchmark_capture.py` AT-SPI setup | `tests.test_benchmark_atspi` |
| Study-only `scripts/` changes | owning focused harness test module |
| `paths.py`, packaging or `pyproject.toml` | discovery + `uv build` |
| `assets/` | inspect/render the changed asset |
| Docs only | none; read the diff |

## The default agent loop

Use fresh observations for decisions. Choose a strong verifier when one exists;
otherwise `verification=observed` reports a fresh state with uncertain effect.
It is not task completion. Inspect the result and reconcile after uncertainty;
never retry an input automatically. For screenshot-grounded pointer or
keyboard input, first capture a fresh image and use its capture ID and image
coordinates; Jev outputs are never coordinates. See [doc 40](40-live-workspace-and-profiles.md).

For semantic work, use polling, not the heartbeat wait:

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

A probe that launches an app or spends provider money is separate from offline
verification. Live desktop acceptance must be watched and explicitly
authorized; see [the acceptance checklist](40-live-workspace-and-profiles.md).

- `uv run python -m tools.local_service_probe` — socket/service smoke.
- `uv run python -m tools.scroll_effect_probe` — offline scroll fixture.
- `uv run python -m tools.vision_wait_probe` — paid; read its caps first.

Keep each probe bounded, self-cleaning, and report the artifact path. Do not
re-run a probe to reproduce an old number; the recorded report is the evidence.
