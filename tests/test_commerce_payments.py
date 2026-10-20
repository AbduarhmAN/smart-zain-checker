import copy
import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import Mock

from telegram_bot.commerce.payment_quotes import freeze_quote, decimal_amount
from telegram_bot.commerce.payment_providers import (
    BinanceTransferAdapter, BscTransferAdapter, CryptoInvoiceAdapter,
    TokenSpec, TransferEvidence, TRANSFER_TOPIC)
from telegram_bot.commerce.payments import PaymentService

NOW = datetime(2026, 10, 14, 1, tzinfo=timezone.utc)
QID = '836aeb46-e43c-49cf-809e-81171b9ac2ec'
TX = '0x' + 'a' * 64
BLOCK = '0x' + 'b' * 64
RECEIVER = '0x' + 'c' * 40
CONTRACT = '0x' + 'd' * 40


def quote(**kwargs):
    return freeze_quote(31, 3000, 'USDT', '50.1', 'fixture-observation', now=NOW,
                        quote_id=QID, **kwargs)


def invoice(q=None):
    q = q or quote()
    return {'invoice_id': 8, 'status': 'paid', 'currency_type': 'crypto', 'asset': q.asset,
            'amount': q.asset_amount, 'payload': q.payload, 'created_at': q.observed_at,
            'paid_at': '2026-10-14T01:01:00+00:00'}


def crypto():
    client = Mock(base_url='https://pay.crypt.bot/api')
    client.get_me.return_value = {'app_id': 7}
    client.get_invoice.return_value = invoice()
    return CryptoInvoiceAdapter(client, app_id=7)


def transfer():
    return {'transactionId': 'TX1', 'orderId': 'ORDER1', 'orderType': 'C2C',
            'currency': 'USDT', 'amount': '0.598803', 'transactionTime': 1791939660123,
            'receiverInfo': {'binanceId': '41', 'accountId': '42'},
            'payerInfo': {'binanceId': '999'},
            'fundsDetail': [{'currency': 'USDT', 'amount': '0.598803'}]}


