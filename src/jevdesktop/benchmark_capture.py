#!/usr/bin/env python3
"""Capture deterministic virtual desktop fixtures for the vision benchmark.

This module captures only local, code-owned synthetic fixtures. It does not call
any vision model. Install/run with ``uv run --with kwin-mcp==0.10.0
--with Pillow python -m jevdesktop.benchmark_capture [output-dir]``.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import shlex
import tempfile
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from jevdesktop import paths

ROOT = paths.repo_root() or paths.PACKAGE_DIR
FIXTURES = paths.fixtures_dir()
SCREEN = (1280, 800)
POEM = "Blue morning opens\nThe river keeps its light\nA bird lifts the day"
APP_ALLOWLIST = {"kate", "firefox"}
MAX_CASES = 5

CASES: tuple[dict[str, Any], ...] = (
    {
        "case_id": "kate_poem",
        "app": "kate",
        "command": "kate --new",
        "task": "Read the three-line poem visible in the Kate editor.",
        "questions": [{"field": "poem", "description": "Transcribe the complete three-line poem exactly, preserving line breaks.", "type": "string"}],
        "expected_facts": {"poem": POEM},
        "verify": "poem",
    },
    {
        "case_id": "web_table_small_text_disabled",
        "app": "firefox",
        "title": "Jev benchmark: table and disabled control",
        "fixture": "table.html",
        "task": "Read the garden table, small archive text, and save-button state.",
        "questions": [
            {"field": "bean_height", "description": "What height is listed for Bean in the Garden observations table? Return the value and unit.", "type": "string"},
            {"field": "archive_code", "description": "What is the archive code printed in the small text below the table?", "type": "string"},
            {"field": "save_disabled", "description": "Is the Save observations button disabled?", "type": "boolean"},
        ],
        "expected_facts": {"bean_height": "42 cm", "archive_code": "G-204", "save_disabled": True},
        "verify": "garden",
    },
    {
        "case_id": "web_loading",
        "app": "firefox",
        "title": "Jev benchmark: loading",
        "fixture": "loading.html",
        "task": "Identify whether the weather archive has finished loading.",
        "questions": [
            {"field": "loading", "description": "Does the page show that forecast data is still loading?", "type": "boolean"},
            {"field": "ready", "description": "Does the page show that the forecast is ready?", "type": "boolean"},
            {"field": "error_visible", "description": "Is an error banner visible?", "type": "boolean"},
        ],
        "expected_facts": {"loading": True, "ready": False, "error_visible": False},
        "verify": "Loading forecast data",
    },
    {
        "case_id": "web_ready",
        "app": "firefox",
        "title": "Jev benchmark: ready",
        "fixture": "ready.html",
        "task": "Read the ready weather archive summary.",
        "questions": [
            {"field": "loading", "description": "Does the page show that forecast data is still loading?", "type": "boolean"},
            {"field": "ready", "description": "Does the page say the forecast is ready?", "type": "boolean"},
            {"field": "error_visible", "description": "Is an error banner visible?", "type": "boolean"},
            {"field": "station", "description": "What station is listed?", "type": "string"},
            {"field": "wind", "description": "What wind is reported, including direction?", "type": "string"},
        ],
        "expected_facts": {"loading": False, "ready": True, "error_visible": False, "station": "North Pier", "wind": "12 km/h west"},
        "verify": "Forecast ready",
    },
    {
        "case_id": "web_error_banner",
        "app": "firefox",
        "title": "Jev benchmark: error",
        "fixture": "error.html",
        "task": "Read the weather archive error banner and reference.",
        "questions": [
            {"field": "error_visible", "description": "Is an error banner visible?", "type": "boolean"},
            {"field": "error_message", "description": "Transcribe the error banner sentences before the Reference code.", "type": "string"},
            {"field": "reference", "description": "What reference code appears in the error banner?", "type": "string"},
        ],
        "expected_facts": {"error_visible": True, "error_message": "Unable to load forecast. Retry later.", "reference": "E-17"},
        "verify": "Unable to load forecast",
    },
)


class CaptureError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _version(command: list[str]) -> str:
    try:
        proc = subprocess.run(command, text=True, capture_output=True, timeout=5, check=False)
        value = (proc.stdout or proc.stderr).strip().splitlines()
        return value[0][:240] if value else f"exit-{proc.returncode}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"unavailable:{type(exc).__name__}"


def _atspi_version() -> str:
    for name in ("pyatspi", "pyatspi2", "atspi"):
        try:
            return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            continue
    try:
        import gi  # type: ignore[import-not-found]
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi  # type: ignore[import-not-found]
        return str(Atspi.get_version())
    except Exception:
        return "not-reported"


def _versions(app: str) -> dict[str, str]:
    kwin_mcp_version = importlib.metadata.version("kwin-mcp")

    app_cmd = ["kate", "--version"] if app == "kate" else ["firefox", "--version"]
    return {
        "kwin_mcp": str(kwin_mcp_version),
        "kwin": _version(["kwin_wayland", "--version"]),
        "atspi": _atspi_version(),
        app: _version(app_cmd),
    }


def _window_records(engine: Any) -> list[dict[str, Any]]:
    # Request the raw KWin registry, then retain the exact identity and caption.
    response = engine._run_kwin_query({})
    if not isinstance(response, dict) or response.get("ok") is not True:
        raise CaptureError(f"raw KWin window query failed: {response!r}")
    result = response.get("result")
    if not isinstance(result, list):
        raise CaptureError("raw KWin query did not return a window list")
    return [item for item in result if isinstance(item, dict)]


def _resolve_window(engine: Any, app: str, title: str | None) -> dict[str, Any]:
    windows = _window_records(engine)
    matches = [
        w for w in windows
        if isinstance(w.get("app"), str)
        and isinstance(w.get("caption"), str)
        and ((app == "kate" and w["app"].lower() in {"kate", "org.kde.kate"})
             or (app == "firefox" and w["app"].lower() in {"firefox", "org.mozilla.firefox"}))
        and (title is None or w["caption"].startswith(title))
    ]
    if len(matches) != 1:
        identities = [{"app": w.get("app"), "caption": w.get("caption"), "id": w.get("id")} for w in windows]
        raise CaptureError(f"expected one exact {app} window match, found {len(matches)}; KWin records={identities!r}")
    window = matches[0]
    if not isinstance(window.get("id"), str) or not window["id"]:
        raise CaptureError("resolved KWin window has no string id")
    frame, client = window.get("frame"), window.get("client")
    for label, rect in (("frame", frame), ("client", client)):
        if not isinstance(rect, dict) or not all(isinstance(rect.get(k), int) for k in ("x", "y", "width", "height")):
            raise CaptureError(f"KWin {label} geometry is malformed")
        if rect["width"] <= 0 or rect["height"] <= 0:
            raise CaptureError(f"KWin {label} geometry is empty")
    return window


def _screenshot_path(engine: Any) -> tuple[Path, float, str]:
    started = time.perf_counter()
    result = engine.screenshot(include_cursor=False)
    elapsed_ms = (time.perf_counter() - started) * 1000
    # kwin-mcp returns an internal path; copy its bytes to the caller-owned output.
    match = re.search(r"Screenshot saved: (.+?) \([0-9.]+ KB\)", result)
    if not match:
        raise CaptureError("kwin-mcp screenshot result did not contain a saved path")
    path = Path(match.group(1))
    if not path.is_file():
        raise CaptureError("kwin-mcp screenshot path does not exist")
    return path, elapsed_ms, result.splitlines()[-1]


def _rect_in_bounds(rect: dict[str, int], width: int, height: int, label: str) -> None:
    x, y, w, h = (rect[k] for k in ("x", "y", "width", "height"))
    if x < 0 or y < 0 or x + w > width or y + h > height:
        raise CaptureError(f"KWin {label} rectangle {x},{y},{w},{h} falls outside screenshot {width}x{height}")


def _save_images(engine: Any, output_dir: Path, case_id: str, app: str, window: dict[str, Any]) -> tuple[list[dict[str, Any]], float]:
    try:
        from PIL import Image
    except ImportError as exc:
        raise CaptureError("Pillow is required: uv run --with Pillow") from exc

    source, capture_ms, mapping_text = _screenshot_path(engine)
    captured_at = _utc_now()
    with Image.open(source) as opened:
        image = opened.convert("RGB")
    width, height = image.size
    if image.size != SCREEN:
        raise CaptureError(f"screenshot dimensions {image.size} differ from fixed session size {SCREEN}")

    frame, client = window["frame"], window["client"]
    # kwin-mcp screenshot mapping is parsed and required to establish screen origin.
    # Fixed virtual sessions must report an origin of (0,0) and dimensions matching
    # the captured source before raw KWin global coordinates can be used as pixels.
    mapping_match = re.search(
        r"Coordinate space: logical; origin \((-?\d+), (-?\d+)\); size (\d+)x(\d+); .*?coverage (\w+)",
        mapping_text,
    )
    if not mapping_match:
        raise CaptureError(f"screenshot coordinate mapping is unavailable or unrecognized: {mapping_text}")
    origin_x, origin_y, mapped_width, mapped_height = (int(mapping_match.group(i)) for i in range(1, 5))
    if ((origin_x, origin_y) != (0, 0) or (mapped_width, mapped_height) != image.size
            or mapping_match.group(5) != "full"):
        raise CaptureError(f"screenshot mapping does not prove complete 1:1 virtual-screen coverage: {mapping_text}")
    _rect_in_bounds(frame, width, height, "frame")
    _rect_in_bounds(client, width, height, "client")
    if (client["x"] < frame["x"] or client["y"] < frame["y"]
            or client["x"] + client["width"] > frame["x"] + frame["width"]
            or client["y"] + client["height"] > frame["y"] + frame["height"]):
        raise CaptureError("KWin client rectangle is not contained by its frame")

    cases: list[dict[str, Any]] = []
    coordinate_mapping = {
        "source_origin": {"x": origin_x, "y": origin_y},
        "source_dimensions": {"width": width, "height": height},
        "coordinate_basis": mapping_text,
    }
    if app == "kate":
        # Fixture-specific text region: the known Kate virtual-window layout
        # places the editor canvas 60 px from its left edge and 90 px below top.
        content_crop = {
            "x": frame["x"] + 60, "y": frame["y"] + 90,
            "width": frame["width"] - 120, "height": 160,
        }
        crop_reason = "fixed Kate editor text region for this synthetic document"
    else:
        # All local pages use the same fixed 1280x800 virtual viewport. This
        # content crop omits browser chrome while retaining fixture text and
        # controls; it is intentionally not a general page-layout detector.
        content_crop = {
            "x": frame["x"], "y": frame["y"] + 85,
            "width": min(800, frame["width"]), "height": min(450, frame["height"] - 85),
        }
        crop_reason = "fixed content viewport for the local synthetic HTML fixtures"
    _rect_in_bounds(content_crop, width, height, "fixture content crop")
    for scope, rect in (("full", {"x": 0, "y": 0, "width": width, "height": height}), ("app", frame), ("crop", content_crop)):
        target = output_dir / f"{case_id}-{scope}.png"
        cropped = image.crop((rect["x"], rect["y"], rect["x"] + rect["width"], rect["y"] + rect["height"]))
        cropped.save(target, format="PNG", optimize=False)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        cases.append({
            "scope": scope,
            "captured_at": captured_at,
            "path": target.name,
            "sha256": digest,
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "width": rect["width"],
            "height": rect["height"],
            "source_dimensions": {"width": width, "height": height},
            "crop": ({"x": rect["x"], "y": rect["y"], "width": rect["width"], "height": rect["height"], "reason": crop_reason if scope == "crop" else "exact raw KWin frame rectangle", "mapping": coordinate_mapping} if scope != "full" else None),
            "window": {"id": window["id"], "app": window["app"], "caption": window["caption"]},
        })
    return cases, capture_ms


def _fixture_url(filename: str) -> str:
    path = (FIXTURES / filename).resolve()
    if not path.is_file() or path.parent != FIXTURES.resolve():
        raise CaptureError(f"unknown local benchmark fixture: {filename}")
    return "file://" + quote(str(path))


def _start_command(case: dict[str, Any]) -> str:
    app = case["app"]
    if app not in APP_ALLOWLIST:
        raise CaptureError(f"app outside fixed benchmark allowlist: {app}")
    if app == "kate":
        return case["command"]
    profile_dir = case.get("_firefox_profile")
    if not isinstance(profile_dir, str):
        raise CaptureError("isolated Firefox profile was not prepared")
    return f"firefox --no-remote --new-instance --profile {shlex.quote(profile_dir)} {_fixture_url(case['fixture'])}"


def _tree_contains(engine: Any, app: str, marker: str) -> str:
    deadline = time.monotonic() + 15
    last_tree = ""
    while time.monotonic() < deadline:
        last_tree = engine.accessibility_tree(app_name=app, max_depth=25)
        if marker.lower() in last_tree.lower():
            return last_tree
        time.sleep(0.2)
    raise CaptureError(f"AT-SPI did not expose required marker {marker!r}; observed tree excerpt={last_tree[:500]!r}")


def _wait_for_editable_editor(engine: Any) -> str:
    deadline = time.monotonic() + 15
    last_tree = ""
    while time.monotonic() < deadline:
        last_tree = engine.accessibility_tree(app_name="kate", max_depth=25)
        if "editable" in last_tree.lower():
            return last_tree
        time.sleep(0.2)
    raise CaptureError(f"Kate has no accessible editable editor yet; observed tree excerpt={last_tree[:500]!r}")


def _enable_virtual_atspi(engine: Any) -> dict[str, bool]:
    """Enable AT-SPI in this isolated session only, then verify both flags."""
    try:
        import dbus
    except ImportError as exc:
        raise CaptureError("dbus-python is required to enable isolated AT-SPI status flags") from exc
    session = engine._get_session()
    address = session.info.dbus_address
    bus = dbus.bus.BusConnection(address)
    try:
        obj = bus.get_object("org.a11y.Bus", "/org/a11y/bus")
        props = dbus.Interface(obj, "org.freedesktop.DBus.Properties")
        for name in ("IsEnabled", "ScreenReaderEnabled"):
            props.Set("org.a11y.Status", name, dbus.Boolean(True))
        actual = props.GetAll("org.a11y.Status")
        result = {name: bool(actual.get(name, False)) for name in ("IsEnabled", "ScreenReaderEnabled")}
        if not all(result.values()):
            raise CaptureError(f"isolated AT-SPI flags did not enable: {result}")
        return result
    except dbus.DBusException as exc:
        raise CaptureError(f"could not enable AT-SPI in isolated session ({type(exc).__name__})") from None
    finally:
        bus.close()


def _verify_disabled_button(tree: str) -> None:
    lines = [line for line in tree.splitlines() if "save observations" in line.lower()]
    if len(lines) != 1:
        raise CaptureError(f"expected one accessible Save observations button; found {len(lines)}")
    line = lines[0].lower()
    if "disabled" in line or "unavailable" in line:
        return
    # AT-SPI serializes positive states only. A named button that is visible but
    # lacks both enabled and sensitive is its explicit disabled-state evidence.
    if "enabled" not in line and "sensitive" not in line and "visible" in line:
        return
    raise CaptureError(f"AT-SPI did not verify Save observations as disabled: {lines[0]}")


def _run_case(engine: Any, output_dir: Path, case: dict[str, Any]) -> dict[str, Any]:
    app = case["app"]
    if app not in APP_ALLOWLIST:
        raise CaptureError(f"app outside fixed benchmark allowlist: {app}")
    case_started = time.perf_counter()
    poem_path: Path | None = None
    window_title = case.get("title")
    command = _start_command(case)
    if case["verify"] == "poem":
        fd, name = tempfile.mkstemp(prefix="jev-benchmark-poem-", suffix=".txt")
        os.close(fd)
        poem_path = Path(name)
        window_title = poem_path.name
        command = f"kate --startanon --new {shlex.quote(str(poem_path))}"
    start_result = engine.session_start(
        app_command=command, screen_width=SCREEN[0], screen_height=SCREEN[1],
        isolate_home=True, keep_home=False, keep_screenshots=True,
        env={"MOZ_ENABLE_ACCESSIBILITY": "1"} if app == "firefox" else None,
    )
    if "Input backend: KWin EIS" not in start_result:
        raise CaptureError("virtual session did not report KWin EIS input backend")
    atspi_flags = _enable_virtual_atspi(engine)

    if case["verify"] == "poem":
        # Fixed text is synthetic and explicitly requested by the benchmark.
        try:
            _wait_for_editable_editor(engine)
            _append_event(output_dir, case["case_id"], app, "keyboard_type_fixed_poem", "pending")
            engine.keyboard_type(POEM)
            tree = _tree_contains(engine, app, "Blue morning opens")
            if not all(line in tree for line in POEM.splitlines()):
                raise CaptureError("Kate AT-SPI verification did not find all poem lines")
            _append_event(output_dir, case["case_id"], app, "keyboard_type_fixed_poem", "passed")
        finally:
            if poem_path is not None:
                poem_path.unlink(missing_ok=True)
    else:
        tree = _tree_contains(engine, app, case["verify"])
        if case["case_id"] == "web_table_small_text_disabled":
            for expected in ("Bean", "42 cm", "G-204", "Save observations"):
                if expected.lower() not in tree.lower():
                    raise CaptureError(f"Firefox AT-SPI verification missing expected fixture fact {expected!r}")
            _verify_disabled_button(tree)
    setup_ms = (time.perf_counter() - case_started) * 1000

    # Query the unfiltered raw KWin set and resolve the exact app identity/caption.
    window = _resolve_window(engine, app, window_title)
    # _format_window is called for a stable, human-readable identity record in the
    # manifest in addition to preserving the raw app/id/caption/geometry fields.
    from kwin_mcp.core import AutomationEngine
    formatted = AutomationEngine._format_window(window)
    images, capture_ms = _save_images(engine, output_dir, case["case_id"], app, window)
    return {
        "case_id": case["case_id"],
        "app": app,
        "task": case["task"],
        "questions": case["questions"],
        "expected_facts": case["expected_facts"],
        "setup_ms": round(setup_ms, 2),
        "capture_ms": round(capture_ms, 2),
        "versions": _versions(app),
        "atspi_flags": atspi_flags,
        "window_identity": {"id": window["id"], "app": window["app"], "caption": window["caption"], "formatted": formatted},
        "verification": "passed: expected synthetic fixture content present in fresh AT-SPI tree",
        "source_provenance": _source_provenance(case, images),
        "images": images,
    }


def _source_provenance(case: dict[str, Any], images: list[dict[str, Any]]) -> dict[str, Any]:
    if case["app"] == "kate":
        material = POEM.encode("utf-8")
        fixture = "fixed poem fixture (typed into Kate)"
    else:
        fixture_path = FIXTURES / case["fixture"]
        material = fixture_path.read_bytes()
        fixture = str(fixture_path.relative_to(ROOT))
    source_hash = images[0]["source_sha256"]
    return {
        "fixture": fixture,
        "fixture_sha256": hashlib.sha256(material).hexdigest(),
        "capture_id": f"{case['case_id']}-{uuid.uuid4().hex[:12]}",
        "capture_time": images[0]["captured_at"],
        "source_screenshot_sha256": source_hash,
        "verification": "AT-SPI synthetic fixture content checked before capture",
    }


def _append_event(output_dir: Path, case_id: str, app: str, event: str, verified: str) -> None:
    record = {
        "timestamp": _utc_now(), "caller_node": "local", "transport": "cli",
        "tool": "benchmark_capture", "app": app,
        "autonomy_mode": "guarded", "fixture": case_id, "event": event,
        "verification": verified,
    }
    with (output_dir / "capture-events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, separators=(",", ":")) + "\n")


def capture_suite(output_dir: Path) -> dict[str, Any]:
    """Capture the five fixed local synthetic cases into caller-owned output_dir."""
    if len(CASES) > MAX_CASES:
        raise CaptureError("fixed case count exceeds the suite cap")
    output_dir = Path(output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if not output_dir.is_dir():
        raise CaptureError("output_dir is not a directory")
    try:
        from kwin_mcp.core import AutomationEngine
    except ImportError as exc:
        raise CaptureError("install pinned driver: uv run --with kwin-mcp==0.10.0") from exc
    manifest: dict[str, Any] = {
        "suite_id": "jev-vision-fixtures-v1",
        "captured_at": _utc_now(),
        "screen_dimensions": {"width": SCREEN[0], "height": SCREEN[1]},
        "source": "local synthetic fixtures only; no external websites or model calls",
        "cases": [],
    }
    for case in CASES:
        if case["app"] not in APP_ALLOWLIST:
            raise CaptureError(f"case exceeds app allowlist: {case['app']}")
        engine = AutomationEngine()
        session_started = False
        profile_dir: Path | None = None
        run_case = dict(case)
        if case["app"] == "firefox":
            profile_dir = Path(tempfile.mkdtemp(prefix="jev-benchmark-firefox-"))
            profile_dir.mkdir(mode=0o700, exist_ok=True)
            prefs = {
                "browser.startup.homepage_override.mstone": "ignore",
                "browser.startup.firstrunSkipsHomepage": True,
                "browser.startup.page": 0,
                "browser.aboutwelcome.enabled": False,
                "browser.shell.checkDefaultBrowser": False,
                "datareporting.policy.firstRunURL": "",
            }
            (profile_dir / "user.js").write_text(
                "\n".join(f"user_pref({json.dumps(k)}, {json.dumps(v)});" for k, v in prefs.items()) + "\n",
                encoding="utf-8",
            )
            run_case["_firefox_profile"] = str(profile_dir)
        _append_event(output_dir, case["case_id"], case["app"], "session_start", "pending")
        try:
            session_started = True  # stop even when startup partially fails
            result = _run_case(engine, output_dir, run_case)
            manifest["cases"].append(result)
            _append_event(output_dir, case["case_id"], case["app"], "capture", "passed")
        except Exception as exc:
            _append_event(output_dir, case["case_id"], case["app"], "capture", f"failed:{type(exc).__name__}")
            raise
        finally:
            if session_started:
                try:
                    engine.session_stop()
                    _append_event(output_dir, case["case_id"], case["app"], "session_stop", "passed")
                except Exception as exc:
                    _append_event(output_dir, case["case_id"], case["app"], "session_stop", f"failed:{type(exc).__name__}")
                    raise CaptureError(f"failed to stop virtual session for {case['case_id']}") from exc
            if profile_dir is not None:
                import shutil
                shutil.rmtree(profile_dir, ignore_errors=False)
    manifest["captured_at_finished"] = _utc_now()
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    output_dir = Path(argv[0]) if argv else ROOT / "run" / "fixtures"
    manifest = capture_suite(output_dir)
    print(json.dumps({"manifest": str(Path(output_dir).resolve() / "manifest.json"), "cases": len(manifest["cases"]), "images": sum(len(c["images"]) for c in manifest["cases"])}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
