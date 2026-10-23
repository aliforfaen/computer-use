# 35 — Third-look review (external)

**Date:** 2026-10-21 · **Reviewer:** Muninn (Hermes agent, intended consumer) · **Status:** outside opinion, not owner direction

Scope: read-only evaluation of `~/lamasync/projects/computer-use` as it stands on disk
(HEAD-equivalent of the 2026-10-14 docs tree). I ran the offline suite in a scratch
venv, inspected the shipped MCP surface, and read the agent-facing artefacts in `run/`.
I did **not** evaluate live behaviour: this host is Ubuntu 24.04 `dev-vm` with no KWin,
and `pygobject`/`pycairo` are not installable here (no sudo, no cairo headers).

## Host reconciliation — 2026-10-23

This review describes a copied checkout on dev-vm. The owner checkout has a
working `.git` and the October changes are retained in its local checkpoint.
The disposable GPU venv was indeed still present here (2.7 GB); it has now been
removed and `/tmp/` explicitly ignored. Grounding and verifier restrictions were
confirmed in current source. Other recommendations remain review proposals.

## Verdict

This is not a prototype. It is a small, honest, well-instrumented **verification-gated
desktop automation service** with unusually good evidence discipline, and its unfinished
20% is not "more features" — it is the two interfaces that decide whether anyone else can
use it: **grounding** (can a vision agent bind what it sees to a handle?) and **the
verification vocabulary** (can an action be expressed at all?).

Scorecard:

| Axis | Grade | Basis |
| --- | --- | --- |
| Evidence discipline | A | Claims in README/HANDOFF are reproducible; test counts match exactly (108 core / 273 full, verified here) |
| Safety posture | A− | Deny-by-default allowlist, 0600 socket in 0700 dir, dual opt-in live mode, durable rotated audit, kill switch, no text in audit |
| Offline test coverage | A− | 273 cases in 10.3 s, 5.7 kLOC of tests vs 7.9 kLOC runtime; behaviour-level, not mock-shaped |
| Live-path coverage | D | Zero automated regression for the AT-SPI/KWin/EIS layer; confidence rests on ~3 watched runs |
| Agent ergonomics | C+ | Exact contract, but 70-char refs, no geometry, and an action that cannot be expressed without a pre-declared postcondition |
| Layering / code shape | B− | Clean core seams, but a 300-line `dispatch`, a "probe" that is really the live runtime, and a pinned private upstream API |
| Process hygiene | D+ | No `.git` in this checkout; 12 source/doc files from Oct 11–14 exist only on this disk; 2.7 GB dead venv at repo root |
| Docs | B | Best-in-class continuity, worst-in-class signal ratio: 368 KB, four places defining "current direction", and a changelog where AGENTS.md's front matter should be |

## What is genuinely strong (and rare)

1. **Truthful outcome reporting.** `_safe_error` whitelists error codes, `capabilities`
   publishes `physical_input_detection: "unavailable"` instead of pretending, `wait` is
   labelled `experimental` with the reason and the evidence file, and the Jev study
   reported a *negative* result (code matched or beat the model on modal choices) at
   length. Most personal automation projects claim success and stop.
2. **Freshness as a first-class invariant.** Generation IDs, `_unique_matching` against a
   re-resolved snapshot, window-identity equality, `_blocked[session] = "verification_pending"`
   before input. The refusal paths are better specified than most products' happy paths.
3. **The core is driver-agnostic in the right place.** `TransactionEngine` ↔ `Backend`
   `Protocol` is a real seam. `Policy`, budgets, audit, focus restoration and cleanup are
   independent of kwin-mcp. That is what makes the strategic suggestion below feasible.
4. **Cheap, fast, honest test loop.** `tools.check` in 4.5 s, whole suite in 10.3 s, no
   network, no sleeps of consequence. The verification runbook maps change type → cheapest
   check. This is the single best predictor that the project can keep improving.

## The three findings I would act on, in order

