"""Customer file totals retain both buckets and exact signs; no I/O."""
from decimal import Decimal
import unittest
from unittest.mock import Mock
from telegram_bot.order_history import OrderHistoryMixin


class WalletActivityDisplayTests(unittest.TestCase):
    def setUp(self):
        self.bot=OrderHistoryMixin();self.bot._commerce=Mock();self.bot._show_panel=Mock()

    def render(self,items,total=None,offset=0):
        self.bot._commerce.return_value.rpc.return_value={'items':items,'total':len(items) if total is None else total}
        self.bot._history_server_ledger(31,offset,None)
        return self.bot._show_panel.call_args.args[1]

    def item(self,kind,amount,buckets,**kw):
        return {'kind':kind,'delta_minor':amount,'bucket_deltas':buckets,'wallet_kind':'customer',
            'filename':'ملف العملاء.xlsx','created_at':'2026-10-14T23:15:44Z',**kw}

    def test_refund_is_one_positive_total_with_available_and_reserved_visible(self):
        panel=self.render([self.item('refund',2368,{'available':2368,'reserved':-2368})])
        self.assertEqual(panel.text.count('استرداد'),1)
        self.assertIn('استرداد · +23.68 جنيه',panel.text)
        self.assertIn('المتاح: +23.68 جنيه',panel.text);self.assertIn('المحجوز: -23.68 جنيه',panel.text)
        self.assertIn('ملف العملاء.xlsx',panel.text)

    def test_file_charge_is_negative(self):
        panel=self.render([self.item('service',-1001,{'reserved':-1001})])
        self.assertIn('رسوم الملف · -10.01 جنيه',panel.text)

    def test_fractional_money_is_not_rounded_away(self):
        value=Decimal('0.125')
        panel=self.render([self.item('refund',value,{'available':value,'reserved':-value})])
        self.assertIn('+0.00125 جنيه',panel.text);self.assertIn('-0.00125 جنيه',panel.text)

    def test_reservation_still_shows_both_sides_and_is_labeled_temporary(self):
        panel=self.render([self.item('reserve',-3000,{'available':-3000,'reserved':3000})])
        self.assertIn('حجز مؤقت للملف',panel.text)
        self.assertIn('المتاح: -30.00 جنيه',panel.text);self.assertIn('المحجوز: +30.00 جنيه',panel.text)

    def test_pagination_uses_activity_count(self):
        panel=self.render([self.item('refund',1,{'available':1})],total=9)
        self.assertIn('oh:ledger:8',str(panel.rows))
        self.bot._commerce.return_value.rpc.assert_called_once_with('customer_wallet_activity',
            {'p_telegram_user_id':31,'p_offset':0,'p_limit':8})

    def test_empty_history_and_safe_filename_rendering(self):
        self.assertIn('لا توجد حركات محفوظة',self.render([]).text)
        panel=self.render([self.item('refund',1,{'available':1},filename='<private>')])
        self.assertIn('&lt;private&gt;',panel.text);self.assertNotIn('<private>',panel.text)


if __name__=='__main__':unittest.main()
