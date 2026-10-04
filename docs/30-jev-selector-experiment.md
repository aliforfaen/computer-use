# 30 — Jev selector experiment

**2026-10-03 · seam implemented, not wired into the owner.** ADR-011 stays
proposed. This doc is the plan for testing the owner's mental model against a
real decision model, cheaply, without changing the working computer-use loop.

## The idea being tested

> "Give a decision model the enumerated candidates we already produced and ask
> it *which one*, with confidence. It cannot name a coordinate or a command, so
> safety stays in our code."

That is the whole claim. It is falsifiable in a few cheap runs:

| Question | How we would know |
| --- | --- |
| Does it pick the same action+target a reference policy picks? | Agreement rate on a fixed state set. |
| Does confidence separate clear from ambiguous states? | Compare confidence against agreement; low confidence should mark the states where a reference policy disagrees or a human hesitates. |
| Does the answer ever escape our enumeration? | It must be impossible: `validate_answer` rejects anything not enumerated. Test this adversarially. |
| Is it fast/cheap enough to sit in a loop? | Per-call latency and tokens at direct rates (doc 01: ~$0.00006 per small state). |
| Where does it break? | Multi-step reasoning, ambiguous labels, huge option lists, states that need text generation. |

## What exists now

- `jev_selector.py` — the seam. Builds a state from `candidates` output
  (labels, roles, states, action names only), renders an indexed table, builds
  the `action` + `target` question heads, enforces the token budget, and
  validates the typed answer so the target must be an index we created.
  `HttpSelector` does one request with a hard call cap and no retries;
  `RecordedSelector` replays a saved answer for free.
- `tools/jev_selector_probe.py` — offline by default (`--dry-run`), `--recorded`
  to replay, `--call` for exactly one paid request (hard cap 3). `--call` reads
  the key from `JEV_API_KEY`, falling back to the private
  `~/.config/jev-desktop/jev.env` (0600); it fails closed with
  `jev_api_key_missing` before sending anything.
- `tests/test_jev_selector.py` — 13 offline cases, including "a target that is
  not one of our indices is rejected" and "the private dotenv supplies the key
  while an exported one wins".

Nothing here executes an action. `transactions.py` still owns execution,
verification and policy; the existing `authorizer` seam is where a selector
could later advise without gaining authority.

## Stages

**Stage 0 — offline (free).** Dump real candidates and inspect the request:

```bash
uv run jev-desktop candidates kcalc > /tmp/kcalc.json
uv run python -m tools.jev_selector_probe --candidates-json /tmp/kcalc.json --goal "enter 1" \
    --save-request /tmp/request.json
```

Check: does the state look like a fair description of the screen? Are the
options the ones a human would consider? Is the token estimate comfortably
under budget (32k state+longest, 64k total)?

**Stage 1 — one paid call.** Confirm the real contract and latency:

```bash
uv run python -m tools.jev_selector_probe --candidates-json /tmp/kcalc.json --goal "enter 1" --call
```

Record the returned model string, latency and usage. One call, one state.

**Stage 1 result (2026-10-04, host `cachy`, kwin 6.7.5).** One virtual KCalc
session, 23 enumerated options (22 buttons + 1 editable text). State 564 tokens,
request 873 estimated. Four paid calls, one goal each:

| goal | answer | confidence | target |
| --- | --- | --- | --- |
| enter the digit one | `click` | 0.99 | button `One` |
| compute two plus two | `click` | 0.93 | button `Two` |
| type 42 into the calculator display | `click` | 0.44 | button `Four` (`type_text` 0.48) |
| the task is finished, report done | `done` | 0.98 | (targetless) |

- `jev-latest` resolved to **`jev-1.13.0`**. Pin that alias for reproducible runs.
- Reported usage was ~1496–1500 input / 258 output tokens per call. The local
  873-token estimate omits the instructions and answer schema the provider adds,
  so budget arithmetic has to use the reported number, not the estimate.
- The first answer was executed through the existing path
  (`act kcalc click <ref> --verification display_text --expected 1`) and the
  code-owned verification passed, so an answer is actionable by today's tooling
  with no new execution surface.
- Confidence tracked ambiguity: 0.99 for an unambiguous goal, 0.44 for "type 42",
  where the model nearly split between clicking digit buttons (a reasonable
  calculator strategy that avoids free text) and typing into the field. Useful
  signal, not a guarantee.
- Every answer passed validation: the target was one of our own refs, and the
  targetless `done` carried no target.

Three findings bound the seam: a request is **stateless**, so multi-step goals
need a driver loop; the state carries **no current values**, so "what does the
display show now" is not answerable from candidates alone; and no answer can
carry typed text, so `type_text` still needs a separate text-argument helper.

**Stage 2 — bounded set (a few calls, still cents).** Fix 5–10 real states
(simple: one obvious control; ambiguous: two plausible targets; targetless:
goal already met, should be `done`; adversarial: an option list with near-
duplicate labels). For each state, decide the reference action+target *before*
calling, then compare. Report agreement, confidence, and every disagreement
verbatim. Keep the per-state cap explicit and record the answers.

**Stage 3 — optional acceleration.** Only if Stage 2 shows signal: compare
"polling + selector advice" against the plain polling loop from
[doc 29](29-verification-runbook.md) on one small task, using doc 24's
methodology (fixed trials, both arms, a real postcondition). This is the only
stage that touches the live loop, and it still ends with our own verification.

## What would not count as evidence

- One successful call, or a demo screenshot of a `choice`.
- Agreement measured on states we wrote to be easy.
- Any claim that the model "understood" the screen: it saw a table we built.
- Treating a `done` answer as completion. It is a proposal; the owner verifies.

## Cost and safety notes

- Direct TypeSafe pricing (doc 01) makes even a generous Stage 2 set cost less
  than a cent. Latency, not money, is the constraint (~254 ms median from this
  host).
- The key lives in the private `~/.config/jev-desktop/jev.env` (0600) as
  `JEV_API_KEY`, or in the environment, which wins. It is deliberately not in
  `daemon.env`: the selector is unwired, so the daemon never needs the key, and
  keeping it out of the service environment keeps it out of the reader request
  subprocess. It is never placed in the request state, a report or the audit log.
- The state never contains coordinates, selectors, file paths, shell commands
  or typed text — the same rule as ADR-012's reader path.
- Every answer is validated in code before use, so a wrong or hostile answer
  can only produce `unenumerated_target`, never an out-of-scope action.

## Open decisions (owner)

1. Pin a model alias for the experiment (`jev-latest` vs a pinned version) and
   record it with every result.
2. Decide the reference policy for Stage 2: your own judgement on saved states,
   or a written rule.
3. Whether `type_text` stays "needs a small LLM for the string" (doc 01) or is
   out of scope for the first experiment.
