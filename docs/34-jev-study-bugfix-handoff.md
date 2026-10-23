# 34 — Jev study bugs: Pi fix handoff

2026-10-11. Authorized implementation and focused verification; results below.
Reuse the existing kwin-mcp owner, selector seam and study harness. Read AGENTS,
HANDOFF, doc 33 and this brief. Preserve all current dirty work and original
study evidence. No commit/push, subagents, paid provider requests, live desktop
input/capture, OCR/Qwen calls, dependency installs or unrelated cleanup.

## Fixes, in priority order

1. **Virtual AT-SPI startup compatibility (runtime change authorized).**
   `src/jevdesktop/benchmark_capture.py::_enable_virtual_atspi` blindly sets
   `ScreenReaderEnabled`; Pi reproduced startup failure on at-spi2-core 2.62,
   whose interface exposes only `IsEnabled`. Discover available properties,
   require and verify `IsEnabled`, and set/verify `ScreenReaderEnabled` only
   when available. Missing optional property is distinct from permission,
   transport or write failure: never swallow arbitrary D-Bus errors. Close the
   bus on all paths. Cover old/new interfaces and failed enabling in focused
   tests. Remove the now-unnecessary study shim and its PYTHONPATH injection.
   Preserve live focus/accessibility restoration; inspect related live paths
   for the same assumption and report scope explicitly. Do not exercise live
   mode or restart/change the owner's running daemon.

2. **Jev opt-in bypass (confirmed source defect).**
   `scripts/jev_real_task_study.py::main` sets `JEV_ENABLED=true` internally.
   Remove that assignment; require `jev_enabled()` before reading credentials,
   launching a daemon or sending a paid request. Disabled --run fails with
   `jev_disabled`. --code-only and dry-run remain free and usable with Jev off.
   `--dry-run` must actually prevent execution even when combined with --run
   or --code-only, or reject conflicting flags before side effects. Update
   commands/docs and test no key loading/daemon startup when disabled.

3. **Study daemon cleanup on startup failure (confirmed source defect).**
   In `main`, `daemon.start()` is outside the cleanup finally block. A readiness
   timeout can leave its process running. Make ownership cleanup unconditional
   once created, including startup timeout/partial startup and exceptions in
   the study. Preserve failure evidence, record whether cleanup is confirmed,
   and retain useful bounded stderr diagnostics without credentials. Test
   timeout/early exit without real processes. Never terminate an external owner.

4. **Measurement provenance (confirmed source defect).**
   `execute_study` hardcodes KWin 6.7.5 in both paths. Obtain current installed
   version via an existing helper or a bounded version command; report unknown
   if unavailable. Record requested and observed Jev model separately. Report
   configured caps rather than defaults when lowered. Leave original historical
   evidence unchanged; describe corrections to the harness in current docs.

5. **Decision validity and completion evidence.**
   The seam checks that action and target exist independently; the document
   extension `validate_probe_answer` also omits the `type == choice` check.
   Reject malformed document answers and unsupported action/target pairs before
   act. Apply equivalent compatibility checking to the shared selector where
   appropriate (focused selector source changes authorized; no daemon wiring).
   Preserve targetless blocked/wait/done and enumerated IDs. Tests should replay
   the actual bad shapes: save_document on Save and on Discard buttons, and a
   malformed action type. Do not reinterpret an invalid answer as a valid click.
   Check the document completion contract against authoritative save evidence:
   the loop currently copies a remembered replacement digest into save results;
   reproduce whether an intervening mutating action can make that stale, and
   invalidate or bind it to fresh evidence so stale content cannot prove done.
   Preserve the partial-progress/old-revision/final-save regressions.

6. **Bounds and truthful outcomes (review hypotheses to reproduce first).**
   run_loop increments provider_calls only after a successful answer, so a
   failed paid attempt may be reported as zero despite HttpSelector counting it.
   Reconcile attempted requests from transport counters, including failure;
   keep offline selection counts separate. The brief promised six actions but
   only max_steps=8 is passed: enforce a separate six-action limit. Review
   harness success reporting (main prints ok true and exits zero even for failed
   arms) and distinguish completed experiment from passed task contracts and
   confirmed cleanup. Return/report failures explicitly. Add small tests for
   these failure shapes, no large matrices.

## Verification and deliverables

