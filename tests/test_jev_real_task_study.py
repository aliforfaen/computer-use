"""Offline tests for the Jev-versus-code study harness helpers.

No owner socket, no provider: only the deterministic policy, the pre-declared
ambiguity cases and the report rendering are exercised. See
docs/33-jev-real-task-study.md.
"""

from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import jevdesktop.jev_selector as js
from scripts.jev_real_task_study import (CodeSelector, DRAFT, REVISION, build_ambiguity_cases,
                                         code_label_rule, evaluate_decision, main, render_markdown,
                                         study_summary, text_record)
from tools.jev_loop_probe import state_from_probe_candidates


def _cand(ref, role, label, actions):
    return {"ref": ref, "role": role, "label": label,
            "states": ["enabled", "sensitive", "showing", "visible"],
            "actions": list(actions)}


EDITOR = _cand("e1", "text", "editable text", ["click", "type_text", "replace_document", "save_document"])
SAVE = _cand("b1", "button", "Save", ["click"])


def _options(candidates):
    return state_from_probe_candidates("kate", "handover.txt", "goal", candidates).options


class CodeSelectorTests(unittest.TestCase):
    def test_phase_machine_chooses_the_first_supporting_target(self):
        options = _options([SAVE, EDITOR])
        selector = CodeSelector(["replace_document", "save_document", "done"])
        first = selector.select({}, options=options)
        self.assertEqual((first.action, first.target_ref), ("replace_document", "e1"))
        second = selector.select({}, options=options)
        self.assertEqual((second.action, second.target_ref), ("save_document", "e1"))
        third = selector.select({}, options=options)
        self.assertEqual((third.action, third.target_ref), ("done", None))

    def test_an_exhausted_plan_is_a_typed_error(self):
        with self.assertRaises(js.SelectorError):
            CodeSelector([]).select({}, options=_options([SAVE, EDITOR]))


class CodeLabelRuleTests(unittest.TestCase):
    def test_in_place_save_matches_the_exact_save_label(self):
        options = _options([SAVE, _cand("b2", "button", "Save As", ["click"])])
        self.assertEqual(code_label_rule(options, "save the document to its existing file"), ("click", "b1"))
        self.assertEqual(code_label_rule(options, "save a copy under a new name"), ("click", "b2"))

    def test_ambiguous_or_missing_labels_have_no_rule(self):
        self.assertIsNone(code_label_rule(_options([SAVE, _cand("b2", "button", "Save", ["click"])]),
                                          "save the document"))
        self.assertIsNone(code_label_rule(_options([_cand("b2", "button", "Discard", ["click"])]),
                                          "save the changes"))


class AmbiguityCaseTests(unittest.TestCase):
    def test_synthetic_cases_declare_acceptable_sets(self):
        cases = {case["case_id"]: case for case in build_ambiguity_cases([])}
        self.assertEqual(set(cases), {"save_vs_save_as", "unsaved_changes_keep",
                                      "insufficient_context", "indistinguishable_duplicates"})
        self.assertIn(("click", "m_save"), cases["unsaved_changes_keep"]["acceptable"])
        self.assertIn(("click", "m_discard"), cases["unsaved_changes_keep"]["forbidden"])
        self.assertEqual(cases["insufficient_context"]["acceptable"], [("blocked", None)])

    def test_a_real_snapshot_adds_two_selection_cases(self):
        cases = build_ambiguity_cases([SAVE, EDITOR])
        ids = [case["case_id"] for case in cases]
        self.assertIn("real_replace_natural", ids)
        self.assertIn("real_replace_shuffled", ids)
        natural = next(case for case in cases if case["case_id"] == "real_replace_natural")
        self.assertEqual(natural["acceptable"], [("replace_document", "e1")])


class EvaluateDecisionTests(unittest.TestCase):
    def _decision(self, action, target_ref):
        return SimpleNamespace(action=action, target_ref=target_ref, confidence=0.5, probabilities={})

    def test_verdicts_cover_accepted_forbidden_and_unexpected(self):
        case = {"acceptable": [("click", "m_save")], "forbidden": [("click", "m_discard")]}
        self.assertEqual(evaluate_decision(case, self._decision("click", "m_save"))["verdict"], "accepted")
        self.assertEqual(evaluate_decision(case, self._decision("click", "m_discard"))["verdict"], "forbidden")
        self.assertEqual(evaluate_decision(case, self._decision("blocked", None))["verdict"], "unexpected")


class ReportTests(unittest.TestCase):
    def test_text_records_carry_digests_and_lengths_not_contents(self):
        record = text_record(REVISION)
        self.assertEqual(record["chars"], len(REVISION))
        self.assertNotIn("handoff", str(record))

    def test_markdown_tolerates_a_selector_error_entry(self):
        report = {"study": "s", "model_requested": "jev-latest", "model_observed": [],
                  "budget": {"provider_calls": 0, "authorized_calls": 12},
                  "arms": {"code": None, "jev": None},
                  "ambiguity": [{"case_id": "c", "source": "synthetic", "verdict": "selector_error",
                                 "code_rule": None}]}
        markdown = render_markdown(report)
        self.assertIn("selector_error", markdown)

    def test_draft_and_revision_differ(self):
        self.assertNotEqual(DRAFT, REVISION)


