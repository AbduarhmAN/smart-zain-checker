"""Offline behavior tests for native Telegram navigation and confirmations."""
import io
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from telegram_bot.bot import TelegramBotRunner
from telegram_bot import views


class TelegramUiTests(unittest.TestCase):
    def setUp(self):
        network = patch('urllib.request.urlopen', side_effect=AssertionError('Live network forbidden'))
        network.start()
        self.addCleanup(network.stop)
        self.state = {'running':True, 'paused':False, 'workbook':'عملاء.xlsx',
                      'kpis':{'total':10, 'completed':4, 'verified':2}, 'verifications':[]}
        self.orchestrator = Mock(session_token='session-a', lock=threading.RLock(),
                                 all_completed_records={1:{'row':1}})
        self.orchestrator.get_live_status.side_effect = lambda: dict(self.state)
        self.queue = Mock()
        self.queue.get_jobs.return_value = []
        self.bot = TelegramBotRunner(self.orchestrator, self.queue, Path.cwd(),
                                      bot_token='test-token', authorized_id=123)
        self.bot._api_call = Mock(return_value={'ok':True})

    def callback(self, action, user=123, chat=123):
        self.bot._handle_update({'callback_query':{'id':'query', 'from':{'id':user},
            'message':{'chat':{'id':chat}, 'message_id':7}, 'data':'zc:'+action}})

    def confirmation(self, action):
        self.callback(action)
        return next(iter(self.bot._confirmations))

    def test_refresh_acknowledges_click_and_edits_existing_message(self):
        self.callback('status')
        self.assertEqual([call.args[0] for call in self.bot._api_call.call_args_list],
                         ['answerCallbackQuery', 'editMessageText'])
        self.assertEqual(self.bot._api_call.call_args.args[1]['message_id'], 7)

    def test_unchanged_panel_does_not_create_duplicate_message(self):
        self.bot._api_call.side_effect = [{'ok':True}, {'ok':False,'description':'Bad Request: message is not modified'}]
        self.callback('status')
        self.assertNotIn('sendMessage', [call.args[0] for call in self.bot._api_call.call_args_list])

    def test_missing_editable_message_falls_back_to_new_panel(self):
        self.bot._api_call.side_effect = [{'ok':True}, {'ok':False}, {'ok':True}]
        self.callback('home')
        self.assertEqual(self.bot._api_call.call_args.args[0], 'sendMessage')

    def test_callback_authorization_checks_sender_and_private_destination(self):
        for user, chat in ((456,123), (123,-555)):
            self.bot._api_call.reset_mock()
            self.callback('pause', user=user, chat=chat)
            self.assertEqual(self.bot._api_call.call_count, 1)
            self.assertTrue(self.bot._api_call.call_args.args[1]['show_alert'])
        self.orchestrator.pause_session.assert_not_called()

    def test_pause_requires_confirmation_and_token_is_single_use(self):
        token = self.confirmation('pause')
        self.orchestrator.pause_session.assert_not_called()
        self.callback('confirm:'+token)
        self.callback('confirm:'+token)
        self.orchestrator.pause_session.assert_called_once()

    def test_confirmation_cannot_apply_to_new_session(self):
        token = self.confirmation('pause')
        self.orchestrator.session_token = 'session-b'
        self.callback('confirm:'+token)
        self.orchestrator.pause_session.assert_not_called()

    def test_expired_confirmation_cannot_change_session(self):
        token = self.confirmation('pause')
        self.bot._confirmations[token]['expires'] = time.monotonic()-1
        self.callback('confirm:'+token)
        self.orchestrator.pause_session.assert_not_called()

    def test_back_navigation_cancels_confirmation(self):
        token = self.confirmation('pause')
        self.callback('status')
        self.callback('confirm:'+token)
        self.orchestrator.pause_session.assert_not_called()

    def test_resume_rechecks_verification_at_confirmation_time(self):
        self.state['paused'] = True
        token = self.confirmation('resume')
        self.state['verifications'] = [{'task_id':'human-verification'}]
        self.callback('confirm:'+token)
        self.orchestrator.resume_session.assert_not_called()

    def test_no_current_records_means_no_stale_report_delivery(self):
        self.state['running'] = False
        self.state['kpis']['total'] = 4
        self.orchestrator.all_completed_records = {}
        token = self.confirmation('results')
        with patch('telegram_bot.notifier.TelegramNotifier') as notifier:
            self.callback('confirm:'+token)
        notifier.assert_not_called()

    def test_report_is_bound_to_authorized_destination_and_waits_for_ack(self):
        self.state['running'] = False
        self.state['kpis']['total'] = 4
        token = self.confirmation('results')
        with patch('pathlib.Path.mkdir'), \
             patch('zain_checker.executive_reporter.export_executive_workbook') as exporter, \
             patch('telegram_bot.notifier.TelegramNotifier') as notifier:
            notifier.return_value.send_excel_document.return_value = False
            self.callback('confirm:'+token)
        self.assertEqual(notifier.call_args.kwargs['chat_id'], 123)
        self.assertTrue(notifier.return_value.send_excel_document.call_args.kwargs['wait'])
        self.assertEqual(exporter.call_args.args[0], [{'row':1}])
        self.assertIn('تعذر إرسال النتائج', self.bot._api_call.call_args.args[1]['text'])

    def test_queue_pages_are_bounded_and_callback_data_is_valid(self):
        jobs = [{'filename':'<ملف>&.xlsx', 'sheet_name':'ورقة', 'status':'pending'}]*300
        panel = views.queue(jobs, 99999)
        self.assertIn('صفحة 60 من 60', panel.text)
        self.assertLess(len(panel.text), 4096)
        self.assertNotIn('<ملف>', panel.text)
        for row in panel.rows:
            for button in row:
                self.assertLessEqual(len(button['callback_data'].encode('utf-8')), 64)

    def test_status_clamps_progress_and_escapes_filenames(self):
        panel = views.status({'workbook':'<b>fake</b>', 'kpis':{'total':2,'completed':100,'verified':0}})
        self.assertIn('100%', panel.text)
        self.assertIn('&lt;b&gt;fake&lt;/b&gt;', panel.text)
        self.assertIn('أرصدة مؤكدة: 0', panel.text)

    def test_stopped_incomplete_session_is_not_reported_as_completed(self):
        self.assertEqual(views.state_label({'running':False,'kpis':{'total':10,'completed':3}}),
                         '⚪ الجلسة غير نشطة')

    def test_upload_opens_review_before_queuing_and_sanitizes_filename(self):
        self.bot._api_call.return_value = {'ok':True,'result':{'file_path':'documents/test.xlsx'}}
        download = Mock()
        download.__enter__ = Mock(return_value=SimpleNamespace(read=lambda:b'OFFLINE_TEST'))
        download.__exit__ = Mock(return_value=False)
        with patch('urllib.request.urlopen', return_value=download), \
             patch('pathlib.Path.exists', return_value=False), \
             patch('pathlib.Path.open', return_value=io.BytesIO()), \
             patch.object(self.bot, '_open_sheet_review') as review:
            self.bot._handle_document_upload({'file_id':'test','file_name':'../عملاء.xlsx'}, 123)
        self.queue.add_job.assert_not_called()
        self.assertEqual(review.call_args.args[0].parent, Path.cwd())
        self.assertNotIn('/', review.call_args.args[0].name)

    def test_unsupported_document_shows_upload_guide_without_downloading(self):
        self.bot._handle_update({'message':{'from':{'id':123},'chat':{'id':123},
                                           'document':{'file_name':'test.pdf'}}})
        self.assertNotIn('getFile', [call.args[0] for call in self.bot._api_call.call_args_list])
        self.queue.add_job.assert_not_called()

    def test_polling_cursor_survives_disable_enable_and_menu_is_private(self):
        self.bot._last_update_id = 41
        seen = []
        def api(method, data, **kwargs):
            seen.append((method, data))
            if method == 'getUpdates':
                self.bot._stop_event.set()
            return {'ok':True, 'result':[]}
        self.bot._api_call = api
        self.bot._poll_loop()
        self.assertEqual(seen[0][0], 'setMyCommands')
        self.assertEqual(seen[0][1]['scope'], {'type':'chat','chat_id':123})
        self.assertEqual(seen[1][1]['offset'], 42)


if __name__ == '__main__':
    unittest.main()
