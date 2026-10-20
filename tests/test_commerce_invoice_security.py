"""Receipt copying, ownership mismatches and automatic bound invoice checks."""
import copy
from datetime import datetime, timezone
import unittest
from unittest.mock import Mock, patch
from telegram_bot.bot import TelegramBotRunner
from telegram_bot.crypto_pay import BinancePayClient
from telegram_bot.commerce.invoice_monitor import BoundInvoiceMonitor
from telegram_bot.commerce.payment_providers import CryptoInvoiceAdapter
from telegram_bot.commerce.payment_quotes import freeze_quote


class InvoiceSecurityTests(unittest.TestCase):
    def setUp(self):
        self.quote = freeze_quote(31, 3000, 'USDT', '50', 'isolated-rate',
            now=datetime(2026, 10, 14, 1, tzinfo=timezone.utc),
            quote_id='836aeb46-e43c-49cf-809e-81171b9ac2ec')
        self.invoice = {'invoice_id': 8, 'status': 'paid', 'currency_type': 'crypto',
            'asset': 'USDT', 'amount': self.quote.asset_amount, 'payload': self.quote.payload,
            'created_at': self.quote.observed_at, 'paid_at': '2026-10-14T01:01:00Z'}
        self.row = {'quote_id': self.quote.id, 'telegram_user_id': 31, 'invoice_id': 8}
        self.request = {'provider': 'cryptopay', 'receiver': '7', 'invoice_id': 8, 'quote': self.quote.as_dict()}
        self.provider = Mock(base_url='https://pay.crypt.bot/api')
        self.provider.get_me.return_value = {'app_id': 7}
        self.provider._call.side_effect = lambda method, args: {'items': [copy.deepcopy(self.invoice)]}
        self.adapter = CryptoInvoiceAdapter(self.provider, app_id=7)
        self.client, self.confirmed = Mock(), []
        def rpc(name, args):
            if name == 'pending_bound_invoices':
                return [] if self.confirmed or args['p_after'] else [copy.deepcopy(self.row)]
            if name == 'payment_request': return copy.deepcopy(self.request)
            if name == 'confirm_payment_request':
                self.confirmed.append(copy.deepcopy(args))
                return {'payment_id': '937d0b53-c6cb-443d-8de8-a7edbe897b27'}
            raise AssertionError(name)
        self.client.rpc.side_effect = rpc
        self.callback = Mock()
        self.monitor = BoundInvoiceMonitor(self.client, lambda: self.adapter, on_confirmed=self.callback)

    def test_paid_invoice_is_credited_to_its_preexisting_owner_without_any_txid_input(self):
        self.assertEqual(self.monitor.tick()['confirmed'], 1)
        self.assertEqual(self.confirmed[0]['p_telegram_user_id'], 31)
        self.assertEqual(self.confirmed[0]['p_binding'], {'kind': 'invoice'})
        self.callback.assert_called_once()
        self.provider._call.assert_called_once_with('getInvoices', {'invoice_ids': '8', 'count': 1})
        self.provider.get_invoice.assert_not_called()

    def test_repeated_monitor_cycle_cannot_credit_again(self):
        self.monitor.tick();self.monitor.tick();self.monitor.tick()
        self.assertEqual(len(self.confirmed), 1);self.callback.assert_called_once()

    def test_lost_confirmation_ack_does_not_create_a_second_payment(self):
        original=self.client.rpc.side_effect
        def lost_ack(name,args):
            result=original(name,args)
            if name=='confirm_payment_request':raise RuntimeError('Committed confirmation response was lost')
            return result
        self.client.rpc.side_effect=lost_ack
        first=self.monitor.tick();self.monitor.tick();self.monitor.tick()
        self.assertEqual(first['requires_review'],1)
        self.assertEqual(len(self.confirmed),1);self.callback.assert_not_called()

    def test_copied_invoice_cannot_credit_a_different_telegram_account(self):
        self.row['telegram_user_id'] = 32
        self.assertEqual(self.monitor.tick()['confirmed'], 0)
        self.assertEqual(self.confirmed, []);self.callback.assert_not_called()

    def test_wrong_payload_receiver_amount_asset_or_time_never_credits(self):
        original = copy.deepcopy(self.invoice)
        for key, value in [('payload', 'zain-commerce:other'), ('amount', '1'), ('asset', 'USDC'),
                           ('paid_at', '2026-10-14T01:16:00Z'), ('created_at', '2026-10-14T00:59:00Z')]:
            self.invoice = {**original, key: value};self.monitor.after = None
            with self.subTest(key=key): self.assertEqual(self.monitor.tick()['confirmed'], 0)
        self.invoice = original;self.request['receiver'] = 'another-app';self.monitor.after = None
        self.assertEqual(self.monitor.tick()['confirmed'], 0);self.assertEqual(self.confirmed, [])

    def test_pending_invoice_never_credits(self):
        self.invoice['status'] = 'active'
        self.assertEqual(self.monitor.tick()['confirmed'], 0)
        self.assertFalse(any(call.args[0] == 'payment_request' for call in self.client.rpc.call_args_list))

    def test_scanned_id_cannot_replace_attached_invoice(self):
        self.request['invoice_id'] = 9
        self.assertEqual(self.monitor.tick()['confirmed'], 0);self.assertEqual(self.confirmed, [])

    def test_unexpected_or_conflicting_provider_batch_is_rejected(self):
        for items in [[{**self.invoice, 'invoice_id': 9}], [self.invoice, {**self.invoice, 'amount': '1'}]]:
            self.provider._call.side_effect = None;self.provider._call.return_value = {'items': items}
            with self.subTest(items=items), self.assertRaises(ValueError): self.monitor.tick()
        self.assertEqual(self.confirmed, [])

    def test_batch_failure_preserves_cursor_for_read_retry(self):
        self.provider._call.side_effect = OSError('simulated provider timeout')
        with self.assertRaises(OSError): self.monitor.tick()
        self.assertIsNone(self.monitor.after);self.assertFalse(self.monitor.lock.locked())

    def test_empty_page_never_calls_payment_provider(self):
        self.monitor.after = self.quote.id
        self.assertEqual(self.monitor.tick()['checked'], 0);self.provider._call.assert_not_called()
        self.assertIsNone(self.monitor.after)

    def test_overlapping_cycle_does_no_work(self):
        self.monitor.lock.acquire()
        try:
            self.assertTrue(self.monitor.tick()['busy']);self.client.rpc.assert_not_called()
        finally:self.monitor.lock.release()


