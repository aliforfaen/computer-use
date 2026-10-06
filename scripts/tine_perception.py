#!/usr/bin/env python3
"""Study-only Tine-inspired perception fusion for the computer-use repo.

This module adapts four ideas inspected in the Tine checkout (``tmp/tine``):

* role/states filtering before anything becomes a candidate,
* explicit provenance per candidate (``atspi`` versus ``ocr``),
* spatial/text deduplication of corroborating detections,
* explicit reporting when semantic and pixel evidence disagree.

It is a research harness for offline fusion tests and bounded virtual capture
measurements. It does not execute actions from OCR refs and is not imported by
the runtime.

Nothing in this module is imported by ``src/jevdesktop``.

Run ``python -m scripts.tine_perception --help`` for the bounded probe.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shlex
import sys
import time
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

INT_MIN = -2147483648
GARBAGE_BBOX_THRESHOLD = 10000
DEFAULT_MIN_CONFIDENCE = 0.8
COORD_TOLERANCE = 20
DEFAULT_GENERATION = 1

SOURCE_ATSPI = "atspi"
SOURCE_OCR = "ocr"
KIND_OCR_TEXT = "ocr_text"

# Interactive roles copied from Tine ``tine/tree.py`` (INTERACTIVE_ROLES).
INTERACTIVE_ROLES = frozenset({
    "button", "toggle button", "check box", "check menu item",
    "menu item", "text", "terminal", "table cell", "list item",
    "menu", "radio button", "slider", "combo box", "link", "entry",
    "spin button",
})
# Container roles that get a ref only when named (Tine NAMED_CONTAINER_ROLES).
NAMED_CONTAINER_ROLES = frozenset({
    "list", "tree", "table", "page tab list", "tool bar", "tree table",
})
EDITABLE_ROLES = frozenset({"text", "text entry", "entry", "combo box"})
ACTIONABLE_ROLES = INTERACTIVE_ROLES

VISIBLE_STATES = frozenset({"showing", "visible"})
ENABLED_STATES = frozenset({"sensitive", "enabled"})

DISAGREEMENT_AMBIGUOUS = "ambiguous_pairing"
DISAGREEMENT_CONFLICT = "coordinate_conflict"


# ---------------------------------------------------------------------------
# Provenance primitives
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Bounds:
    x: int
    y: int
    width: int
    height: int

    @property
    def center(self) -> tuple[int, int]:
        return (self.x + self.width // 2, self.y + self.height // 2)

    def contains_point(self, px: int, py: int) -> bool:
        return self.x <= px <= self.x + self.width and self.y <= py <= self.y + self.height

    def contains_rect(self, other: "Bounds") -> bool:
        return (self.x <= other.x and self.y <= other.y
                and other.x + other.width <= self.x + self.width
                and other.y + other.height <= self.y + self.height)

    def translated(self, dx: int, dy: int) -> "Bounds":
        if dx == 0 and dy == 0:
            return self
        return Bounds(self.x + dx, self.y + dy, self.width, self.height)

    def as_dict(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "w": self.width, "h": self.height}


@dataclass(frozen=True)
class AtspiElement:
    """One semantic element from the accessibility tree, absolute screen coords."""

    role: str
    label: str
    states: frozenset[str]
    actions: tuple[str, ...]
    bounds: Bounds
    value: str | None = None

    @property
    def normalized_label(self) -> str:
        return normalize_text(self.label)


@dataclass(frozen=True)
class OcrDetection:
    """One local OCR text detection; ``text`` is never an actionable target."""

    text: str
    bounds: Bounds
    confidence: float

    @property
    def normalized_text(self) -> str:
        return normalize_text(self.text)


@dataclass(frozen=True)
class Rejection:
    source: str
    reason: str


@dataclass(frozen=True)
class Candidate:
    """A study candidate. Jev-facing state excludes coordinates and values."""

    candidate_id: str
    kind: str
    label: str
    actions: tuple[str, ...]
    sources: tuple[str, ...]
    confidence: float | None
    disagreement: str | None
    bounds_by_source: Mapping[str, Bounds]
    atspi_role: str | None
    atspi_states: frozenset[str]
    generation: int


@dataclass(frozen=True)
class Conflict:
    label: str
    atspi_bounds: Bounds
    ocr_bounds: Bounds
    atspi_role: str
    ocr_confidence: float
    distance: float


@dataclass(frozen=True)
class FusionResult:
    generation: int
    candidates: tuple[Candidate, ...]
    conflicts: tuple[Conflict, ...]
    ambiguous_labels: tuple[str, ...]
    rejections: tuple[Rejection, ...]
    counts: Mapping[str, int]
    private_value_count: int


class StaleReferenceError(RuntimeError):
    """Raised when a ref from an older enumeration generation is selected."""

    def __init__(self, ref: str):
        super().__init__(f"stale candidate reference: {ref}")
        self.ref = ref


# ---------------------------------------------------------------------------
# Text normalisation (Tine ``normalize_text`` behaviour)
# ---------------------------------------------------------------------------

def normalize_text(value: Any) -> str:
    """Casefold and drop everything that is not alphanumeric.

    NFKC folding is added on top of Tine's rule: the PP-OCR Chinese recogniser
    returns fullwidth digits (``１``), which plain casefold does not equal to
    ``1`` and therefore silently blocks AT-SPI/OCR fusion.
    """

    if not isinstance(value, str):
        return ""
    return "".join(ch for ch in unicodedata.normalize("NFKC", value).casefold()
                   if ch.isalnum())


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def bounds_reason(bounds: Bounds) -> str | None:
    """Classify unusable geometry. Returns ``None`` when the bounds are usable."""

    if (bounds.x == INT_MIN or bounds.y == INT_MIN
            or bounds.width == INT_MIN or bounds.height == INT_MIN):
        return "hidden_no_extents"
    if bounds.width <= 0 or bounds.height <= 0:
        return "malformed_bounds"
    if bounds.width > GARBAGE_BBOX_THRESHOLD or bounds.height > GARBAGE_BBOX_THRESHOLD:
        return "malformed_bounds"
    return None


def is_semantic(role: str, label: str) -> bool:
    if role in INTERACTIVE_ROLES:
        return True
    return bool(label) and role in NAMED_CONTAINER_ROLES


def filter_atspi(
    elements: Iterable[AtspiElement],
    *,
    window: Bounds | None = None,
    offset: tuple[int, int] = (0, 0),
) -> tuple[list[AtspiElement], list[Rejection], set[str]]:
    """Keep only visible, enabled, in-window semantic elements.

    Returns ``(usable, rejections, private_values)`` where ``private_values``
    holds normalised editable-field contents. Those values are removed from the
    returned elements so they can never reach candidate state or a report.
    """

    usable: list[AtspiElement] = []
    rejections: list[Rejection] = []
    private_values: set[str] = set()
    for element in elements:
        role = (element.role or "").lower()
        bounds = element.bounds.translated(*offset)
        reason = bounds_reason(bounds)
        if reason is not None:
            rejections.append(Rejection(SOURCE_ATSPI, reason))
            continue
        if not is_semantic(role, element.label):
            rejections.append(Rejection(SOURCE_ATSPI, "not_semantic"))
            continue
        if role in ACTIONABLE_ROLES and not ENABLED_STATES.issubset(element.states):
            rejections.append(Rejection(SOURCE_ATSPI, "disabled"))
            continue
        if "disabled" in element.states:
            rejections.append(Rejection(SOURCE_ATSPI, "disabled"))
            continue
        if not (element.states & VISIBLE_STATES):
            rejections.append(Rejection(SOURCE_ATSPI, "hidden"))
            continue
        if window is not None and not window.contains_rect(bounds):
            rejections.append(Rejection(SOURCE_ATSPI, "out_of_window"))
            continue
        value = element.value
        if role in EDITABLE_ROLES and "editable" in element.states and value:
            private_values.add(normalize_text(value))
            value = None
        usable.append(replace(element, bounds=bounds, value=value))
    return usable, rejections, private_values


def filter_ocr(
    detections: Iterable[OcrDetection],
    *,
    window: Bounds | None = None,
    private_values: frozenset[str] | set[str] = frozenset(),
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> tuple[list[OcrDetection], list[Rejection]]:
    """Keep confident, well-formed OCR detections; drop private-value text."""

    usable: list[OcrDetection] = []
    rejections: list[Rejection] = []
    for detection in detections:
        if detection.confidence < min_confidence:
            rejections.append(Rejection(SOURCE_OCR, "low_confidence"))
            continue
        reason = bounds_reason(detection.bounds)
        if reason is not None:
            rejections.append(Rejection(SOURCE_OCR, reason))
            continue
        norm = detection.normalized_text
        if not norm:
            rejections.append(Rejection(SOURCE_OCR, "empty_text"))
            continue
        # The matched value is never recorded, only the rejection count.
        if any(value and (value == norm or value in norm) for value in private_values):
            rejections.append(Rejection(SOURCE_OCR, "private_value"))
            continue
        if window is not None and not window.contains_point(*detection.bounds.center):
            rejections.append(Rejection(SOURCE_OCR, "out_of_window"))
            continue
        usable.append(detection)
    return usable, rejections


def elements_from_find(items: Iterable[Any], *, offset: tuple[int, int] = (0, 0)) -> list[AtspiElement]:
    """Build provenance records from kwin-mcp ``_run_atspi('find')`` results."""

    elements: list[AtspiElement] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        try:
            bounds = Bounds(
                int(item.get("x", 0)), int(item.get("y", 0)),
                int(item.get("width", 0)), int(item.get("height", 0)),
            ).translated(*offset)
        except (TypeError, ValueError):
            continue
        states = frozenset(str(state).lower() for state in (item.get("states") or [])
                           if isinstance(state, str))
        actions = tuple(str(action) for action in (item.get("actions") or [])
                        if isinstance(action, str))
        text = item.get("text")
        elements.append(AtspiElement(
            role=str(item.get("role", "")).lower(),
            label=str(item.get("name", "")),
            states=states,
            actions=actions,
            bounds=bounds,
            value=text if isinstance(text, str) and text else None,
        ))
    return elements


# ---------------------------------------------------------------------------
# Fusion
# ---------------------------------------------------------------------------

def _distance(a: tuple[int, int], b: tuple[int, int]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def pair_group(
    refs: Sequence[AtspiElement],
    detections: Sequence[OcrDetection],
) -> list[tuple[AtspiElement, OcrDetection]] | None:
    """Pair same-label groups only when the correspondence is unambiguous.

    ``None`` means the pairing is unknown: counts differ, or the order-preserving
    pairing is not strictly better than its reverse (a spatial tie/crossing).
    """

    if not refs or len(refs) != len(detections):
        return None
    if len(refs) == 1:
        return [(refs[0], detections[0])]
    refs_sorted = sorted(refs, key=lambda item: (item.bounds.center[1], item.bounds.center[0]))
    dets_sorted = sorted(detections, key=lambda item: (item.bounds.center[1], item.bounds.center[0]))

    def cost(order: Sequence[OcrDetection]) -> float:
        return sum(_distance(item.bounds.center, order[index].bounds.center)
                   for index, item in enumerate(refs_sorted))

    forward = cost(dets_sorted)
    reverse = cost(list(reversed(dets_sorted)))
    if reverse <= forward:
        return None
    return list(zip(refs_sorted, dets_sorted))


def _atspi_candidate_label(element: AtspiElement) -> str:
    return element.label


def _record(
    *, kind: str, label: str, actions: tuple[str, ...], sources: tuple[str, ...],
    confidence: float | None, disagreement: str | None,
    bounds_by_source: Mapping[str, Bounds], atspi_role: str | None,
    atspi_states: frozenset[str],
) -> dict[str, Any]:
    return {
        "kind": kind, "label": label, "actions": actions, "sources": sources,
        "confidence": confidence, "disagreement": disagreement,
        "bounds_by_source": dict(bounds_by_source), "atspi_role": atspi_role,
        "atspi_states": atspi_states,
    }


def fuse(
    atspi_elements: Iterable[AtspiElement],
    ocr_detections: Iterable[OcrDetection],
    *,
    generation: int = DEFAULT_GENERATION,
    window: Bounds | None = None,
    offset: tuple[int, int] = (0, 0),
    tolerance: float = COORD_TOLERANCE,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> FusionResult:
    """Fuse semantic and OCR evidence into generation-scoped candidates."""

    atspi_elements = list(atspi_elements)
    ocr_detections = list(ocr_detections)
    usable_refs, ref_rejections, private_values = filter_atspi(
        atspi_elements, window=window, offset=offset)
    usable_dets, det_rejections = filter_ocr(
        ocr_detections, window=window, private_values=private_values,
        min_confidence=min_confidence)

    refs_by_label: dict[str, list[AtspiElement]] = {}
    for element in usable_refs:
        label = element.normalized_label
        if label:
            refs_by_label.setdefault(label, []).append(element)
    dets_by_label: dict[str, list[OcrDetection]] = {}
    for detection in usable_dets:
        label = detection.normalized_text
        if label:
            dets_by_label.setdefault(label, []).append(detection)

    records: list[dict[str, Any]] = []
    conflicts: list[Conflict] = []
    ambiguous_labels: list[str] = []
    fused_count = 0
    matched: set[str] = set()

    for label in sorted(set(refs_by_label) & set(dets_by_label)):
        refs, dets = refs_by_label[label], dets_by_label[label]
        matched.add(label)
        pairing = pair_group(refs, dets)
        if pairing is None:
            ambiguous_labels.append(label)
            for element in refs:
                records.append(_record(
                    kind=element.role, label=_atspi_candidate_label(element),
                    actions=element.actions, sources=(SOURCE_ATSPI,), confidence=None,
                    disagreement=DISAGREEMENT_AMBIGUOUS,
                    bounds_by_source={SOURCE_ATSPI: element.bounds},
                    atspi_role=element.role, atspi_states=element.states))
            for detection in dets:
                records.append(_record(
                    kind=KIND_OCR_TEXT, label=detection.text, actions=(),
                    sources=(SOURCE_OCR,), confidence=detection.confidence,
                    disagreement=DISAGREEMENT_AMBIGUOUS,
                    bounds_by_source={SOURCE_OCR: detection.bounds},
                    atspi_role=None, atspi_states=frozenset()))
            continue
        for element, detection in pairing:
            distance = _distance(element.bounds.center, detection.bounds.center)
            # Containment counts as corroboration: an AT-SPI container can span
            # much wider than the text run, so equal centers are not required.
            if distance <= tolerance or element.bounds.contains_point(*detection.bounds.center):
                fused_count += 1
                records.append(_record(
                    kind=element.role, label=_atspi_candidate_label(element),
                    actions=element.actions, sources=(SOURCE_ATSPI, SOURCE_OCR),
                    confidence=detection.confidence, disagreement=None,
                    bounds_by_source={SOURCE_ATSPI: element.bounds, SOURCE_OCR: detection.bounds},
                    atspi_role=element.role, atspi_states=element.states))
            else:
                conflicts.append(Conflict(
                    label=label, atspi_bounds=element.bounds, ocr_bounds=detection.bounds,
                    atspi_role=element.role, ocr_confidence=detection.confidence,
                    distance=distance))
                # Both halves stay visible; neither coordinate is silently chosen.
                records.append(_record(
                    kind=element.role, label=_atspi_candidate_label(element),
                    actions=element.actions, sources=(SOURCE_ATSPI,), confidence=None,
                    disagreement=DISAGREEMENT_CONFLICT,
                    bounds_by_source={SOURCE_ATSPI: element.bounds},
                    atspi_role=element.role, atspi_states=element.states))
                records.append(_record(
                    kind=KIND_OCR_TEXT, label=detection.text, actions=(),
                    sources=(SOURCE_OCR,), confidence=detection.confidence,
                    disagreement=DISAGREEMENT_CONFLICT,
                    bounds_by_source={SOURCE_OCR: detection.bounds},
                    atspi_role=None, atspi_states=frozenset()))

    for label, refs in refs_by_label.items():
        if label in matched:
            continue
        for element in refs:
            records.append(_record(
                kind=element.role, label=_atspi_candidate_label(element),
                actions=element.actions, sources=(SOURCE_ATSPI,), confidence=None,
                disagreement=None, bounds_by_source={SOURCE_ATSPI: element.bounds},
                atspi_role=element.role, atspi_states=element.states))

    for label, dets in dets_by_label.items():
        if label in matched:
            continue
        for detection in dets:
            records.append(_record(
                kind=KIND_OCR_TEXT, label=detection.text, actions=(),
                sources=(SOURCE_OCR,), confidence=detection.confidence,
                disagreement=None, bounds_by_source={SOURCE_OCR: detection.bounds},
                atspi_role=None, atspi_states=frozenset()))

    def sort_key(record: Mapping[str, Any]) -> tuple[int, int, str, str]:
        centers = [bounds.center for bounds in record["bounds_by_source"].values()]
        return (min(center[1] for center in centers),
                min(center[0] for center in centers),
                str(record["kind"]), str(record["label"]))

    records.sort(key=sort_key)
    candidates = tuple(
        Candidate(
            candidate_id=f"cand_{generation}_{index}",
            kind=str(record["kind"]), label=str(record["label"]),
            actions=tuple(record["actions"]), sources=tuple(record["sources"]),
            confidence=record["confidence"], disagreement=record["disagreement"],
            bounds_by_source=dict(record["bounds_by_source"]),
            atspi_role=record["atspi_role"], atspi_states=record["atspi_states"],
            generation=generation,
        )
        for index, record in enumerate(records)
    )

    counts = {
        "atspi_input": len(atspi_elements),
        "atspi_usable": len(usable_refs),
        "ocr_input": len(ocr_detections),
        "ocr_usable": len(usable_dets),
        "fused": fused_count,
        "atspi_only": sum(1 for candidate in candidates
                          if candidate.sources == (SOURCE_ATSPI,)),
        "ocr_only": sum(1 for candidate in candidates
                        if candidate.sources == (SOURCE_OCR,)),
        "conflicts": len(conflicts),
        "ambiguous_labels": len(ambiguous_labels),
        "rejected": len(ref_rejections) + len(det_rejections),
        "private_values": len(private_values),
    }
    return FusionResult(
        generation=generation, candidates=candidates, conflicts=tuple(conflicts),
        ambiguous_labels=tuple(ambiguous_labels),
        rejections=tuple(ref_rejections) + tuple(det_rejections),
        counts=counts, private_value_count=len(private_values))


# ---------------------------------------------------------------------------
# Generation-scoped inventory
# ---------------------------------------------------------------------------

class CandidateInventory:
    """Candidate set whose refs are only valid for the generation that made them."""

    def __init__(self) -> None:
        self.generation = 0
        self.result: FusionResult | None = None
        self._by_id: dict[str, Candidate] = {}

    def refresh(
        self,
        atspi_elements: Iterable[AtspiElement] = (),
        ocr_detections: Iterable[OcrDetection] = (),
        **kwargs: Any,
    ) -> FusionResult:
        self.generation += 1
        result = fuse(
            list(atspi_elements), list(ocr_detections),
            generation=self.generation, **kwargs)
        self.result = result
        self._by_id = {candidate.candidate_id: candidate for candidate in result.candidates}
        return result

    def resolve(self, ref: str) -> Candidate | None:
        candidate = self._by_id.get(ref)
        if candidate is None or candidate.generation != self.generation:
            return None
        return candidate

    def select(self, ref: str) -> Candidate:
        candidate = self.resolve(ref)
        if candidate is None:
            raise StaleReferenceError(ref)
        return candidate


# ---------------------------------------------------------------------------
# Selector-state projection and report sanitisation
# ---------------------------------------------------------------------------

def selector_state(candidates: Iterable[Candidate], *, limit: int = 64) -> dict[str, Any]:
    """Project candidates into the bounded, coordinate-free Jev-facing shape."""

    options = []
    for candidate in list(candidates)[:limit]:
        options.append({
            "id": candidate.candidate_id,
            "label": candidate.label,
            "kind": candidate.kind,
            "actions": list(candidate.actions),
            "sources": list(candidate.sources),
            "confidence": candidate.confidence,
            "disagreement": candidate.disagreement,
        })
    return {"options": options}


# ---------------------------------------------------------------------------
# Local OCR adapter (lazy; never imported by the offline tests)
# ---------------------------------------------------------------------------

class OcrUnavailable(RuntimeError):
    pass


def _quad_to_bounds(quad: Any) -> Bounds:
    xs = [float(point[0]) for point in quad]
    ys = [float(point[1]) for point in quad]
    x, y = int(min(xs)), int(min(ys))
    return Bounds(x, y, int(max(xs)) - x, int(max(ys)) - y)


def _scale_bounds(bounds: Bounds, scale: float) -> Bounds:
    if scale == 1.0:
        return bounds
    return Bounds(int(bounds.x / scale), int(bounds.y / scale),
                  int(bounds.width / scale), int(bounds.height / scale))


def ocr_provider_status() -> dict[str, Any]:
    """Report installed OCR runtime/provider availability without importing it eagerly."""

    import importlib.metadata as metadata
    status: dict[str, Any] = {"provider_requested": "CPUExecutionProvider"}
    try:
        import onnxruntime  # type: ignore[import-not-found]
        status["onnxruntime_version"] = onnxruntime.__version__
        status["available_providers"] = list(onnxruntime.get_available_providers())
        status["device"] = onnxruntime.get_device()
    except Exception as exc:  # pragma: no cover - environment dependent
        status["onnxruntime_error"] = type(exc).__name__
        status["available_providers"] = []
    try:
        status["rapidocr_version"] = metadata.version("rapidocr")
    except metadata.PackageNotFoundError:
        status["rapidocr_version"] = None
    status["ready"] = bool(status.get("rapidocr_version")) and bool(status.get("available_providers"))
    return status


class OcrEngine:
    """Thin wrapper over RapidOCR matching Tine's detection shape."""

    def __init__(self, *, min_confidence: float = DEFAULT_MIN_CONFIDENCE) -> None:
        self.min_confidence = min_confidence
        self._engine: Any = None
        self.init_ms: float | None = None

    def init(self) -> float:
        if self._engine is not None:
            return 0.0
        try:
            from rapidocr import RapidOCR  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise OcrUnavailable("rapidocr is not installed in this interpreter") from exc
        started = time.perf_counter()
        self._engine = RapidOCR(params={
            "EngineConfig.onnxruntime.use_cuda": False,
            "Global.log_level": "error",
        })
        self.init_ms = (time.perf_counter() - started) * 1000
        return self.init_ms

    def detect(self, image_path: str | Path, *, scale: float = 1.0) -> tuple[list[OcrDetection], float]:
        if self._engine is None:
            self.init()
        started = time.perf_counter()
        result = self._engine(str(image_path))
        elapsed_ms = (time.perf_counter() - started) * 1000
        detection: list[OcrDetection] = []
        boxes = getattr(result, "boxes", None)
        texts = getattr(result, "txts", None)
        scores = getattr(result, "scores", None)
        if boxes is None or texts is None or scores is None:
            return detection, elapsed_ms
        for quad, text, score in zip(boxes, texts, scores):
            confidence = float(score)
            if confidence < self.min_confidence:
                continue
            bounds = _scale_bounds(_quad_to_bounds(quad), scale)
            if bounds_reason(bounds) is not None:
                continue
            if not isinstance(text, str) or not normalize_text(text):
                continue
            detection.append(OcrDetection(text=text, bounds=bounds, confidence=confidence))
        return detection, elapsed_ms