class QuoteTests(unittest.TestCase):
    def test_ceiling_rounding_covers_egp_without_assuming_a_peg(self):
        q = quote()
        self.assertEqual(q.asset_amount, '0.598803')
        self.assertGreaterEqual(Decimal(q.asset_amount) * Decimal(q.egp_per_asset), Decimal('30'))
        self.assertEqual(q.amount_minor, 3000)

    def test_invalid_exact_amounts_rejected(self):
        for amount in [True, 1.2, 'NaN', 'Infinity', '-1', '0', '1e100']:
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                decimal_amount(amount)

    def test_changed_rate_or_amount_invalidates_snapshot(self):
        for key, value in [('amount_minor', 3100), ('egp_per_asset', '51'), ('asset_amount', '.6')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                from telegram_bot.commerce.payment_quotes import PaymentQuote
                PaymentQuote(**{**quote().as_dict(), key: value})


class InvoiceTests(unittest.TestCase):
    def test_exact_bound_invoice_and_scoped_reference(self):
        result = crypto().verify(quote(), 8)
        self.assertEqual(result.reference, '7:8')
        self.assertEqual(result.invoice_payload, quote().payload)

    def test_wrong_app_or_testnet_rejected(self):
        for url, identity in [('https://testnet-pay.crypt.bot/api', 7),
                              ('https://pay.crypt.bot/api', 9)]:
            client = Mock(base_url=url)
            client.get_me.return_value = {'app_id': identity}
            with self.assertRaises(ValueError):
                CryptoInvoiceAdapter(client, app_id=7)

    def test_mismatches_pending_and_outside_window_rejected(self):
        for key, value in [('invoice_id', 9), ('status', 'active'), ('currency_type', 'fiat'),
                           ('asset', 'USDC'), ('amount', '0.5'), ('payload', 'other'),
                           ('paid_at', '2026-10-14T01:16:00Z'),
                           ('created_at', '2026-10-14T00:59:00Z')]:
            adapter = crypto()
            adapter.client.get_invoice.return_value = {**invoice(), key: value}
            with self.subTest(key=key), self.assertRaises(ValueError):
                adapter.verify(quote(), 8)


class BinanceTests(unittest.TestCase):
    def adapter(self, rows=None):
        client = Mock()
        client.get_recent_transactions.return_value = [transfer()] if rows is None else rows
        return BinanceTransferAdapter(client, receiver_uid=41, receiver_pay_id=42)

    def test_transaction_and_alias_share_one_canonical_reference(self):
        self.assertEqual(self.adapter().verify('ORDER1').reference, 'TX1')
        self.assertEqual(self.adapter().verify('TX1').reference, 'TX1')

    def test_payer_uid_never_identifies_a_payment(self):
        with self.assertRaises(ValueError):
            self.adapter().verify('999')

    def test_missing_wrong_receiver_wrong_asset_outgoing_refund_and_mixed_assets(self):
        mutations = [{'receiverInfo': {}}, {'receiverInfo': {'binanceId': '40', 'accountId': '42'}},
                     {'currency': 'BNB'}, {'amount': '-1'}, {'orderType': 'PAY_REFUND'},
                     {'fundsDetail': []}, {'fundsDetail': [{'currency': 'ETH', 'amount': '.598803'}]},
                     {'fundsDetail': [{'currency': 'USDT', 'amount': '1'}]}, {'amount': float('.598803')}]
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.adapter([{**transfer(), **mutation}]).verify('TX1')

    def test_no_amount_tolerance_or_old_transfers(self):
        for params in [{'minimum': '.6'}, {'not_before': '2026-10-15T00:00:00Z'}]:
            with self.subTest(params=params), self.assertRaises(ValueError):
                self.adapter().verify('TX1', **params)

    def test_conflicting_duplicate_provider_records_rejected(self):
        other = {**transfer(), 'amount': '1', 'fundsDetail': [{'currency': 'USDT', 'amount': '1'}]}
        with self.assertRaises(ValueError):
            self.adapter([transfer(), other]).verify('TX1')


class BscTests(unittest.TestCase):
    def setUp(self):
        self.log = {'address': CONTRACT, 'topics': [TRANSFER_TOPIC, '0x' + '0' * 24 + 'e' * 40,
                    '0x' + '0' * 24 + RECEIVER[2:]], 'data': '0x' + format(598803, '064x'),
                    'removed': False, 'transactionHash': TX, 'blockHash': BLOCK,
                    'blockNumber': '0x64', 'logIndex': '0x1'}
        self.receipt = {'status': '0x1', 'transactionHash': TX, 'blockNumber': '0x64',
                        'blockHash': BLOCK, 'logs': [self.log]}
        self.block = {'number': '0x64', 'hash': BLOCK, 'transactions': [TX],
                      'timestamp': hex(int(NOW.timestamp()) + 60)}
        self.chain = '0x38'
        self.final = {'number': '0x66'}
        self.decimals = 6

    def rpc(self, method, params):
        if method == 'eth_chainId': return self.chain
        if method == 'eth_getTransactionReceipt': return self.receipt
        if method == 'eth_getBlockByNumber': return self.final if params[0] == 'finalized' else self.block
        if method == 'eth_call': return '0x' + format(self.decimals, '064x')
        raise AssertionError(method)

    def verify(self):
        return BscTransferAdapter(self.rpc, receiver=RECEIVER,
                    token=TokenSpec('USDT', CONTRACT, 6)).verify(TX, minimum='.598803')

    def test_exact_contract_receiver_units_and_canonical_reference(self):
        evidence = self.verify()
        self.assertEqual(evidence.amount, '0.598803')
        self.assertEqual(evidence.reference, '56:' + TX)

    def test_spoofed_token_receiver_removed_log_and_decimals_fail(self):
        original = copy.deepcopy(self.log)
        for mutation in [{'address': '0x' + 'f' * 40}, {'removed': True},
                         {'topics': [TRANSFER_TOPIC, original['topics'][1], '0x' + '0' * 64]},
                         {'blockHash': '0x' + 'f' * 64}]:
            self.log.clear(); self.log.update({**original, **mutation})
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): self.verify()
        self.log.clear(); self.log.update(original)
        self.decimals = 18
        with self.assertRaises(ValueError): self.verify()

    def test_finality_wrong_chain_and_duplicate_logs_fail(self):
        self.final = {'number': '0x63'}
        with self.assertRaises(ValueError): self.verify()
        self.final = {'number': '0x66'}; self.chain = '0x1'
        with self.assertRaises(ValueError): self.verify()
        self.chain = '0x38'; self.receipt['logs'].append(self.log)
        with self.assertRaises(ValueError): self.verify()

    def test_no_native_bnb_fallback(self):
        self.receipt['logs'] = []
        with self.assertRaises(ValueError): self.verify()


