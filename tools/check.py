#!/usr/bin/env python3
"""Fast core check: the shipped surfaces only, in a couple of seconds.

    uv run python -m tools.check

Excludes the reader, live and benchmark harness modules; see
docs/29-verification-runbook.md for the per-change mapping.
"""

from __future__ import annotations

import sys
import time
import unittest

CORE_MODULES = (
    "tests.test_desktop_service",
    "tests.test_transactions",
    "tests.test_desktop_surfaces",
    "tests.test_desktop_daemon",
    "tests.test_desktop_tray_and_service",
    "tests.test_desktop_worker_documents",
    "tests.test_observation",
)


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(loader.loadTestsFromName(name) for name in CORE_MODULES)
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    elapsed = time.monotonic() - started
    print(f"\ncore check: {result.testsRun} tests in {elapsed:.2f}s "
          f"({len(CORE_MODULES)} modules; full suite: "
          f"uv run python -m unittest discover -s tests)")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