class StudySummaryTests(unittest.TestCase):
    def _report(self, *, code_ok=True, jev_ok=True, cleanup="confirmed", daemon_clean=True, jev_present=True):
        arms = {"code": {"final_contract_passed": code_ok,
                         "session_stop": {"cleanup": cleanup}}}
        if jev_present:
            arms["jev"] = {"final_contract_passed": jev_ok, "session_stop": {"cleanup": cleanup}}
        return {"arms": arms, "budget": {"provider_calls": 5},
                "study_daemon": {"cleanup_confirmed": daemon_clean},
                "ambiguity": [{"verdict": "accepted"}]}

    def test_ok_requires_completed_contracts_and_confirmed_cleanup(self):
        summary = study_summary(self._report(), output=Path("run/x"))
        self.assertTrue(summary["ok"])
        self.assertTrue(summary["completed"])
        self.assertTrue(summary["task_contracts_passed"])
        self.assertTrue(summary["cleanup_confirmed"])

    def test_a_failed_arm_is_not_success(self):
        summary = study_summary(self._report(jev_ok=False), output=Path("run/x"))
        self.assertFalse(summary["ok"])
        self.assertTrue(summary["completed"])
        self.assertFalse(summary["task_contracts_passed"])
        self.assertFalse(summary["contracts"]["jev"])

    def test_unconfirmed_cleanup_is_not_success(self):
        self.assertFalse(study_summary(self._report(cleanup="unconfirmed"), output=Path("run/x"))["ok"])
        self.assertFalse(study_summary(self._report(daemon_clean=False), output=Path("run/x"))["ok"])

    def test_a_missing_arm_means_the_experiment_did_not_complete(self):
        summary = study_summary(self._report(jev_present=False), output=Path("run/x"))
        # The code arm alone is still a complete experiment for --code-only.
        self.assertTrue(summary["completed"])


class _RecordingDaemon:
    instances: list = []

    def __init__(self, root):
        self.root = root
        self.socket = Path("/tmp/jev-study-test-does-not-exist.sock")
        self.started = 0
        self.stopped = 0
        _RecordingDaemon.instances.append(self)

    def start(self):
        self.started += 1
        raise RuntimeError("study_daemon_readiness_timeout")

    def stop(self):
        self.stopped += 1
        return {"cleanup_confirmed": True, "process_exited": True}


class HarnessControlTests(unittest.TestCase):
    def setUp(self):
        _RecordingDaemon.instances = []

    def _stdout(self, argv):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(argv)
        return code, buffer.getvalue()

    def test_run_is_refused_when_jev_is_disabled_without_side_effects(self):
        with mock.patch.dict(os.environ, {"JEV_ENABLED": ""}), \
             mock.patch("scripts.jev_real_task_study._load_dotenv_key") as load_key, \
             mock.patch("scripts.jev_real_task_study.StudyDaemon", _RecordingDaemon):
            code, output = self._stdout(["--run"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["error"], "jev_disabled")
        load_key.assert_not_called()
        self.assertEqual(_RecordingDaemon.instances, [])

    def test_dry_run_wins_over_run_and_starts_nothing(self):
        with mock.patch.dict(os.environ, {"JEV_ENABLED": "true"}), \
             mock.patch("scripts.jev_real_task_study.StudyDaemon", _RecordingDaemon):
            code, output = self._stdout(["--run", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output)["dry_run"])
        self.assertEqual(_RecordingDaemon.instances, [])

    def test_a_startup_timeout_still_stops_the_owned_daemon(self):
        with mock.patch("scripts.jev_real_task_study.StudyDaemon", _RecordingDaemon), \
             mock.patch("scripts.jev_real_task_study.execute_study") as execute:
            code, output = self._stdout(["--code-only"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["error"], "study_daemon_readiness_timeout")
        execute.assert_not_called()
        self.assertEqual(len(_RecordingDaemon.instances), 1)
        self.assertEqual(_RecordingDaemon.instances[0].stopped, 1)

    def test_a_failed_experiment_exits_nonzero(self):
        class FakeDaemon:
            def __init__(self, root):
                self.socket = Path("/tmp/jev-study-test-does-not-exist.sock")

            def start(self):
                return None

            def stop(self):
                return {"cleanup_confirmed": True, "process_exited": True}

        failed = {"arms": {"code": {"final_contract_passed": False, "session_stop": {"cleanup": "confirmed"}}},
                  "budget": {"provider_calls": 0}, "study_daemon": {"cleanup_confirmed": True}, "ambiguity": []}
        with mock.patch("scripts.jev_real_task_study.StudyDaemon", FakeDaemon), \
             mock.patch("scripts.jev_real_task_study.execute_study", return_value=failed), \
             mock.patch("scripts.jev_real_task_study._write_reports"):
            code, output = self._stdout(["--code-only"])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(output)["ok"])
        self.assertFalse(json.loads(output)["task_contracts_passed"])


if __name__ == "__main__":
    unittest.main()
