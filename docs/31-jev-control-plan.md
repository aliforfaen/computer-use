# 31 — Jev control plan: loops, dialogs, ambiguity, browsers

Status: **plan, plus one working proof of concept.** The selector is still
unwired from the daemon (ADR-023): `tools/jev_loop_probe.py` is a *client* of the
owner socket, so execution, policy, budgets, focus handling and audit stay where
they are. Nothing in this document changes what the daemon may do.

Companion reading: [doc 30](30-jev-selector-experiment.md) (the seam and its
stages), [doc 29](29-verification-runbook.md) (which check to run),
[doc 02](02-prior-art.md) (what already exists and is being reused).

## 1. The loop shape

```
candidates ──> state (+ history) ──> Jev select ──> act + code-owned check ──> re-enumerate
     ▲                                                                              │
     └──────────────────── stop on done / blocked / budget / failed check ───────────┘
```

Four rules make it safe, and all four are code, not model:

1. **The model only ever chooses an index we created.** `validate_answer` rejects
   any action or target that was not enumerated, so a wrong or hostile answer can
   produce `unenumerated_target`, never an out-of-scope action.
2. **No step without an acceptable check.** Every action carries a verification
   that policy accepts, and the *expectation* comes from the command line or from
   policy — never from the model. A click the policy cannot verify is refused
   rather than guessed at.
3. **`done` is a proposal.** The loop reports `done_verified` only when a strong
   check already passed. Otherwise it reports `done_unverified`.
4. **A failed check stops the loop**, and the transaction engine latches the
   session (`verification_pending`) so the next call needs a reset. The unknown
   state after a failed action is not written over.

### Progress needs history

A request is stateless and the state carries no values, so a bare option list is
identical at every step — the model would repeat its first answer forever. The
loop therefore appends its own record of completed actions, as **labels only**
(`click 'One'`), bounded to 20 entries. No values, no typed text, no results.

## 2. What the proof of concept showed (2026-10-04, virtual KCalc)

`tools/jev_loop_probe.py`, one virtual session, `jev-1.13.0`:

| run | goal | result |
| --- | --- | --- |
| A | enter the digits 1, 2 then 3 in order | **`done_verified`** — `One` 0.91 → display `1`, `Two` 0.99 → `12`, `Three` 0.99 → `123`, then `done` 0.97. 4 calls, 6071 in / 1032 out tokens, 5.7 s |
| B | compute 2 plus 2 and press equals to show the result | **`wrong_choice`** on step 1 — the model clicked `Equals` (0.93) against an empty display; the check failed, the loop stopped, nothing else ran |
| C | compute two plus two | **`verification_unavailable`** on step 2 — the model chose `Add` correctly (0.99), but the loop refused to act because no AT-SPI postcondition exists for an operator click |

Three findings worth more than the pass itself:

- **History works.** In run A step 2 the model progressed (`Two`, not `One`
  again) purely from the appended labels, with no values in the state.
- **Verification catches a wrong choice.** Run B is the guardrail doing its job:
  the action ran, the display did not become `4`, and the loop stopped instead of
  building on a wrong state. Goal phrasing mattered — "press equals" biased the
  model to jump to the last step.
- **Some clicks cannot be verified at all.** Run C: KCalc exposes no "pending
  operation" in its accessibility tree, so after `Add` the display still reads
  `2` and a display check would be vacuous. The loop refuses it, which means a
  *strictly verified* loop cannot drive arithmetic — only value-producing clicks.
  Doing arithmetic would need an app-level read that does not exist today, or a
  weaker (label-only) check that proves the click landed but not what it did.

### Running it

```bash
uv run jev-desktop session start kcalc
uv run python -m tools.jev_loop_probe --app kcalc --goal "enter the digits 1, 2 then 3 in order" \
    --expect One=1 --expect Two=12 --expect Three=123 --report-dir run/jev-loop-kcalc
uv run jev-desktop session stop
```

`--dry-run` enumerates once and sends nothing. `--recorded FILE` replays a
scripted answer list offline. `--goal-sequence "a|b|c"` supplies per-step goals
when the plan is scripted rather than delegated. Restart
`jev-desktop.service` after changing daemon-side code — a running service keeps
the code it started with (this cost one confusing run: the old narrow verifier
was still in the live daemon). Evidence for the three runs above, with the exact
commands: `run/jev-loop-2026-10-04/`.

## 3. Where Jev earns its place

The shape that works is **choosing one control among plausible near-duplicates**,
plus **loop control**. Ranked by value:

### 3.1 Modal and confirmation dialogs — the best fit

