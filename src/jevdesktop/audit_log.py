"""Append-only JSONL audit storage with bounded size and age.

The audit log is the durable record required for every owner call, so it lives
in the persistent config directory rather than the runtime directory. Rotation
is plain code: size-based backup files plus age-based pruning. Writes stay
0600, append-only, fsynced and free of secret values.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MAX_BYTES = 4 * 1024 * 1024
DEFAULT_BACKUPS = 3
DEFAULT_MAX_AGE_DAYS = 30.0

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class AuditRotation:
    """Bounded retention for one audit file: `name`, `name.1`, ... `name.N`."""

    max_bytes: int = DEFAULT_MAX_BYTES
    backups: int = DEFAULT_BACKUPS
    max_age_days: float = DEFAULT_MAX_AGE_DAYS

    def __post_init__(self) -> None:
        if type(self.max_bytes) is not int or self.max_bytes <= 0:
            raise ValueError("audit max_bytes must be a positive integer")
        if type(self.backups) is not int or not 1 <= self.backups <= 20:
            raise ValueError("audit backups must be an integer from 1 to 20")
        if (isinstance(self.max_age_days, bool) or not isinstance(self.max_age_days, (int, float))
                or not 0 < self.max_age_days < float("inf")):
            raise ValueError("audit max_age_days must be a finite positive number")

    def summary(self) -> dict[str, object]:
        return {"max_bytes": self.max_bytes, "backups": self.backups, "max_age_days": self.max_age_days}


DEFAULT_ROTATION = AuditRotation()


def _lock_for(path: Path) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(str(path), threading.Lock())


def append_jsonl(path: str | os.PathLike[str], data: bytes, *, rotation: AuditRotation = DEFAULT_ROTATION) -> None:
    """Append one newline-terminated JSON line, rotating and pruning as needed."""
    if not isinstance(data, bytes) or not data.endswith(b"\n"):
        raise ValueError("audit payload must be newline-terminated bytes")
    target = Path(path)
    with _lock_for(target):
        _rotate_if_needed(target, rotation)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.fchmod(fd, 0o600)
            remaining = memoryview(data)
            while remaining:
                written = os.write(fd, remaining)
                remaining = remaining[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        _prune_backups(target, rotation)


def _backup_path(path: Path, index: int) -> Path:
    return path.with_name(f"{path.name}.{index}")


def _rotate_if_needed(path: Path, rotation: AuditRotation) -> None:
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return
    if size < rotation.max_bytes:
        return
    oldest = _backup_path(path, rotation.backups)
    try:
        oldest.unlink()
    except FileNotFoundError:
        pass
    for index in range(rotation.backups - 1, 0, -1):
        source = _backup_path(path, index)
        if source.exists():
            source.replace(_backup_path(path, index + 1))
    path.replace(_backup_path(path, 1))


def _prune_backups(path: Path, rotation: AuditRotation) -> None:
    cutoff = time.time() - rotation.max_age_days * 86400
    for index in range(1, rotation.backups + 1):
        backup = _backup_path(path, index)
        try:
            if backup.stat().st_mtime < cutoff:
                backup.unlink()
        except FileNotFoundError:
            pass


def prune_directory(directory: str | os.PathLike[str], *, pattern: str, max_age_days: float) -> int:
    """Remove old files (for example stale session journals); returns the count."""
    if (isinstance(max_age_days, bool) or not isinstance(max_age_days, (int, float))
            or not 0 < max_age_days < float("inf")):
        raise ValueError("max_age_days must be a finite positive number")
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    try:
        entries = list(Path(directory).glob(pattern))
    except OSError:
        return 0
    for entry in entries:
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
                removed += 1
        except OSError:
            continue
    return removed
