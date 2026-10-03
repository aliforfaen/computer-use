"""Structural tests for the wait-benchmark harness.

The rendered-fixture, palette and event-endpoint checks were dropped once the
baseline in doc 17 was recorded; this keeps the plan and normalization rules
that a future re-run still depends on.
"""

from __future__ import annotations

import unittest

from wait_benchmark import CASES, _normalized_status, make_plan


class WaitBenchmarkTests(unittest.TestCase):
    def test_seeded_arm_order_is_reproducible_and_bounded(self) -> None:
        first = make_plan(31, 2, 10)
        self.assertEqual(first, make_plan(31, 2, 10))
        self.assertEqual(len(first), 10)
        self.assertEqual({arm for _, _, arm in first}, {"polling", "watcher"})
        with self.assertRaises(ValueError):
            make_plan(1, 1, 51)

    def test_fixture_matrix_has_all_requested_conditions(self) -> None:
        self.assertEqual({case["case"] for case in CASES},
                         {"loading_ready", "loading_error", "no_change", "animation_noise", "outside_dialog"})
        self.assertEqual(CASES[2]["expected"], "timeout")
        self.assertGreater(CASES[-1]["delay_ms"], CASES[-1]["dialog_ms"])

    def test_error_and_unexpected_judgments_survive_result_normalization(self) -> None:
        self.assertEqual(_normalized_status("error"), "error")
        self.assertEqual(_normalized_status("unexpected"), "unexpected")
        self.assertEqual(_normalized_status("timeout"), "timeout")


if __name__ == "__main__":
    unittest.main()
