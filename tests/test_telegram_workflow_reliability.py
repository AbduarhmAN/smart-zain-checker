"""Retained/in-memory Telegram workflow checks. No live requests or file deletion."""
import threading
import time
import unittest
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from openpyxl import Workbook
from telegram_bot import views
from telegram_bot.sheet_workflow import SheetWorkflowMixin, review_panel
from telegram_bot.ui import TelegramUiMixin
from telegram_bot.commerce.bot_workflow import CommerceBotMixin


class WorkflowHarness(CommerceBotMixin, SheetWorkflowMixin, TelegramUiMixin):
    def __init__(self):
        self.authorized_id = 123
        self.project_root = Path.cwd()
        self.bot_token = "offline-fixture"
        self._ui_lock = threading.RLock()
        self._sheet_drafts = {}
        self._confirmations = {}
        self._waiting_user_inputs = {}
        self.queue_service = Mock()
        self.queue_service.get_jobs.return_value = []
        self.orchestrator = Mock(lock=threading.RLock(), current_job_id=None, session_token="fixture")
        self.orchestrator.get_live_status.return_value = {
            "workbook": "private-admin-file.xlsx", "running": False,
            "kpis": {"total": 2, "completed": 2},
        }
        self._api_call = Mock(return_value={"ok": True})
        self._show_panel = Mock()


class WorkflowReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.network = patch("urllib.request.urlopen", side_effect=AssertionError("Live network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.bot = WorkflowHarness()
        self.wb = Workbook()
        self.wb.active.append(["رقم الحساب", "المبلغ"])
        self.wb.active.append(["1000000001", 0])
        for context in (patch("telegram_bot.sheet_workflow.load_workbook", return_value=self.wb),
                        patch("telegram_bot.sheet_workflow.file_version", return_value=(123, 456))):
            context.start()
            self.addCleanup(context.stop)

    def open(self, chat=123):
        self.bot._open_sheet_review(Path("fixture.xlsx"), chat)
        return next(reversed(self.bot._sheet_drafts))

    def actions(self, panel):
        return [button["callback_data"] for row in panel.rows for button in row if "callback_data" in button]

    def test_review_rendering_never_fetches_currency_rate(self):
        token = self.open()
        panel = review_panel(self.bot._sheet_drafts[token], token)
        self.assertIn("الرسوم وطرق الدفع", str(panel.rows))
        self.assertNotIn("30 ج.م", str(panel.rows))

    def test_invalid_review_hides_save_and_payment_but_keeps_mapping(self):
        self.wb.active["B2"] = None
        token = self.open()
        actions = self.actions(review_panel(self.bot._sheet_drafts[token], token))
        self.assertNotIn(f"zc:map:{token}:save", actions)
        self.assertNotIn(f"zc:map:{token}:pay", actions)
        self.assertIn(f"zc:map:{token}:field:amount_col:0", actions)

    def test_customer_cannot_bypass_payment_by_forging_save(self):
        token = self.open(456)
        self.bot._sheet_callback(f"map:{token}:save", 456, 1)
        self.bot.queue_service.add_job.assert_not_called()
        self.assertIn("أكمل الدفع", self.bot._show_panel.call_args.args[1].text)

    def test_customer_review_has_no_admin_queue_link(self):
        token = self.open(456)
        self.assertNotIn(f"zc:map:{token}:save", self.actions(review_panel(self.bot._sheet_drafts[token], token)))
        self.assertNotIn("zc:queue:0", self.actions(self.bot._drafts_panel(456)))

    def test_customers_home_never_reads_or_exposes_admin_session(self):
        with patch("telegram_bot.payment_manager.PAYMENT_MGR") as manager:
            manager.get_user_account_id.return_value = "fixture-user"
            manager.get_wallet_balance.return_value = 0
            self.bot._dispatch_ui("home", 456, 1)
        panel = self.bot._show_panel.call_args.args[1]
        self.assertNotIn("private-admin-file", panel.text)
        self.assertNotIn("zc:queue:0", self.actions(panel))
        self.bot.orchestrator.get_live_status.assert_not_called()

    def test_customer_drafts_callback_works_but_group_callback_is_denied(self):
        self.open(456)
        callback = {"id": "offline", "from": {"id": 456},
                    "message": {"chat": {"id": 456}, "message_id": 7}, "data": "zc:drafts:0"}
        self.bot._handle_callback(callback)
        self.assertIn("ملفات تحتاج مراجعة", self.bot._show_panel.call_args.args[1].text)
        self.bot._show_panel.reset_mock()
        callback["message"]["chat"]["id"] = -456
        self.bot._handle_callback(callback)
        self.bot._show_panel.assert_not_called()
        self.assertTrue(self.bot._api_call.call_args.args[1]["show_alert"])

    def test_ui_only_expansion_uses_cached_validation(self):
        token = self.open()
        with patch("telegram_bot.sheet_workflow.validate_sheet") as validator:
            self.bot._sheet_callback(f"map:{token}:advanced", 123, 1)
            self.bot._sheet_callback(f"map:{token}:mawarid", 123, 1)
        validator.assert_not_called()

    def test_schema_inspection_does_not_double_scan_data_rows(self):
        from domain.workbook import inspect_sheet_schema
        with patch("telegram_bot.sheet_workflow.inspect_sheet_schema", wraps=inspect_sheet_schema) as inspect:
            self.open()
        self.assertFalse(inspect.call_args.kwargs["count_rows"])

    def test_no_header_toggle_includes_first_data_row(self):
        self.wb.active["A1"] = "1000000002"
        self.wb.active["B1"] = 10
        token = self.open()
        draft = self.bot._sheet_drafts[token]
        draft["mapping"].update(lookup_col="A", service_col="A", amount_col="B")
        self.bot._sheet_callback(f"map:{token}:headers", 123, 1)
        self.assertTrue(draft["validation"]["ok"])
        self.assertEqual(draft["validation"]["rows"], 2)

    def test_pending_mapping_preserves_mawarid_flag(self):
        self.bot.queue_service.get_job.return_value = SimpleNamespace(
            id="pending", status="pending", completed=0, sheet_index=0,
            column_mapping={"lookup_col": "A", "amount_col": "B", "has_headers": True, "mawarid_format": True},
            mode="account_only", amount_target="remaining")
        self.bot._open_sheet_review(Path("fixture.xlsx"), 123, job=self.bot.queue_service.get_job.return_value)
        draft = next(iter(self.bot._sheet_drafts.values()))
        self.assertTrue(draft["mapping"]["mawarid_format"])

    def test_failed_invoice_is_visible_and_does_not_enqueue(self):
        token = self.open(456)
        with patch("telegram_bot.crypto_pay.CRYPTO_PAY") as provider, \
             patch("telegram_bot.crypto_pay.calculate_egp_to_usdt", return_value=(0.5, 60)), \
             patch("telegram_bot.payment_manager.PAYMENT_MGR") as manager:
            manager.get_config.return_value = {"sheet_price_egp": 30}
            manager.generate_operation_id.return_value = "fixture-operation"
            provider.create_invoice.return_value = None
            self.bot._sheet_callback(f"map:{token}:pay", 456, 1)
        self.assertIn("تعذر إنشاء فاتورة", self.bot._show_panel.call_args.args[1].text)
        self.bot.queue_service.add_job.assert_not_called()

    def test_repeated_invoice_open_keeps_original_quote_and_one_invoice(self):
        token = self.open(456)
        with patch("telegram_bot.crypto_pay.CRYPTO_PAY") as provider, \
             patch("telegram_bot.crypto_pay.calculate_egp_to_usdt", return_value=(0.5, 60)) as conversion, \
             patch("telegram_bot.payment_manager.PAYMENT_MGR") as manager:
            manager.get_config.return_value = {"sheet_price_egp": 30}
            manager.generate_operation_id.return_value = "fixture-operation"
            manager.get_wallet_balance.return_value = 0
            manager.get_user_account_id.return_value = "fixture-user"
            provider.create_invoice.return_value = {"invoice_id": 7, "bot_invoice_url": "https://example.invalid/fixture"}
            self.bot._sheet_callback(f"map:{token}:pay", 456, 1)
            manager.get_config.return_value = {"sheet_price_egp": 45}
            self.bot._sheet_callback(f"map:{token}:pay", 456, 1)
        provider.create_invoice.assert_called_once()
        conversion.assert_called_once()
        self.assertEqual(self.bot._sheet_drafts[token]["egp_amount"], 30)

    def test_wrong_chat_does_not_consume_other_users_confirmation(self):
        self.bot._confirmations["fixture"] = {"chat": 123, "expires": time.monotonic() + 20, "action": "pause"}
        self.bot._confirm_ui_action("fixture", 456, 7)
        self.assertIn("fixture", self.bot._confirmations)
        self.bot.orchestrator.pause_session.assert_not_called()

    def test_pending_result_save_has_no_result_download_control(self):
        for updates in ({"finalizing": True}, {"result_ready": False}):
            state = {"running": False, "kpis": {"total": 1, "completed": 1}, **updates}
            self.assertTrue(self.bot._action_problem("results", state))
            self.assertNotIn("zc:results", self.actions(views.status(state)))

    def test_status_shows_elapsed_time_without_client_timezone_assumptions(self):
        panel = views.status({"session_timing": {"started_at": 1700000000, "server_time": 1700003661}})
        self.assertIn("01:01:01", panel.text)
        self.assertIn("توقيت بغداد", panel.text)

    def test_zero_is_rendered_as_a_value_not_missing(self):
        self.assertEqual(views.safe(0), "0")

    def test_wallet_fee_uses_configuration_and_never_invents_a_conversion(self):
        panel = views.wallet_panel(456, 100, sheet_price_egp=45)
        self.assertIn("45.00 جنيه", panel.text)
        self.assertNotIn("30.00", panel.text)
        self.assertNotIn("USDT", panel.text)
        self.assertIn("0.75 USDT", views.wallet_panel(456, 100, live_rate=60, sheet_price_egp=45).text)

    def test_customer_panels_only_include_usable_navigation_and_semantic_buttons(self):
        for panel in (views.customer_home(456), views.upload(admin=False), views.help_panel(admin=False)):
            self.assertLess(len(panel.text), 4096)
            for row in panel.rows:
                for button in row:
                    self.assertLessEqual(len(button.get("callback_data", "").encode()), 64)
            self.assertNotIn("zc:status", self.actions(panel))
            self.assertNotIn("zc:queue:0", self.actions(panel))

    def test_shared_customer_notices_never_offer_admin_status_or_queue_actions(self):
        panel = views.notice("تعذر الإجراء", "حاول مجددًا")
        rows = views.customer_rows(panel.rows + [[views.button("الجولة", "start_queue")]])
        self.assertEqual([b["callback_data"] for row in rows for b in row], ["zc:home"])
        self.assertEqual(views.customer_rows([])[0][0]["callback_data"], "zc:home")
        TelegramUiMixin._show_panel(self.bot, 456, panel)
        keyboard = self.bot._api_call.call_args.args[1]["reply_markup"]["inline_keyboard"]
        self.assertEqual([b["callback_data"] for row in keyboard for b in row], ["zc:home"])

    def test_customer_payment_urls_and_wallet_actions_are_preserved(self):
        rows = [[views.url_button("الدفع", "https://example.invalid/fixture"),
                 views.button("الكود", "wallet:redeem")], [views.button("طلب آخر", "drafts:1")]]
        self.assertEqual(views.customer_rows(rows), rows)

    def retained_store(self):
        fixture_parent = Path.cwd() / "outputs" / "telegram_audit_20261007" / "retained_workflow_fixtures"
        fixture_parent.mkdir(parents=True, exist_ok=True)
        root = Path(tempfile.mkdtemp(prefix="draft_", dir=fixture_parent))
        self.wb.save(root / "fixture.xlsx")
        self.bot.project_root = root
        self.bot._restore_sheet_drafts()
        self.bot._open_sheet_review(root / "fixture.xlsx", 456)
        return root, next(iter(self.bot._sheet_drafts))

    def restart(self, root):
        restored = WorkflowHarness()
        restored.project_root = root
        restored._restore_sheet_drafts()
        return restored

    def test_durable_review_recovery_keeps_owner_mapping_and_invoice_quote(self):
        root, token = self.retained_store()
        draft = self.bot._sheet_drafts[token]
        draft.update(invoice_id=7, invoice={"invoice_id":7}, egp_amount=30, usdt_amount=0.5, live_rate=60)
        draft["mapping"]["mawarid_format"] = True
        self.assertTrue(self.bot._persist_sheet_drafts())
        restored = self.restart(root)
        recovered = restored._sheet_drafts[token]
        self.assertEqual(recovered["chat"], 456)
        self.assertEqual(recovered["egp_amount"], 30)
        self.assertEqual(recovered["invoice_id"], 7)
        self.assertTrue(recovered["mapping"]["mawarid_format"])
        self.assertEqual(recovered["path"], root / "fixture.xlsx")
        self.assertGreater(recovered["expires"], time.monotonic())

    def test_changed_workbook_or_expired_review_is_not_recovered(self):
        root, token = self.retained_store()
        with patch("telegram_bot.sheet_workflow.file_version", return_value=(999, 999)):
            self.assertFalse(self.restart(root)._sheet_drafts)
        store = root / ".zain-telegram-drafts.json"
        snapshot = json.loads(store.read_text(encoding="utf-8"))
        snapshot["drafts"][token]["expires_at"] = time.time() - 1
        store.write_text(json.dumps(snapshot), encoding="utf-8")
        self.assertFalse(self.restart(root)._sheet_drafts)

    def test_recovery_rejects_paths_outside_project_root(self):
        root, token = self.retained_store()
        store = root / ".zain-telegram-drafts.json"
        snapshot = json.loads(store.read_text(encoding="utf-8"))
        snapshot["drafts"][token]["path"] = str(root.parent / "other.xlsx")
        store.write_text(json.dumps(snapshot), encoding="utf-8")
        self.assertFalse(self.restart(root)._sheet_drafts)

    def test_failed_atomic_checkpoint_retains_last_known_good_snapshot(self):
        root, token = self.retained_store()
        store = root / ".zain-telegram-drafts.json"
        original = store.read_text(encoding="utf-8")
        self.bot._sheet_drafts[token]["mapping"]["customer_col"] = "A"
        with patch("telegram_bot.sheet_workflow.os.replace", side_effect=PermissionError("fixture")):
            self.assertFalse(self.bot._persist_sheet_drafts())
        self.assertEqual(store.read_text(encoding="utf-8"), original)
        self.assertTrue(self.bot.draft_storage_error)
        self.assertTrue(list(root.glob("*.tmp")))

    def test_corrupt_state_is_reported_and_retained_without_deletion(self):
        root, _ = self.retained_store()
        store = root / ".zain-telegram-drafts.json"
        store.write_text("invalid fixture JSON", encoding="utf-8")
        restored = self.restart(root)
        self.assertTrue(restored.draft_storage_error)
        self.assertEqual(store.read_text(encoding="utf-8"), "invalid fixture JSON")

    def test_cleared_review_state_does_not_return_after_restart(self):
        root, token = self.retained_store()
        self.bot._sheet_drafts.pop(token)
        self.bot._persist_sheet_drafts()
        self.assertFalse(self.restart(root)._sheet_drafts)


if __name__ == "__main__":
    unittest.main()
