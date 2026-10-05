"""Filesystem anchors shared by the runtime, scripts and probes.

The checkout stays the source of truth for local assets and development
fixtures, so these helpers walk up to the project root and fall back to the
packaged location when the package is installed instead of used in place.
"""

from __future__ import annotations

import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent

ICON_NAME = "jev-icon.svg"
DESKTOP_ENTRY_NAME = "jev-desktop.desktop"
ICON_THEME_NAME = "jev-icon"


def repo_root() -> Path | None:
    """Return the checkout root, or ``None`` for an installed (non-checkout) copy."""
    for parent in PACKAGE_DIR.parents:
        if (parent / "pyproject.toml").is_file() and (parent / "AGENTS.md").is_file():
            return parent
    return None


def _first_file(candidates: list[Path]) -> Path | None:
    for candidate in candidates:
        if candidate.expanduser().is_file():
            return candidate.expanduser()
    return None


def data_home() -> Path:
    base = os.environ.get("XDG_DATA_HOME")
    return Path(base) if base else Path.home() / ".local" / "share"


def icon_path() -> Path | None:
    """Locate the canonical ``jev-icon.svg``.

    Search order: explicit override, packaged copy (a symlink to ``assets/`` in
    a checkout), checkout ``assets/``, then an installed XDG data copy.
    """
    override = os.environ.get("JEV_DESKTOP_ICON")
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override))
    candidates.append(PACKAGE_DIR / "assets" / ICON_NAME)
    root = repo_root()
    if root is not None:
        candidates.append(root / "assets" / ICON_NAME)
    candidates.append(data_home() / "jev-desktop" / ICON_NAME)
    return _first_file(candidates)


def desktop_entry_path() -> Path | None:
    """Locate the canonical ``jev-desktop.desktop`` in the checkout."""
    override = os.environ.get("JEV_DESKTOP_DESKTOP_ENTRY")
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override))
    root = repo_root()
    if root is not None:
        candidates.append(root / "assets" / DESKTOP_ENTRY_NAME)
    candidates.append(PACKAGE_DIR / "assets" / DESKTOP_ENTRY_NAME)
    return _first_file(candidates)


def icon_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "jev-desktop" / "icons"


def fixtures_dir() -> Path:
    """Return the ``benchmark_fixtures`` directory (checkout first, then package)."""
    root = repo_root()
    if root is not None and (root / "benchmark_fixtures").is_dir():
        return root / "benchmark_fixtures"
    return PACKAGE_DIR / "benchmark_fixtures"


def run_dir() -> Path:
    """Return the checkout ``run/`` directory, falling back to the working dir."""
    root = repo_root()
    return (root if root is not None else Path.cwd()) / "run"
