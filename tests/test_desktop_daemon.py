from __future__ import annotations

import argparse
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from desktop_daemon import _make_service
import desktop_cli


class DesktopDaemonReaderConfigTests(unittest.TestCase):
    def test_cli_passes_reader_key_file_and_deadlines_to_daemon(self):
        captured = []
        with patch("desktop_daemon.main", side_effect=lambda argv: captured.extend(argv) or 0):
            result = desktop_cli.main(["daemon", "--foreground", "--allow-app", "kcalc",
                "--reader-provider", "deepseek", "--max-reader-calls", "3", "--dotenv", ".env",
                "--reader-timeout", "8", "--reader-total-timeout", "12"])
        self.assertEqual(result, 0)
        self.assertEqual(captured[captured.index("--reader-provider") + 1], "deepseek")
        self.assertEqual(captured[captured.index("--max-reader-calls") + 1], "3")
        self.assertEqual(captured[captured.index("--dotenv") + 1], ".env")
        self.assertEqual(captured[captured.index("--reader-timeout") + 1], "8.0")
        self.assertEqual(captured[captured.index("--reader-total-timeout") + 1], "12.0")

    def test_reader_is_configured_from_dotenv_without_cost_claims(self):
        key_name = "JEV_TEST_READER_KEY"
        old = os.environ.pop(key_name, None)
        try:
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                dotenv = root / ".env"
                dotenv.write_text(f"# test key\n{key_name}=test-value-never-printed\n", encoding="utf-8")
                os.chmod(dotenv, 0o600)
                args = argparse.Namespace(
                    allow_app=["kate"], audit=root / "audit.jsonl", run_dir=root,
                    reader_provider="deepseek", reader_base_url=None, reader_model=None,
                    reader_key_env=key_name, max_reader_calls=4, dotenv=dotenv,
                    reader_timeout=2.0, reader_total_timeout=3.0,
                    idle_timeout=180.0, max_session_lifetime=1800.0,
                    max_actions=64, max_observations=256,
                )
                service = _make_service(args)
                try:
                    capabilities = service.dispatch("capabilities")["reader"]
                    self.assertTrue(capabilities["available"])
                    self.assertEqual(capabilities["max_calls_per_session"], 4)
                    self.assertEqual(capabilities["configuration"]["provider"], "deepseek")
                    self.assertEqual(capabilities["configuration"]["model"], "deepseek-flash")
                    self.assertEqual(capabilities["configuration"]["request_execution"], "isolated_process")
                    self.assertTrue(capabilities["configuration"]["hard_timeout_covers"].endswith("response_headers_and_body"))
                    self.assertEqual(service.reader.config.timeout_seconds, 2.0)
                    self.assertEqual(service.reader.config.total_timeout_seconds, 3.0)
                    self.assertIn("provider billing is not measured", capabilities["accounting"])
                finally:
                    service.close()
        finally:
            os.environ.pop(key_name, None)
            if old is not None:
                os.environ[key_name] = old


if __name__ == "__main__":
    unittest.main()