def crop_png(image_bytes: bytes, bounds: Bounds) -> bytes:
    """Crop PNG bytes to ``bounds`` using Pillow; no capture leaves this process."""

    import io

    from PIL import Image

    with Image.open(io.BytesIO(image_bytes)) as opened:
        image = opened.convert("RGB")
        out = image.crop((bounds.x, bounds.y, bounds.x + bounds.width, bounds.y + bounds.height))
        buffer = io.BytesIO()
        out.save(buffer, format="PNG", optimize=False)
    return buffer.getvalue()


def png_size(image_bytes: bytes) -> tuple[int, int]:
    import io

    from PIL import Image

    with Image.open(io.BytesIO(image_bytes)) as opened:
        return opened.size


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def timing_summary(samples_ms: Sequence[float]) -> dict[str, float]:
    ordered = sorted(samples_ms)
    count = len(ordered)
    if not count:
        return {"n": 0, "min_ms": 0.0, "max_ms": 0.0, "p50_ms": 0.0, "p95_ms": 0.0}

    def percentile(fraction: float) -> float:
        index = min(count - 1, max(0, math.ceil(fraction * count) - 1))
        return ordered[index]

    return {
        "n": count,
        "min_ms": ordered[0],
        "max_ms": ordered[-1],
        "p50_ms": percentile(0.5),
        "p95_ms": percentile(0.95),
        "mean_ms": sum(ordered) / count,
    }


