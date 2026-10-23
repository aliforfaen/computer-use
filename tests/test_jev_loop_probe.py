"""Offline tests for the Jev driver loop.

No network, no owner socket, no provider: the loop takes an injected client and
selector, so every stop reason can be provoked. See
docs/31-jev-control-plan.md.
"""

from __future__ import annotations

import json
import unittest

import jevdesktop.jev_selector as js
from tools.jev_loop_probe import run_loop, validate_probe_answer


def _candidate(ref, role, label, actions):
    return {"ref": ref, "role": role, "label": label,
            "states": ["enabled", "sensitive", "showing", "visible"],
            "actions": list(actions), "unavailable_reason": None}


CANDIDATES = [
    _candidate("c_one", "button", "One", ["click"]),
    _candidate("c_two", "button", "Two", ["click"]),
    _candidate("c_text", "text", "editable text", ["click", "type_text"]),
]
# Options are sorted by role then label, so indices are: 1 One, 2 Two, 3 editable text.
PASSED = {"ok": True, "status": "ok", "verification": "passed", "evidence": {"exact_visible_text": True}}

# A Kate-like state: sorted by (role, label), the Save button is index 1 and the
# editor is index 2. The editor is the only candidate that supports documents.
KATE_CANDIDATES = [
    _candidate("k_save", "button", "Save", ["click"]),
    _candidate("k_editor", "text", "editable text",
               ["click", "type_text", "replace_document", "save_document"]),
]
DRAFT = "Shift handoff draft.\n"
REVISION = "Shift handoff revision with a correction.\n"


class FakeClient:
    def __init__(self, *, candidates=None, acts=None, candidates_ok=True):
        self._candidates = CANDIDATES if candidates is None else candidates
        self._acts = list(acts or [])
        self._candidates_ok = candidates_ok
        self.calls: list[tuple] = []

    def candidates(self, app):
        self.calls.append(("candidates", app))
        if not self._candidates_ok:
            return {"ok": False, "error": {"code": "session_not_started"}}
        return {"ok": True, "app": app, "window_title": "KCalc", "candidates": self._candidates}

    def act(self, app, action, target_ref, verification, *, expected=None, text=None, direction=None, steps=1):
        self.calls.append(("act", app, action, target_ref, verification, expected, text))
        if not self._acts:
            return {"ok": False, "error": {"code": "act_failed"}}
        return self._acts.pop(0)


class ScriptedSelector:
    """Replays answers in order and records the exact requests the loop built."""

    def __init__(self, answers, model="scripted"):
        self.answers = list(answers)
        self.requests: list[dict] = []
        self.calls = 0
        self.model = model

    def select(self, request, *, options):
        self.requests.append(request)
        if self.calls >= len(self.answers):
            raise js.SelectorError("scripted_answers_exhausted")
        answer = self.answers[self.calls]
        self.calls += 1
        decision = js.validate_answer(answer, options=options)
        return js.SelectedDecision(action=decision.action, target_ref=decision.target_ref,
                                   confidence=decision.confidence, probabilities=decision.probabilities,
                                   model=self.model, usage={"input_tokens": 10, "output_tokens": 5})


def _answer(action, target=None):
    answers = {"action": {"type": "choice", "choice": action, "confidence": 0.9,
                          "probabilities": {action: 0.9}}}
    if target is not None:
        answers["target"] = {"type": "choice", "choice": target}
    return answers


def _run(client, selector, **kwargs):
    kwargs.setdefault("max_steps", 8)
    kwargs.setdefault("max_calls", 8)
    kwargs.setdefault("counts_provider_calls", True)
    return run_loop(client=client, selector=selector, app="kcalc", goal="enter 1 then 2", **kwargs)


