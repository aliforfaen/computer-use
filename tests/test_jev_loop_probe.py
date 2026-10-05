"""Offline tests for the Jev driver loop.

No network, no owner socket, no provider: the loop takes an injected client and
selector, so every stop reason can be provoked. See
docs/31-jev-control-plan.md.
"""

from __future__ import annotations

import unittest

import jevdesktop.jev_selector as js
from tools.jev_loop_probe import run_loop


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

    def act(self, app, action, target_ref, verification, *, expected=None, direction=None, steps=1):
        self.calls.append(("act", app, action, target_ref, verification, expected))
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
        report = _run(client, selector, expectations={"One": "1", "Two": "12"})
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

    def test_owner_errors_and_empty_states_are_reported_not_guessed(self):
        report = _run(FakeClient(candidates_ok=False), ScriptedSelector([]), expectations={})
        self.assertEqual(report["outcome"], "owner_error")
        self.assertEqual(report["error"], "session_not_started")
        report = _run(FakeClient(candidates=[]), ScriptedSelector([]), expectations={})
        self.assertEqual(report["outcome"], "no_options")

    def test_wait_re_enumerates_without_acting(self):
        client = FakeClient(acts=[dict(PASSED)])
        selector = ScriptedSelector([_answer("wait"), _answer("click", 1), _answer("done")])
        report = _run(client, selector, expectations={"One": "1"})
        self.assertEqual(report["outcome"], "done_verified")
        self.assertEqual(len(report["steps"]), 3)
        self.assertEqual(len([call for call in client.calls if call[0] == "act"]), 1)


class LoopHistoryTests(unittest.TestCase):
    def test_completed_actions_are_appended_to_the_next_state_as_labels_only(self):
        client = FakeClient(acts=[dict(PASSED), dict(PASSED)])
        selector = ScriptedSelector([_answer("click", 1), _answer("click", 2), _answer("done")])
        report = _run(client, selector, expectations={"One": "1", "Two": "12"})
        self.assertEqual(report["outcome"], "done_verified")
        self.assertNotIn("already done", selector.requests[0]["state"])
        second = selector.requests[1]["state"]
        self.assertIn("already done (oldest first):", second)
        self.assertIn("- click 'One'", second)
        # Labels only: the verified display value must not leak into the state.
        self.assertNotIn("12", second)
        self.assertNotIn("expected", second)


if __name__ == "__main__":
    unittest.main()
