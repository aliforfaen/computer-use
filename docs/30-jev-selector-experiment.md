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
  to replay, `--call` for exactly one paid request (hard cap 3).
- `tests/test_jev_selector.py` — 10 offline cases, including "a target that is
  not one of our indices is rejected".

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
- The key stays in the ignored `.env` as `JEV_API_KEY` and is read from the
  environment only; it is never placed in the request state or in logs.
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