class LoopOutcomeTests(unittest.TestCase):
    def test_verified_steps_then_done_is_done_verified(self):
        client = FakeClient(acts=[dict(PASSED), dict(PASSED)])
        selector = ScriptedSelector([_answer("click", 1), _answer("click", 2), _answer("done")])
        report = _run(client, selector, expectations={"One": "1", "Two": "12"}, final_expected="12")
        self.assertEqual(report["outcome"], "done_verified")
        self.assertEqual(report["stop_reason"], "model_done")
        self.assertEqual(report["provider_calls"], 3)
        self.assertEqual(report["usage"], {"input_tokens": 30, "output_tokens": 15})
        self.assertEqual(report["verified_postcondition"]["expected"], "12")
        # Every click carried a caller-supplied display expectation, never the model's word.
        acts = [call for call in client.calls if call[0] == "act"]
        self.assertEqual([(call[4], call[5]) for call in acts], [("display_text", "1"), ("display_text", "12")])
        self.assertTrue(all(step["ok"] for step in report["steps"]))

    def test_done_without_a_passed_check_is_not_success(self):
        report = _run(FakeClient(), ScriptedSelector([_answer("done")]), expectations={"One": "1"})
        self.assertEqual(report["outcome"], "done_unverified")
        self.assertIn("no strong check", report["note"])

    def test_partial_progress_does_not_prove_task_completion(self):
        report = _run(FakeClient(acts=[dict(PASSED)]),
                      ScriptedSelector([_answer("click", 1), _answer("done")]),
                      expectations={"One": "1", "Two": "12"}, final_expected="12")
        self.assertEqual(report["outcome"], "done_unverified")

    def test_no_final_contract_cannot_claim_completion(self):
        report = _run(FakeClient(acts=[dict(PASSED)]),
                      ScriptedSelector([_answer("click", 1), _answer("done")]),
                      expectations={"One": "1"})
        self.assertEqual(report["outcome"], "done_unverified")

    def test_later_action_invalidates_previous_completion_evidence(self):
        report = _run(FakeClient(acts=[dict(PASSED), dict(PASSED)]),
                      ScriptedSelector([_answer("click", 1), _answer("click", 2), _answer("done")]),
                      expectations={"One": "1"}, final_expected="1")
        self.assertEqual(report["outcome"], "done_unverified")
        self.assertIsNone(report["verified_postcondition"])

    def test_a_click_the_policy_cannot_verify_is_refused(self):
        client = FakeClient(acts=[{"ok": False, "error": {"code": "unsupported_verification"}}])
        report = _run(client, ScriptedSelector([_answer("click", 2)]), expectations={})
        self.assertEqual(report["outcome"], "verification_unavailable")
        self.assertEqual(report["error"], "unsupported_verification")
        self.assertIn("refuses to act unverified", report["note"])

    def test_a_vacuous_expectation_is_refused_by_the_precondition(self):
        client = FakeClient(acts=[{"ok": False, "error": {"code": "action_precondition_failed"}}])
        report = _run(client, ScriptedSelector([_answer("click", 2)]), expectations={"Two": "2"})
        self.assertEqual(report["outcome"], "verification_unavailable")
        self.assertEqual(report["error"], "action_precondition_failed")

    def test_a_failed_check_stops_the_loop_immediately(self):
        client = FakeClient(acts=[{"ok": False, "error": {"code": "verification_failed"}}])
        report = _run(client, ScriptedSelector([_answer("click", 1), _answer("done")]),
                      expectations={"One": "9"})
        self.assertEqual(report["outcome"], "wrong_choice")
        self.assertEqual(report["stop_reason"], "verification_failed")
        self.assertEqual(len(report["steps"]), 1)

    def test_typing_is_refused_because_only_a_text_helper_may_type(self):
        report = _run(FakeClient(), ScriptedSelector([_answer("type_text", 3)]), expectations={})
        self.assertEqual(report["outcome"], "unsupported_action")
        self.assertEqual(report["error"], "type_text")

    def test_an_unenumerated_target_is_an_invalid_answer(self):
        report = _run(FakeClient(), ScriptedSelector([_answer("click", 99)]), expectations={"One": "1"})
        self.assertEqual(report["outcome"], "invalid_answer")
        self.assertEqual(report["error"], "unenumerated_target")

    def test_budgets_stop_the_loop(self):
        acts = [dict(PASSED), dict(PASSED), dict(PASSED)]
        answers = [_answer("click", 1)] * 3
        report = _run(FakeClient(acts=acts), ScriptedSelector(answers),
                      expectations={"One": "1"}, max_steps=2)
        self.assertEqual(report["outcome"], "budget_exhausted")
        self.assertEqual(report["stop_reason"], "step_budget_exhausted")
        report = _run(FakeClient(acts=list(acts)), ScriptedSelector(list(answers)),
                      expectations={"One": "1"}, max_steps=8, max_calls=1)
        self.assertEqual(report["stop_reason"], "call_budget_exhausted")

    def test_a_separate_action_limit_stops_the_loop(self):
        acts = [dict(PASSED), dict(PASSED)]
        answers = [_answer("click", 1), _answer("click", 2), _answer("click", 1)]
        client = FakeClient(acts=acts)
        report = _run(client, ScriptedSelector(answers),
                      expectations={"One": "1", "Two": "12"}, max_actions=2)
        self.assertEqual(report["stop_reason"], "action_budget_exhausted")
        self.assertEqual(report["outcome"], "budget_exhausted")
        self.assertEqual(report["actions"], 2)
        self.assertEqual(len([call for call in client.calls if call[0] == "act"]), 2)

    def test_a_failed_paid_attempt_is_counted_not_reported_as_zero(self):
        class FailingTransportSelector:
            def __init__(self):
                self.transport_calls = 0

            def select(self, request, *, options):
                self.transport_calls += 1
                raise js.SelectorError("selector_transport_failed")

        report = _run(FakeClient(), FailingTransportSelector(), expectations={})
        self.assertEqual(report["outcome"], "invalid_answer")
        self.assertEqual(report["error"], "selector_transport_failed")
        self.assertEqual(report["provider_calls"], 1)
        self.assertEqual(report["provider_attempts"], 1)
        self.assertEqual(report["provider_answers"], 0)

    def test_provider_answers_counts_only_validated_decisions(self):
        report = _run(FakeClient(acts=[dict(PASSED)]),
                      ScriptedSelector([_answer("click", 1), _answer("done")]),
                      expectations={"One": "1"}, final_expected="1")
        self.assertEqual(report["provider_calls"], 2)
        self.assertEqual(report["provider_answers"], 2)
        self.assertEqual(report["outcome"], "done_verified")

    def test_an_invalid_answer_counts_as_an_attempt_but_not_an_answer(self):
        report = _run(FakeClient(), ScriptedSelector([_answer("click", 99)]), expectations={"One": "1"})
        self.assertEqual(report["outcome"], "invalid_answer")
        self.assertEqual(report["provider_calls"], 1)
        self.assertEqual(report["provider_answers"], 0)

    def test_owner_errors_and_empty_states_are_reported_not_guessed(self):
        report = _run(FakeClient(candidates_ok=False), ScriptedSelector([]), expectations={})
        self.assertEqual(report["outcome"], "owner_error")
        self.assertEqual(report["error"], "session_not_started")
        report = _run(FakeClient(candidates=[]), ScriptedSelector([]), expectations={})
        self.assertEqual(report["outcome"], "no_options")

    def test_wait_re_enumerates_without_acting(self):
        client = FakeClient(acts=[dict(PASSED)])
        selector = ScriptedSelector([_answer("wait"), _answer("click", 1), _answer("done")])
        report = _run(client, selector, expectations={"One": "1"}, final_expected="1")
        self.assertEqual(report["outcome"], "done_verified")
        self.assertEqual(len(report["steps"]), 3)
        self.assertEqual(len([call for call in client.calls if call[0] == "act"]), 1)