The most brittle part of any automation, and exactly one decision among a handful
of enumerated buttons: Save / Discard / Cancel, overwrite yes/no, "document has
unsaved changes". The plan for the experiment:

- Open an untitled Kate document, type a marker, close the window, let Jev choose.
- Goals to compare: "keep the work", "throw the changes away", "keep editing".
- **Missing verifier:** only `document_saved` exists today, which covers Save. A
  Discard/Cancel choice has no postcondition, so the experiment needs a new one
  (for example `window_closed` / `document_absent`) before it can run under the
  same no-unverified-step rule.
- **Missing risk word:** `_check_action_policy`'s guarded-mode risk list is
  `delete|remove|submit|send|purchase|format`. `Discard`, `Don't save` and
  `Overwrite` are destructive and are *not* in it. That is a real gap to close
  before any modal experiment: a destructive modal button must require explicit
  approval in guarded mode regardless of which model chose it.

### 3.2 Kate — the most verifiable app we have

Kate already has strong code-owned postconditions: `target_text` (the entry now
contains exactly this text) and `document_saved` (the bytes on disk match). That
makes Kate a *better* next PoC than arithmetic: "replace the document with X,
then save" is two verified steps with real postconditions, and it exercises the
text path that `type_text`/`replace_document` need.

Note the boundary: `replace_document` takes the text from the caller. A free-text
goal ("write a summary") needs a separate text-argument helper; Jev selects the
control, it never produces the text.

### 3.3 Ambiguity resolution

This repo already hit the canonical case: Firefox exposes a hidden second
`scroll pane` with identical role/label/bounds, which broke a `(role, label)`
lookup. Two useful experiments:

- **Identical labels:** feed a state with two options that share a label and see
  whether the choice is stable and the confidence drops. Verification stays ours
  (`_scroll_viewport` already resolves the *live* element, so a model that picks
  the dead one still gets caught).
- **Near-duplicates:** `Clear` vs `All clear` in KCalc, `Save` vs `Save As` in
  Kate. A rule-based picker is brittle here; a goal-phrased choice is not.

### 3.4 Loop control

`scroll` vs `done` in a "keep looking until the text appears" loop, and `wait` vs
`done` in the existing polling loop. Jev decides, our verification gates. The
`wait` action already re-enumerates without acting, so this needs no new
machinery.

## 4. Browsers: reuse, do not reinvent

Agreed, and it is already installed. **Do not route browsers through Jev.**

| Need | Use | Why |
| --- | --- | --- |
| Web pages, forms, scraping, tests | `agent-browser` (installed at `/home/messhias/.npm-global/bin/agent-browser`, skill already available) | Drives Chrome/Chromium over CDP, auto-detects existing Chrome, **Brave**, Playwright and Puppeteer installs. `snapshot -i` returns element refs, i.e. enumerated options *with a DOM* and its own postcondition checks |
| Orca's embedded browser | `orca-cli` | Already owns that surface |
| Native app windows and dialogs | this repo (`kwin-mcp` + AT-SPI) | The only path that can see a Qt dialog at all |

Brave is Chromium, so the CDP path is the same one agent-browser already uses;
Firefox stays useful as the *native* AT-SPI fixture target for this repo's
verification tests, which is what it is used for today.

The honest reason not to involve Jev in browsers: page automation already has
semantic selection and its own verification, so a decision model adds a paid
round trip and a new failure mode for no capability. Jev's value is on surfaces
with **no DOM** — a Qt dialog, a calculator, a native editor.

## 5. What stays unwired, and what wiring would need

- The loop is a probe. The daemon does not call Jev and has no selector path.
- Before any live use, wiring needs: an `authorizer` seam that decides *whether*
  to consult the model (the seam exists and currently allows everything), an
  audit record per piece of advice (the probe writes none — a real gap), a
  driver-owned step budget, and the reset-after-failed-check behaviour the engine
  already enforces.
- No new execution surface: the loop acts through the same `act` call the CLI
  uses, with the same policy, budgets and audit.

## 6. Suggested order

1. **Kate verified loop** — `replace_document` + `document_saved`, two strong
   postconditions, no new verifier needed. Closes the loop on a second app.
2. **Close the modal guardrail gaps** — add the destructive risk words, then add
   the missing modal postcondition verifier.
3. **Modal experiment** — Jev choosing Save / Discard / Cancel on a real dialog.
4. **Ambiguity runs** — duplicate and near-duplicate labels, confidence reported.
5. Only then discuss wiring, with the audit and authorizer work above.

Arithmetic on a calculator is deliberately *not* on the list: run C showed it
cannot be verified from the accessibility tree, and a PoC that has to weaken its
own verification to pass is not evidence of anything.