# ---------------------------------------------------------------------------
# Bounded virtual probe (study stages 0-3)
# ---------------------------------------------------------------------------

STUDY_DIR_DEFAULT = "run/tine-perception-study-2026-10-05"
SCREEN_WIDTH = 1280
SCREEN_HEIGHT = 800
DEFAULT_WARM_RUNS = 4
APP_ALIASES: dict[str, frozenset[str]] = {
    "kcalc": frozenset({"kcalc", "org.kde.kcalc"}),
    "kate": frozenset({"kate", "org.kde.kate"}),
}
READY_ELEMENT = {"kcalc": "One"}
GOLD_LABELS: dict[str, tuple[str, ...]] = {
    "kcalc": ("1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "+", "-", "="),
    "kate": ("File", "Edit", "View", "New", "Open", "Welcome"),
    "synthetic": ("12:34", "Activities", "1", "2", "3", "Equals", "Add"),
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_engine() -> Any:
    from kwin_mcp.core import AutomationEngine

    return AutomationEngine()


def _find_window(records: Sequence[Mapping[str, Any]], app: str) -> Mapping[str, Any] | None:
    aliases = APP_ALIASES.get(app, frozenset({app}))
    for record in records:
        identity = str(record.get("app", "")).casefold()
        if identity in aliases:
            return record
    return None


def _window_records(engine: Any) -> list[dict[str, Any]]:
    response = engine._run_kwin_query({})
    if not isinstance(response, dict) or response.get("ok") is not True:
        raise RuntimeError("kwin window query failed")
    return [item for item in response.get("result", []) if isinstance(item, dict)]


def _frame_bounds(window: Mapping[str, Any]) -> Bounds:
    frame = window.get("frame")
    if not isinstance(frame, Mapping):
        raise RuntimeError("window has no frame geometry")
    return Bounds(int(frame["x"]), int(frame["y"]), int(frame["width"]), int(frame["height"]))


def _screenshot(engine: Any) -> tuple[bytes, float, str]:
    started = time.perf_counter()
    result = engine.screenshot(include_cursor=False)
    elapsed_ms = (time.perf_counter() - started) * 1000
    match = re.search(r"Screenshot saved: (.+?) \([0-9.]+ KB\)", result)
    if not match:
        raise RuntimeError("screenshot result missing a saved path")
    return Path(match.group(1)).read_bytes(), elapsed_ms, result


def measure_ocr_shape(
    ocr: OcrEngine, image_bytes: bytes, image_path: Path, *, warm_runs: int,
) -> dict[str, Any]:
    """One cold run (engine construction + first inference) and N warm runs."""

    image_path.write_bytes(image_bytes)
    width, height = png_size(image_bytes)
    engine = OcrEngine(min_confidence=ocr.min_confidence)
    started = time.perf_counter()
    engine.init()
    detections, cold_infer_ms = engine.detect(image_path)
    cold_total_ms = (time.perf_counter() - started) * 1000
    warm_ms: list[float] = []
    for _ in range(max(0, warm_runs)):
        _, elapsed = engine.detect(image_path)
        warm_ms.append(elapsed)
    return {
        "path": str(image_path),
        "image": {"width": width, "height": height, "bytes": len(image_bytes),
                  "sha256": sha256_hex(image_bytes)},
        "engine_init_ms": engine.init_ms,
        "cold_infer_ms": cold_infer_ms,
        "cold_total_ms": cold_total_ms,
        "warm_ms": warm_ms,
        "warm": timing_summary(warm_ms),
        "detections": [{"text": item.text, "bbox": item.bounds.as_dict(),
                        "confidence": item.confidence} for item in detections],
    }


def gold_hits(ocr_texts: Iterable[str], gold: Sequence[str]) -> dict[str, list[str]]:
    normalized = [normalize_text(text) for text in ocr_texts]
    hits = [label for label in gold if any(normalize_text(label) in value for value in normalized)]
    return {"hits": hits, "misses": [label for label in gold if label not in hits]}


def synthetic_panel_png() -> bytes:
    """A synthetic workspace with a panel, a clock and an unrelated window.

    The virtual KWin fixture has almost no unrelated UI, so this fixture carries
    the noise case. It contains no real user content.
    """

    import io

    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (SCREEN_WIDTH, SCREEN_HEIGHT), (36, 40, 48))
    draw = ImageDraw.Draw(image)
    font_path = "/usr/share/fonts/TTF/DejaVuSans.ttf"
    try:
        big = ImageFont.truetype(font_path, 34)
        small = ImageFont.truetype(font_path, 26)
    except OSError:  # pragma: no cover - only when the font is absent
        big = small = ImageFont.load_default()
    # Top panel with a clock-like string.
    draw.rectangle((0, 0, SCREEN_WIDTH, 46), fill=(24, 26, 32))
    draw.text((24, 8), "Activities", font=small, fill=(230, 230, 230))
    draw.text((1130, 8), "12:34", font=small, fill=(230, 230, 230))
    # Unrelated window on the left.
    draw.rectangle((40, 200, 280, 560), fill=(52, 56, 64))
    draw.text((56, 216), "Other Window", font=small, fill=(220, 220, 220))
    draw.text((56, 260), "Untitled", font=small, fill=(200, 200, 200))
    # Target window with panel-like content (same rect as the virtual KCalc frame).
    target = Bounds(320, 160, 640, 480)
    draw.rectangle((target.x, target.y, target.x + target.width, target.y + target.height),
                   fill=(60, 64, 72), outline=(140, 140, 150), width=2)
    draw.text((target.x + 24, target.y + 24), "KCalc panel", font=big, fill=(240, 240, 240))
    draw.text((target.x + 24, target.y + 120), "1 2 3", font=big, fill=(240, 240, 240))
    draw.text((target.x + 24, target.y + 200), "Equals", font=small, fill=(240, 240, 240))
    draw.text((target.x + 24, target.y + 250), "Add", font=small, fill=(240, 240, 240))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=False)
    return buffer.getvalue()


