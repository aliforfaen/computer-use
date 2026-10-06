"""Offline tests for the bounded decision-model selector seam.

No network and no provider calls: the HTTP path is exercised with a fake client
and a fake environment. See docs/30-jev-selector-experiment.md.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

import jevdesktop.jev_selector as js
from tools.jev_selector_probe import KEY_NAME, _load_api_key, main as selector_probe_main


def _candidate(ref, role, label, actions, states=None, **extra):
    return {"ref": ref, "role": role, "label": label,
            "states": list(states or ("enabled", "sensitive", "showing", "visible")),
            "actions": list(actions), "unavailable_reason": None, **extra}


CANDIDATES = [
    _candidate("cand_a", "button", "One", ["click"]),
    _candidate("cand_b", "text", "Editor", ["type_text"], value="secret typed text"),
    _candidate("cand_c", "scroll pane", "Page", ["scroll"]),
    _candidate("cand_hidden", "button", "Hidden", ["click"], states=("enabled", "sensitive")),
    _candidate("cand_dead", "button", "Gone", ["click"], unavailable_reason="target_disabled_or_hidden"),
]


class StateBuilderTests(unittest.TestCase):
    def test_state_keeps_usable_options_and_drops_values_bounds_and_hidden_targets(self):
        state = js.state_from_candidates("kcalc", "KCalc", "enter 1", CANDIDATES)
        # Sorted by role then label, so the same desktop state renders identically.
        self.assertEqual([option.label for option in state.options], ["One", "Page", "Editor"])
        self.assertEqual([option.role for option in state.options], ["button", "scroll pane", "text"])
        text = js.render_state_text(state)
        self.assertIn("label='One'", text)
        self.assertIn("actions=click", text)
        for leaked in ("secret typed text", "cand_a", "bounds", "x=", "/home/", ";"):
            self.assertNotIn(leaked, text)
        # Options are enumerated positionally; the opaque ref stays in memory only.
        self.assertEqual(js.build_questions(state)["target"]["criteria"]["1"], "button: One")

    def test_goal_is_required_and_option_count_is_bounded(self):
        with self.assertRaises(js.SelectorError):
            js.state_from_candidates("kate", "Kate", "   ", CANDIDATES)
        with self.assertRaises(js.SelectorError):
            js.state_from_candidates("kate", "Kate", "goal", CANDIDATES, limit=js.MAX_OPTIONS + 1)
        state = js.state_from_candidates("kate", "Kate", "goal", CANDIDATES, limit=2)
        self.assertEqual(len(state.options), 2)

    def test_request_matches_the_documented_shape_and_budget_guard_fails_closed(self):
        state = js.state_from_candidates("kcalc", "KCalc", "enter 1", CANDIDATES)
        request = js.build_request(state, model="jev-1.13.0")
        self.assertEqual(request["model"], "jev-1.13.0")
        self.assertIsInstance(request["state"], str)
        self.assertEqual(set(request["questions"]), {"action", "target"})
        self.assertEqual(request["questions"]["action"]["type"], "choice")
        self.assertIn("click", request["questions"]["action"]["criteria"])
        with self.assertRaises(js.SelectorError):
            js.check_budget("x" * 4_000_000, {"action": {"type": "choice"}})


class AnswerValidationTests(unittest.TestCase):
    def setUp(self):
        self.state = js.state_from_candidates("kcalc", "KCalc", "enter 1", CANDIDATES)

    def _answer(self, action, target=None, confidence=0.9):
        answers = {"action": {"type": "choice", "choice": action, "confidence": confidence,
                              "probabilities": {"click": 0.9}}}
        if target is not None:
            answers["target"] = {"type": "choice", "choice": target}
        return answers

    def test_target_is_resolved_to_our_own_reference(self):
        decision = js.validate_answer(self._answer("click", "1"), options=self.state.options)
        self.assertEqual(decision.action, "click")
        self.assertEqual(decision.target_ref, "cand_a")
        self.assertEqual(decision.confidence, 0.9)
        self.assertEqual(js.validate_answer(self._answer("click", 3), options=self.state.options).target_ref,
                         "cand_b")
        self.assertEqual(js.validate_answer(self._answer("click", 2), options=self.state.options).target_ref,
                         "cand_c")

    def test_targetless_actions_need_no_target(self):
        decision = js.validate_answer(self._answer("done"), options=self.state.options)
        self.assertEqual((decision.action, decision.target_ref), ("done", None))

    def test_unanumerated_choices_are_rejected(self):
        for answers in (self._answer("launch_missiles", "1"),
                        self._answer("click", "99"),
                        self._answer("click", "One"),
                        self._answer("click", None),
                        {"action": {"type": "nlu", "choice": "click"}},
                        {"action": "click"}):
            with self.assertRaises(js.SelectorError):
                js.validate_answer(answers, options=self.state.options)

    def test_out_of_range_confidence_is_dropped_not_trusted(self):
        decision = js.validate_answer(self._answer("wait", confidence=7.0), options=self.state.options)
        self.assertIsNone(decision.confidence)


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def post(self, url, json=None, headers=None):
        self.requests.append({"url": url, "json": json, "headers": headers})
        return self.response


class HttpSelectorTests(unittest.TestCase):
    def setUp(self):
        self.state = js.state_from_candidates("kcalc", "KCalc", "enter 1", CANDIDATES)
        self.request = js.build_request(self.state)
        self.payload = {"model": "jev-1.13.0", "answers": {
            "action": {"type": "choice", "choice": "click", "confidence": 0.97, "probabilities": {"click": 0.97}},
            "target": {"type": "choice", "choice": "1"}}, "usage": {"input_tokens": 62, "output_tokens": 12}}

    def test_http_selector_sends_one_bounded_request_and_never_leaks_the_key_into_state(self):
        client = FakeClient(FakeResponse(200, self.payload))
        selector = js.HttpSelector(js.JevConfig(), client=client,
                                   environ={"JEV_ENABLED": "true", "JEV_API_KEY": "test-key"})
        decision = selector.select(self.request, options=self.state.options)
        self.assertEqual((decision.action, decision.target_ref, decision.model), ("click", "cand_a", "jev-1.13.0"))
        self.assertEqual(decision.usage["input_tokens"], 62)
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(client.requests[0]["headers"]["Authorization"], "Bearer test-key")
        self.assertNotIn("test-key", json.dumps(client.requests[0]["json"]))
        with self.assertRaises(js.SelectorError):
            selector.select(self.request, options=self.state.options)  # call cap

    def test_http_selector_is_disabled_by_default_and_accepts_common_true_values(self):
        for enabled in (None, "false", "0", "off", "no"):
            env = {"JEV_API_KEY": "test-key"}
            if enabled is not None:
                env["JEV_ENABLED"] = enabled
            client = FakeClient(FakeResponse(200, self.payload))
            selector = js.HttpSelector(js.JevConfig(), client=client, environ=env)
            with self.assertRaisesRegex(js.SelectorError, "selector_disabled"):
                selector.select(self.request, options=self.state.options)
            self.assertEqual(client.requests, [])
        for enabled in ("1", "true", "YES", "on"):
            self.assertTrue(js.jev_enabled({"JEV_ENABLED": enabled}))

    def test_missing_key_and_provider_errors_are_safe_and_typed(self):
        with self.assertRaisesRegex(js.SelectorError, "key_not_configured"):
            js.HttpSelector(js.JevConfig(), client=FakeClient(FakeResponse(200, self.payload)),
                            environ={"JEV_ENABLED": "true"}).select(self.request, options=self.state.options)
        for status, expected in ((429, "selector_http_429"), (500, "selector_http_500")):
            selector = js.HttpSelector(js.JevConfig(), client=FakeClient(FakeResponse(status, None)),
                                       environ={"JEV_ENABLED": "true", "JEV_API_KEY": "k"})
            with self.assertRaisesRegex(js.SelectorError, expected):
                selector.select(self.request, options=self.state.options)
        broken = js.HttpSelector(js.JevConfig(), client=FakeClient(FakeResponse(200, {"answers": []})),
                                 environ={"JEV_ENABLED": "true", "JEV_API_KEY": "k"})
        with self.assertRaisesRegex(js.SelectorError, "response_invalid"):
            broken.select(self.request, options=self.state.options)

    def test_recorded_selector_replays_an_answer_without_network(self):
        selector = js.RecordedSelector(self.payload["answers"], model="jev-recorded")
        decision = selector.select(self.request, options=self.state.options)
        self.assertEqual((decision.action, decision.target_ref), ("click", "cand_a"))
        self.assertEqual(decision.model, "jev-recorded")
        self.assertEqual(decision.usage, {})


class ProbeDotenvTests(unittest.TestCase):
    """The probe reads the Jev key from a private dotenv file, never from state."""

    def _env_file(self, tmp: str, body: str) -> Path:
        path = Path(tmp) / "jev.env"
        path.write_text(body, encoding="utf-8")
        return path

    def test_dotenv_supplies_the_key_when_the_environment_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._env_file(tmp, "# private key file\nJEV_API_KEY='not-a-real-key'\n")
            with mock.patch.dict(os.environ):
                os.environ.pop(KEY_NAME, None)
                self.assertTrue(_load_api_key(path))
                self.assertEqual(os.environ[KEY_NAME], "not-a-real-key")

    def test_environment_wins_and_a_missing_file_stays_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._env_file(tmp, f"{KEY_NAME}=from-file\n")
            with mock.patch.dict(os.environ, {KEY_NAME: "from-env"}):
                self.assertTrue(_load_api_key(path))
                self.assertEqual(os.environ[KEY_NAME], "from-env")
            with mock.patch.dict(os.environ):
                os.environ.pop(KEY_NAME, None)
                self.assertFalse(_load_api_key(Path(tmp) / "missing.env"))
                self.assertFalse(_load_api_key(None))

    def test_default_dotenv_is_the_private_config_dir(self):
        from jevdesktop.desktop_daemon import default_config_dir
        self.assertEqual(default_config_dir() / "jev.env", Path(os.environ.get(
            "XDG_CONFIG_HOME", Path.home() / ".config")) / "jev-desktop" / "jev.env")

    def test_paid_probe_stops_when_jev_is_disabled_before_loading_key_or_candidates(self):
        output = StringIO()
        with mock.patch.dict(os.environ, {"JEV_ENABLED": "false"}, clear=True):
            with mock.patch("tools.jev_selector_probe._load_api_key") as load_key:
                with redirect_stdout(output):
                    result = selector_probe_main([
                        "--candidates-json", "/missing/never-read.json",
                        "--goal", "enter 1", "--call",
                    ])
        self.assertEqual(result, 2)
        self.assertIn('"error": "jev_disabled"', output.getvalue())
        load_key.assert_not_called()


class HistoryTests(unittest.TestCase):
    """The driver loop's history is labels only, collapsed and bounded."""

    def test_history_renders_between_the_goal_and_the_options(self):
        state = js.state_from_candidates("kcalc", "KCalc", "enter 1 2 3", CANDIDATES,
                                         history=["click 'One'", "click 'Two'"])
        text = js.render_state_text(state)
        self.assertLess(text.index("goal:"), text.index("already done"))
        self.assertLess(text.index("already done"), text.index("options:"))
        self.assertIn("- click 'One'", text)
        self.assertIn("- click 'Two'", text)

    def test_history_entries_are_collapsed_bounded_and_drop_non_strings(self):
        state = js.state_from_candidates("kcalc", "KCalc", "enter 1", CANDIDATES,
                                         history=["click\n'One'", "   ", 7, "x" * 200])
        self.assertEqual(state.history[0], "click 'One'")
        self.assertEqual(state.history[1], "x" * js.HISTORY_ENTRY_CHARS)
        self.assertEqual(len(state.history), 2)
        long_state = js.state_from_candidates("kcalc", "KCalc", "enter 1", CANDIDATES,
                                             history=[f"step {index}" for index in range(50)])
        self.assertEqual(len(long_state.history), js.HISTORY_ENTRIES)
        self.assertEqual(long_state.history[-1], "step 49")

    def test_a_state_without_history_renders_exactly_as_before(self):
        state = js.state_from_candidates("kcalc", "KCalc", "enter 1", CANDIDATES)
        self.assertEqual(state.history, ())
        self.assertNotIn("already done", js.render_state_text(state))


class RecordedSelectorSequenceTests(unittest.TestCase):
    def setUp(self):
        self.state = js.state_from_candidates("kcalc", "KCalc", "enter 1", CANDIDATES)
        self.request = js.build_request(self.state)

    def _answer(self, action, target=None):
        answers = {"action": {"type": "choice", "choice": action, "confidence": 0.9}}
        if target is not None:
            answers["target"] = {"type": "choice", "choice": target}
        return answers

    def test_a_list_replays_one_answer_per_request(self):
        selector = js.RecordedSelector([self._answer("click", 1), self._answer("done")])
        first = selector.select(self.request, options=self.state.options)
        second = selector.select(self.request, options=self.state.options)
        self.assertEqual((first.action, first.target_ref), ("click", "cand_a"))
        self.assertEqual(second.action, "done")
        self.assertEqual(selector.calls, 2)
        with self.assertRaises(js.SelectorError):
            selector.select(self.request, options=self.state.options)

    def test_a_dict_still_replays_the_same_answer_every_time(self):
        selector = js.RecordedSelector(self._answer("click", 2))
        for _ in range(3):
            self.assertEqual(selector.select(self.request, options=self.state.options).target_ref, "cand_c")


if __name__ == "__main__":
    unittest.main()