### 1. Grounding gap: the agent cannot bind pixels to handles

`_candidate()` (`desktop_service.py:1216`) deliberately withholds bounds
("Editable contents and coordinates never leave the owner process") and replaces the label
of any editable with the string `editable text`. Meanwhile the ref is
`cand_<32-hex-generation>_<32-hex-id>` — **70 characters on average** in the Firefox
sample, with the generation already tracked separately.

Consequence: a vision agent gets an image and a list of names with no positions, so for
icon-only buttons, unlabelled canvases, canvas apps and anything where AT-SPI lies, it
cannot connect what it sees to what it may click. The Kate `Edit` crop miss in doc 32 is a
symptom of exactly this: perception and handles are two unrelated objects.

The proven fix is one the same desktop already runs: Hermes' native `computer_use`
(cua-driver) does `capture mode="som"` and returns a screenshot **with indices drawn on
it** plus `#N  AXButton 'Back' @ (x,y,w,h)`, and every action takes `element=N` with a
stale-token refusal. Steal the contract, keep Jev's freshness model:

- render `1..N` into the observation image from `candidate.bounds` (the capture mapping
  already exists and is already validated by the `coordinate_basis` string);
- expose bounds **as a region and an ordinal in reading order**, not raw screen
  coordinates, if withheld coordinates are a policy decision rather than an oversight;
- shorten refs to `cand_3` scoped by `generation` in the response header — same staleness
  guarantee, ~60 fewer characters per candidate per turn;
- keep the value-redaction, but only redact a label when it equals or contains the
  current value. `label = "editable text"` destroys field identity for no security gain in
  the common case.

### 2. The verification requirement is where human-like use dies

`desktop_act` requires one of nine `verification` kinds and an exact `expected`
(`desktop_mcp.py:66`). This bought the project its honesty, and it is now its ceiling.
The loop probe already documented the failure mode: on a KCalc operator click it returned
`verification_unavailable` and **refused to act**, because no postcondition exists for
"press Add".

That is the wrong trade. Refusing to *claim* success and refusing to *act* are different
decisions, and only the first one is about truthfulness. A human does the unverifiable
thing constantly and re-observes. Proposed shape:

- `verification: "observed"` (or `mode: "exploratory"`) — act, then require an immediate
  observation in the same call. Result is `outcome: "unverified"`, the audit records
  `postcondition: none`, the next snapshot is returned so the caller pays no extra
  round-trip. Truthfulness preserved, fluidity restored.
- add an explicit `uncertain` result class alongside `ok`/`failed` so downstream code
  cannot accidentally treat it as success.

### 3. The verification vocabulary *is* the product — make it a registry

Today verification is a hardcoded enum plus `_verifier()`/`_check_verifier()` in the
service (`desktop_service.py:901–1195`), and two of the nine kinds are KCalc-shaped
(`display_text`) while two more are Kate-shaped (`document_saved`, plus Kate-only actions).
That means "useful observations across native applications" is currently true only for
three apps, and each new app is a service edit.

Generic primitives that would cover most native apps without app-specific code:

| Verifier | Covers |
| --- | --- |
| `accessible_state` — target state/predicate delta (`checked`, `selected`, `expanded`, `enabled`, `value == X`) | toggles, tabs, checkboxes, combo boxes — the majority of native controls |
| `window_appeared` / `window_gone` — new top-level window matching role+title glob | dialogs, modals, "about", file choosers |
| `clipboard_contains` — after a copy action | universal evidence extractor, and the cheapest read channel for text that AT-SPI won't expose |
| `file_state` — path exists / mtime / sha256 / size | save, export, screenshot-to-disk |
| `region_changed` — bounded screenshot diff in the target's box | canvas apps, anything with no semantic surface |
| `semantic_tree_delta` — node count / named-node set changed | "something happened" fallback that is still falsifiable |

With app *profiles* declaring `target_focused` + `accessible_state` instead of code paths,
Dolphin, Okular, LibreOffice and System Settings become config, not features. Do this
before adding a fourth app.

