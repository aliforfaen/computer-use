"""Small offline integration checks for the live owner-facing contract."""
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from jevdesktop.desktop_service import DesktopService, DesktopWorkerClient
from tests.test_desktop_service import FakeWorker


class Worker(FakeWorker, DesktopWorkerClient):
    def start(self, app, *args, **kwargs):
        self.start_options = kwargs
        self.profile = kwargs.get('profile')
        return FakeWorker.start(self, app)

    def rpc(self, method, **params):
        if method != 'workspace_discover':
            raise AssertionError(method)
        return {'windows': [
            {'id': 'window-1', 'app': 'org.kde.kate', 'caption': 'Example document', 'active': True},
            {'id': 'background', 'app': 'other.app', 'caption': 'Private title', 'active': False}],
            'active_window_id': 'window-1'}

    def focus_live_app(self):
        self.focused = getattr(self, 'focused', 0) + 1
        return {'focused': True, 'selected_window_id': 'window-1', 'related_windows': []}

    def restore_live_focus(self):
        raise AssertionError('must not restore focus between calls')

    def terminate(self):
        self.proc.dead = True

    def close(self):
        self.terminate()
        return True


class LiveSessionAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.worker = Worker()
        self.service = DesktopService(Path(self.tmp.name) / 'audit.jsonl',
                                      allowed_apps={'kate'}, worker_factory=lambda: self.worker)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.service.close)

    def start(self, **extras):
        return self.service.dispatch('session_start', dict(app='kate', desktop_mode='live',
            owner_present_override=True, **extras))

    def test_live_attach_defaults_to_no_semantics_and_preserves_ownership(self):
        response = self.start(attach=True, window_id='window-1')
        self.assertTrue(response['ok'], response)
        self.assertFalse(response['session']['semantic_candidates'])
        self.assertEqual(response['session']['ownership'], 'attached')
        self.assertEqual(self.worker.start_options['window_id'], 'window-1')
        result = self.service.dispatch('candidates', {'app': 'kate'})
        self.assertEqual(result['error']['code'], 'semantic_candidates_disabled')
        self.assertEqual(self.worker.focused, 1)

    def test_opt_in_semantics_requires_explicit_a11y_permission(self):
        result = self.start(semantic_candidates=True)
        self.assertEqual(result['error']['code'], 'live_temporary_a11y_required')

    def test_attach_requires_exact_window(self):
        result = self.start(attach=True)
        self.assertEqual(result['error']['code'], 'invalid_params')

    def test_workspace_reports_context_and_redacts_other_app_titles(self):
        self.assertTrue(self.start(attach=True, window_id='window-1')['ok'])
        result = self.service.dispatch('workspace')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['windows'][0]['profile_id'], 'kate')
        self.assertNotIn('caption', result['windows'][1])
        self.assertEqual(result['active_window_id'], 'window-1')

    def test_relay_query_is_available_without_desktop_session(self):
        self.service.relay_manager = SimpleNamespace(snapshot=lambda: {'forwards': [], 'stale': False})
        result = self.service.dispatch('relays')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['forwards'], [])

    def test_interruption_is_reported_without_acting(self):
        self.assertTrue(self.start()['ok'])
        self.worker.focus_live_app = lambda: {'focused': False, 'interruption':
            {'app': 'other.app', 'caption': 'Private title'}}
        result = self.service.dispatch('observe', {'app': 'kate'})
        self.assertEqual(result['error']['code'], 'live_desktop_interrupted')
        status = self.service.dispatch('status')['session']
        self.assertNotIn('caption', status['workspace']['interruption'])

    def test_visual_recovery_does_not_choose_semantic_recovery(self):
        self.assertTrue(self.start(semantic_candidates=True, temporary_a11y=True)['ok'])
        session = self.service._session
        session.visual_input_uncertain = True
        session.adapter = SimpleNamespace(capture=lambda *a, **k: SimpleNamespace(capture_id='fresh'),
            observe=lambda *a, **k: SimpleNamespace(metadata={'capture_id': 'fresh'}))
        result = self.service.dispatch('reconcile', {'app': 'kate', 'acknowledge_uncertain': True})
        self.assertTrue(result['ok'], result)
        self.assertFalse(result['input_replayed'])
        self.assertFalse(session.visual_input_uncertain)
        self.assertEqual(session.valid_input_captures, set())
        self.assertEqual(session.pending_input_captures, {'fresh'})

    def test_reconcile_without_pending_input_is_refused(self):
        self.assertTrue(self.start()['ok'])
        result = self.service.dispatch('reconcile', {'app': 'kate', 'acknowledge_uncertain': True})
        self.assertEqual(result['error']['code'], 'no_recovery_pending')

    def test_same_label_controls_can_act_when_geometry_is_distinct(self):
        self.worker.rows = [dict(role='button', name='Save', states=['enabled','sensitive','showing','visible'],
            actions=['click'], x=x, y=20, width=30, height=20, mapped=True) for x in (10, 100)]
        self.assertTrue(self.service.dispatch('session_start', {'app': 'kate'})['ok'])
        candidates = self.service.dispatch('candidates', {'app': 'kate'})['candidates']
        result = self.service.dispatch('act', {'app': 'kate', 'action': 'click',
            'target_ref': candidates[0]['ref'], 'verification': 'observed'})
        self.assertTrue(result['ok'], result)
        self.assertFalse(result['verified'])

    def test_only_returning_a_current_image_arms_coordinate_input(self):
        self.assertTrue(self.start()['ok'])
        session = self.service._session
        refs = iter(['first', 'second'])
        session.adapter = SimpleNamespace(capture=lambda *a, **k: SimpleNamespace(capture_id=next(refs)),
            observe=lambda ident, mode='metadata', **k: SimpleNamespace(metadata={'capture_id': ident},
                image=b'image' if mode == 'image' else None, reader=None, errors=[]))
        self.assertTrue(self.service.dispatch('observe', {'app': 'kate'})['ok'])
        self.assertEqual(session.valid_input_captures, set())
        result = self.service.dispatch('observe', {'app': 'kate', 'capture_id': 'first', 'output': 'image'})
        self.assertTrue(result['ok'], result)
        self.assertEqual(session.valid_input_captures, {'first'})
        session.valid_input_captures.clear()
        session.pending_input_captures.clear()
        self.assertTrue(self.service.dispatch('observe', {'app': 'kate', 'capture_id': 'first', 'output': 'image'})['ok'])
        self.assertEqual(session.valid_input_captures, set())

    def test_poisoned_worker_is_cleaned_even_when_process_is_alive(self):
        self.assertTrue(self.start()['ok'])
        self.worker._poisoned = True
        result = self.service.dispatch('status')
        self.assertIsNone(result['session'])
        self.assertEqual(result['lifecycle']['last_stop_reason'], 'worker_disconnected')

    def test_profile_listing_deduplicates_aliases(self):
        result = self.service.dispatch('profiles')
        self.assertTrue(result['ok'], result)
        self.assertEqual(len({p['id'] for p in result['profiles']}), len(result['profiles']))


if __name__ == '__main__':
    unittest.main()