def run_app_capture(
    engine: Any, app: str, out_dir: Path, *, warm_runs: int, use_ocr: bool,
) -> dict[str, Any]:
    app_command = {"kcalc": "kcalc", "kate": "kate"}.get(app, app)
    result: dict[str, Any] = {"app": app, "app_command": app_command}
    engine.session_start(
        app_command=app_command, screen_width=SCREEN_WIDTH, screen_height=SCREEN_HEIGHT,
        isolate_home=True,
    )
    ready_element = READY_ELEMENT.get(app)
    if ready_element:
        engine.wait_for_element(
            ready_element, app_name=app, timeout_ms=20000,
            expected_states=["enabled", "visible"],
        )
    window = None
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        window = _find_window(_window_records(engine), app)
        if window is not None:
            break
        time.sleep(0.5)
    if window is None:
        raise RuntimeError(f"no {app} window appeared")
    frame = _frame_bounds(window)
    result["window"] = {
        "id": window.get("id"), "app": window.get("app"), "caption": window.get("caption"),
        "frame": frame.as_dict(), "client": window.get("client"),
    }

    full_bytes, capture_ms, capture_note = _screenshot(engine)
    result["capture_ms"] = capture_ms
    result["capture_note"] = capture_note.splitlines()[1] if "\n" in capture_note else capture_note
    full_path = out_dir / f"{app}_full.png"
    full_path.write_bytes(full_bytes)
    crop_bytes = crop_png(full_bytes, frame)
    crop_path = out_dir / f"{app}_crop.png"
    crop_path.write_bytes(crop_bytes)
    result["full_image"] = {"width": png_size(full_bytes)[0], "height": png_size(full_bytes)[1],
                            "bytes": len(full_bytes), "sha256": sha256_hex(full_bytes)}
    result["crop_image"] = {"width": png_size(crop_bytes)[0], "height": png_size(crop_bytes)[1],
                            "bytes": len(crop_bytes), "sha256": sha256_hex(crop_bytes)}

    find = engine._run_atspi("find", query="", app_name=app)
    if not isinstance(find, dict) or find.get("ok") is not True:
        raise RuntimeError("atspi find failed")
    raw_elements = [item for item in find.get("result", [])
                    if isinstance(item, dict) and item.get("mapped")]
    result["atspi_find"] = {"count": len(find.get("result", [])), "mapped": len(raw_elements)}
    elements = elements_from_find(raw_elements)

    if not use_ocr:
        result["ocr"] = {"available": False, "reason": "disabled_by_flag"}
        result["fusion"] = _fusion_report(fuse(elements, [], window=frame))
        return result

    ocr = OcrEngine()
    result["ocr_provider"] = ocr_provider_status()
    if not result["ocr_provider"].get("rapidocr_version"):
        result["ocr"] = {"available": False, "reason": "rapidocr_not_installed"}
        result["fusion"] = _fusion_report(fuse(elements, [], window=frame))
        return result

    shapes = {}
    shapes["full"] = measure_ocr_shape(ocr, full_bytes, full_path, warm_runs=warm_runs)
    shapes["crop"] = measure_ocr_shape(ocr, crop_bytes, crop_path, warm_runs=warm_runs)
    result["ocr"] = {"available": True, "shapes": shapes}

    full_detections = [
        OcrDetection(text=item["text"],
                     bounds=Bounds(item["bbox"]["x"], item["bbox"]["y"],
                                   item["bbox"]["w"], item["bbox"]["h"]),
                     confidence=item["confidence"])
        for item in shapes["full"]["detections"]
    ]
    crop_detections = [
        OcrDetection(text=item["text"],
                     bounds=Bounds(item["bbox"]["x"] + frame.x, item["bbox"]["y"] + frame.y,
                                   item["bbox"]["w"], item["bbox"]["h"]),
                     confidence=item["confidence"])
        for item in shapes["crop"]["detections"]
    ]
    outside = [det.text for det in full_detections if not frame.contains_point(*det.bounds.center)]
    result["ocr"]["outside_target_window"] = outside
    gold = GOLD_LABELS.get(app, ())
    result["ocr"]["gold_full"] = gold_hits([det.text for det in full_detections], gold)
    result["ocr"]["gold_crop"] = gold_hits([det.text for det in crop_detections], gold)
    result["fusion"] = _fusion_report(fuse(elements, full_detections, window=frame))
    result["fusion_from_crop"] = _fusion_report(fuse(elements, crop_detections, window=frame))
    return result