class LoopHistoryTests(unittest.TestCase):
    def test_completed_actions_are_appended_to_the_next_state_as_labels_only(self):
        client = FakeClient(acts=[dict(PASSED), dict(PASSED)])
        selector = ScriptedSelector([_answer("click", 1), _answer("click", 2), _answer("done")])
        report = _run(client, selector, expectations={"One": "1", "Two": "12"}, final_expected="12")
        self.assertEqual(report["outcome"], "done_verified")
        self.assertNotIn("already done", selector.requests[0]["state"])
        second = selector.requests[1]["state"]
        self.assertIn("already done (oldest first):", second)
        self.assertIn("- click 'One'", second)
        # Labels only: the verified display value must not leak into the state.
        self.assertNotIn("12", second)
        self.assertNotIn("expected", second)


class ProbeScriptedSelector(ScriptedSelector):
    """Like ScriptedSelector, but validates the study-local document actions too."""

    def select(self, request, *, options):
        self.requests.append(request)
        if self.calls >= len(self.answers):
            raise js.SelectorError("scripted_answers_exhausted")
        answer = self.answers[self.calls]
        self.calls += 1
        decision = validate_probe_answer(answer, options=options)
        return js.SelectedDecision(action=decision.action, target_ref=decision.target_ref,
                                   confidence=decision.confidence, probabilities=decision.probabilities,
                                   model=self.model, usage={"input_tokens": 10, "output_tokens": 5})


