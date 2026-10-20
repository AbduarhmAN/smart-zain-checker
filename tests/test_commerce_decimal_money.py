import io
from decimal import Decimal
import unittest
from unittest.mock import Mock
from domain import decimal_json
from domain.decimal_money import egp_from_minor
from telegram_bot.commerce.client import CommerceClient
from telegram_bot.commerce.payment_quotes import freeze_quote,PaymentQuote
from telegram_bot.commerce.finance import positive_money,money_minor_input
from telegram_bot.commerce.topup_store import TopupStore
from telegram_bot.commerce.outbox import Outbox
from pathlib import Path
import tempfile

class ExactMoneyTests(unittest.TestCase):
    def test_display_preserves_smallest_requested_fraction_and_every_digit(self):
        self.assertEqual(egp_from_minor(Decimal('0.001')),'0.00001')
        self.assertEqual(egp_from_minor(Decimal('0.1')),'0.001')
        self.assertEqual(egp_from_minor(Decimal('0.2')),'0.002')
        self.assertEqual(egp_from_minor(Decimal('0.0000000000001')),'0.000000000000001')
        self.assertEqual(egp_from_minor(100),'1.00')
        self.assertEqual(egp_from_minor(Decimal('123456789012345678.12345678')),'1234567890123456.7812345678')
    def test_exact_percent_split_is_conserved_without_rounding(self):
        gross=Decimal('1');partner=gross*Decimal('0.3');discount=gross*Decimal('0.2')
        self.assertEqual((gross-discount,partner-discount,gross-partner),(Decimal('0.8'),Decimal('0.1'),Decimal('0.7')))
        self.assertEqual((partner-discount)+(gross-partner),gross-discount)
    def test_json_decimal_is_a_number_and_durable_minor_roundtrip_is_exact(self):
        data={'amount_minor':Decimal('123456789012345678.12345678'),'discount_minor':Decimal('0.0001')}
        encoded=decimal_json.dumps(data)
        self.assertIn('123456789012345678.12345678',encoded)
        self.assertNotIn('"0.0001"',encoded)
        self.assertEqual(decimal_json.loads(encoded),data)
    def test_rpc_never_parses_money_through_float(self):
        response=io.BytesIO(b'{"available_minor":0.00000000000000123456789}')
        opener=Mock(return_value=response)
        client=CommerceClient(key_provider=lambda:'synthetic-service-key',opener=opener)
        result=client.rpc('wallet_state',{'p_telegram_user_id':31})
        self.assertEqual(result['available_minor'],Decimal('0.00000000000000123456789'))
    def test_micro_withdrawal_input_is_not_forced_to_piastre(self):
        self.assertEqual(money_minor_input('0.00001'),Decimal('0.001'))
        self.assertEqual(positive_money(Decimal('0.001')),Decimal('0.001'))
        for value in [Decimal('NaN'),Decimal('Infinity'),Decimal('-0.01'),True,0.001]:
            with self.subTest(value=value),self.assertRaises(ValueError):positive_money(value)
    def test_fractional_egp_quote_and_outbox_preserve_exact_egp(self):
        quote=freeze_quote(31,Decimal('0.8'),'USDT','50','synthetic-fx')
        self.assertEqual(PaymentQuote(**decimal_json.loads(decimal_json.dumps(quote.as_dict()))).amount_minor,Decimal('0.8'))
        folder=Path(tempfile.mkdtemp(prefix='exact_money_retained_'))
        outbox=Outbox(folder/'outbox.sqlite')
        outbox.enqueue('micro-command','settle_order',{'p_amount_minor':Decimal('0.001')})
        claimed=outbox.lease()
        self.assertEqual(claimed['arguments']['p_amount_minor'],Decimal('0.001'))

if __name__=='__main__':unittest.main()