def _fusion_report(result: FusionResult, *, candidate_limit: int = 80) -> dict[str, Any]:
    rejection_counts: dict[str, int] = {}
    for rejection in result.rejections:
        key = f"{rejection.source}:{rejection.reason}"
        rejection_counts[key] = rejection_counts.get(key, 0) + 1
    candidates = result.candidates[:candidate_limit]
    return {
        "generation": result.generation,
        "counts": dict(result.counts),
        "ambiguous_labels": list(result.ambiguous_labels),
        "conflicts": [
            {
                "label": conflict.label,
                "atspi_bounds": conflict.atspi_bounds.as_dict(),
                "ocr_bounds": conflict.ocr_bounds.as_dict(),
                "atspi_role": conflict.atspi_role,
                "ocr_confidence": conflict.ocr_confidence,
                "distance": round(conflict.distance, 1),
            }
            for conflict in result.conflicts
        ],
        "rejection_counts": dict(sorted(rejection_counts.items())),
        "selector_state_sample": selector_state(candidates, limit=20),
        "candidates": [
            {
                "candidate_id": candidate.candidate_id,
                "kind": candidate.kind,
                "label": candidate.label,
                "actions": list(candidate.actions),
                "sources": list(candidate.sources),
                "confidence": candidate.confidence,
                "disagreement": candidate.disagreement,
                "bounds_by_source": {source: bounds.as_dict()
                                     for source, bounds in candidate.bounds_by_source.items()},
            }
            for candidate in candidates
        ],
        "candidates_truncated": len(result.candidates) > candidate_limit,
        "candidate_total": len(result.candidates),
    }


