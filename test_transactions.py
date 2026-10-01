from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from transactions import Action, AuditLog, Policy, TransactionEngine, TransactionError, Verification


class FakeBackend:
    def __init__(self):
        self.tree = []
        self.identity = "17"
        self.is_active = True
        self.clicks = []
        self.typed = []
        self.on_click = None
        self.on_type = None
        self.block_click = None
        self.release_click = threading.Event()

    def accessibility_elements(self, app):
        return [dict(row) for row in self.tree]

    def window_geometry(self, app):
        return (
            f'Windows (1):\n- org.kde.{app} "document"\n    id: {self.identity}\n'
            "    frame: 0, 0, 640x400\n    client: (0, 0, 640x400)"
        )

    def active_window(self):
        marker = " [active]" if self.is_active else ""
        return f'Active window:\n- org.kde.kate "document"{marker}\n    id: {self.identity}'

    def mouse_click(self, x, y, button="left"):
        self.clicks.append((x, y, button))
        if self.block_click is not None:
            self.block_click.set()
            self.release_click.wait(2)
        if self.on_click:
            self.on_click()

    def keyboard_type(self, text):
        self.typed.append(text)
        if self.on_type:
            self.on_type(text)


def button(*, states=None, name="Apply", x=10, actions=("Press",)):
    return {
        "role": "button", "name": name,
        "states": list(states if states is not None else ("enabled", "sensitive", "showing", "visible")),
        "actions": list(actions), "x": x, "y": 20, "width": 80, "height": 30,
        "mapped": True, "text": "",
    }


