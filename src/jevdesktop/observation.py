"""In-memory, bounded screenshot observations for virtual KWin sessions."""

from __future__ import annotations

import hashlib
import math
import re
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
import io
import copy

from jevdesktop.vision_reader import Reader, ReaderResult


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    @classmethod
    def from_value(cls, value: "Rect | dict[str, int] | tuple[int, int, int, int]") -> "Rect":
        if isinstance(value, Rect):
            return value
        if isinstance(value, dict):
            try:
                return cls(value["x"], value["y"], value["width"], value["height"])
            except (KeyError, TypeError) as exc:
                raise ObservationError("invalid_crop", "crop must have integer x, y, width and height") from exc
        if isinstance(value, tuple) and len(value) == 4:
            return cls(*value)
        raise ObservationError("invalid_crop", "crop must have integer x, y, width and height")


@dataclass(frozen=True)
class CaptureRef:
    capture_id: str
    captured_at: str
    session_id: str
    window_id: str
    app: str
    caption: str = field(repr=False)
    width: int
    height: int
    source_width: int
    source_height: int
    image_sha256: str
    source_sha256: str
    scope: str
    mapping: Mapping[str, Any] = field(repr=False)


@dataclass(frozen=True)
class Observation:
    capture: CaptureRef
    metadata: dict[str, Any]
    image: bytes | None = field(default=None, repr=False)
    data: dict[str, Any] | None = field(default=None, repr=False)
    reader: ReaderResult | None = None
    errors: tuple[dict[str, str], ...] = field(default_factory=tuple)