## Second-order findings

**Action vocabulary is smaller than a human's, and the driver already has more.**
Six actions (`click`, `type_text`, `replace_document`, `save_document`, `navigate_url`,
`scroll`); no clipboard read/write, no drag, no right-click/context menu, no window
move/resize/maximise, no arbitrary key chords. kwin-mcp (doc 02, prior art) ships clipboard
and window management among its 33 tools — the layer is *narrower than its own backend*.
Clipboard is the one I would add first: it is the universal cross-app data channel, it is
also a verifier, and typing long text character-by-character through EIS is both slow and
fragile. The worker already snapshots and restores the clipboard for unicode input
(`desktop_worker.py:532`), so the plumbing exists.

**Private upstream coupling.** `engine._run_atspi` (`transactions.py:169`),
`engine._get_session().info.session_type` (`:154`), `AutomationEngine` instantiated inside
daemon recovery (`desktop_service.py:309`) — all private, all against an exact `==0.10.0`
pin. This is the correct *interim* choice and the wrong *long-term* one. Two concrete
moves: (a) a conformance test asserting the private surface still behaves, so a kwin-mcp
bump fails loudly instead of corrupting a live session; (b) offer upstream a typed
`atspi_find` result (they clearly have `ElementInfo`) and an AT-SPI `Action` invoke — that
single PR would let Jev stop clicking pixel centres and delete the riskiest code in the
project.

**`live_desktop_probe.py` is not a probe.** 582 lines in the *installed* package, imported
by `desktop_service` and `desktop_worker` through underscore-private names
(`_active_row`, `_restore_exact`, `_set_a11y_flags_at`, `_window_rows`). The live focus /
a11y / window-inventory runtime is living in a module named after an experiment. Extract
`jevdesktop/live_focus.py`; leave the probe as a thin CLI. Same shape problem, smaller:
`benchmark_capture.py` is half runtime (`_enable_virtual_atspi`, legitimately shipped) and
half harness (`capture_suite`, only used by `scripts/`).

**`dispatch` is 300 lines** (`desktop_service.py:594–901`) with five repeated
`self._validate`-style blocks and twelve `raise ValueError(...)` sites funnelling into one
handler. Not urgent, but it is the one function a contributor will be afraid to touch.

**No `SO_PEERCRED` / uid check on accepted connections.** 0600-in-0700 is adequate for a
single-user host; a `SO_PEERCRED` uid equality check is four lines of defence-in-depth,
and it costs nothing on the code path you already own.

## Where the installed test suite stops

273 offline cases, all of them against a `Backend` fake or the fake IPC server. Nothing
executes a real AT-SPI tree. The highest-risk layer (tree parsing `_TREE_LINE`/`_BOUNDS`
regexes, candidate filtering, scroll witnesses, focus restore) is validated by three
watched host runs and doc prose.

Cheap fix with disproportionate return: **record and replay.** You already write capture
JSON to `run/`. Save 3–5 real trees + captures per app (Kate, Firefox, KCalc, one dialog),
sanitise them, and replay them through `TransactionEngine` in the offline suite. That
converts "live-verified once" into "regression-covered forever", and it makes the
perception-packet work in your current priority list measurable rather than narrative.

## Process findings (fix these this week)

1. **This checkout has no `.git`.** `git log` fails; `origin` is
   `aliforfaen/computer-use` (public, last push **2026-10-06 13:21Z**, 771 KB). Twelve
   sources are newer: `AGENTS.md`, `docs/{04,05,25,29,30,31,32,33,34,HANDOFF}.md`,
   `scripts/jev_real_task_study.py`, `src/jevdesktop/{benchmark_capture,jev_selector}.py`,
   `tests/test_{benchmark_atspi,jev_loop_probe,jev_real_task_study,jev_selector}.py`,
   `tools/{check,jev_loop_probe,jev_selector_probe}.py`. HANDOFF already says the October
   work is uncommitted — but "uncommitted" here means **absent from version control
   anywhere**, in a single directory. Restore the remote or confirm lamasync is a real
   backup; either way, get these twelve files into history.