def editor(*, text="", focused=False):
    states = ["editable", "enabled", "sensitive", "showing", "visible"]
    if focused:
        states.append("focused")
    return {
        "role": "text", "name": "", "states": states, "actions": ["SetFocus"],
        "x": 10, "y": 60, "width": 500, "height": 200,
        "mapped": True, "text": text,
    }


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.audit_path = Path(self.temp.name) / "audit.jsonl"
        self.backend = FakeBackend()
        self.engine = TransactionEngine(
            self.backend,
            audit=AuditLog(self.audit_path),
            policy=Policy(allowed_apps=frozenset({"kate", "firefox"})),
        )

    def tearDown(self):
        self.temp.cleanup()

    def _snapshot_button(self):
        self.backend.tree = [button()]
        return self.engine.observe("s-test", "kate")

    def _act_click(self, snap, verifier=lambda _snap: Verification(True)):
        target = snap.candidates[0]
        return self.engine.act("s-test", "kate", Action("click", target.ref), verifier=verifier)

    def test_candidate_refs_are_generation_scoped_and_reject_stale(self):
        first = self._snapshot_button()
        second = self.engine.observe("s-test", "kate")
        with self.assertRaisesRegex(TransactionError, "target_reference_stale"):
            self._act_click(first)
        self.assertNotEqual(first.candidates[0].ref, second.candidates[0].ref)
        self.assertEqual(self.backend.clicks, [])

    def test_rejects_changed_target_before_input(self):
        first = self._snapshot_button()
        self.backend.tree = [button(x=120)]
        with self.assertRaisesRegex(TransactionError, "target_changed_or_ambiguous"):
            self._act_click(first)
        self.assertEqual(self.backend.clicks, [])

    def test_disabled_button_is_enumerated_but_never_clicked(self):
        self.backend.tree = [button(states=("showing", "visible"), actions=())]
        snap = self.engine.observe("s-test", "kate")
        self.assertEqual(len(snap.candidates), 1)
        with self.assertRaisesRegex(TransactionError, "target_disabled_or_hidden"):
            self._act_click(snap)
        self.assertEqual(self.backend.clicks, [])

    def test_absent_ambiguous_or_inactive_targets_fail_closed(self):
        self.backend.tree = []
        empty = self.engine.observe("s-test", "kate")
        with self.assertRaisesRegex(TransactionError, "target_reference_stale"):
            self.engine.act(
                "s-test", "kate", Action("click", "missing"),
                verifier=lambda _snap: Verification(True),
            )

        self.backend.tree = [button(), button(x=130)]
        duplicate = self.engine.observe("s-test", "kate")
        self.assertEqual(duplicate.candidates, ())

        self.backend.tree = [button()]
        inactive = self.engine.observe("s-test", "kate")
        self.backend.is_active = False
        with self.assertRaisesRegex(TransactionError, "target_window_not_active"):
            self._act_click(inactive)
        self.assertEqual(self.backend.clicks, [])

    def test_verification_failure_latches_session_until_explicit_reset(self):
        snap = self._snapshot_button()
        with self.assertRaisesRegex(TransactionError, "verification_failed"):
            self._act_click(snap, lambda _snap: Verification(False, evidence="secret body"))
        with self.assertRaisesRegex(TransactionError, "session_requires_reset"):
            self.engine.observe("s-test", "kate")
        self.engine.reset_session("s-test")
        self.assertEqual(len(self.engine.observe("s-test", "kate").candidates), 1)

    def test_cancel_before_input_and_post_input_cancel(self):
        snap = self._snapshot_button()
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaisesRegex(TransactionError, "cancelled"):
            self.engine.act(
                "s-test", "kate", Action("click", snap.candidates[0].ref),
                verifier=lambda _snap: Verification(True), cancel=cancelled,
            )
        self.assertEqual(self.backend.clicks, [])

        self.engine.reset_session("s-test")
        snap = self._snapshot_button()
        cancelled.clear()
        self.backend.on_click = cancelled.set
        with self.assertRaisesRegex(TransactionError, "cancelled_after_action"):
            self.engine.act(
                "s-test", "kate", Action("click", snap.candidates[0].ref),
                verifier=lambda _snap: Verification(True), cancel=cancelled,
            )
        with self.assertRaisesRegex(TransactionError, "session_requires_reset"):
            self.engine.observe("s-test", "kate")

    def test_cancel_during_verification_cannot_report_success(self):
        snap = self._snapshot_button()
        event = threading.Event()
        def verifier(_after):
            event.set()
            return Verification(True)
        with self.assertRaisesRegex(TransactionError, "cancelled_after_action"):
            self.engine.act("s-test", "kate", Action("click", snap.candidates[0].ref),
                            verifier=verifier, cancel=event)
        with self.assertRaisesRegex(TransactionError, "session_requires_reset"):
            self.engine.observe("s-test", "kate")

    def test_verifier_requires_boolean_and_session_identity_is_bound(self):
        snap = self._snapshot_button()
        with self.assertRaisesRegex(TransactionError, "session_identity_mismatch"):
            self.engine.observe("other-session", "kate")
        with self.assertRaisesRegex(TransactionError, "verifier_contract_invalid"):
            self._act_click(snap, lambda _: Verification("false"))

    def test_audit_redacts_typed_text_evidence_and_errors(self):
        secret = "PERSONAL-POEM-DO-NOT-LOG"
        self.backend.tree = [editor(focused=True)]
        snap = self.engine.observe("s-test", "kate")
        self.backend.on_type = lambda value: setattr(self.backend, "tree", [editor(text=value, focused=True)])
        result = self.engine.act(
            "s-test", "kate", Action("type_text", snap.candidates[0].ref, text=secret),
            verifier=lambda after: Verification(after.candidates[0].value == secret, evidence=secret),
        )
        self.assertEqual(result.evidence, secret)
        output = self.audit_path.read_text()
        self.assertNotIn(secret, output)
        records = [json.loads(line) for line in output.splitlines()]
        self.assertTrue(all(record["jev_answers"] is None for record in records))
        self.assertTrue(all("typed_text" not in record for record in records))

    def test_yolo_still_enforces_allowlist_and_caps_but_skips_approval(self):
        denied = TransactionEngine(self.backend, audit=AuditLog(self.audit_path))
        with self.assertRaisesRegex(TransactionError, "app_not_allowed"):
            denied.observe("s-deny", "kate", mode="yolo")

        snap = self._snapshot_button()
        result = self.engine.act(
            "s-test", "kate", Action("click", snap.candidates[0].ref),
            verifier=lambda _snap: Verification(True), mode="yolo",
        )
        self.assertEqual(result.status, "ok")

    def test_action_cap_and_exact_observation_reservation(self):
        self.engine = TransactionEngine(self.backend, audit=AuditLog(self.audit_path),
            policy=Policy(allowed_apps=frozenset({"kate"}), max_actions=1, max_observations=3))
        snap = self._snapshot_button()
        result = self._act_click(snap)
        self.assertEqual(result.verification, "passed")
        with self.assertRaisesRegex(TransactionError, "task_action_budget_exceeded"):
            self._act_click(result.after)
        self.assertEqual(len(self.backend.clicks), 1)

    def test_supervised_requires_approval(self):
        snap = self._snapshot_button()
        with self.assertRaisesRegex(TransactionError, "approval_denied"):
            self.engine.act(
                "s-test", "kate", Action("click", snap.candidates[0].ref),
                verifier=lambda _snap: Verification(True), mode="supervised",
            )

    def test_concurrent_call_is_rejected_while_transaction_owns_session(self):
        snap = self._snapshot_button()
        self.backend.block_click = threading.Event()
        reached_click = threading.Event()
        self.backend.block_click = threading.Event()

        def action_thread():
            self.backend.block_click = reached_click
            self.engine.act(
                "s-test", "kate", Action("click", snap.candidates[0].ref),
                verifier=lambda _snap: Verification(True),
            )

        worker = threading.Thread(target=action_thread)
        worker.start()
        self.assertTrue(reached_click.wait(1))
        with self.assertRaisesRegex(TransactionError, "session_busy"):
            self.engine.observe("s-test", "kate")
        with self.assertRaisesRegex(TransactionError, "session_busy"):
            self.engine.reset_session("s-test")
        self.backend.release_click.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    unittest.main()
