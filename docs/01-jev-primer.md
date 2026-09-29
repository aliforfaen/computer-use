# 01 — Jev primer

Short version of what Jev is, from the perspective of this project. Sources at the bottom.
Claims that come from the vendor or from demo posts are marked as such.

## What it is

- **Jev** is TypeSafe AI's "System One" decision model. Early access opened
  **2026-09-15**. Hosted, proprietary; weights and architecture are not published.
- It is **not an LLM**. It does not generate text. You send a `state` plus typed
  `questions`; it returns typed `answers`. `choice` and `score` include confidence;
  `noul` returns a yes-probability alone.
- Because answers are typed and enumerable, the calling code branches on them directly —
  no parsing, no regex, no free-form output to sanitize.
- Reported latency **70–500 ms** end to end (gateway-reported). Three synthetic
  calls from `cachy` took **890–1,732 ms** on 2026-09-29; measure again with
  desktop states before selecting time budgets.
  The owner's key is for the independent `jevtypesafeai.com` hosted gateway,
  which lists **$0.42 / 1M input tokens**. The official TypeSafe direct API
  currently lists **$0.042 / 1M input tokens**. Treat both as time-sensitive.

## The three primitives

| Type | Input | Output | Use in this project |
| --- | --- | --- | --- |
| `choice` | up to 255 labelled options | winning `choice`, per-option `probability`, `confidence` | *what to do*, *which element* |
| `score` | 2–10 ordered level descriptions | fractional `score`, per-level probabilities, `confidence` | progress / confusion / urgency rating |
| `noul` | just `instructions` | calibrated 0–1 probability of "yes" | gates: risk, done-ness, escalate, need generated text |

All three can be mixed in **one** request and are evaluated in parallel — this is what makes
a single round trip per decision cycle possible.

## API shape

```jsonc
POST https://jevtypesafeai.com/api/v1/decide  // owner's hosted jv_live_ key
Authorization: Bearer jv_live_…
{
  "model": "jev-latest",          // or pinned, e.g. "jev-1.13.0"
  "state": "…",                    // string | object | array
  "questions": {
    "action": { "type": "choice", "instructions": "…",
                "criteria": { "click": "press a control", "type_text": "…" } }
  }
}
// → { "model": "jev-1.13.0",
//     "answers": { "action": { "type":"choice", "choice":"click","confidence":0.93,
//                              "probabilities": { … } } },
//     "usage": { "input_tokens": 62, "output_tokens": 12 } }
```

- The independent hosted gateway and TypeSafe's official
  `https://api.typesafe.ai/v1/systemone` endpoint use the same request shape
  but **different keys**. The current `.env` key is for the hosted gateway;
  do not send it to the official endpoint. The official SDK reads
  `TYPESAFE_API_KEY`; this project's hosted key uses `JEV_API_KEY`.
- Also reachable through aggregators: Vercel AI Gateway `typesafe-ai/jev`, Cloudflare model
  catalog, OpenRouter; each route has its own credentials and pricing.
- Budgets: **64k tokens** for `state` + all questions combined, and **32k tokens** for
  `state` + the single longest question. Trim over-budget requests in code.
- The official direct API documents HTTP 401, 422, 429, and 529 errors. Check
  the hosted gateway's actual error contract separately before implementing retries.
- Vendor's own note: accuracy is weaker on questions that require multi-step reasoning.
  Keep each question a judgement call over presented candidates, not a planning problem.

## Why it fits computer use

The `choice` primitive *is* a bounded action space. The pattern proven by
`browser-use/jev-ultrafast`:

1. observe → build an **indexed table** of candidate elements with names/roles/values
2. one request with heads: `action` (CLICK / TYPE_TEXT / SELECT / SCROLL / WAIT / DONE / BLOCKED)
   and `target` (candidates filtered to those compatible with every possible action)
3. execute only the matching head in plain code, re-validate the target first
4. `TYPE_TEXT` alone needs a small LLM to write the string; that output is JSON-parsed

Two decisions, one network round trip. No screenshots in the default loop; the state is text.
Safety comes from the fact that the model can only choose an index we created — never a
coordinate, selector or command.

## Cost sanity check

A desktop step with, say, 1.5k input tokens ≈ **$0.00063** at the owner's hosted
gateway price. A 50-step task ≈ **$0.0315** in input charges.
Cost is a non-issue at this scale; latency and correctness are the real constraints.

## Sources

- https://jevtypesafeai.com/jev/api (owner's hosted endpoint, key and pricing)
- https://jevtypesafeai.com/terms (independent gateway/operator)
- https://docs.typesafe.ai/api (official direct API contract)
- https://docs.typesafe.ai/models (official direct model aliases and pricing)
- https://docs.typesafe.ai/introduction (vendor)
- https://github.com/browser-use/jev-ultrafast (reference loop implementation)
- https://en.wikipedia.org/wiki/Jev_(AI_model) (independent summary, limited depth)
- https://newsletter.victordibia.com/p/how-jev-works-calibrated-decision (calibration writeup)
