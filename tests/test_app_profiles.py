from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from jevdesktop.app_profiles import ProfileError, load_profiles, profile_for


class AppProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "app_profiles.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, profiles):
        self.path.write_text(json.dumps({"profiles": profiles}), encoding="utf-8")

    def test_builtin_profiles_and_aliases(self):
        profiles = load_profiles(self.path)
        self.assertEqual(profile_for("org.kde.kate", profiles).id, "kate")
        self.assertEqual(profile_for("KCALC", profiles).atspi_app, "kcalc")
        self.assertEqual(profile_for("firefox", profiles).command,
                         ("firefox", "--no-remote", "--new-instance"))

    def test_custom_profile_and_casefolded_aliases(self):
        self._write([{"id": "writer", "command": ["writer", "--new"],
                      "window_app_ids": ["writer.desktop"], "atspi_app": "writer-atspi",
                      "aliases": ["org.example.Writer"]}])
        profiles = load_profiles(self.path)
        profile = profile_for("ORG.EXAMPLE.WRITER", profiles)
        self.assertEqual(profile.id, "writer")
        self.assertEqual(profile.command, ("writer", "--new"))
        self.assertEqual(profile.window_app_ids, ("writer.desktop",))
        self.assertEqual(profile.atspi_app, "writer-atspi")
        with self.assertRaises(TypeError):
            profiles["other"] = profile

    def test_invalid_argv_and_profile_conflicts_fail_closed(self):
        for command in ("writer --new", ["writer", "\x00bad"], ["writer"] * 33):
            with self.subTest(command=command):
                self._write([{"id": "writer", "command": command,
                              "window_app_ids": ["writer.desktop"], "atspi_app": "writer"}])
                with self.assertRaises(ProfileError) as exc:
                    load_profiles(self.path)
                self.assertEqual(exc.exception.code, "profile_invalid")
        self._write([{"id": "kate", "command": ["evil"],
                      "window_app_ids": ["kate"], "atspi_app": "kate"}])
        with self.assertRaisesRegex(ProfileError, "profile_alias_conflict"):
            load_profiles(self.path)

    def test_invalid_document_and_missing_profile(self):
        self.path.write_text("not json", encoding="utf-8")
        with self.assertRaisesRegex(ProfileError, "profile_config_invalid"):
            load_profiles(self.path)
        profiles = load_profiles(self.path.parent / "missing.json")
        with self.assertRaisesRegex(ProfileError, "profile_not_found"):
            profile_for("not-configured", profiles)


if __name__ == "__main__":
    unittest.main()