First use offline focused tests plus tools.check; broaden only if a changed
runtime contract warrants it. Run one free --code-only virtual Kate smoke using
an isolated daemon, a NEW output directory under run/jev-study-fixes-2026-10-11/,
and no shim. Confirm draft/save/revise/save, exact final bytes and owned-process,
socket and session cleanup. Do not overwrite run/jev-real-task-2026-10-10/.
Record current AT-SPI/KWin/kwin-mcp versions and the runtime fix actually loaded.

Update HANDOFF and this document with fixed/reproduced/unresolved findings,
commands, tests, smoke evidence and limits. Keep doc 33's historical comparison
intact. The owner will notify Codex when complete: no coordinator monitoring or
blocking questions. If genuinely blocked, finish with concrete evidence.

The design improvement of choosing a SINGLE complete action-target option ID
is a follow-up proposal, not required in this bugfix. Explain how compatible
pair validation prepares for it; avoid a broad selector protocol rewrite here.

## Results (2026-10-11)

All six fixes are implemented and covered by focused offline tests. No paid Jev
request was made and no live input was exercised, so the Jev-arm and live-path
changes are verified offline only (see limits). The historical study evidence in
`run/jev-real-task-2026-10-10/` is unchanged.

### 1. Virtual AT-SPI startup compatibility — fixed (runtime)

Reproduced: at-spi2-core 2.62.0.1 exposes only `org.a11y.Status.IsEnabled`;
the shipped `benchmark_capture._enable_virtual_atspi` also set
`ScreenReaderEnabled`, so `props.Set` raised `InvalidArgs` and every virtual
session failed with `atspi_setup_failed` / `worker_failed`.

`_enable_virtual_atspi` now discovers the interface with `GetAll`, requires and
verifies `IsEnabled`, and sets/verifies `ScreenReaderEnabled` only when the bus
exposes it. A missing optional property is distinct from a permission, transport
or write failure; arbitrary `DBusException`s are still converted to a typed
`CaptureError`, never swallowed. The bus is created inside the guarded block and
closed on every path, including connect failure.

Focused tests `tests/test_benchmark_atspi.py` (7 cases) cover the new interface,
the old interface, a missing required property, a write failure, a read-back
failure, a connect failure and missing `dbus-python`. The module was added to
`tools.check` because it now guards a shipped runtime contract.

The study shim (`scripts/study_shims/`) and its `PYTHONPATH` injection are
removed; the harness runs the shipped runtime only.

**Live-path scope (inspected, not changed).** The same two-property assumption
remains in `live_desktop_probe._a11y_flags_at` / `_set_a11y_flags_at` (both index
`ScreenReaderEnabled`), in `desktop_worker._start_live` (sets both flags) and in
the `desktop_service` journal restoration check
(`set(flags) == {"IsEnabled", "ScreenReaderEnabled"}`). On 2.62 a live temporary
AT-SPI enable would still fail with `live_atspi_unavailable`. This was left
unchanged deliberately: it is a different path with a restore-exact contract
tied to the worker journal, it cannot be validated without exercising live mode,
and the brief scoped the runtime fix to the virtual startup regression. It is
the first follow-up below.

### 2. Jev opt-in bypass — fixed

The harness no longer sets `JEV_ENABLED=true`. `--run` now checks
`jev_enabled()` before reading credentials, before starting a daemon and before
any request; with Jev disabled it prints `{"error": "jev_disabled"}` and exits 2.
`--code-only` and `--dry-run` stay free and work with Jev off. `--dry-run` now
wins over `--run` and `--code-only` (it prints the plan and starts nothing)
instead of being ignored. Tests assert no key load and no daemon construction
when disabled, and no daemon construction under `--dry-run`.

### 3. Daemon cleanup on startup failure — fixed

`daemon.start()` moved inside the ownership `try`/`finally`: once a disposable
daemon is created, `stop()` runs on readiness timeout, early exit, study
exceptions and normal completion. `stop()` reports `cleanup_confirmed`
(process exited with a return code) and a bounded 2 KB `stderr_tail` from
`runtime/daemon.stderr.log`, which is now captured instead of discarded. A
startup-timeout test drives a fake daemon through `main` and asserts the owned
process was stopped and the study never ran.

### 4. Measurement provenance — fixed

`_host_facts()` now reads the installed versions from the existing
`live_desktop_probe._version_facts()` helper and `benchmark_capture._atspi_version()`,
falling back to `unknown`. Requested and observed Jev models stay separate
(`model_requested` / `model_observed`), and the report's budget records the
configured caps (`task_call_cap`, `ambiguity_call_cap`, `max_actions`) next to
the defaults.

### 5. Decision validity and completion evidence — fixed

