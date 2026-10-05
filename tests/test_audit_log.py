from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from jevdesktop.audit_log import AuditRotation, append_jsonl, prune_directory


def _line(index: int) -> bytes:
    return (json.dumps({"i": index, "pad": "x" * 40}) + "\n").encode()


class AuditLogTests(unittest.TestCase):
    def test_rotation_keeps_bounded_backups_and_private_permissions(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "audit.jsonl"
            rotation = AuditRotation(max_bytes=200, backups=2, max_age_days=30)
            for index in range(12):
                append_jsonl(path, _line(index), rotation=rotation)
            self.assertTrue(path.exists())
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertTrue((path.with_name("audit.jsonl.1")).exists())
            self.assertTrue((path.with_name("audit.jsonl.2")).exists())
            for index in range(3, 6):
                self.assertFalse(path.with_name(f"audit.jsonl.{index}").exists())
            # Newest lines stay readable and ordered.
            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(records, sorted(records, key=lambda record: record["i"]))
            self.assertEqual(records[-1]["i"], 11)

    def test_age_pruning_removes_only_expired_backups(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "audit.jsonl"
            path.write_text("current\n", encoding="utf-8")
            stale = path.with_name("audit.jsonl.1")
            stale.write_text("old\n", encoding="utf-8")
            fresh = path.with_name("audit.jsonl.2")
            fresh.write_text("recent\n", encoding="utf-8")
            old = time.time() - 10 * 86400
            os.utime(stale, (old, old))

            # Small append triggers rotation bookkeeping and the age sweep.
            append_jsonl(path, _line(0), rotation=AuditRotation(max_bytes=1024, backups=3, max_age_days=5))
            self.assertFalse(stale.exists())
            self.assertTrue(fresh.exists())
            self.assertTrue(path.exists())

    def test_prune_directory_removes_only_old_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stale = root / "old.json"
            fresh = root / "new.json"
            other = root / "keep.txt"
            for entry in (stale, fresh, other):
                entry.write_text("x", encoding="utf-8")
            old = time.time() - 40 * 86400
            os.utime(stale, (old, old))
            os.utime(other, (old, old))

            removed = prune_directory(root, pattern="*.json", max_age_days=30)
            self.assertEqual(removed, 1)
            self.assertFalse(stale.exists())
            self.assertTrue(fresh.exists())
            self.assertTrue(other.exists())

    def test_invalid_rotation_is_rejected(self):
        for kwargs in ({"max_bytes": 0}, {"backups": 0}, {"backups": 21}, {"max_age_days": 0}):
            with self.assertRaises(ValueError):
                AuditRotation(**kwargs)
        with self.assertRaises(ValueError):
            append_jsonl("/tmp/unused.jsonl", b"not-newline-terminated")


if __name__ == "__main__":
    unittest.main()