class PaymentServiceTests(unittest.TestCase):
    def setUp(self):
        self.q = quote()
        self.request = {'quote': self.q.as_dict(), 'provider': 'cryptopay', 'receiver': '7',
                        'invoice_id': 8, 'invoice_state': 'attached'}
        self.client = Mock()
        self.client.rpc.side_effect = lambda name, args: self.request if name == 'payment_request' else {'state': 'claimed'}
        self.service = PaymentService(self.client, admin_ids=[10])

    def test_issued_invoice_confirms_only_stored_customer_amount(self):
        self.service.confirm_invoice(QID, 31, crypto())
        name, args = self.client.rpc.call_args.args
        self.assertEqual(name, 'confirm_payment_request')
        self.assertEqual(args['p_binding'], {'kind': 'invoice'})
        self.assertEqual(args['p_evidence']['reference'], '7:8')

    def test_wrong_customer_wrong_application_or_unissued_invoice_rejected(self):
        for user, receiver, invoice_id in [(32, '7', 8), (31, '9', 8), (31, '7', None)]:
            self.request.update(receiver=receiver, invoice_id=invoice_id)
            with self.subTest(user=user, receiver=receiver), self.assertRaises(ValueError):
                self.service.confirm_invoice(QID, user, crypto())

    def test_existing_invoice_does_not_create_another(self):
        adapter = crypto()
        result = self.service.create_invoice(QID, 31, adapter, now=NOW)
        self.assertEqual(result['invoice_id'], 8)
        adapter.client._call.assert_not_called()

    def test_invoice_start_claim_precedes_exact_provider_request(self):
        self.request.update(invoice_id=None, invoice_state='none')
        adapter = crypto()
        adapter.client._call.return_value = invoice()
        self.service.create_invoice(QID, 31, adapter, now=NOW)
        payload = adapter.client._call.call_args.args[1]
        self.assertEqual(payload['amount'], self.q.asset_amount)
        self.assertEqual(payload['payload'], self.q.payload)
        self.assertEqual(payload['expires_in'], 900)
        names = [call.args[0] for call in self.client.rpc.call_args_list]
        self.assertLess(names.index('begin_payment_invoice'), names.index('attach_payment_invoice'))

    def test_ambiguous_invoice_attempt_never_retries_creation(self):
        self.request['invoice_id'] = None
        self.client.rpc.side_effect = lambda name, args: self.request if name == 'payment_request' else {'state': 'started'}
        adapter = crypto()
        with self.assertRaises(ValueError):
            self.service.create_invoice(QID, 31, adapter, now=NOW)
        adapter.client._call.assert_not_called()

    def test_raw_transfer_needs_authorized_admin_assignment(self):
        adapter = Mock()
        with self.assertRaises(ValueError):
            self.service.assign_transfer(QID, 31, adapter, 'TX1', actor_id=31, approval_reference='update:4')
        adapter.verify.assert_not_called()

    def test_admin_assignment_contains_audit_and_exact_evidence(self):
        self.request.update(provider='binance_pay', receiver='uid:41', invoice_id=None)
        adapter = Mock()
        adapter.verify.return_value = TransferEvidence('binance_pay', 'TX1', 'USDT', self.q.asset_amount,
            '2026-10-14T01:01:00Z', 'uid:41', json.dumps({'transaction_id': 'TX1'}))
        self.service.assign_transfer(QID, 31, adapter, 'TX1', actor_id=10, approval_reference='update:4')
        adapter.verify.assert_called_once_with('TX1', asset='USDT', minimum=self.q.asset_amount,
                                               not_before=self.q.observed_at)
        self.assertEqual(self.client.rpc.call_args.args[1]['p_binding']['actor_id'], 10)

    def test_overpayment_and_wrong_receiver_require_reconciliation(self):
        self.request.update(provider='binance_pay', receiver='uid:41', invoice_id=None)
        for amount, receiver in [('1', 'uid:41'), (self.q.asset_amount, 'uid:42')]:
            adapter = Mock()
            adapter.verify.return_value = TransferEvidence('binance_pay', 'TX1', 'USDT', amount,
                '2026-10-14T01:01:00Z', receiver, '{}')
            with self.subTest(amount=amount, receiver=receiver), self.assertRaises(ValueError):
                self.service.assign_transfer(QID, 31, adapter, 'TX1', actor_id=10, approval_reference='update:4')


if __name__ == '__main__':
    unittest.main()
