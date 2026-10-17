"""Offline transport/event/lifecycle checks; retained fixtures, no deletions."""
import io
import json
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from telegram_bot.notifier import TelegramNotifier
from telegram_bot.integration import TelegramIntegration
from manager.event_bus import EventBus


def response(data):
    stream=io.BytesIO(json.dumps(data).encode());return stream


class NotifierReliability(unittest.TestCase):
    def test_stopped_free_customer_receives_partial_file_before_remaining_queue(self):
        path=Path(tempfile.mkdtemp(prefix='free-stop-notifier-retained-'))/'partial.xlsx';path.write_bytes(b'synthetic-partial')
        self.bot.broadcast_message=Mock();self.bot.broadcast_excel_document=Mock()
        self.bot.send_message=Mock();self.bot.send_excel_document=Mock()
        self.bot._on_session_completed('session_completed',{'workbook':'free','total':3,'cancelled':2,
            'result_file':str(path),'round_pending':1,'customer_stop':True,'telegram_chat_id':31})
        self.bot.send_excel_document.assert_called_once()
        self.assertEqual(self.bot.send_excel_document.call_args.kwargs['chat_id'],31)
        self.assertIn('النتيجة المنجزة',self.bot.send_excel_document.call_args.kwargs['caption'])

    def setUp(self):
        self.config=patch('telegram_bot.notifier.settings',return_value={
            'bot_token':'test-only','chat_id':123,'admin_targets':[123]})
        self.config.start();self.addCleanup(self.config.stop)
        self.bot=TelegramNotifier(bot_token='test-only',chat_id=123)
        self.network=patch('urllib.request.urlopen',side_effect=AssertionError('No live network'))
        self.urlopen=self.network.start();self.addCleanup(self.network.stop)

    def test_retry_confirmed_rate_limit_only(self):
        error=urllib.error.HTTPError('https://test.invalid',429,'limit',{},response({'ok':False,'error_code':429,'parameters':{'retry_after':1}}))
        self.addCleanup(error.close)
        self.urlopen.side_effect=[error,response({'ok':True})]
        with patch.object(self.bot._delivery_disabled,'wait',return_value=False) as wait:
            self.assertTrue(self.bot.send_message('hello',wait=True))
        wait.assert_called_once_with(1);self.assertEqual(self.urlopen.call_count,2)

    def test_ambiguous_send_timeout_never_duplicates(self):
        self.urlopen.side_effect=TimeoutError()
        self.assertFalse(self.bot.send_message('hello',wait=True));self.assertEqual(self.urlopen.call_count,1)
        self.assertEqual(self.bot.delivery_status()['failed'],1)

    def test_stop_suppresses_new_deliveries_and_callbacks(self):
        bus=EventBus()
        with patch('telegram_bot.notifier.EVENT_BUS',bus):
            self.bot.start_listening();self.bot.start_listening()
            self.assertEqual(len(bus._subscribers['session_completed']),1)
            self.bot.stop_listening();self.assertFalse(self.bot.send_message('hello'))
            self.assertEqual(bus._subscribers['session_completed'],[])

    def test_background_documents_keep_acceptance_order(self):
        seen=[]
        first=threading.Event();release=threading.Event()
        def deliver(index):
            seen.append(index)
            if index==1:first.set();release.wait(2)
            return True
        self.assertTrue(self.bot._submit_delivery(lambda:deliver(1),False));self.assertTrue(first.wait(1))
        self.assertTrue(self.bot._submit_delivery(lambda:deliver(2),False));release.set()
        self.bot._delivery_queue.join();self.assertEqual(seen,[1,2])
        self.assertEqual(self.bot.delivery_status()['delivered'],2)

    def test_disabled_generation_cancels_old_queue(self):
        release=threading.Event();started=threading.Event();seen=[]
        def first():started.set();release.wait(2);return True
        self.bot._submit_delivery(first,False);self.assertTrue(started.wait(1))
        self.bot._submit_delivery(lambda:seen.append(2),False)
        self.bot.stop_listening();self.bot.start_listening();release.set();self.bot._delivery_queue.join()
        self.bot.stop_listening();self.assertEqual(seen,[])

    def test_customer_receives_only_own_file(self):
        self.bot.send_excel_document=Mock(return_value=True)
        folder=Path(tempfile.mkdtemp(prefix='zain_notifier_'))
        files=[folder/'one.xlsx',folder/'two.xlsx']
        for file in files:file.write_bytes(b'PK\x03\x04synthetic')
        one,two=map(str,files)
        payload={'result_file':one,'round_result_files':[one,two],
                 'round_deliveries':[{'file':one,'chat_id':456},{'file':two,'chat_id':789},{'file':one,'chat_id':456}]}
        self.bot._on_session_completed('session_completed',payload)
        customer_calls=[call for call in self.bot.send_excel_document.call_args_list
                        if call.kwargs.get('chat_id') not in self.bot.admin_targets]
        self.assertEqual([(call.args[0].name,call.kwargs['chat_id']) for call in customer_calls],[('one.xlsx',456),('two.xlsx',789)])

    def test_invalid_and_oversized_message_never_sends(self):
        self.assertFalse(self.bot.send_message('x'*4097,wait=True));self.urlopen.assert_not_called()

    def test_upload_caption_and_filename_are_safe(self):
        folder=Path(tempfile.mkdtemp(prefix='zain_notifier_'))
        file=folder/'offline.xlsx';file.write_bytes(b'PK\x03\x04sample')
        self.urlopen.side_effect=None;self.urlopen.return_value=response({'ok':True})
        self.assertTrue(self.bot.send_excel_document(file,caption='ملف العملاء',wait=True,chat_id=456))
        request=self.urlopen.call_args.args[0]
        self.assertIn(b'456',request.data);self.assertIn(b'offline.xlsx',request.data)

    def test_status_does_not_wait_on_connection_io(self):
        notifier,runner=Mock(),Mock()
        notifier.bot_token='test-only';notifier.chat_id=123;notifier.delivery_status.return_value={}
        runner.authorized_id=123;runner.status.return_value={'polling':True}
        entered=threading.Event();release=threading.Event()
        def check():entered.set();release.wait(2);return {'connected':True}
        notifier.check_connection.side_effect=check
        with patch('telegram_bot.notifier.TelegramNotifier',return_value=notifier),patch('telegram_bot.bot.TelegramBotRunner',return_value=runner),patch('telegram_bot.commerce.bridge.CommerceBridge'):
            service=TelegramIntegration(SimpleNamespace(telegram_enabled=True),Mock(),Path.cwd())
        thread=threading.Thread(target=service.check_connection);thread.start();self.assertTrue(entered.wait(1))
        self.assertTrue(service.status()['polling']);release.set();thread.join(2)

    def test_start_failure_rolls_back_notifications_and_enabled_flag(self):
        notifier,runner=Mock(),Mock();runner.authorized_id=123;runner.start.side_effect=RuntimeError('start')
        orchestrator=SimpleNamespace(telegram_enabled=False)
        with patch('telegram_bot.notifier.TelegramNotifier',return_value=notifier),patch('telegram_bot.bot.TelegramBotRunner',return_value=runner),patch('telegram_bot.commerce.bridge.CommerceBridge'):
            service=TelegramIntegration(orchestrator,Mock(),Path.cwd())
        with self.assertRaises(RuntimeError):service.set_enabled(True)
        self.assertFalse(orchestrator.telegram_enabled);notifier.stop_listening.assert_called_once();runner.stop.assert_called_once()