class LegacyReceiptSecurityTests(unittest.TestCase):
    def test_legacy_binance_never_authorizes_a_public_reference(self):
        client = BinancePayClient(receiver_id=41)
        record = {'transactionId': 'TX1', 'orderId': 'ORDER1', 'amount': '10', 'currency': 'USDT',
                  'payerInfo': {'binanceId': '999'}, 'receiverInfo': {'binanceId': 41}}
        with patch.object(client, 'get_recent_transactions', return_value=[record]):
            for reference in ('TX1', 'ORDER1', '999'):
                with self.subTest(reference=reference):self.assertFalse(client.verify_payment(reference)[0])

    def test_legacy_topup_and_sheet_transfer_do_not_call_provider_or_write_money(self):
        bot = TelegramBotRunner.__new__(TelegramBotRunner)
        bot._show_panel = Mock();bot._commerce_text_input = Mock(return_value=False)
        with patch('telegram_bot.payment_manager.PAYMENT_MGR') as manager,\
             patch('telegram_bot.crypto_pay.BINANCE_PAY.verify_payment') as binance,\
             patch('telegram_bot.crypto_pay.BSC_CHAIN.verify_transaction') as bsc:
            for action in ('topup_tx', 'tx_id'):
                bot._handle_user_text_input(31, 'fixture', 'copied-txid', {'action': action, 'method_key': 'binance'})
            binance.assert_not_called();bsc.assert_not_called()
            manager.credit_wallet.assert_not_called();manager.complete_payment_draft.assert_not_called()


if __name__ == '__main__':unittest.main()
