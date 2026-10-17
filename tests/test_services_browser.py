"""Browser service boundary tests: no real Chromium or customer traffic."""
import base64
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import signal
import subprocess
import sqlite3
from unittest.mock import Mock, patch

from services.browser import BrowserApplication
from services import browser_client
from workers import stealth_service_engine as browser


class BrowserServicesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root/'.commerce').mkdir()
        sqlite3.connect(self.root/'.commerce'/'outbox.sqlite').close()
        (self.root / 'config_workers.json').write_text(json.dumps({'workers': [
            {'worker_id': 'test', 'proxy_url': 'http://fixture.invalid:8080', 'use_proxy': True}]}))
        self.transport = Mock()
        self.transport.query_service_stealth.return_value = ('ok', 0.00001, 'Success')
        self.transport.pending_verifications.return_value = []
        self.app = BrowserApplication(self.root, transport=self.transport)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.app.close)

    def payload(self, **kwargs):
        return {'task_id': 'fixture-task', 'number': '٢٠٠٠١', 'timeout': 12, **kwargs}

    def wait(self, task_id='fixture-task'):
        for _ in range(100):
            result = self.app.status({'task_id': task_id})
            if result['done']:
                return result
            time.sleep(.01)
        self.fail('Browser fixture failed to finish')

    def test_same_task_replay_runs_once_and_retains_amount(self):
        self.app.submit(self.payload())
        self.assertEqual(self.wait()['result'], ['ok', .00001, 'Success'])
        self.app.submit(self.payload())
        self.assertEqual(self.transport.query_service_stealth.call_count, 1)
        self.assertEqual(self.transport.query_service_stealth.call_args.args[0], '20001')
        with self.assertRaises(ValueError):
            self.app.submit(self.payload(number='20002'))

    def test_wire_rejects_urls_account_numbers_paths_and_unknown_proxy(self):
        for data in (self.payload(number='10001'), self.payload(number='https://fixture.invalid'),
                     self.payload(task_id='../bad'), self.payload(proxy_url='http://unconfigured.invalid'),
                     self.payload(timeout=float('nan')), self.payload(timeout=121), self.payload(command='launch')):
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.app.submit(data)
        self.transport.query_service_stealth.assert_not_called()
        self.assertNotIn('exec', self.app.handlers())
        with self.assertRaises(ValueError):
            self.app.native_worker({'worker_id': 'unknown'}, 'worker_launch')

    def test_capacity_cancellation_and_shutdown(self):
        entered, release = threading.Event(), threading.Event()
        def slow(*args, cancel_event, **kwargs):
            entered.set()
            release.wait(2)
            self.assertTrue(cancel_event.is_set())
            return 'network_error', None, 'cancelled'
        self.transport.query_service_stealth.side_effect = slow
        self.app.limit = 1
        try:
            self.app.submit(self.payload())
            self.assertTrue(entered.wait(1))
            with self.assertRaises(RuntimeError):
                self.app.submit(self.payload(task_id='second'))
            self.assertTrue(self.app.cancel({'task_id': 'fixture-task'})['cancel_requested'])
            release.set()
            self.assertEqual(self.wait()['state'], 'cancelled')
        finally:
            release.set()

    def test_verification_is_explicit_and_images_encoded(self):
        self.transport.query_service_stealth.side_effect = lambda *a, on_verification, **kw: (
            on_verification('Human verification required') or ('verification_required', None, 'Human required'))
        self.app.submit(self.payload(allow_verification=True))
        self.assertEqual(self.wait()['verification'], 'Human verification required')
        self.transport.verification_image.return_value = b'fixturePNG'
        self.assertEqual(base64.b64decode(self.app.handlers()['verification_image']({'id': 'fixture'} )['image']), b'fixturePNG')
        self.app.handlers()['verification_submit']({'id': 'fixture', 'answer': 'operator-input'})
        self.transport.submit_verification.assert_called_once_with('fixture', 'operator-input')

    def test_remote_browser_helpers_do_not_start_local_browser(self):
        rpc = Mock()
        rpc.call.side_effect = lambda op, p, **kw: (
            {'accepted': True} if op == 'query_submit' else {'done': True, 'result': ['ok', .00001, 'Success']})
        with patch.dict(os.environ, {'ZAIN_SERVICE_MODE': '1', 'ZAIN_SERVICE_ROLE': 'engine'}), \
             patch.object(browser_client, 'client', return_value=rpc), \
             patch.object(browser, 'get_stealth_service_worker', side_effect=AssertionError('local browser forbidden')):
            self.assertEqual(browser.query_service_stealth('20001')[1], .00001)
        self.assertEqual([c.args[0] for c in rpc.call.call_args_list], ['query_submit', 'query_status'])

    def test_remote_failure_fails_closed_without_local_fallback(self):
        with patch.object(browser_client, 'client') as factory:
            factory.return_value.call.side_effect = OSError('fixture offline')
            self.assertEqual(browser_client.query('20001')[0], 'network_error')
            self.assertEqual(factory.return_value.call.call_count, 1)

    def test_browser_role_owns_browser_and_route_capacity_is_bounded(self):
        ready = threading.Event()
        ready.set()
        existing = Mock(is_alive=lambda: True, is_ready_event=ready)
        with patch.dict(os.environ, {'ZAIN_SERVICE_MODE': '1', 'ZAIN_SERVICE_ROLE': 'browser', 'ZAIN_BROWSER_ROUTE_LIMIT': '1'}), \
             patch.object(browser, '_GLOBAL_STEALTH_WORKER', existing), patch.object(browser, '_PROXY_BROWSER_WORKERS', {}), \
             patch.object(browser, 'StealthServiceWorker', side_effect=AssertionError('capacity must prevent launch')):
            with self.assertRaises(RuntimeError):
                browser.get_stealth_service_worker('http://fixture.invalid:8080')
            self.assertIs(browser.get_stealth_service_worker(), existing)

    def test_owned_linux_process_group_cleanup_never_targets_stale_pid(self):
        from workers import launcher
        process = Mock()
        process.poll.return_value = None
        with patch.object(launcher.sys, 'platform', 'linux'), \
             patch.object(launcher.signal, 'SIGKILL', 9, create=True), \
             patch.object(launcher, '_OWNED_PROCESSES', {123: process}), \
             patch.object(launcher.os, 'getpgid', return_value=123, create=True), \
             patch.object(launcher.os, 'killpg', create=True) as kill:
            process.wait.side_effect = [subprocess.TimeoutExpired('fixture', 3), None]
            self.assertTrue(launcher.terminate_worker_process(123))
            self.assertEqual(kill.call_args_list[0].args, (123, signal.SIGTERM))
            self.assertEqual(kill.call_args_list[1].args, (123, signal.SIGKILL))
            self.assertFalse(launcher.terminate_worker_process(999))
            self.assertEqual(kill.call_count, 2)

    def test_process_group_guard_refuses_unowned_group(self):
        from workers import launcher
        process = Mock()
        process.poll.return_value = None
        with patch.object(launcher.sys, 'platform', 'linux'), \
             patch.object(launcher, '_OWNED_PROCESSES', {123: process}), \
             patch.object(launcher.os, 'getpgid', return_value=456, create=True), \
             patch.object(launcher.os, 'killpg', create=True) as kill:
            self.assertFalse(launcher.terminate_worker_process(123))
            kill.assert_not_called()

    def test_shutdown_closes_only_registered_bridge_sockets(self):
        owned = Mock()
        with patch.object(browser, '_GLOBAL_STEALTH_WORKER', None), \
             patch.object(browser, '_PROXY_BROWSER_WORKERS', {}), \
             patch.object(browser, '_SOCKS5_BRIDGES', {'fixture': 1234}), \
             patch.object(browser, '_SOCKS5_LISTENERS', {'fixture': owned}), \
             patch.object(browser, '_SOCKS5_CONNECTIONS', set()), \
             patch.dict(os.environ, {'ZAIN_SERVICE_MODE': '1', 'ZAIN_SERVICE_ROLE': 'browser'}):
            browser.stop_service_browsers()
            owned.close.assert_called_once()
            self.assertFalse(browser._SOCKS5_BRIDGES)

    def test_native_worker_actor_does_not_launch_chrome_in_engine(self):
        from workers.actor import WorkerActor
        actor = WorkerActor('test', 'Fixture', self.root/'profile', self.root/'extension', use_direct_api=False)
        with patch.dict(os.environ, {'ZAIN_SERVICE_MODE': '1', 'ZAIN_SERVICE_ROLE': 'engine'}), \
             patch.object(browser_client, 'worker', return_value={'ok': True, 'pid': 123, 'alive': True}) as remote, \
             patch('workers.actor.launch_worker_chrome', side_effect=AssertionError('local launch forbidden')):
            self.assertTrue(actor.launch())
            self.assertEqual(remote.call_args.args, ('worker_launch', 'test'))
            self.assertTrue(actor.is_alive())
            self.assertTrue(actor.terminate())

    def test_engine_browser_dispatch_uses_its_existing_task_identity(self):
        from workers.actor import WorkerActor
        actor = WorkerActor('test', 'Fixture', self.root/'profile', self.root/'extension')
        actor.current_task = {'task_id': 'engine-lease-fixture'}
        with patch.dict(os.environ, {'ZAIN_SERVICE_MODE': '1', 'ZAIN_SERVICE_ROLE': 'engine'}), \
             patch.object(browser_client, 'query', return_value=('ok', .00001, 'Success')) as query:
            self.assertEqual(actor.execute_task_api('20001')[1], .00001)
            self.assertEqual(query.call_args.kwargs['task_id'], 'engine-lease-fixture')

    def test_fixture_requires_explicit_isolated_marker(self):
        from services.browser import build
        with patch.dict(os.environ, {'ZAIN_SERVICE_TESTING': '1'}):
            with self.assertRaises(RuntimeError):
                build(self.root)
            (self.root / '.services').mkdir()
            (self.root / '.services' / 'TEST_FIXTURE').write_text('smart-zain-isolated-tests')
            app = build(self.root)
            try:
                app.submit(self.payload())
                for _ in range(100):
                    result = app.status({'task_id': 'fixture-task'})
                    if result['done']:
                        break
                    time.sleep(.01)
                self.assertEqual(result['result'][:2], ['ok', 12.34])
            finally:
                app.close()
            with self.assertRaises(RuntimeError):
                build(Path(__file__).resolve().parent.parent)

    def test_memory_pressure_defers_work_and_keeps_cancellation_available(self):
        from services.ipc import RetryLater
        with patch('services.resources.admit', return_value=False):
            with self.assertRaises(RetryLater):
                self.app.submit(self.payload())
            self.assertIsNone(self.app.store.get('fixture-task'))
            # Accepted work already on disk stays queued rather than failing.
            self.app.store.submit('accepted', 'fixture-fingerprint', {'number': '20001', 'timeout': 12,
                                  'proxy_url': None, 'allow_verification': False}, 16)
            self.app.wake.set()
            self.assertEqual(self.app.status({'task_id': 'accepted'})['state'], 'pending')
            self.app.cancel({'task_id': 'accepted'})
            self.assertEqual(self.app.status({'task_id': 'accepted'})['state'], 'cancelled')
            self.assertTrue(self.app.health()['ready'])
        self.transport.query_service_stealth.assert_not_called()

    def test_pending_cancel_tombstone_survives_late_admission(self):
        self.app.cancel({'task_id': 'fixture-task'})
        self.app.submit(self.payload())
        self.assertEqual(self.app.status({'task_id': 'fixture-task'})['state'], 'cancelled')
        self.transport.query_service_stealth.assert_not_called()

    def test_restart_recovers_accepted_task_and_retains_completed_result(self):
        self.app.close()
        self.app.store.submit('recover', 'fingerprint', {'number': '20001', 'timeout': 12,
                              'proxy_url': None, 'allow_verification': False}, 16)
        self.assertEqual(self.app.store.claim()['id'], 'recover')  # interrupted read
        recovered = BrowserApplication(self.root, transport=self.transport)
        self.addCleanup(recovered.close)
        for _ in range(100):
            result = recovered.status({'task_id': 'recover'})
            if result['done']:
                break
            time.sleep(.01)
        self.assertEqual(result['result'], ['ok', .00001, 'Success'])
        recovered.close()
        restarted = BrowserApplication(self.root, transport=self.transport)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.status({'task_id': 'recover'})['result'], result['result'])
        self.assertEqual(self.transport.query_service_stealth.call_count, 1)
        self.assertTrue((self.root/'.commerce'/'outbox.sqlite').is_file())
        self.assertFalse(list((self.root/'.services').glob('*.sqlite')) if (self.root/'.services').exists() else [])

    def test_pending_ipc_admission_observes_same_request_without_new_attempt(self):
        from services.ipc import ServiceUnavailable
        rpc = Mock()
        rpc.call.side_effect = [ServiceUnavailable('pending'), {'accepted': True},
                               {'done': True, 'result': ['ok', .00001, 'Success']}]
        with patch.object(browser_client, 'client', return_value=rpc), patch.object(browser_client.time, 'sleep'):
            self.assertEqual(browser_client.query('20001', task_id='same-lease')[0], 'ok')
        submits = [call for call in rpc.call.call_args_list if call.args[0] == 'query_submit']
        self.assertEqual(len(submits), 2)
        self.assertEqual([call.kwargs['request_id'] for call in submits], ['same-lease', 'same-lease'])


if __name__ == '__main__':
    unittest.main()
