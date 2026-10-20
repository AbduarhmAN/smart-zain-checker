"""Offline transport/routing checks. Retained fixtures; no Telegram traffic."""
import io
import json
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from telegram_bot.bot import TelegramBotRunner
from offline_http import post as offline_post,get as offline_get


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def response(value):
    return Response(json.dumps(value).encode('utf-8'))


class TransportReliabilityTests(unittest.TestCase):
    def setUp(self):
        post_patch = patch('requests.Session.post',new=offline_post)
        get_patch = patch('requests.Session.get',new=offline_get)
        post_patch.start(); get_patch.start()
        self.addCleanup(post_patch.stop); self.addCleanup(get_patch.stop)
        self.network = patch('urllib.request.urlopen', side_effect=AssertionError('Live network forbidden'))
        self.urlopen = self.network.start()
        self.addCleanup(self.network.stop)
        self.root = Path(tempfile.mkdtemp(prefix='telegram_transport_retained_'))
        self.orchestrator = Mock(lock=threading.RLock(), session_token='session')
        self.queue = Mock()
        self.bot = TelegramBotRunner(self.orchestrator, self.queue, self.root,
                                     bot_token='test-token', authorized_id=123)
        self.bot._commerce_client = Mock()
        self.bot._commerce_client.rpc.side_effect = lambda name,args: (
            {'is_partner':False} if name=='partner_sheet_context' else self.fail('Unexpected commerce RPC'))

    def message(self, text='', sender=123, chat=123, **fields):
        return {'message': {'from': {'id': sender}, 'chat': {'id': chat}, 'text': text, **fields}}

    def test_http_error_body_preserves_edit_not_modified(self):
        self.urlopen.side_effect = urllib.error.HTTPError('https://invalid', 400, 'bad', {},
            io.BytesIO(json.dumps({'ok': False, 'error_code': 400,
                'description': 'Bad Request: message is not modified'}).encode()))
        result = self.bot._api_call('editMessageText', {})
        self.assertIn('not modified', result['description'])
        self.assertEqual(self.urlopen.call_count, 1)

    def test_rate_limit_waits_exact_server_delay_then_retries_send(self):
        self.urlopen.side_effect = [response({'ok': False, 'error_code': 429,
            'parameters': {'retry_after': 7}}), response({'ok': True, 'result': {'message_id': 1}})]
        with patch.object(self.bot._stop_event, 'wait', return_value=False) as wait:
            self.assertTrue(self.bot._api_call('sendMessage', {})['ok'])
        wait.assert_called_once_with(7.0)

    def test_http_429_body_retries(self):
        self.urlopen.side_effect = [urllib.error.HTTPError('https://invalid', 429, 'rate', {},
            io.BytesIO(b'{"ok":false,"parameters":{"retry_after":2}}')),
            response({'ok': True})]
        with patch.object(self.bot._stop_event, 'wait', return_value=False):
            self.assertTrue(self.bot._api_call('sendMessage', {})['ok'])

    def test_ambiguous_send_timeout_is_not_retried(self):
        self.urlopen.side_effect = TimeoutError('timeout')
        self.assertIsNone(self.bot._api_call('sendMessage', {}))
        self.assertEqual(self.urlopen.call_count, 1)

    def test_adapter_never_retries_outside_the_explicit_bot_policy(self):
        adapter=self.bot._session.get_adapter('https://api.telegram.org')
        self.assertEqual(adapter.max_retries.total,0)

    def test_unknown_partner_menu_is_a_deferred_setup_not_a_warning(self):
        self.urlopen.side_effect=None
        self.urlopen.return_value=response({'ok':False,'error_code':400,'description':'Bad Request: chat not found'})
        with patch('telegram_bot.bot.logger.warning') as warning:
            result=self.bot._api_call('getChat',{'chat_id':456})
        self.assertFalse(result['ok']);warning.assert_not_called()

    def test_read_transport_failures_retry_bounded(self):
        self.urlopen.side_effect = urllib.error.URLError('offline')
        with patch.object(self.bot._stop_event, 'wait', return_value=False) as wait:
            self.assertIsNone(self.bot._api_call('getUpdates', {}))
        self.assertEqual(self.urlopen.call_count, 3)
        self.assertEqual(wait.call_count, 2)

    def test_unsafe_server_failure_does_not_duplicate_send(self):
        self.urlopen.side_effect = None
        self.urlopen.return_value = response({'ok': False, 'error_code': 503})
        self.assertEqual(self.bot._api_call('sendMessage', {})['error_code'], 503)
        self.assertEqual(self.urlopen.call_count, 1)

    def test_safe_server_failure_can_recover(self):
        self.urlopen.side_effect = [response({'ok': False, 'error_code': 503}), response({'ok': True})]
        with patch.object(self.bot._stop_event, 'wait', return_value=False):
            self.assertTrue(self.bot._api_call('getFile', {})['ok'])

    def test_stop_interrupts_rate_limit_wait(self):
        self.urlopen.side_effect = None
        self.urlopen.return_value = response({'ok': False, 'error_code': 429, 'parameters': {'retry_after': 7}})
        with patch.object(self.bot._stop_event, 'wait', return_value=True):
            self.assertFalse(self.bot._api_call('sendMessage', {})['ok'])
        self.assertEqual(self.urlopen.call_count, 1)

    def test_invalid_api_shape_is_rejected(self):
        self.urlopen.side_effect = None
        self.urlopen.return_value = response([])
        self.assertIsNone(self.bot._api_call('getMe'))

    def test_group_messages_cannot_change_admin_state(self):
        self.bot._dispatch_ui = Mock()
        self.bot._handle_update(self.message('/pause', chat=-444))
        self.bot._dispatch_ui.assert_not_called()
        self.orchestrator.pause_session.assert_not_called()

    def test_non_admin_controls_remain_restricted(self):
        self.bot._reply = Mock()
        self.bot._dispatch_ui = Mock()
        self.bot._handle_update(self.message('/status', sender=456, chat=456))
        self.bot._dispatch_ui.assert_not_called()
        self.bot._reply.assert_called_once()

    def test_document_caption_does_not_become_payment_input(self):
        self.bot._waiting_user_inputs[123] = {'action': 'tx_id', 'token': 'draft'}
        self.bot._handle_user_text_input = Mock()
        self.bot._handle_document_upload = Mock()
        doc = {'file_name': 'customers.xlsx', 'file_id': 'f'}
        self.bot._handle_update(self.message('caption', document=doc))
        self.bot._handle_document_upload.assert_called_once_with(doc, 123)
        self.bot._handle_user_text_input.assert_not_called()
        self.assertNotIn(123, self.bot._waiting_user_inputs)

    def test_photo_keeps_waiting_input(self):
        self.bot._waiting_user_inputs[123] = {'action': 'redeem'}
        self.bot._handle_update(self.message(photo=[{'file_id': 'p'}]))
        self.assertIn(123, self.bot._waiting_user_inputs)

    def test_uppercase_command_routes_correctly(self):
        self.bot._dispatch_ui = Mock()
        self.bot._handle_update(self.message('/STATUS@TestBot'))
        self.bot._dispatch_ui.assert_called_once_with('status', 123)

    def test_missing_getfile_path_never_attempts_download(self):
        self.bot._api_call = Mock(return_value={'ok': True, 'result': {}})
        self.bot._reply = Mock()
        self.bot._handle_document_upload({'file_id': 'f', 'file_name': 'a.xlsx'}, 123)
        self.urlopen.assert_not_called()
        self.bot._reply.assert_called_once()

    def test_oversized_document_rejected_before_getfile(self):
        self.bot._api_call = Mock()
        self.bot._reply = Mock()
        self.bot._handle_document_upload({'file_id': 'f', 'file_name': 'a.xlsx',
            'file_size': 20 * 1024 * 1024 + 1}, 123)
        self.bot._api_call.assert_not_called()

    def test_malicious_getfile_path_is_rejected(self):
        self.bot._api_call = Mock(return_value={'ok': True, 'result': {'file_path': '../a.xlsx'}})
        self.bot._reply = Mock()
        self.bot._handle_document_upload({'file_id': 'f', 'file_name': 'a.xlsx'}, 123)
        self.urlopen.assert_not_called()

    def test_download_not_xlsx_does_not_create_project_file(self):
        self.bot._api_call = Mock(return_value={'ok': True, 'result': {'file_path': 'documents/f.xlsx'}})
        self.bot._reply = Mock()
        self.urlopen.side_effect = None
        self.urlopen.return_value = Response(b'<html>not an xlsx</html>')
        self.bot._handle_document_upload({'file_id': 'f', 'file_name': 'a.xlsx'}, 123)
        self.assertFalse((self.root / 'a.xlsx').exists())

    def test_download_sanitizes_name_and_preserves_existing_file(self):
        old = self.root / 'a.xlsx'
        old.write_bytes(b'original')
        self.bot._api_call = Mock(return_value={'ok': True, 'result': {'file_path': 'documents/f.xlsx'}})
        self.bot._open_sheet_review = Mock()
        self.urlopen.side_effect = None
        self.urlopen.return_value = Response(b'PK\x03\x04fixture')
        self.bot._handle_document_upload({'file_id': 'f', 'file_name': 'a.xlsx'}, 123)
        saved = self.bot._open_sheet_review.call_args.args[0]
        self.assertNotEqual(saved, old)
        self.assertEqual(old.read_bytes(), b'original')
        self.assertEqual(saved.read_bytes(), b'PK\x03\x04fixture')

    def test_checkpoint_restores_offset_only_for_same_bot(self):
        self.bot._checkpoint_update(16)
        restored = TelegramBotRunner(self.orchestrator, self.queue, self.root, bot_token='test-token')
        different = TelegramBotRunner(self.orchestrator, self.queue, self.root, bot_token='other-token')
        self.assertEqual(restored._last_update_id, 16)
        self.assertEqual(different._last_update_id, 0)
        self.assertNotIn('test-token', self.bot._offset_path.read_text(encoding='utf-8'))

    def test_download_retries_transient_read_failure(self):
        self.urlopen.side_effect = [TimeoutError('timeout'), Response(b'PK\x03\x04fixture')]
        with patch.object(self.bot._stop_event, 'wait', return_value=False):
            self.assertEqual(self.bot._download_file('https://invalid/file', 100), b'PK\x03\x04fixture')
        self.assertEqual(self.urlopen.call_count, 2)

    def test_stale_cursor_does_not_hide_randomized_updates_after_inactivity(self):
        self.bot._checkpoint_update(999)
        saved = json.loads(self.bot._offset_path.read_text())
        saved['saved_at'] = time.time() - 8 * 86400
        self.bot._offset_path.write_text(json.dumps(saved), encoding='utf-8')
        restored = TelegramBotRunner(self.orchestrator, self.queue, self.root, bot_token='test-token')
        self.assertEqual(restored._last_update_id, 0)

    def test_health_reports_stopped_and_live_polling_truthfully(self):
        self.assertFalse(self.bot.status()['polling'])
        self.bot._thread = Mock()
        self.bot._thread.is_alive.return_value = True
        self.assertTrue(self.bot.status()['polling'])
        self.bot._stop_event.set()
        self.assertFalse(self.bot.status()['polling'])
        self.assertTrue(self.bot.status()['stopping'])

    def test_bad_update_does_not_drop_remaining_batch_or_replay_duplicates(self):
        self.bot._sync_partner_menus=Mock()
        self.bot._start_partner_sheet_notifications=Mock()
        handled = []
        def handle(update):
            handled.append(update['update_id'])
            if update['update_id'] == 1:
                raise ValueError('bad update')
            if update['update_id'] == 3:
                self.bot._stop_event.set()
        self.bot._handle_update = handle
        self.bot._reply = Mock()
        self.bot._api_call = Mock(side_effect=[{'ok': True}, {'ok': True}, {'ok': True,
            'result': [{'update_id': 1}, {'update_id': 1}, {'update_id': 2}, {'update_id': 3}]}])
        # Intake and dispatch are independent now: the cursor may advance only
        # after durable intake, and the bounded dispatcher processes the inbox.
        self.bot._start_payout_notifications = Mock()
        self.bot._start_partner_lifecycle_notifications = Mock()
        self.bot._recover_admin_role_action = Mock()
        dispatcher = threading.Thread(target=self.bot._dispatch_inbox)
        dispatcher.start()
        try:
            self.bot._poll_loop()
        finally:
            self.bot._stop_event.set()
            dispatcher.join(2)
        deadline = time.monotonic() + 2
        while len(handled) < 3 and time.monotonic() < deadline:time.sleep(.01)
        self.assertEqual(handled, [1, 2, 3])
        self.assertEqual(self.bot._last_update_id, 3)
        self.assertEqual(json.loads(self.bot._offset_path.read_text())['update_id'], 3)
        request = next(call.args[1] for call in self.bot._api_call.call_args_list if call.args[0]=='getUpdates')
        self.assertEqual(request['allowed_updates'], ['message', 'callback_query','edited_message'])

    def test_lifecycle_start_is_idempotent_and_stop_joins(self):
        ready = threading.Event()
        def loop():
            ready.set()
            self.bot._stop_event.wait(2)
        self.bot._poll_loop = loop
        self.bot.start()
        self.assertTrue(ready.wait(1))
        first = self.bot._thread
        self.bot.start()
        self.assertIs(self.bot._thread, first)
        self.bot.stop()
        self.assertFalse(first.is_alive())

    def test_redeem_command_uses_imported_views(self):
        manager = Mock()
        manager.redeem_code.return_value = (True, 'ok', 30)
        manager.get_wallet_balance.return_value = 60
        manager.get_user_account_id.return_value = 'account'
        self.bot._show_panel = Mock()
        with patch.dict('sys.modules', {'telegram_bot.payment_manager': SimpleNamespace(PAYMENT_MGR=manager),
            'telegram_bot.crypto_pay': SimpleNamespace(get_live_usd_egp_rate=lambda: 50)}):
            self.bot._handle_update(self.message('/redeem CODE'))
        self.bot._show_panel.assert_called_once()
        manager.redeem_code.assert_called_once_with('CODE', 123)

    def test_admin_invalid_money_never_credits_or_creates_code(self):
        manager = Mock()
        self.bot._reply = Mock()
        with patch.dict('sys.modules', {'telegram_bot.payment_manager': SimpleNamespace(PAYMENT_MGR=manager),
            'telegram_bot.crypto_pay': SimpleNamespace(get_live_usd_egp_rate=lambda: 50)}):
            for command in ('/gencode nan', '/gencode -10', '/addcode CODE inf',
                            '/credit_user 456 -10', '/credit_user 456 nonsense'):
                self.bot._handle_update(self.message(command))
        manager.credit_wallet.assert_not_called()
        manager.generate_redeem_code.assert_not_called()
        manager.add_redeem_code.assert_not_called()

    def test_text_input_rejects_expired_or_foreign_draft(self):
        self.bot._api_call = Mock()
        manager = Mock()
        with patch.dict('sys.modules', {'telegram_bot.payment_manager': SimpleNamespace(PAYMENT_MGR=manager)}):
            for owner, expires in ((456, time.monotonic() + 100), (123, time.monotonic() - 1)):
                self.bot._sheet_drafts['draft'] = {'chat': owner, 'expires': expires}
                self.bot._handle_user_text_input(123, '', 'proof', {'action': 'tx_id', 'token': 'draft'})
        manager.record_pending_payment.assert_not_called()


if __name__ == '__main__':
    unittest.main()