def _kate_index(ref):
    state = js.state_from_candidates("kate", "handover.txt", "goal", KATE_CANDIDATES)
    return next(index for index, option in enumerate(state.options, start=1) if option.ref == ref)


KATE_EDITOR = _kate_index("k_editor")
KATE_SAVE_BUTTON = _kate_index("k_save")


class DocumentLoopTests(unittest.TestCase):
    def _run_kate(self, acts, answers, **kwargs):
        kwargs.setdefault("text_plan", [DRAFT, REVISION])
        kwargs.setdefault("final_expected", REVISION)
        kwargs.setdefault("max_steps", 8)
        kwargs.setdefault("max_calls", 8)
        kwargs.setdefault("counts_provider_calls", True)
        client = FakeClient(candidates=KATE_CANDIDATES, acts=acts)
        selector = ProbeScriptedSelector(answers)
        report = run_loop(client=client, selector=selector, app="kate",
                          goal="replace the document and save it", **kwargs)
        return client, selector, report

    def test_replace_and_save_of_the_final_revision_is_done_verified(self):
        answers = [_answer("replace_document", KATE_EDITOR), _answer("save_document", KATE_EDITOR),
                   _answer("replace_document", KATE_EDITOR), _answer("save_document", KATE_EDITOR),
                   _answer("done")]
        client, _, report = self._run_kate([dict(PASSED)] * 4, answers)
        self.assertEqual(report["outcome"], "done_verified")
        self.assertEqual(report["provider_calls"], 5)
        acts = [call for call in client.calls if call[0] == "act"]
        self.assertEqual([(call[2], call[4]) for call in acts],
                         [("replace_document", "target_text"), ("save_document", "document_saved"),
                          ("replace_document", "target_text"), ("save_document", "document_saved")])
        # Caller-owned text only: the draft then the revision, as both text and expected.
        self.assertEqual([call[6] for call in acts], [DRAFT, None, REVISION, None])
        self.assertEqual([call[5] for call in acts], [DRAFT, None, REVISION, None])
        self.assertEqual(report["verified_postcondition"]["kind"], "document_saved")

    def test_saving_the_old_revision_is_not_completion(self):
        answers = [_answer("replace_document", KATE_EDITOR), _answer("save_document", KATE_EDITOR),
                   _answer("done")]
        _, _, report = self._run_kate([dict(PASSED), dict(PASSED)], answers,
                                      text_plan=[DRAFT], final_expected=REVISION)
        self.assertEqual(report["outcome"], "done_unverified")

    def test_early_done_before_any_save_is_not_completion(self):
        _, _, report = self._run_kate([dict(PASSED)],
                                      [_answer("replace_document", KATE_EDITOR), _answer("done")])
        self.assertEqual(report["outcome"], "done_unverified")

    def test_a_later_revision_invalidates_the_saved_completion_evidence(self):
        answers = [_answer("replace_document", KATE_EDITOR), _answer("save_document", KATE_EDITOR),
                   _answer("replace_document", KATE_EDITOR), _answer("save_document", KATE_EDITOR),
                   _answer("done")]
        _, _, report = self._run_kate([dict(PASSED)] * 4, answers,
                                      text_plan=[REVISION, DRAFT], final_expected=REVISION)
        self.assertEqual(report["outcome"], "done_unverified")

    def test_replace_without_caller_text_is_refused_without_acting(self):
        client, _, report = self._run_kate([], [_answer("replace_document", KATE_EDITOR)], text_plan=[])
        self.assertEqual(report["outcome"], "unsupported_action")
        self.assertEqual(report["error"], "text_plan_exhausted")
        self.assertEqual([call for call in client.calls if call[0] == "act"], [])

    def test_a_target_that_does_not_support_replacement_is_refused(self):
        # save_document on a Save button is an enumerated action on an enumerated
        # target but not a supported pair: it must be rejected, never reinterpreted.
        client, _, report = self._run_kate([], [_answer("replace_document", KATE_SAVE_BUTTON)])
        self.assertEqual(report["outcome"], "invalid_answer")
        self.assertEqual(report["stop_reason"], "selector_error")
        self.assertEqual(report["error"], "action_not_supported_by_target")
        self.assertEqual([call for call in client.calls if call[0] == "act"], [])

    def test_save_document_on_a_button_is_an_invalid_answer(self):
        client, _, report = self._run_kate([], [_answer("save_document", KATE_SAVE_BUTTON)])
        self.assertEqual(report["outcome"], "invalid_answer")
        self.assertEqual(report["error"], "action_not_supported_by_target")
        self.assertEqual([call for call in client.calls if call[0] == "act"], [])

    def test_a_malformed_document_action_type_is_rejected(self):
        malformed = {"action": {"type": "nlu", "choice": "save_document", "confidence": 0.9},
                     "target": {"type": "choice", "choice": KATE_EDITOR}}
        client, _, report = self._run_kate([], [malformed])
        self.assertEqual(report["outcome"], "invalid_answer")
        self.assertEqual(report["error"], "action_answer_invalid")
        self.assertEqual([call for call in client.calls if call[0] == "act"], [])

    def test_an_intervening_action_invalidates_the_remembered_document_digest(self):
        # replace(A) -> click -> save: the owner only proves disk==editor at save
        # time, so the remembered A must not be reused as the saved content.
        answers = [_answer("replace_document", KATE_EDITOR), _answer("click", KATE_EDITOR),
                   _answer("save_document", KATE_EDITOR), _answer("done")]
        _, _, report = self._run_kate([dict(PASSED)] * 3, answers, text_plan=[REVISION])
        self.assertEqual(report["outcome"], "done_unverified")
        self.assertEqual(report["verified_postcondition"]["kind"], "document_saved")
        self.assertIsNone(report["verified_postcondition"]["sha256"])

    def test_document_text_never_reaches_state_history_or_report(self):
        answers = [_answer("replace_document", KATE_EDITOR), _answer("save_document", KATE_EDITOR),
                   _answer("replace_document", KATE_EDITOR), _answer("save_document", KATE_EDITOR),
                   _answer("done")]
        _, selector, report = self._run_kate([dict(PASSED)] * 4, answers)
        self.assertEqual(report["outcome"], "done_verified")
        rendered = json.dumps(report)
        self.assertNotIn(DRAFT.strip(), rendered)
        self.assertNotIn(REVISION.strip(), rendered)
        for request in selector.requests:
            self.assertNotIn(DRAFT.strip(), request["state"])
            self.assertNotIn(REVISION.strip(), request["state"])

    def test_a_failed_document_action_stops_with_its_code(self):
        _, _, report = self._run_kate(
            [{"ok": False, "error": {"code": "verification_failed"}}],
            [_answer("replace_document", KATE_EDITOR)])
        self.assertEqual(report["outcome"], "wrong_choice")
        self.assertEqual(report["stop_reason"], "verification_failed")


class ProbeVocabularyTests(unittest.TestCase):
    def test_document_actions_are_enumerated_in_state_and_question_criteria(self):
        from tools.jev_loop_probe import build_probe_request, state_from_probe_candidates
        state = state_from_probe_candidates("kate", "handover.txt", "save the document", KATE_CANDIDATES)
        editor = next(option for option in state.options if option.ref == "k_editor")
        self.assertIn("replace_document", editor.actions)
        self.assertIn("save_document", editor.actions)
        request = build_probe_request(state)
        self.assertIn("replace_document", request["questions"]["action"]["criteria"])
        self.assertIn("save_document", request["questions"]["action"]["criteria"])
        self.assertIn("replace_document", request["state"])

    def test_an_unenumerated_document_target_is_rejected(self):
        from tools.jev_loop_probe import state_from_probe_candidates
        state = state_from_probe_candidates("kate", "handover.txt", "save", KATE_CANDIDATES)
        with self.assertRaises(js.SelectorError):
            validate_probe_answer(_answer("replace_document", 99), options=state.options)


if __name__ == "__main__":
    unittest.main()