2. **`tmp/tine-ocr-venv-gpu` is 2.7 GB and still present**, while HANDOFF (2026-10-06) and
   AGENTS both say the GPU environment was removed. Documentation is drifting from disk.
3. **`tmp/` is not in `.gitignore`** despite AGENTS saying the CPU venv belongs under
   "ignored `tmp/`". Only the venv-generated nested `.gitignore` files saved the repo from
   a 2.7 GB commit. Add `/tmp/`.
4. **The docs multiply faster than the product.** 34 numbered docs, 368 KB, and "current
   direction — 2026-10-11" is verbatim-repeated in README, AGENTS.md, HANDOFF and doc 25.
   `AGENTS.md` is 254 lines whose first 127 are a dated changelog *before* "Start here" —
   every new agent session pays ~4 k tokens of superseded history to reach the rules.
   Collapse to: one `STATUS.md` (generated or hand-kept, ≤60 lines) + `AGENTS.md` = rules
   only + `docs/decisions/ADR-*.md` + `docs/archive/` for everything dated. Keep the
   instinct — it is the reason I can review this at all — just stop paying rent on
   history in the always-read path.

## Strategic note (the bit that needs your judgement)

Two desktop-driving stacks now live on the same machine: this service, and Hermes'
native `computer_use` over cua-driver (SoM capture, background input that does not steal
focus, cross-platform, no KDE dependency). They are converging on the same problem from
opposite ends — cua has the better *hands and eyes*, Jev has the better *conscience*
(verification, audit, budgets, allowlist, focus restore, virtual sessions, kill switch).

Jev should therefore not compete on the action layer. The highest-value version of this
project is the **trust layer**: an owner-side service that any driver plugs into, exposing
verified actions, a durable audit and lifecycle containment, with drivers behind
`Backend`. The code is already 80% shaped for that — `Policy` + `TransactionEngine` +
`AuditLog` + `Backend` + the journal/cleanup machinery. Two consequences:

- Finish the `Backend` protocol hygiene first (drop `document_key`/`document_bytes` into an
  app-capability object; they are Kate-isms in a generic interface) and add one
  conformance suite, so a second backend is a weekend rather than a rewrite.
- Then a macOS/Windows/other-Linux story is reachable without touching the safety core —
  which is the only way a personal tool outlives one host.

The alternative reading is equally defensible: keep it a KDE-native, three-app,
verification-gated automation harness, finish the observation packet, call it done, and
use it. If you pick that, then (a) fix grounding and the `unverified` action class, (b)
write down three real end-to-end desktop tasks you personally want done, and (c) treat
everything in doc 30–34 as archived. It is close to being finished; it is further from
being *finished proving it can be used* than from being finished.

## What I could not evaluate

- Live focus restoration under a human actually typing, and the flicker cost of
  focus-routed EIS input — this is the UX question that decides whether the thing is
  pleasant or merely correct. Worth one watched session with a timer.
- Whether `verification_unavailable` blocks enough real tasks to matter: predict it from
  the action list (`click` on non-toggle controls is the bulk of human interaction).
- Reader cost/quality end to end (no provider key here).
- Anything on the tailnet/remote path — not implemented, correctly deferred.

## Suggested next 5 tasks (if you want a concrete queue)

1. Commit and push the twelve October files; add `/tmp/` to `.gitignore`; delete the 2.7 GB venv.
2. SoM overlay on `desktop_observe` + short refs + label redaction narrowed. (Highest
   leverage per hour on perception.)
3. `verification: "observed"` exploratory path + `uncertain` result class, with the audit
   recording `postcondition: none`.
4. Verification registry with the six generic primitives; port KCalc/Kate to profiles as
   the proof.
5. Recorded AT-SPI trees + captures as offline replay fixtures.