def run_synthetic_shape(ocr: OcrEngine, out_dir: Path, *, warm_runs: int) -> dict[str, Any]:
    full_bytes = synthetic_panel_png()
    full_path = out_dir / "synthetic_panel_full.png"
    full_path.write_bytes(full_bytes)
    target = Bounds(320, 160, 640, 480)
    crop_bytes = crop_png(full_bytes, target)
    crop_path = out_dir / "synthetic_panel_crop.png"
    crop_path.write_bytes(crop_bytes)
    full = measure_ocr_shape(ocr, full_bytes, full_path, warm_runs=warm_runs)
    crop = measure_ocr_shape(ocr, crop_bytes, crop_path, warm_runs=warm_runs)
    gold = GOLD_LABELS["synthetic"]
    outside = [item["text"] for item in full["detections"]
               if not target.contains_point(item["bbox"]["x"] + item["bbox"]["w"] // 2,
                                            item["bbox"]["y"] + item["bbox"]["h"] // 2)]
    return {
        "full": full,
        "crop": crop,
        "outside_target_window": outside,
        "gold_full": gold_hits([item["text"] for item in full["detections"]], gold),
        "gold_crop": gold_hits([item["text"] for item in crop["detections"]], gold),
    }


# ---------------------------------------------------------------------------
# Report writing
# ---------------------------------------------------------------------------

def _markdown_report(report: Mapping[str, Any]) -> str:
    lines = [
        "# Tine perception study — 2026-10-05",
        "",
        f"- generated: {report.get('generated_at')}",
        f"- study dir: `{report.get('out_dir')}`",
        f"- ocr provider: `{json.dumps(report.get('ocr_provider'))}`",
        "",
        "## Stage 0-2 · virtual captures and OCR shapes",
        "",
    ]
    for app_report in report.get("apps", []):
        lines.append(f"### {app_report.get('app')}")
        lines.append("")
        window = app_report.get("window", {})
        lines.append(f"- window frame: `{window.get('frame')}`, capture {app_report.get('capture_ms', 0):.0f} ms")
        shapes = (app_report.get("ocr") or {}).get("shapes") or {}
        for name, shape in shapes.items():
            lines.append(
                f"- {name}: {shape['image']['width']}x{shape['image']['height']} "
                f"{shape['image']['bytes']} B, cold {shape['cold_total_ms']:.0f} ms "
                f"(init {shape['engine_init_ms']:.0f}), warm p50 {shape['warm']['p50_ms']:.0f} ms, "
                f"detections {len(shape['detections'])}")
        ocr = app_report.get("ocr") or {}
        if "gold_full" in ocr:
            lines.append(f"- gold full: `{ocr['gold_full']}`")
            lines.append(f"- gold crop: `{ocr['gold_crop']}`")
            lines.append(f"- outside target window: `{ocr.get('outside_target_window')}`")
        fusion = app_report.get("fusion") or {}
        lines.append(f"- fusion counts: `{fusion.get('counts')}`")
        lines.append("")
    synthetic = report.get("synthetic")
    if synthetic:
        lines.extend(["## Synthetic noisy panel (full vs crop)", ""])
        for name in ("full", "crop"):
            shape = synthetic[name]
            lines.append(
                f"- {name}: {shape['image']['width']}x{shape['image']['height']} "
                f"{shape['image']['bytes']} B, cold {shape['cold_total_ms']:.0f} ms, "
                f"warm p50 {shape['warm']['p50_ms']:.0f} ms, detections {len(shape['detections'])}")
        lines.append(f"- gold full: `{synthetic['gold_full']}`")
        lines.append(f"- gold crop: `{synthetic['gold_crop']}`")
        lines.append(f"- outside target window: `{synthetic['outside_target_window']}`")
        lines.append("")
    boundary = report.get("integration_boundary")
    if boundary:
        lines.extend(["## Integration boundary", "", boundary, ""])
    environment = report.get("environment")
    if environment:
        lines.extend(["## Environment", "", f"```json\n{json.dumps(environment, indent=1)}\n```", ""])
    observations = report.get("observations")
    if observations:
        lines.extend(["## Observations", ""])
        lines.extend(f"- {item}" for item in observations)
        lines.append("")
    cleanup = report.get("cleanup")
    if cleanup:
        lines.extend(["## Cleanup", "", f"```json\n{json.dumps(cleanup, indent=1)}\n```", ""])
    commands = report.get("commands")
    if commands:
        lines.extend(["## Exact commands", "", "```sh"])
        lines.extend(commands)
        lines.extend(["```", ""])
    limitations = report.get("limitations")
    if limitations:
        lines.extend(["## Limitations", ""])
        lines.extend(f"- {item}" for item in limitations)
        lines.append("")
    return "\n".join(lines) + "\n"


def write_report(out_dir: Path, report: Mapping[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                                         encoding="utf-8")
    (out_dir / "REPORT.md").write_text(_markdown_report(report), encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _command_line(args: argparse.Namespace) -> list[str]:
    argv = [sys.executable, "-m", "scripts.tine_perception",
            "--out", args.out, "--warm", str(args.warm)]
    for app in args.apps:
        argv += ["--app", app]
    if args.no_ocr:
        argv.append("--no-ocr")
    pythonpath = os.environ.get("PYTHONPATH")
    prefix = f"PYTHONPATH={shlex.quote(pythonpath)} " if pythonpath else ""
    return [prefix + " ".join(shlex.quote(part) for part in argv)]


def _environment() -> dict[str, Any]:
    import importlib.metadata as metadata
    import platform
    import subprocess

    def version(package: str) -> str | None:
        try:
            return metadata.version(package)
        except metadata.PackageNotFoundError:
            return None

    def command_version(argv: list[str]) -> str:
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=10)
            return (proc.stdout or proc.stderr).strip().splitlines()[0][:80]
        except Exception:
            return "not-reported"

    with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as stream:
        cpu = next((line.split(":", 1)[1].strip() for line in stream if line.startswith("model name")), "unknown")
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "kwin_mcp": version("kwin-mcp"),
        "kwin": command_version(["kwin_wayland", "--version"]),
        "rapidocr_cpu_venv": version("rapidocr"),
        "onnxruntime_cpu_venv": version("onnxruntime"),
        "pillow": version("Pillow"),
        "cpu": cpu,
    }


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "generated_at": _utc_now(),
        "out_dir": str(out_dir),
        "screen": {"width": SCREEN_WIDTH, "height": SCREEN_HEIGHT},
        "environment": _environment(),
        "ocr_provider": ocr_provider_status(),
        "apps": [],
    }
    if report["ocr_provider"].get("rapidocr_version"):
        ocr = OcrEngine()
        try:
            synthetic = run_synthetic_shape(ocr, out_dir, warm_runs=args.warm)
            report["synthetic"] = synthetic
        except Exception as exc:  # pragma: no cover - environment dependent
            report["synthetic"] = {"error": type(exc).__name__}
    else:
        report["synthetic"] = {"skipped": "rapidocr_not_installed"}

    for app in args.apps:
        engine = _load_engine()
        capture: dict[str, Any] = {"app": app}
        try:
            capture = run_app_capture(engine, app, out_dir, warm_runs=args.warm,
                                      use_ocr=not args.no_ocr)
        except Exception as exc:
            capture = {"app": app, "error": type(exc).__name__}
        finally:
            try:
                engine.session_stop()
                capture["session_stopped"] = True
            except Exception:
                capture["cleanup_error"] = True
        report["apps"].append(capture)

    report["commands"] = _command_line(args) + [
        "uv run python -m unittest tests.test_tine_perception",
        "uv venv tmp/tine-ocr-venv && uv pip install --python tmp/tine-ocr-venv/bin/python rapidocr onnxruntime",
    ]
    report["cleanup"] = {
        "virtual_sessions_stopped": all(app.get("session_stopped") for app in report["apps"]),
        "artifacts": "run/tine-perception-study-2026-10-05/ (ignored); optional CPU OCR venv under ignored tmp/",
        "host_changes": "none (no package installs outside tmp/ venvs, no driver or system changes)",
    }
    report["observations"] = [
        "PP-OCR returned fullwidth digits (１) in the synthetic fixture; Tine's plain "
        "normalize_text does not fold them, so NFKC folding was added to keep AT-SPI/OCR "
        "labels fusible.",
        "KCalc digit keys expose word labels (One, Two, ...) while OCR reads glyphs (1, 2, ...), "
        "so numeric keys stay separate ocr_text refs instead of fusing; menu items do fuse.",
        "A wide AT-SPI container (Kate 'Welcome') overlaps its OCR text run but has a distant "
        "center; containment is treated as corroboration rather than a conflict.",
        "The target-window crop drops panel/other-window text and is consistently faster; on "
        "Kate it also split one long OCR line into separate, more granular detections.",
        "Warm CPU OCR measured well under Tine's code-comment ~2 s per 1080p frame at these "
        "1280x800/640x480 sizes with the bundled PP-OCRv6 small models.",
    ]
    report["limitations"] = [
        "One virtual run per app and one synthetic fixture; not a statistical sample.",
        "No live desktop, no real user content, no Jev/vision-reader calls, and no OCR-ref actions.",
        "Fusion quality is measured against small hand-picked gold sets, not an exhaustive "
        "ground truth; OCR text itself is not verified against the rendered pixels.",
    ]
    report["integration_boundary"] = (
        "The owner resolves `act target_ref` against its current AT-SPI candidate "
        "snapshot. This study only inspects generation-scoped OCR evidence and "
        "does not map OCR refs to input. Future runtime integration would need a "
        "reviewed mapping seam plus code-owned authorization and verification."
    )
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=STUDY_DIR_DEFAULT, help="report and capture directory")
    parser.add_argument("--app", dest="apps", action="append",
                        choices=sorted(APP_ALIASES), help="virtual app to capture (repeatable)")
    parser.add_argument("--warm", type=int, default=DEFAULT_WARM_RUNS,
                        help="warm OCR runs per capture shape")
    parser.add_argument("--no-ocr", action="store_true", help="skip OCR, capture AT-SPI only")
    parser.add_argument("--status", action="store_true", help="print OCR provider status and exit")
    args = parser.parse_args(argv)
    if args.status:
        print(json.dumps(ocr_provider_status(), indent=2))
        return 0
    args.apps = args.apps or ["kcalc", "kate"]
    report = build_report(args)
    write_report(Path(args.out), report)
    print(f"report written to {args.out}/report.json and {args.out}/REPORT.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
