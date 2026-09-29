import unittest

from p2_kcalc import Button, ProbeError, parse_buttons, parse_decision, parse_display, verify_transition


class P2ValidationTests(unittest.TestCase):
    def test_only_visible_enabled_sensitive_press_button_is_candidate(self):
        tree = '''
  - [button] "One" (enabled, focusable, sensitive, showing, visible) @ screen (328, 493, 100x56) [actions: Press, SetFocus]
  - [button] "Two" (enabled, sensitive, showing, visible) @ unavailable (empty-extents) [actions: Press]
  - [button] "Three" (enabled, sensitive, showing, visible) @ screen (0, 0, 0x10) [actions: Press]
  - [button] "Four" (enabled, showing, visible) @ screen (1, 2, 20x20) [actions: Press]
  - [button] "Five" (enabled, sensitive, showing, visible) @ screen (1, 2, 20x20) [actions: SetFocus]
'''
        self.assertEqual(parse_buttons(tree), [Button("button.One", "One", 328, 493, 100, 56)])

    def test_duplicate_visible_names_fail_closed(self):
        line = '  - [button] "One" (enabled, sensitive, showing, visible) @ screen (1, 2, 20x20) [actions: Press]'
        with self.assertRaises(ProbeError):
            parse_buttons(f"{line}\n{line}")

    def test_display_parser_distinguishes_blank_from_one(self):
        base = '- [text] "" (editable, enabled, focusable, sensitive, showing, visible) @ screen (1, 2, 30x10) [actions: SetFocus]'
        self.assertEqual(parse_display(base), "")
        self.assertEqual(parse_display(base.replace(" [actions:", " text='1' [actions:")), "1")

    def test_selector_rejects_low_confidence_wrong_and_unenumerated_choices(self):
        def result(action="press", target="button.One", action_conf=0.95, target_conf=0.95):
            return {"answers": {
                "action": {"type": "choice", "choice": action, "confidence": action_conf},
                "target": {"type": "choice", "choice": target, "confidence": target_conf},
            }}

        self.assertEqual(parse_decision(result(), {"button.One"})[:2], ("press", "button.One"))
        for invalid in (
            result(action_conf=0.79),
            result(target_conf=0.79),
            result(action="type_text"),
            result(target="button.Zero"),
            result(target=None),
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ProbeError):
                parse_decision(invalid, {"button.One"})

    def test_verifier_requires_exact_empty_to_one_transition(self):
        self.assertTrue(verify_transition("", "1"))
        self.assertFalse(verify_transition("0", "1"))
        self.assertFalse(verify_transition("", "1.0"))


if __name__ == "__main__":
    unittest.main()
