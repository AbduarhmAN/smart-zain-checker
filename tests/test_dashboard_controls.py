"""Offline tests for confirmed Telegram delivery and dashboard controls."""
import io
import json
import threading
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from server.web_api import WebApiHandler
from telegram_bot.notifier import TelegramNotifier
from telegram_bot.integration import TelegramIntegration
from telegram_bot.bot import TelegramBotRunner

class DashboardControls(unittest.TestCase):
    def setUp(self):
        self.block = patch("urllib.request.urlopen", side_effect=AssertionError("Live network forbidden"))
        self.network = self.block.start()
        self.addCleanup(self.block.stop)

    def response(self, accepted):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps({"ok": accepted, "result": {"username": "test_bot"}}).encode()
        return response

    def notifier(self):
        return TelegramNotifier(bot_token="test-token", chat_id=123)

    def test_message_button_waits_for_telegram_acknowledgment(self):
        self.network.side_effect = None
        self.network.return_value = self.response(False)
        self.assertFalse(self.notifier().send_message("test", wait=True))
        self.network.return_value = self.response(True)
        self.assertTrue(self.notifier().send_message("test", wait=True))

    def test_document_button_does_not_report_rejected_upload_as_success(self):
        document = Mock(spec=Path)
        document.exists.return_value = True
        document.name = "test.xlsx"
        document.read_bytes.return_value = b"OFFLINE_TEST"
        self.network.side_effect = None
        self.network.return_value = self.response(False)
        self.assertFalse(self.notifier().send_excel_document(document, wait=True))

    def test_connection_check_only_reads_identity(self):
        self.network.side_effect = None
        self.network.return_value = self.response(True)
        state = self.notifier().check_connection()
        self.assertTrue(state["connected"])
        self.assertEqual(state["username"], "test_bot")
        self.assertTrue(self.network.call_args.args[0].endswith("/getMe"))

    def handler(self, enabled=True, accepted=False):
        handler = WebApiHandler.__new__(WebApiHandler)
        integration = Mock()
        integration.notifier.send_message.return_value = accepted
        handler.orchestrator = SimpleNamespace(telegram_enabled=enabled, telegram_integration=integration,
                lock=threading.RLock(), all_completed_records={}, result_path=Path("UNUSED_RESULT.xlsx"))
        handler.send_json = Mock()
        return handler, integration

    def test_ping_endpoint_rejects_false_delivery_result(self):
        handler, integration = self.handler()
        handler.handle_telegram_ping()
        self.assertEqual(handler.send_json.call_args.args[0], 502)
        self.assertTrue(integration.notifier.send_message.call_args.kwargs["wait"])

    def test_disabled_telegram_cannot_send(self):
        handler, integration = self.handler(enabled=False)
        handler.handle_telegram_ping()
        self.assertEqual(handler.send_json.call_args.args[0], 409)
        integration.notifier.send_message.assert_not_called()

    def test_empty_current_session_cannot_send_stale_result_file(self):
        handler, integration = self.handler()
        handler.handle_telegram_action("/api/telegram/send-results", {})
        self.assertEqual(handler.send_json.call_args.args[0], 409)
        integration.notifier.send_excel_document.assert_not_called()

    def test_duplicate_toggle_does_not_duplicate_subscribers_or_polling(self):
        notifier, runner = Mock(), Mock()
        orchestrator = SimpleNamespace(telegram_enabled=False)
        with patch("telegram_bot.notifier.TelegramNotifier", return_value=notifier), \
             patch("telegram_bot.bot.TelegramBotRunner", return_value=runner):
            service = TelegramIntegration(orchestrator, Mock(), Path.cwd())
            service.set_enabled(True)
            service.set_enabled(True)
            service.set_enabled(False)
            service.set_enabled(False)
        runner.start.assert_called_once()
        runner.stop.assert_called_once()
        notifier.start_listening.assert_called_once()
        notifier.stop_listening.assert_called_once()

    def test_invalid_toggle_value_is_not_truthy_enable(self):
        handler, integration = self.handler()
        handler.handle_telegram_action("/api/telegram/toggle", {"enabled": "false"})
        self.assertEqual(handler.send_json.call_args.args[0], 400)
        integration.set_enabled.assert_not_called()

    def test_preview_honors_explicit_first_sheet(self):
        handler, _ = self.handler()
        handler.project_root = Path.cwd()
        first = SimpleNamespace(title="الفروقات الصافية")
        errors = SimpleNamespace(title="المهلات والأخطاء")
        workbook = Mock(worksheets=[first, errors])
        with patch("pathlib.Path.exists", return_value=True), \
             patch("openpyxl.load_workbook", return_value=workbook), \
             patch("domain.workbook.inspect_sheet_schema", return_value={"document_type":"نتائج", "indices":{}}) as inspect, \
             patch("domain.workbook.extract_customer_records", return_value=[]):
            handler.handle_detect_results_sheet(SimpleNamespace(query="workbook=test.xlsx&sheet_index=0"))
        inspect.assert_called_once_with(first)
        self.assertEqual(handler.send_json.call_args.args[1]["sheet_index"], 0)
        workbook.close.assert_called_once()

    def bot(self):
        runner = TelegramBotRunner(Mock(), Mock(), Path.cwd(), bot_token="test-token", authorized_id=123)
        runner.orchestrator.lock = threading.RLock()
        runner.orchestrator.session_token = 'test-session'
        runner.queue_service.get_jobs.return_value = []
        runner._api_call = Mock()
        return runner

    def bot_update(self, text, sender=123):
        return {"message":{"from":{"id":sender},"chat":{"id":123},"text":text}}

    def test_bot_replies_include_persistent_arabic_keyboard(self):
        bot = self.bot()
        bot._reply(123, "test")
        payload = bot._api_call.call_args.args[1]
        self.assertEqual(len(payload["reply_markup"]["keyboard"]), 3)
        self.assertTrue(payload["reply_markup"]["is_persistent"])
        self.assertNotIn("parse_mode", payload)

    def test_status_button_uses_actual_counters(self):
        bot = self.bot()
        bot.orchestrator.get_live_status.return_value = {"running":True,"kpis":{"completed":3,"total":9,"verified":1}}
        bot._handle_update(self.bot_update("📊 حالة الفحص"))
        text = bot._api_call.call_args.args[1]["text"]
        self.assertIn("أرصدة مؤكدة: 1", text)
        self.assertIn("تمت معالجتها: 3 من 9", text)

    def test_unauthorized_sender_cannot_use_bot_buttons(self):
        bot = self.bot()
        bot._handle_update(self.bot_update("⏸ إيقاف مؤقت", sender=456))
        bot.orchestrator.pause_session.assert_not_called()
        bot._api_call.assert_not_called()

    def test_resume_button_requires_active_session_and_completed_verification(self):
        bot = self.bot()
        for state in ({"running":False}, {"running":True,"verifications":[{"task_id":"test"}]}):
            bot.orchestrator.get_live_status.return_value = state
            bot._handle_update(self.bot_update("▶ استئناف الفحص"))
        bot.orchestrator.resume_session.assert_not_called()

if __name__ == "__main__":
    unittest.main()
