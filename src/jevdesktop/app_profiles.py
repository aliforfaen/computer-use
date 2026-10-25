"""Validated launch and identity profiles for configured desktop applications.

Profiles describe argv and application identities only; the app allowlist and
transaction authorization remain separate policy decisions.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


_PROFILE_ID = re.compile(r"[a-z][a-z0-9._-]{0,63}\Z")
_APP_ID = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")
_MAX_PROFILES = 64
_MAX_ARGV_ITEMS = 32
_MAX_ARG_CHARS = 4096


class ProfileError(ValueError):
    """Safe profile configuration failure with a stable public code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class AppProfile:
    id: str
    command: tuple[str, ...]
    window_app_ids: tuple[str, ...]
    atspi_app: str
    aliases: tuple[str, ...] = ()


_BUILTINS = (
    AppProfile("kate", ("kate", "--new", "--block"), ("kate", "org.kde.kate"), "kate",
               ("org.kde.kate",)),
    AppProfile("firefox", ("firefox", "--no-remote", "--new-instance"),
               ("firefox", "org.mozilla.firefox"), "firefox", ("org.mozilla.firefox",)),
    AppProfile("kcalc", ("kcalc",), ("kcalc", "org.kde.kcalc"), "kcalc", ("org.kde.kcalc",)),
)


def profiles_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return root / "jev-desktop" / "app_profiles.json"


def _validate_profile(raw: object) -> AppProfile:
    if not isinstance(raw, dict):
        raise ProfileError("profile_invalid")
    allowed = {"id", "command", "window_app_ids", "atspi_app", "aliases"}
    if set(raw) - allowed or not {"id", "command", "window_app_ids", "atspi_app"}.issubset(raw):
        raise ProfileError("profile_invalid")
    profile_id = raw["id"]
    command = raw["command"]
    window_ids = raw["window_app_ids"]
    atspi_app = raw["atspi_app"]
    aliases = raw.get("aliases", [])
    if not isinstance(profile_id, str) or not _PROFILE_ID.fullmatch(profile_id):
        raise ProfileError("profile_invalid")
    if (not isinstance(command, list) or not command or len(command) > _MAX_ARGV_ITEMS
            or any(not isinstance(part, str) or not part or len(part) > _MAX_ARG_CHARS or "\x00" in part
                   for part in command)):
        raise ProfileError("profile_invalid")
    if (not isinstance(window_ids, list) or not window_ids or len(window_ids) > 32
            or any(not isinstance(value, str) or not _APP_ID.fullmatch(value) for value in window_ids)):
        raise ProfileError("profile_invalid")
    if (not isinstance(atspi_app, str) or not atspi_app.strip() or len(atspi_app) > 128
            or any(ord(char) < 32 for char in atspi_app)):
        raise ProfileError("profile_invalid")
    if (not isinstance(aliases, list) or len(aliases) > 32
            or any(not isinstance(value, str) or not _APP_ID.fullmatch(value) for value in aliases)):
        raise ProfileError("profile_invalid")
    names = [profile_id.casefold(), *(value.casefold() for value in aliases)]
    if len(set(names)) != len(names):
        raise ProfileError("profile_invalid")
    return AppProfile(profile_id, tuple(command), tuple(window_ids), atspi_app, tuple(aliases))


def load_profiles(path: str | os.PathLike[str] | None = None) -> Mapping[str, AppProfile]:
    """Load built-ins plus validated custom profiles into a read-only alias map.

    Missing configuration is normal. Invalid JSON or conflicting aliases fail
    closed with a code-only `ProfileError`.
    """
    result: dict[str, AppProfile] = {}
    for profile in _BUILTINS:
        for name in (profile.id, *profile.aliases):
            result[name.casefold()] = profile
    config_path = Path(path) if path is not None else profiles_path()
    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return MappingProxyType(result)
    except (OSError, UnicodeError):
        raise ProfileError("profile_config_unreadable") from None
    try:
        document = json.loads(raw_text)
    except (json.JSONDecodeError, RecursionError):
        raise ProfileError("profile_config_invalid") from None
    if not isinstance(document, dict) or set(document) != {"profiles"}:
        raise ProfileError("profile_config_invalid")
    records = document["profiles"]
    if not isinstance(records, list) or len(records) > _MAX_PROFILES:
        raise ProfileError("profile_config_invalid")
    seen: set[str] = set()
    for record in records:
        profile = _validate_profile(record)
        names = (profile.id, *profile.aliases)
        folded = [name.casefold() for name in names]
        if any(name in result or name in seen for name in folded):
            raise ProfileError("profile_alias_conflict")
        seen.update(folded)
        for name in folded:
            result[name] = profile
    return MappingProxyType(result)


def profile_for(app: str, profiles: Mapping[str, AppProfile] | None = None) -> AppProfile:
    """Resolve an app id or alias without granting it permission to launch."""
    if not isinstance(app, str) or not app.strip():
        raise ProfileError("profile_not_found")
    values = profiles if profiles is not None else load_profiles()
    profile = values.get(app.casefold())
    if profile is None:
        raise ProfileError("profile_not_found")
    return profile
