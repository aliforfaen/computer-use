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
        self.app = "kate"
        self.is_active = True
        self.clicks = []
        self.typed = []
        self.keys = []
        self.scrolls = []
        self.on_click = None
        self.on_type = None
        self.document_keys = []
        self.saved_bytes = b""
        self.on_document_key = None
        self.block_click = None
        self.release_click = threading.Event()

    def accessibility_elements(self, app):
        return [dict(row) for row in self.tree]

    def window_geometry(self, app):
        window_app = {"kate": "org.kde.kate", "firefox": "org.mozilla.firefox",
                      "kcalc": "org.kde.kcalc"}.get(self.app, self.app)
        return (
            f'Windows (1):\n- {window_app} "{getattr(self, "title", "document")}"\n    id: {self.identity}\n'
            "    frame: 0, 0, 640x400\n    client: (0, 0, 640x400)"
        )

    def active_window(self):
        marker = " [active]" if self.is_active else ""
        window_app = {"kate": "org.kde.kate", "firefox": "org.mozilla.firefox",
                      "kcalc": "org.kde.kcalc"}.get(self.app, self.app)
        return f'Active window:\n- {window_app} "{getattr(self, "title", "document")}"{marker}\n    id: {self.identity}'

    def mouse_click(self, x, y, button="left"):
        self.clicks.append((x, y, button))
        if self.block_click is not None:
            self.block_click.set()
            self.release_click.wait(2)
        if self.on_click:
            self.on_click()

    def keyboard_type(self, text):
        self.typed.append(text)
        if self.tree and self.tree[0].get("role") in {"text", "text entry", "entry", "combo box"}:
            self.tree[0]["text"] = text
        if self.on_type:
            self.on_type(text)

    def keyboard_key(self, key):
        self.keys.append(key)

    def mouse_scroll(self, x, y, delta, *, steps=1):
        self.scrolls.append((x, y, delta, steps))
        for row in self.tree:
            if row.get("role") in {"scroll bar", "scrollbar"}:
                row["value"] = row.get("value", 0) + delta * steps

    def document_key(self, operation):
        self.document_keys.append(operation)
        if self.on_document_key:
            self.on_document_key(operation)
        if operation == "save":
            current = self.tree[0].get("text", "") if self.tree else ""
            self.saved_bytes = current.encode("utf-8")

    def document_bytes(self):
        return self.saved_bytes


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

    def test_firefox_links_are_fresh_click_targets(self):
        self.backend.app = "firefox"
        self.backend.tree = [{"role": "link", "name": "About", "states": ["enabled", "sensitive", "showing", "visible"],
                              "actions": ["Jump"], "x": 20, "y": 20, "width": 70, "height": 24,
                              "mapped": True, "text": ""}]
        snap = self.engine.observe("s-firefox", "firefox")
        self.assertEqual(snap.candidates[0].label, "About")
        self.assertEqual(snap.candidates[0].actions, ("click",))

    def test_firefox_address_navigation_replaces_url_and_restricts_scheme(self):
        self.backend.app = "firefox"
        self.backend.tree = [{"role": "combo box", "name": "Search with Google or enter address",
                              "states": ["editable", "enabled", "sensitive", "showing", "visible", "focused"],
                              "actions": ["SetFocus"], "x": 20, "y": 20, "width": 400, "height": 28,
                              "mapped": True, "text": "https://old.example/"}]
        snap = self.engine.observe("s-firefox", "firefox")
        candidate = snap.candidates[0]
        self.assertIn("navigate_url", candidate.actions)
        url = "https://www.python.org/about/"
        result = self.engine.act("s-firefox", "firefox", Action("navigate_url", candidate.ref, text=url),
                                verifier=lambda after: Verification(after.candidates[0].value == url))
        self.assertEqual(result.verification, "passed")
        self.assertEqual(self.backend.keys, ["ctrl+a", "Return"])
        self.assertEqual(self.backend.typed, [url])

    def test_firefox_address_rejects_non_web_navigation_before_input(self):
        self.backend.app = "firefox"
        self.backend.tree = [{"role": "combo box", "name": "Address bar",
                              "states": ["editable", "enabled", "sensitive", "showing", "visible", "focused"],
                              "actions": ["SetFocus"], "x": 20, "y": 20, "width": 400, "height": 28,
                              "mapped": True, "text": ""}]
        snap = self.engine.observe("s-firefox", "firefox")
        with self.assertRaisesRegex(TransactionError, "navigation_url_not_allowed"):
            self.engine.act("s-firefox", "firefox", Action("navigate_url", snap.candidates[0].ref,
                                                               text="file:///etc/passwd"),
                            verifier=lambda _after: Verification(True))
        self.assertEqual(self.backend.typed, [])
        self.assertEqual(self.backend.keys, [])

    def test_scroll_requires_and_measures_accessible_position_change(self):
        self.backend.app = "firefox"
        self.backend.tree = [
            {"role": "document frame", "name": "Page", "states": ["enabled", "sensitive", "showing", "visible"],
             "actions": [], "x": 20, "y": 80, "width": 500, "height": 300, "mapped": True, "text": ""},
            {"role": "scroll bar", "name": "Vertical", "states": ["enabled", "sensitive", "showing", "visible"],
             "actions": [], "x": 510, "y": 80, "width": 12, "height": 300, "mapped": True,
             "text": "", "value": 0.0, "value_max": 100.0},
        ]
        snap = self.engine.observe("s-firefox", "firefox")
        page = next(c for c in snap.candidates if c.role == "document frame")
        before_value = next(c.value_number for c in snap.candidates if c.role == "scroll bar")
        result = self.engine.act("s-firefox", "firefox", Action("scroll", page.ref, direction="down", steps=2),
                                verifier=lambda after: Verification(any(c.role == "scroll bar" and c.value_number != before_value
                                                                          for c in after.candidates)))
        self.assertEqual(result.verification, "passed")
        self.assertEqual(self.backend.scrolls, [(270, 230, 2, 2)])

    def test_scroll_without_atspi_scroll_position_is_not_attempted(self):
        self.backend.app = "firefox"
        self.backend.tree = [{"role": "document frame", "name": "Page", "states": ["enabled", "sensitive", "showing", "visible"],
                              "actions": [], "x": 20, "y": 80, "width": 500, "height": 300,
                              "mapped": True, "text": ""}]
        snap = self.engine.observe("s-firefox", "firefox")
        with self.assertRaisesRegex(TransactionError, "scroll_position_unavailable"):
            self.engine.act("s-firefox", "firefox", Action("scroll", snap.candidates[0].ref,
                                                               direction="down", steps=1),
                            verifier=lambda _after: Verification(True))
        self.assertEqual(self.backend.scrolls, [])

    def test_rejects_changed_target_before_input(self):
        first = self._snapshot_button()
        self.backend.tree = [button(x=120)]
        with self.assertRaisesRegex(TransactionError, "target_changed_or_ambiguous"):
            self._act_click(first)
        self.assertEqual(self.backend.clicks, [])

    def test_fresh_precondition_refuses_input_without_poisoning_session(self):
        first = self._snapshot_button()
        seen = []
        def refuse(snapshot):
            seen.append(snapshot)
            return False
        with self.assertRaisesRegex(TransactionError, "action_precondition_failed"):
            self.engine.act("s-test", "kate", Action("click", first.candidates[0].ref),
                             verifier=lambda _: Verification(True), precondition=refuse)
        self.assertEqual(self.backend.clicks, [])
        self.assertEqual(len(seen), 1)
        self.assertNotEqual(seen[0].generation, first.generation)
        next_snapshot = self.engine.observe("s-test", "kate")
        self.assertEqual(self._act_click(next_snapshot).status, "ok")

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
        self.assertEqual(len(duplicate.candidates), 2)
        self.assertTrue(all(c.unavailable_reason == "target_ambiguous" and not c.actions
                            for c in duplicate.candidates))

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

    def test_replace_document_revises_focused_editor_and_verifies_exact_text(self):
        content = "Project handover\nSaved through Kate."
        self.backend.tree = [editor(text="Original handover", focused=True)]
        snap = self.engine.observe("s-test", "kate")
        self.backend.on_type = lambda value: setattr(
            self.backend, "tree", [editor(text=value, focused=True)])
        result = self.engine.act(
            "s-test", "kate", Action("replace_document", snap.candidates[0].ref, text=content),
            verifier=lambda after: Verification(after.candidates[0].value == content),
        )
        self.assertEqual(result.status, "ok")
        self.assertEqual(self.backend.document_keys, ["select_all"])
        self.assertEqual(self.backend.typed, [content])
        self.assertEqual(result.after.candidates[0].value, content)

    def test_replace_document_refuses_unchanged_text_before_input(self):
        self.backend.tree = [editor(text="existing", focused=True)]
        snap = self.engine.observe("s-test", "kate")
        with self.assertRaisesRegex(TransactionError, "unchanged_document_text"):
            self.engine.act("s-test", "kate", Action("replace_document", snap.candidates[0].ref,
                                                         text="existing"),
                            verifier=lambda _: Verification(True))
        self.assertEqual(self.backend.document_keys, [])
        self.assertEqual(self.backend.typed, [])

    def test_replace_document_rechecks_window_before_typing_after_select_all(self):
        self.backend.tree = [editor(text="Original", focused=True)]
        snap = self.engine.observe("s-test", "kate")
        self.backend.on_document_key = lambda operation: setattr(self.backend, "is_active", False)
        with self.assertRaisesRegex(TransactionError, "target_window_not_active"):
            self.engine.act("s-test", "kate", Action("replace_document", snap.candidates[0].ref,
                                                         text="Revision"),
                            verifier=lambda _: Verification(True))
        self.assertEqual(self.backend.document_keys, ["select_all"])
        self.assertEqual(self.backend.typed, [])

    def test_save_document_uses_fixed_save_action_and_verifies_disk_bytes(self):
        content = "Handover: exact UTF-8 ✓\n"
        self.backend.tree = [editor(text=content, focused=True)]
        snap = self.engine.observe("s-test", "kate")
        result = self.engine.act(
            "s-test", "kate", Action("save_document", snap.candidates[0].ref),
            verifier=lambda after: Verification(
                self.backend.document_bytes() == after.candidates[0].value.encode("utf-8")),
        )
        self.assertEqual(result.status, "ok")
        self.assertEqual(self.backend.document_keys, ["save"])
        self.assertEqual(self.backend.document_bytes(), content.encode("utf-8"))

    def test_save_document_rejects_disk_bytes_that_differ_from_editor_text(self):
        content = "Handover still visible in Kate."
        self.backend.tree = [editor(text=content, focused=True)]
        snap = self.engine.observe("s-test", "kate")
        self.backend.document_bytes = lambda: b"stale file contents"
        with self.assertRaisesRegex(TransactionError, "verification_failed"):
            self.engine.act(
                "s-test", "kate", Action("save_document", snap.candidates[0].ref),
                verifier=lambda after: Verification(
                    self.backend.document_bytes() == after.candidates[0].value.encode("utf-8")),
            )
        self.assertEqual(self.backend.document_keys, ["save"])

    def test_document_actions_reject_text_that_may_have_been_truncated(self):
        over_limit = "x" * 4097
        self.backend.tree = [editor(text=over_limit, focused=True)]
        snap = self.engine.observe("s-test", "kate")
        with self.assertRaisesRegex(TransactionError, "document_text_too_large_or_truncated"):
            self.engine.act("s-test", "kate", Action("save_document", snap.candidates[0].ref),
                            verifier=lambda _: Verification(True))
        self.assertEqual(self.backend.document_keys, [])

    def test_document_actions_are_kate_only(self):
        self.backend.tree = [editor(focused=True)]
        self.backend.app = "firefox"
        snap = self.engine.observe("s-test", "firefox")
        with self.assertRaisesRegex(TransactionError, "unsupported_action"):
            self.engine.act("s-test", "firefox", Action("save_document", snap.candidates[0].ref),
                            verifier=lambda _: Verification(True))
        self.assertEqual(self.backend.document_keys, [])

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