class ObservationError(RuntimeError):
    """Safe, structured failure that never includes screenshot or provider data."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass
class _StoredCapture:
    ref: CaptureRef
    image: bytes = field(repr=False)
    expires_at: float


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return copy.deepcopy(value)


class CaptureStore:
    """TTL and capacity bounded store; each entry owns immutable image bytes."""

    def __init__(self, *, ttl_seconds: float = 300.0, max_items: int = 32,
                 max_capture_bytes: int = 8 * 1024 * 1024, max_total_bytes: int = 32 * 1024 * 1024,
                 clock=time.monotonic):
        if (not isinstance(ttl_seconds, (int, float)) or not math.isfinite(ttl_seconds)
                or ttl_seconds <= 0 or type(max_items) is not int or max_items < 1
                or type(max_capture_bytes) is not int or max_capture_bytes < 1
                or type(max_total_bytes) is not int or max_total_bytes < max_capture_bytes):
            raise ValueError("ttl_seconds and max_items must be positive")
        self.ttl_seconds = float(ttl_seconds)
        self.max_items = int(max_items)
        self.max_capture_bytes = int(max_capture_bytes)
        self.max_total_bytes = int(max_total_bytes)
        self._clock = clock
        self._items: OrderedDict[str, _StoredCapture] = OrderedDict()

    def _prune(self) -> None:
        now = self._clock()
        expired = [capture_id for capture_id, item in self._items.items() if item.expires_at <= now]
        for capture_id in expired:
            del self._items[capture_id]
        while len(self._items) > self.max_items or sum(len(item.image) for item in self._items.values()) > self.max_total_bytes:
            self._items.popitem(last=False)

    def put(self, ref: CaptureRef, image: bytes) -> None:
        if not isinstance(image, bytes):
            raise TypeError("capture bytes must be immutable bytes")
        if len(image) > self.max_capture_bytes:
            raise ObservationError("capture_too_large", "captured image exceeds the in-memory size limit")
        if hashlib.sha256(image).hexdigest() != ref.image_sha256:
            raise ObservationError("capture_hash_mismatch", "captured image hash does not match its reference")
        ref = CaptureRef(**{**ref.__dict__, "mapping": _freeze(_thaw(ref.mapping))})
        self._prune()
        self._items[ref.capture_id] = _StoredCapture(ref, image, self._clock() + self.ttl_seconds)
        self._items.move_to_end(ref.capture_id)
        self._prune()

    def get(self, capture_id: str) -> _StoredCapture:
        self._prune()
        item = self._items.get(capture_id)
        if item is None:
            raise ObservationError("capture_expired_or_unknown", "capture is unavailable or has expired")
        if hashlib.sha256(item.image).hexdigest() != item.ref.image_sha256:
            del self._items[capture_id]
            raise ObservationError("capture_hash_mismatch", "stored capture failed its integrity check")
        self._items.move_to_end(capture_id)
        return item

    def clear(self) -> None:
        self._items.clear()


class ObservationAdapter:
    """Capture one window from a virtual KWin session and observe that frame."""

    def __init__(self, engine: Any, session_id: str, *, store: CaptureStore | None = None,
                 reader: Reader | None = None, allowed_apps: set[str] | frozenset[str] | None = None):
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id is required")
        self.engine = engine
        self.session_id = session_id
        self.store = store or CaptureStore()
        self.reader = reader
        self.allowed_apps = frozenset(value.casefold() for value in (allowed_apps or set()))

    @staticmethod
    def _read_windows(engine: Any) -> list[dict[str, Any]]:
        query = getattr(engine, "_run_kwin_query", None)
        if not callable(query):
            raise ObservationError("window_query_unavailable", "KWin window query is unavailable")
        try:
            response = query({})
        except Exception as exc:
            raise ObservationError("window_query_failed", "KWin window query failed") from exc
        if not isinstance(response, dict) or response.get("ok") is not True or not isinstance(response.get("result"), list):
            raise ObservationError("window_query_failed", "KWin window query returned an invalid result")
        return [item for item in response["result"] if isinstance(item, dict)]

    @staticmethod
    def _app_matches(actual: Any, requested: str) -> bool:
        if not isinstance(actual, str):
            return False
        actual_norm = actual.casefold()
        requested_norm = requested.casefold()
        aliases = {"org.kde.kate": "kate", "org.mozilla.firefox": "firefox", "org.kde.kcalc": "kcalc"}
        return actual_norm == requested_norm or aliases.get(actual_norm) == requested_norm

    def _resolve_window(self, app: str, title: str | None) -> dict[str, Any]:
        if not isinstance(app, str) or not app.strip():
            raise ObservationError("invalid_app", "app identity is required")
        if app.casefold() not in self.allowed_apps:
            raise ObservationError("app_not_allowed", "app is not in the configured allowlist")
        windows = self._read_windows(self.engine)
        matches = [w for w in windows if self._app_matches(w.get("app"), app)
                   and isinstance(w.get("caption"), str)
                   and (title is None or w["caption"] == title or w["caption"].startswith(title))]
        if len(matches) != 1:
            code = "window_not_found" if not matches else "window_ambiguous"
            raise ObservationError(code, "expected exactly one matching application window")
        window = matches[0]
        if not isinstance(window.get("id"), str) or not window["id"]:
            raise ObservationError("invalid_window_identity", "matched window has no stable id")
        frame = window.get("frame")
        if not isinstance(frame, dict) or any(type(frame.get(k)) is not int for k in ("x", "y", "width", "height")):
            raise ObservationError("invalid_window_geometry", "matched window frame geometry is invalid")
        if frame["width"] <= 0 or frame["height"] <= 0:
            raise ObservationError("invalid_window_geometry", "matched window frame geometry is empty")
        return window

    def _screenshot(self) -> tuple[Path, str]:
        try:
            result = self.engine.screenshot(include_cursor=False)
        except Exception as exc:
            raise ObservationError("capture_failed", "KWin screenshot capture failed") from exc
        if not isinstance(result, str):
            raise ObservationError("capture_result_invalid", "KWin screenshot result is invalid")
        match = re.search(r"Screenshot saved: (.+?) \([0-9.]+ KB\)", result)
        mapping_line = next((line for line in result.splitlines() if line.startswith("Coordinate space:")), None)
        if not match or mapping_line is None:
            raise ObservationError("mapping_unavailable", "screenshot path or coordinate mapping is unavailable")
        path = Path(match.group(1))
        if not path.is_file():
            raise ObservationError("capture_file_missing", "KWin screenshot file is unavailable")
        return path, mapping_line

    @staticmethod
    def _rect(value: Any, width: int, height: int) -> Rect:
        rect = Rect.from_value(value)
        if any(type(n) is not int for n in (rect.x, rect.y, rect.width, rect.height)):
            raise ObservationError("invalid_crop", "crop coordinates must be integers")
        if rect.x < 0 or rect.y < 0 or rect.width <= 0 or rect.height <= 0 or rect.x + rect.width > width or rect.y + rect.height > height:
            raise ObservationError("crop_out_of_bounds", "requested crop falls outside the captured screenshot")
        return rect

    def capture(self, app: str, *, title: str | None = None, scope: str = "app",
                crop: Rect | dict[str, int] | tuple[int, int, int, int] | None = None) -> CaptureRef:
        if scope not in {"app", "full", "crop"}:
            raise ObservationError("unsupported_scope", "scope must be app, full, or crop")
        if (scope == "crop") != (crop is not None):
            raise ObservationError("explicit_crop_required", "crop scope requires an explicit rectangle; other scopes do not accept one")
        try:
            from PIL import Image
        except ImportError as exc:  # pragma: no cover - depends on caller install
            raise ObservationError("pillow_unavailable", "Pillow is required for mapped capture") from exc
        window = self._resolve_window(app, title)
        path, mapping_text = self._screenshot()
        mapping_match = re.search(r"Coordinate space: logical; origin \((-?\d+), (-?\d+)\); size (\d+)x(\d+); .*?coverage (\w+)", mapping_text)
        try:
            source = path.read_bytes()
            with Image.open(io.BytesIO(source)) as opened:
                image = opened.convert("RGB")
        except Exception as exc:
            raise ObservationError("capture_decode_failed", "screenshot image could not be decoded") from exc
        if not mapping_match:
            raise ObservationError("unsupported_mapping", "screenshot mapping format is unsupported")
        ox, oy, mapped_w, mapped_h = (int(mapping_match.group(i)) for i in range(1, 5))
        width, height = image.size
        if (ox, oy) != (0, 0) or (mapped_w, mapped_h) != (width, height) or mapping_match.group(5) != "full":
            raise ObservationError("unsupported_mapping", "only complete virtual 1:1 screenshot mapping is supported")
        current = self._resolve_window(app, title)
        if (current.get("id"), current.get("app"), current.get("caption"), current.get("frame")) != (
                window.get("id"), window.get("app"), window.get("caption"), window.get("frame")):
            raise ObservationError("window_changed_during_capture", "application window identity or geometry changed during capture")
        frame = self._rect(window["frame"], width, height)
        if scope == "full":
            selected = Rect(0, 0, width, height)
        elif scope == "app":
            selected = frame
        else:
            selected = self._rect(crop, width, height)
        out = image.crop((selected.x, selected.y, selected.x + selected.width, selected.y + selected.height))
        buffer = io.BytesIO()
        out.save(buffer, format="PNG", optimize=False)
        image_bytes = buffer.getvalue()
        ref = CaptureRef(
            capture_id=uuid.uuid4().hex,
            captured_at=_utc_now(),
            session_id=self.session_id,
            window_id=window["id"],
            app=window["app"],
            caption=window["caption"],
            width=selected.width,
            height=selected.height,
            source_width=width,
            source_height=height,
            image_sha256=hashlib.sha256(image_bytes).hexdigest(),
            source_sha256=hashlib.sha256(source).hexdigest(),
            scope=scope,
            mapping=_freeze({"origin": {"x": 0, "y": 0}, "source_dimensions": {"width": width, "height": height},
                     "selected_rect": {"x": selected.x, "y": selected.y, "width": selected.width, "height": selected.height},
                     "coordinate_basis": mapping_text}),
        )
        self.store.put(ref, image_bytes)
        return ref

    def capabilities(self) -> dict[str, Any]:
        return {"capture_scopes": ["app", "full", "crop"], "crop_requires_explicit_rect": True,
                "virtual_mapping": "complete_1_to_1_only", "allowed_apps": sorted(self.allowed_apps),
                "reader": self.reader.capabilities() if self.reader else None}

    @staticmethod
    def _metadata(ref: CaptureRef) -> dict[str, Any]:
        return {"capture_id": ref.capture_id, "captured_at": ref.captured_at, "session_id": ref.session_id,
                "window": {"id": ref.window_id, "app": ref.app},
                "dimensions": {"width": ref.width, "height": ref.height}, "source_dimensions": {"width": ref.source_width, "height": ref.source_height},
                "image_sha256": ref.image_sha256, "source_sha256": ref.source_sha256, "scope": ref.scope, "mapping": _thaw(ref.mapping)}

    def observe(self, capture_id: str | CaptureRef, *, mode: str = "metadata",
                questions: list[dict[str, Any]] | None = None) -> Observation:
        if mode not in {"metadata", "image", "data", "both"}:
            raise ObservationError("unsupported_mode", "mode must be metadata, image, data, or both")
        key = capture_id.capture_id if isinstance(capture_id, CaptureRef) else capture_id
        if not isinstance(key, str):
            raise ObservationError("invalid_capture_id", "capture id must be a string")
        stored = self.store.get(key)
        if stored.ref.session_id != self.session_id:
            raise ObservationError("capture_session_mismatch", "capture belongs to another session")
        wants_data = mode in {"data", "both"}
        if wants_data and (self.reader is None or not questions):
            raise ObservationError("reader_or_questions_required", "data observation requires a configured reader and questions")
        if wants_data and self.reader is not None and questions:
            try:
                reader_result = self.reader.interpret(stored.image, questions)
            except Exception:
                reader_result = ReaderResult("error", error="reader_failed")
        else:
            reader_result = None
        errors: tuple[dict[str, str], ...] = ()
        if reader_result is not None and reader_result.status not in {"ok", "uncertain"}:
            errors = ({"code": reader_result.error or "reader_error", "message": "vision reader could not produce a valid observation"},)
        data = reader_result.data if reader_result is not None else None
        return Observation(stored.ref, self._metadata(stored.ref), stored.image if mode in {"image", "both"} else None,
                           data, reader_result, errors)
