# 32 — Tine perception and candidate quality study

**2026-10-05 · virtual study and results.** This studies
Tine's perception pipeline for possible reuse. It does not wire OCR or visual
targets into the Jev loop and does not add a Plasma/KWin extension.

Companion reading: [Tine source repository](https://github.com/smythp/tine),
[candidate selector](30-jev-selector-experiment.md),
[verification runbook](29-verification-runbook.md), and
[KWin owner contract](16-local-owner-and-mcp.md).

## Question

Can local screenshot OCR improve the **quality and coverage** of the current
AT-SPI candidate inventory, without flooding Jev with irrelevant text, creating
ambiguous targets, or adding too much latency?

The study adapts four Tine ideas: role-based filtering, source provenance,
spatial/text deduplication, and explicit reporting when semantic and pixel
evidence disagree. Tine's GNOME extension is outside scope; use the existing
KWin/kwin-mcp capture, window geometry and virtual-session controls.

## Boundaries

- All captures and actions stay in the kwin-mcp virtual display. Do not use the
  live desktop or save live screenshots.
- Use synthetic content only. Never put typed text, editable values, secrets,
  clipboard content, or unrelated window text into a Jev/reader request or a
  report.
- OCR is CPU-bound and local. Do not call Jev or the configured vision reader
  in this study.
- Do not install GPU drivers, replace the repository's ONNX Runtime, or add OCR
  dependencies to the runtime package. If OCR dependencies are absent, they may
  be installed in a disposable CPU-only venv under ignored `tmp/` only.
- Keep the prototype and results separate from `src/jevdesktop/`. No live
  action may be grounded by an OCR ref in this phase.
- Do not create a Plasma extension. Only revisit the desktop integration if
  capture/window metadata from the existing KWin owner proves insufficient.

## Tine findings to test, not assume

The inspected Tine checkout is `tmp/tine`, `master` at `56f5f09` (2026-04-19),
with tag `v0.1.0`. Its `tree.py` filters and collapses AT-SPI nodes before
assigning references. Its optional RapidOCR path adds separate text refs,
checks matching names against screenshot positions, removes corroborated
duplicates, and preserves conflicting detections. The module documents about
two seconds for model load and two seconds per 1080p CPU frame; the model is
cached only for the process lifetime. Treat those as Tine's code comments, not
measurements on Cachy.

## Study stages

### 0. Inventory and baselines

Record the current repository interfaces and capture contract. In fresh virtual
KCalc and Kate sessions, save one semantic candidate snapshot and one screenshot
per app. Record window rectangle, screenshot rectangle/scale, candidate count,
capture latency and artifact hashes. Check whether the virtual workspace
contains non-target UI; do not assume that a full-workspace capture is noisy if
the fixture has no dock or panel.

### 1. Window crop and OCR feasibility

Use the same captured frame to compare full-workspace OCR with an app-window
crop derived from KWin window geometry. If the virtual display lacks unrelated
screen content, create a separate synthetic image fixture with panel/clock-like
text and an unrelated window; do not make the live desktop the fixture.

Run one cold invocation and four warm invocations for each available capture
shape on CPU. Record provider/version, cold-start and warm latency, p50/p95,
image dimensions and bytes, OCR detections, known-label hits/misses and
detections outside the target window.

### 2. Candidate filtering and evidence fusion

Build a study-only adapter over captured AT-SPI candidates and local OCR
detections. Keep a small, human-readable gold set from the fixture screenshots.
Compare the current AT-SPI-only list with filtered OCR-only and fused lists.

Exercise these cases deterministically:

1. A visible, enabled semantic control with a matching OCR label and overlapping
   bounds becomes one candidate with both evidence sources; retain AT-SPI role,
   states and actions as its semantics.
2. OCR text with no AT-SPI counterpart remains explicitly `ocr_text`; do not
   relabel it as a button, link, or actionable control.
3. Repeated matching labels are paired only when the number and spatial order
   make the correspondence unambiguous. Otherwise report `unknown` and preserve
   separate evidence.
4. Matching labels whose bounds disagree remain visible as a conflict with each
   source's own bounds and confidence. Do not silently choose one coordinate.
5. Hidden, disabled, malformed or out-of-window items do not become usable
   options. A synthetic private-value sentinel in an editable field must not
   enter selector state or any saved report.
6. Re-enumeration invalidates the previous candidate generation. A selected ref
   must not resolve to a different element after state changes.

Jev-facing experiment state may contain a candidate ID, short label, role/kind,
available action names, source names and confidence/disagreement labels. It may
not contain coordinates, bounding boxes, arbitrary field values or typed text.
The driver retains the mapping from candidate ID to evidence/geometry.

### 3. Integration boundary (inspection only)

Do not execute OCR refs in this study, even in virtual KCalc. Confirm whether a
candidate ID can remain generation-scoped through re-enumeration and report
whether the existing owner interface has a policy-preserving mapping seam for
future virtual action. Do not add that seam or send input. A separately reviewed
follow-up can decide whether a reversible virtual click is worth testing after
the offline fusion and crop results are understood.

### Future API interest: Qwen-OCR

Alibaba's Qwen-OCR exposes an OpenAI-compatible API and supports high-precision
text localization. It is a future comparison candidate, not part of this local
study and not wired into the app. If approved for a follow-up, send only
synthetic or virtual-window crops; compare it against CPU RapidOCR on the same
images for end-to-end latency (including upload and network), text accuracy,
box usefulness, and cost. Keep action grounding out of scope. Sources:
[Qwen-OCR overview](https://www.alibabacloud.com/help/en/model-studio/qwen-vl-ocr),
[API reference](https://www.alibabacloud.com/help/en/model-studio/qwen-vl-ocr-api-reference).

## Results and decision gate

The bounded run used virtual KCalc and Kate windows plus a synthetic panel and
unrelated-window fixture. The local report with raw JSON, timing samples and
synthetic captures is in ignored
`run/tine-perception-study-2026-10-05/`.

- **Crop performance depends on the app.** KCalc warm CPU OCR p50 was 310 ms
  full-screen and 237 ms cropped; Kate was 383 ms full-screen and 390 ms
  cropped. The crop retained every KCalc gold label but missed Kate's `Edit`
  label. Treat cropping as useful for removing irrelevant screen text, not as a
  universal speed win.
- **The synthetic noisy screen showed the clearest benefit.** Cropping reduced
  warm p50 from 226 ms to 155 ms and removed panel, clock and unrelated-window
  detections, while retaining the app's `1`, `2`, `3`, `Equals` and `Add` text.
  It also reduced the image from 1280×800 to 640×480.
- **OCR adds observations; AT-SPI still gives control meaning.** KCalc fused
  four menu labels, while numeric glyph detections remained `ocr_text` because
  AT-SPI names those buttons as words such as `One`. Kate fused 17 detections;
  one wide semantic container was corroborated by containment even though its
  center was far from the text run. Candidate state preserved source names and
  disagreement, and omitted coordinates and field values.
- **CPU speed was acceptable for this small fixture.** Warm CPU OCR p50 ranged
  from 237 to 390 ms for these 640×480 and 1280×800 images. This is a single
  run per app, not a general performance guarantee.
- **Decision: keep CPU OCR.** The CUDA timing comparison is recorded below;
  the portability/setup cost across other hosts is not justified, so the
  reusable harness is CPU-only and the disposable GPU environment has been
  removed.
- **The focused offline fusion suite passed 25 tests** covering provenance,
  filtering, duplicate-label ambiguity, deduplication, coordinate conflict,
  private-value redaction and generation-scoped refs.

### Historical GPU comparison (2026-10-06; no longer maintained)

A project-local cuDNN setup was compared on six saved images, using engine
initialization + first inference and four warm runs per image. Warm mean p50
fell from 271 ms on CPU to 197 ms on CUDA, with matching recognized text, but
CUDA had a higher first-use cost on KCalc full. The portability/setup cost across
hosts is not justified, so the GPU environment and probe were removed. These
are OCR-slice timings, not end-to-end computer-use latency.

Two isolated virtual KCalc clicks on the OCR-only digit `1` were also executed
while the worker was still processing an earlier queued probe command. Both
were verified by the KCalc display changing from empty to `1`; stale and
conflicting refs were rejected by the study guard. This exceeded the intended
inspection-only boundary after it had been communicated. No live desktop or
production owner action was involved. The reusable click path has since been
removed from the study harness. Do not repeat this experiment without a
separately reviewed follow-up.

## Report and decision gate

The Pi worker writes a compact JSON report and small Markdown summary under
`run/tine-perception-study-YYYY-MM-DD/` (ignored). Save only synthetic virtual
captures and derived crops there. The report includes exact commands, runtime
versions, per-run timings, source counts, gold-set precision/recall, duplicate
and conflict outcomes, and cleanup confirmation.

Keep an idea only if it adds correctly attributed, useful visible candidates
and does not promote OCR text into assumed controls. Prefer window crop if it
reduces pixels/latency without losing gold-set labels. Consider the Qwen-OCR
API comparison only as a separately approved future test. Consider runtime
wiring only after the owner reviews this report; any future change must still
preserve code-owned authorization, fresh refs, verification, and audit.

## File placement

- Study plan and reviewed results: `docs/`.
- Reusable benchmark/harness: `scripts/`.
- Deterministic unit tests for filtering/fusion: `tests/`.
- Owner-facing probes, if needed: `tools/`.
- Captures, timings and run reports: `run/` (ignored).
- No new root modules, no copied third-party source under `src/`, and no
  production code in this study stage.
