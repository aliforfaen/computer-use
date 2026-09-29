# 07 — KCalc selector probe

**2026-09-29 · decision-only, no UI actions from Jev.** This is a throwaway
probe using `kwin-mcp==0.10.0` for observation and TypeSafe direct
`jev-1.13.0` for bounded choices. No state compiler or MCP server was added.

## What the virtual KCalc session exposed

An isolated `session_start(app_command='kcalc', isolate_home=true)` on KWin
6.7.5 produced a 237-element AT-SPI tree. A broad pass found 78 interactive
named/text candidates, many from hidden scientific controls or disabled items.
The usable basic-mode list included digit buttons, Add/Subtract/Multiply/Divide,
Equals, Decimal point, Plus-minus, All clear, and Clear. Candidate IDs such as
`button.One` should derive from visible, enabled role + accessible name, then
be revalidated before execution; raw tree position is too fragile.

The display's unnamed editable AT-SPI field was **empty** in the initial state;
do not call it numeric zero. The earlier live `1` key probe left a broad tree
snapshot unchanged while its window crop changed. A screenshot **difference**
proves a change, not its value. A targeted follow-on read, described below,
found a semantic postcondition for the first single-digit task.

| Read | Samples | Range | Median |
| --- | ---: | ---: | ---: |
| KWin ScreenShot2, 1280×800 virtual session | 10 | 147–161 ms | 150 ms |
| Warm KCalc AT-SPI tree | 5 | 165–174 ms | 172 ms |

ScreenShot2 succeeded on all ten captures without a prompt. Spectacle fallback
was not exercised. The disposable virtual session and files were removed.

### Display postcondition follow-on

In a fresh isolated session, the editable display field had no `text=` value
before the action. `kwin-mcp` clicked the visible One button at the center of
its AT-SPI-reported bounds. A new AT-SPI read of the **same field** returned
`text='1'`; the cropped screenshot also showed a blank display changing to
`1`. The exact verified postcondition is **empty → `1`**, not `0` → `1`.
The click took about 45 ms, the warm post-action tree read about 163 ms, and
the following ScreenShot2 capture about 152 ms. This supports one narrow P2
task with semantic verification. It does not establish how KCalc exposes
multi-digit values or expression results.

Local Tesseract 5.5.3 lacks an English recognition model on this host, so the
OCR trial could not initialize; no package was installed. OCR is unnecessary
for the single-digit AT-SPI postcondition above.

## Bounded TypeSafe choices

Six **synthetic** KCalc task states used the captured basic-button names as
enumerated targets. Calls went only to TypeSafe direct with pinned `jev-1.13.0`;
all returned HTTP 200. They took **299–446 ms** (median **362 ms**) and used
8,072 input tokens total, about **$0.000339** at the
[published direct rate](https://docs.typesafe.ai/models). These are selector
probes, not a measured end-to-end desktop loop.

| State / task | Observed choice | Implication |
| --- | --- | --- |
| Display `0`; enter `1` | `press` → `button.One`, confidence 1.0 | Expected simple choice. |
| Display `42`; clear display | `press` → `button.All clear`, target probability 0.89 | Expected target; `Clear` retained 0.11 probability. |
| Display `1`; goal already met | `done` (0.97), but target still `button.One` | Ignore target on targetless actions; verify `DONE` in code. |
| Display `0`; clear display | `press` (0.66) → `button.All clear` | Redundant action; code-owned goal check should stop first. |
| `button.One` absent; enter `1` | `press` (0.95) → `button.Zero` (0.46) | The model can choose an invalid semantic pair; do not execute it. |

The sixth call disambiguated Clear versus All clear and chose All clear with
0.90 target probability. The displayed values above were **synthetic fields**,
not values read from KCalc's AT-SPI tree. No model-selected UI action ran.

### One real-tree decision

A fresh isolated KCalc tree yielded **19 visible, enabled basic buttons with
a press/click action** after filtering the formatted `kwin-mcp` output. The
editable display field was blank. A single direct TypeSafe request for
"enter digit 1" used only these observed candidates: `jev-1.13.0` chose
`press` → `button.One`, both with confidence 1.0. The target was present in
the observed candidate table. HTTP was 200; request latency was **573 ms**
with **1,503 input tokens**. Session startup and tree read took about
**1,056 ms** separately. No action was executed; the virtual session and
temporary files were removed.

`kwin-mcp==0.10.0` exposes the tree through formatted text in this path, so
P1's production adapter must define a stable extraction boundary. A first
throwaway MCP-client setup failed with an invalid `XDG_RUNTIME_DIR`; direct
`AutomationEngine` use in the retry inherited the correct runtime directory.
This setup issue did not affect the Jev request or the earlier virtual probes.

## Next narrow gate

Before an executed P2 step, the code must filter visible/enabled candidates,
check whether the task goal is already satisfied, reject missing or incompatible
action-target pairs, re-read the target before acting, and verify the effect
through an observation that can establish the requested result. Keep low
confidence or unobservable outcomes as a stop/escalate path. For the first
KCalc task, the observed empty → `1` editable-field transition is a concrete
postcondition. The one real-tree decision establishes the narrow P1 path for
"enter digit 1"; broader calculator tasks need their own observation probes.
