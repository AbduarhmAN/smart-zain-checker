"""Offline payment reliability regression tests; fixtures are retained."""
from pathlib import Path
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

from telegram_bot.payment_manager import PaymentManager, PaymentPersistenceError
from telegram_bot.crypto_pay import CryptoPayClient, calculate_egp_to_usdt
from telegram_bot.ui import TelegramUiMixin
from telegram_bot.commerce.bot_workflow import CommerceBotMixin


class PaymentReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix='zain_payment_audit_'))
        self.manager = PaymentManager(self.folder)

    def test_decimal_wallet_arithmetic(self):
        for _ in range(3):
            self.manager.credit_wallet(1, .1)
        self.assertEqual(self.manager.get_wallet_balance(1), .3)
        self.assertEqual(self.manager.debit_wallet(1, .3), (True, 0.0))

    def test_half_cent_rounds_explicitly(self):
        self.assertEqual(self.manager.credit_wallet(1, '1.005'), 1.01)

    def test_invalid_amounts_do_not_change_wallet(self):
        for value in [-1, 'NaN', 'Infinity', '-Infinity', 'oops']:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.manager.credit_wallet(1, value)
                with self.assertRaises(ValueError):
                    self.manager.debit_wallet(1, value)
        self.assertEqual(self.manager.get_wallet_balance(1), 0)

    def test_credit_and_debit_retry_keys(self):
        self.manager.credit_wallet(1, 20, idempotency_key='deposit-1')
        self.manager.credit_wallet(1, 20, idempotency_key='deposit-1')
        self.manager.debit_wallet(1, 5, idempotency_key='check-1')
        self.manager.debit_wallet(1, 5, idempotency_key='check-1')
        self.assertEqual(self.manager.get_wallet_balance(1), 15)
        with self.assertRaises(ValueError):
            self.manager.debit_wallet(1, 6, idempotency_key='check-1')

    def test_insufficient_funds_no_mutation(self):
        self.manager.credit_wallet(1, 2)
        self.assertEqual(self.manager.debit_wallet(1, 3), (False, 2))
        self.assertEqual(len(self.manager.get_or_create_user(1)['history']), 1)

    def test_instances_share_account_lock(self):
        other = PaymentManager(self.folder)
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda i: (self.manager if i % 2 else other).credit_wallet(1, .1), range(60)))
        self.assertEqual(self.manager.get_wallet_balance(1), 6)

    def test_unreadable_wallet_fails_closed(self):
        self.manager.wallets_path.write_text('{bad json', encoding='utf-8')
        with self.assertRaises(PaymentPersistenceError):
            self.manager.credit_wallet(1, 3)
        self.assertEqual(self.manager.wallets_path.read_text(encoding='utf-8'), '{bad json')

    def test_non_object_wallet_fails_closed(self):
        self.manager.wallets_path.write_text('[]', encoding='utf-8')
        with self.assertRaises(PaymentPersistenceError):
            self.manager.get_wallet_balance(1)

    def test_failed_replace_preserves_old_balance(self):
        self.manager.credit_wallet(1, 10)
        with patch('telegram_bot.payment_manager.os.replace', side_effect=OSError('mock disk failure')):
            with self.assertRaises(PaymentPersistenceError):
                self.manager.credit_wallet(1, 4)
        self.assertEqual(self.manager.get_wallet_balance(1), 10)

    def test_redeem_retry_after_config_write_failure(self):
        self.manager.add_redeem_code('safe', 12)
        original = self.manager._save_json
        def fail_config(path, data):
            if path == self.manager.config_path:
                raise PaymentPersistenceError('mock failure')
            return original(path, data)
        with patch.object(self.manager, '_save_json', side_effect=fail_config):
            with self.assertRaises(PaymentPersistenceError):
                self.manager.redeem_code('safe', 1)
        self.assertTrue(self.manager.redeem_code('safe', 1)[0])
        self.assertEqual(self.manager.get_wallet_balance(1), 12)
        self.assertFalse(self.manager.redeem_code('safe', 1)[0])

    def test_promo_claim_retry_reuses_same_coupon_without_another_use(self):
        self.manager.add_promo_code('free', max_uses=1)
        self.assertTrue(self.manager.apply_promo_code('free', 1, claim_id='token')[0])
        self.assertTrue(self.manager.apply_promo_code('free', 1, claim_id='token')[0])
        self.assertFalse(self.manager.apply_promo_code('free', 1, claim_id='another')[0])
        self.assertFalse(self.manager.apply_promo_code('free', 2, claim_id='token')[0])
        self.assertEqual(self.manager.get_config()['promo_codes']['FREE']['used_by'], ['1'])

    def test_promo_claim_write_failure_does_not_consume_coupon(self):
        self.manager.add_promo_code('free', max_uses=1)
        with patch.object(self.manager, '_save_json', side_effect=PaymentPersistenceError('mock write failure')):
            with self.assertRaises(PaymentPersistenceError):
                self.manager.apply_promo_code('free', 1, claim_id='token')
        self.assertTrue(self.manager.apply_promo_code('free', 1, claim_id='token')[0])

    def test_default_config_isolation(self):
        cfg = self.manager.get_config()
        cfg['promo_codes']['WELCOME']['used_by'].append('changed')
        other = PaymentManager(Path(tempfile.mkdtemp(prefix='zain_payment_other_')))
        self.assertNotIn('changed', other.get_config()['promo_codes']['WELCOME']['used_by'])

    def draft(self, owner=1):
        path = self.folder / 'mock.xlsx'
        path.write_bytes(b'not loaded, version fixture only')
        return {'path': path, 'chat': owner, 'expires': time.monotonic()+30,
                'version': (path.stat().st_size, path.stat().st_mtime_ns), 'sheet_index': 0,
                'sheet_name': 'Sheet1', 'mode': 'account_only', 'amount_target': 'contract',
                'metadata': {}, 'mapping': {}, 'validation': {'ok': True, 'rows': 3},
                'invoice_id': 10, 'egp_amount': 30.0, 'usdt_amount': .6, 'live_rate': 50}

    def pending(self, token='token', tx='tx-1'):
        self.manager.record_pending_payment(token, 'Transfer', tx, 1, 'test', 'Sheet1', .6,
                                            self.draft(), operation_id='261007-0001')

    def test_pending_path_serialization(self):
        self.pending()
        self.assertIsInstance(self.manager.get_pending_payment('token')['draft_info']['path'], str)

    def test_duplicate_transaction_rejected(self):
        self.pending()
        with self.assertRaises(ValueError):
            self.pending('another')

    def test_manual_approval_retained_until_fulfilment(self):
        self.pending()
        self.assertEqual(self.manager.resolve_pending_payment('token', True)['status'], 'approved')
        self.assertIsNotNone(self.manager.resolve_pending_payment('token', True))
        self.manager.mark_payment_fulfilled('token', 'job-1')
        self.assertIsNone(self.manager.resolve_pending_payment('token', True))
        self.assertEqual(self.manager.get_operation('261007-0001')['status'], 'fulfilled')

    def test_rejected_request_cannot_be_approved(self):
        self.pending()
        self.manager.resolve_pending_payment('token', False)
        self.assertIsNone(self.manager.resolve_pending_payment('token', True))

    def test_rejected_manual_request_can_submit_new_evidence(self):
        self.pending()
        self.manager.resolve_pending_payment('token', False)
        self.pending(tx='tx-2')
        record = self.manager.get_pending_payment('token')
        self.assertEqual(record['status'], 'pending')
        self.assertEqual(record['tx_id'], 'tx-2')
        self.assertEqual(record['previous_attempts'][0]['tx_id'], 'tx-1')

    def test_manual_retry_after_queue_failure(self):
        self.pending()
        harness = self.harness(self.manager.get_pending_payment('token')['draft_info'])
        with patch('telegram_bot.payment_manager.PAYMENT_MGR', self.manager):
            with patch.object(harness.queue_service, 'add_job', side_effect=OSError('mock queue failure')):
                harness._handle_admin_payment_callback('adminpay:approve:token', 99, 1)
            self.assertEqual(self.manager.get_pending_payment('token')['status'], 'approved')
            harness._handle_admin_payment_callback('adminpay:approve:token', 99, 1)
            harness._handle_admin_payment_callback('adminpay:approve:token', 99, 1)
        self.assertEqual(len(harness.queue_service.get_jobs()), 1)
        self.assertEqual(self.manager.get_pending_payment('token')['status'], 'fulfilled')

    def test_fulfilment_write_failure_retries_existing_job(self):
        harness = self.harness()
        self.manager.credit_wallet(1, 50)
        with patch('telegram_bot.payment_manager.PAYMENT_MGR', self.manager):
            with patch.object(self.manager, 'complete_payment_draft', side_effect=PaymentPersistenceError('mock write failure')):
                harness._handle_payment_method_callback('pay:wallet:token', 1, 1, {})
            harness._handle_payment_method_callback('pay:wallet:token', 1, 1, {})
        self.assertEqual(self.manager.get_wallet_balance(1), 20)
        self.assertEqual(len(harness.queue_service.get_jobs()), 1)

    def test_crypto_invoice_query_shape_and_matching_id(self):
        client = CryptoPayClient(token='mock')
        with patch.object(client, '_call', return_value={'items': [{'invoice_id': 99}, {'invoice_id': 10}]}) as call:
            self.assertEqual(client.get_invoice(10)['invoice_id'], 10)
            call.assert_called_once_with('getInvoices', {'invoice_ids': '10'})

    def test_crypto_bound_invoice_validation(self):
        client = CryptoPayClient(token='mock')
        valid = {'invoice_id': 10, 'status': 'paid', 'amount': '.6', 'asset': 'USDT', 'payload': 'token'}
        with patch.object(client, 'get_invoice', return_value=valid):
            self.assertTrue(client.is_invoice_paid(10, expected_amount=.6, expected_asset='USDT', expected_payload='token'))
            for params in [{'expected_amount': .7}, {'expected_asset': 'USDC'}, {'expected_payload': 'another'}]:
                self.assertFalse(client.is_invoice_paid(10, **params))
        with patch.object(client, 'get_invoice', return_value={**valid, 'amount': 'NaN'}):
            self.assertFalse(client.is_invoice_paid(10, expected_amount=.6))

    def test_invalid_crypto_amount_never_requests(self):
        client = CryptoPayClient(token='mock')
        with patch.object(client, '_call') as call:
            for amount in [-1, 0, float('nan'), float('inf')]:
                self.assertIsNone(client.create_invoice(amount))
            call.assert_not_called()

    def test_no_token_never_requests(self):
        client = CryptoPayClient(token='')
        with patch('urllib.request.urlopen') as network:
            self.assertIsNone(client.get_me())
            network.assert_not_called()

    def test_rate_failure_has_no_invented_quote(self):
        import telegram_bot.crypto_pay as crypto
        with patch.object(crypto, '_LAST_RATE', None), patch.object(crypto, '_LAST_RATE_TIME', 0), \
             patch('urllib.request.urlopen', side_effect=OSError('offline')):
            with self.assertRaises(RuntimeError):
                crypto.get_live_usd_egp_rate()

    def test_conversion_uses_decimal_quote(self):
        with patch('telegram_bot.crypto_pay.get_live_usd_egp_rate', return_value=50):
            self.assertEqual(calculate_egp_to_usdt(30), (.6, 50))
            with self.assertRaises(ValueError):
                calculate_egp_to_usdt(float('nan'))

    def harness(self, draft=None):
        class Harness(CommerceBotMixin, TelegramUiMixin):
            def _show_panel(self, chat, panel, message=None):
                self.panels.append(panel)
            def _api_call(self, *args):
                self.messages.append(args)
                return {'ok': True}
        harness = Harness()
        harness.authorized_id = 99
        harness._ui_lock = threading.RLock()
        harness.orchestrator = SimpleNamespace(lock=threading.RLock())
        harness._sheet_drafts = {'token': draft or self.draft()}
        harness._waiting_user_inputs = {}
        harness.panels, harness.messages = [], []
        jobs = []
        def add(**args):
            job = {'id': 'job-1', **args}
            jobs.append(job)
            return SimpleNamespace(id='job-1')
        harness.queue_service = SimpleNamespace(get_jobs=lambda: jobs, add_job=add)
        return harness

    def test_wallet_callback_foreign_owner_not_charged(self):
        harness = self.harness()
        self.manager.credit_wallet(2, 40)
        with patch('telegram_bot.payment_manager.PAYMENT_MGR', self.manager):
            harness._handle_payment_method_callback('pay:wallet:token', 2, 1, {})
        self.assertEqual(self.manager.get_wallet_balance(2), 40)
        self.assertEqual(harness.queue_service.get_jobs(), [])

    def test_wallet_callback_duplicate_safe(self):
        harness = self.harness()
        self.manager.credit_wallet(1, 50)
        with patch('telegram_bot.payment_manager.PAYMENT_MGR', self.manager):
            harness._handle_payment_method_callback('pay:wallet:token', 1, 1, {})
            harness._handle_payment_method_callback('pay:wallet:token', 1, 1, {})
        self.assertEqual(self.manager.get_wallet_balance(1), 20)
        self.assertEqual(len(harness.queue_service.get_jobs()), 1)
        self.assertEqual(harness.queue_service.get_jobs()[0]['column_mapping']['telegram_chat_id'], 1)

    def test_wallet_retry_after_queue_failure_does_not_double_debit(self):
        harness = self.harness()
        self.manager.credit_wallet(1, 50)
        with patch('telegram_bot.payment_manager.PAYMENT_MGR', self.manager):
            with patch.object(harness.queue_service, 'add_job', side_effect=OSError('mock queue failure')):
                harness._handle_payment_method_callback('pay:wallet:token', 1, 1, {})
            harness._handle_payment_method_callback('pay:wallet:token', 1, 1, {})
        self.assertEqual(self.manager.get_wallet_balance(1), 20)
        self.assertEqual(len(harness.queue_service.get_jobs()), 1)

    def test_crypto_callback_rejects_foreign_invoice_id(self):
        harness = self.harness()
        with patch('telegram_bot.payment_manager.PAYMENT_MGR', self.manager), \
             patch('telegram_bot.crypto_pay.CRYPTO_PAY.is_invoice_paid') as verify:
            harness._handle_crypto_payment_check('paycheck:11:token', 1, 1)
            verify.assert_not_called()
        self.assertEqual(harness.queue_service.get_jobs(), [])

    def test_crypto_snapshot_recovers_after_restart_and_expiry(self):
        draft = self.draft()
        draft['expires'] = time.monotonic()-60
        self.manager.record_payment_draft('token', 1, draft)
        harness = self.harness(draft)
        harness._sheet_drafts.clear()
        with patch('telegram_bot.payment_manager.PAYMENT_MGR', self.manager), \
             patch('telegram_bot.crypto_pay.CRYPTO_PAY.is_invoice_paid', return_value=True) as verify:
            harness._handle_crypto_payment_check('paycheck:10:token', 1, 1)
            verify.assert_called_once_with(10, expected_amount=.6, expected_payload='token', expected_asset='USDT')
        self.assertEqual(len(harness.queue_service.get_jobs()), 1)
        self.assertEqual(self.manager.get_pending_payment('token')['status'], 'fulfilled')

    def test_manual_admin_guard(self):
        self.pending()
        harness = self.harness()
        with patch('telegram_bot.payment_manager.PAYMENT_MGR', self.manager):
            harness._handle_admin_payment_callback('adminpay:approve:token', 1, 1)
        self.assertEqual(self.manager.get_pending_payment('token')['status'], 'pending')

    def promo_input(self, harness, code):
        from telegram_bot.bot import TelegramBotRunner
        return TelegramBotRunner._handle_user_text_input(harness, 1, 'mock', code,
            {'action': 'promo', 'token': 'token', 'message_id': 1})

    def test_free_coupon_queue_failure_is_retriable_after_restart(self):
        harness = self.harness()
        self.manager.add_promo_code('free', max_uses=1)
        with patch('telegram_bot.payment_manager.PAYMENT_MGR', self.manager):
            with patch.object(harness.queue_service, 'add_job', side_effect=OSError('mock queue failure')):
                self.promo_input(harness, 'free')
            harness._sheet_drafts.clear()
            self.promo_input(harness, 'free')
            self.promo_input(harness, 'free')
        self.assertEqual(len(harness.queue_service.get_jobs()), 1)
        self.assertEqual(self.manager.get_config()['promo_codes']['FREE']['used_by'], ['1'])
        self.assertEqual(harness.queue_service.get_jobs()[0]['column_mapping']['telegram_chat_id'], 1)
        self.assertEqual(self.manager.get_pending_payment('token')['status'], 'fulfilled')

    def test_percent_coupon_retry_does_not_apply_twice(self):
        harness = self.harness()
        self.manager.add_promo_code('half', 'percent', 1, 50)
        with patch('telegram_bot.payment_manager.PAYMENT_MGR', self.manager), \
             patch('telegram_bot.crypto_pay.calculate_egp_to_usdt', return_value=(.3, 50)):
            self.promo_input(harness, 'half')
            harness._sheet_drafts.clear()
            self.promo_input(harness, 'half')
        self.assertEqual(harness._sheet_drafts['token']['egp_amount'], 15)
        self.assertEqual(self.manager.get_config()['promo_codes']['HALF']['used_by'], ['1'])

    def test_percent_rate_failure_preserves_discount_and_claim(self):
        harness = self.harness()
        self.manager.add_promo_code('half', 'percent', 1, 50)
        with patch('telegram_bot.payment_manager.PAYMENT_MGR', self.manager):
            with patch('telegram_bot.crypto_pay.calculate_egp_to_usdt', side_effect=RuntimeError('offline')):
                self.promo_input(harness, 'half')
            self.assertEqual(self.manager.get_pending_payment('token')['draft_info']['egp_amount'], 15)
            harness._sheet_drafts.clear()
            with patch('telegram_bot.crypto_pay.calculate_egp_to_usdt', return_value=(.3, 50)) as convert:
                self.promo_input(harness, 'half')
                convert.assert_called_once_with(15.0)
        self.assertEqual(harness._sheet_drafts['token']['egp_amount'], 15)

    def test_percent_quote_is_used_when_creating_replacement_invoice(self):
        from telegram_bot.sheet_workflow import SheetWorkflowMixin
        harness = self.harness()
        harness._persist_sheet_drafts = lambda: True
        self.manager.add_promo_code('half', 'percent', 1, 50)
        with patch('telegram_bot.payment_manager.PAYMENT_MGR', self.manager), \
             patch('telegram_bot.crypto_pay.calculate_egp_to_usdt', return_value=(.3, 50)), \
             patch('telegram_bot.crypto_pay.CRYPTO_PAY.create_invoice', return_value={
                 'invoice_id': 20, 'pay_url': 'https://t.me/mock'}) as create:
            self.promo_input(harness, 'half')
            SheetWorkflowMixin._sheet_callback(harness, 'map:token:pay', 1, 1)
            self.assertEqual(create.call_args.kwargs['amount'], .3)
            self.assertEqual(self.manager.get_pending_payment('token')['draft_info']['egp_amount'], 15)

    def test_foreign_user_cannot_claim_coupon_for_draft(self):
        harness = self.harness(self.draft(owner=2))
        self.manager.add_promo_code('free', max_uses=1)
        with patch('telegram_bot.payment_manager.PAYMENT_MGR', self.manager):
            self.promo_input(harness, 'free')
        self.assertEqual(self.manager.get_config()['promo_codes']['FREE']['used_by'], [])

    def test_invalid_coupon_does_not_consume_usage(self):
        self.manager.add_promo_code('broken', 'percent', 1, -50)
        self.assertFalse(self.manager.apply_promo_code('broken', 1, claim_id='token')[0])
        self.assertEqual(self.manager.get_config()['promo_codes']['BROKEN']['used_by'], [])

    def test_previous_full_price_invoice_remains_bound_to_order(self):
        draft = self.draft()
        draft['invoice'] = {'invoice_id': 10, 'pay_url': 'https://t.me/mock'}
        harness = self.harness(draft)
        self.manager.add_promo_code('half', 'percent', 1, 50)
        with patch('telegram_bot.payment_manager.PAYMENT_MGR', self.manager), \
             patch('telegram_bot.crypto_pay.calculate_egp_to_usdt', return_value=(.3, 50)), \
             patch('telegram_bot.crypto_pay.CRYPTO_PAY.is_invoice_paid', return_value=True) as paid:
            self.promo_input(harness, 'half')
            harness._handle_crypto_payment_check('paycheck:10:token', 1, 1)
            paid.assert_called_once_with(10, expected_amount=.6, expected_payload='token', expected_asset='USDT')
        self.assertEqual(len(harness.queue_service.get_jobs()), 1)

    def test_file_changes_block_wallet(self):
        draft = self.draft()
        draft['path'].write_bytes(b'changed')
        harness = self.harness(draft)
        self.manager.credit_wallet(1, 50)
        with patch('telegram_bot.payment_manager.PAYMENT_MGR', self.manager):
            harness._handle_payment_method_callback('pay:wallet:token', 1, 1, {})
        self.assertEqual(self.manager.get_wallet_balance(1), 50)


if __name__ == '__main__':
    unittest.main()