`jev_selector.validate_answer` now rejects an action/target pair the target does
not support (`action_not_supported_by_target`), in addition to the existing
independent action and target checks. `tools.jev_loop_probe.validate_probe_answer`
adds the same compatibility check for the document actions and restores the
`type == "choice"` check for the action and the target in its document branch.
An invalid pair is reported as `invalid_answer` before `act` and is never
reinterpreted as a click. Targetless `blocked`/`wait`/`done` and enumerated IDs
are preserved.

Completion was made evidence-bound: any successful action whose verification is
not the replacement (`target_text`) or the save (`document_saved`) clears the
remembered replacement digest. Reproduced stale case: `replace(A) -> click ->
save -> done` is now `done_unverified`, and the save's recorded `sha256` is
`null`; previously the save copied the remembered `A` digest and could prove
`done`. Tests replay `save_document` on the Save and Discard buttons, a malformed
action type, and the intervening-action case; the partial-progress,
old-revision and final-save regressions are preserved.

### 6. Bounds and truthful outcomes — fixed

`run_loop` now counts an attempted selector request before sending it:
`provider_calls` is attempts, `provider_answers` is validated decisions, and
`provider_attempts` mirrors the transport counter when a selector exposes one
(`ProbeHttpSelector.transport_calls`). A failed paid attempt is therefore no
longer reported as zero, and offline selection counts stay separate. A separate
`max_actions` cap (six for the study) is enforced with
`action_budget_exhausted`, independent of `max_steps` and `max_calls`.

The harness now separates a completed experiment from passed task contracts and
confirmed cleanup via `study_summary`: `ok` requires `completed`,
`task_contracts_passed` and `cleanup_confirmed`, and `main` exits non-zero when
`ok` is false. A failed arm or unconfirmed cleanup can no longer print `ok: true`
or exit zero.

### Verification run

```bash
uv run python -m unittest tests.test_jev_selector tests.test_jev_loop_probe \
    tests.test_jev_real_task_study tests.test_benchmark_atspi   # 76 pass
uv run python -m tools.check                                    # 108 pass (8 modules)
uv run python -m unittest discover -s tests                     # 273 pass

# one free code-only virtual Kate smoke, shipped runtime, no shim, new dir
env -u JEV_ENABLED uv run python -m scripts.jev_real_task_study \
    --code-only --study-id jev-study-fixes-2026-10-11 \
    --output run/jev-study-fixes-2026-10-11
```

Smoke result (`run/jev-study-fixes-2026-10-11/report.json`): `ok: true`,
`completed: true`, `task_contracts_passed: true`, `cleanup_confirmed: true`,
exit 0. The code arm was `done_verified` in 4 actions (replace, save, replace,
save, done) with the exact final 582-byte revision
(`sha256 cd700ffe…70ed`); `session_stop {stopped: true, cleanup: confirmed}`;
the disposable daemon exited with return code 0 and `cleanup_confirmed: true`;
no leftover `kate`/`kwin-mcp`/study-daemon process and the private socket was
removed. The owner's running daemon was never used or restarted.

Provenance recorded in the smoke report:

```
kwin: kwin 6.7.5
kwin_mcp: kwin-mcp==0.10.0
atspi: (major=2, minor=62, micro=0)
runtime: {virtual_atspi_required: IsEnabled,
          virtual_atspi_optional: [ScreenReaderEnabled],
          study_shim: false}
```

### Unresolved findings and limits

- **Cleanup reporting remains too strong (reviewed 2026-10-14).**
  `StudyDaemon.stop` treats any process exit, including forced termination, as
  `cleanup_confirmed`. This does not prove child-process or socket cleanup.
  Fix the report before reusing the harness; the recorded normal-shutdown
  smoke has separate session, socket and process evidence.

- **Live-path AT-SPI compatibility is unchanged** (scope in fix 1). A live
  temporary-AT-SPI task on at-spi2-core 2.62 would still fail; the read/set
  helpers, the worker's enable and the service journal check all need the same
  "discover then set only present properties" treatment, validated live.
- **No paid call was made.** The Jev-arm behaviour (compatible-pair rejection,
  attempt counting) is exercised only with injected selectors; the first
  `--run` is the real check.
- **The smoke is code-only and virtual.** No destructive modal was opened and no
  live input or capture was performed.
- **Single action-target option ID** remains a follow-up design change. The
  compatibility predicate added here (`action in option.actions`) is exactly what
  such an ID would encode, so it is the prerequisite, not a competing approach.
