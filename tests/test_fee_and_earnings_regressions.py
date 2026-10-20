"""Observed-rate, fee-review and earnings navigation checks without live I/O."""
import io
import threading
import time
from pathlib import Path
from unittest.mock import Mock,patch
import unittest
from telegram_bot import crypto_pay,views
from telegram_bot.sheet_workflow import SheetWorkflowMixin
from telegram_bot.ui import TelegramUiMixin
from telegram_bot.commerce.partner_finance import PartnerFinanceMixin


class FeeRegressionTests(unittest.TestCase):
    def test_decimal_exchange_rate_from_real_json_is_accepted(self):
        with patch.object(crypto_pay,'_LAST_RATE',None),patch.object(crypto_pay,'_LAST_RATE_TIME',0), \
             patch('urllib.request.urlopen',return_value=io.BytesIO(b'{"rates":{"EGP":48.6391}}')) as network:
            self.assertEqual(crypto_pay.get_live_usd_egp_rate(),48.6391)
        self.assertEqual(network.call_count,1)

    def test_invalid_rates_do_not_become_payment_quotes(self):
        for value in ('true','-1','0','"48.5"'):
            with self.subTest(value=value),patch.object(crypto_pay,'_LAST_RATE',None),patch.object(crypto_pay,'_LAST_RATE_TIME',0), \
                 patch('urllib.request.urlopen',side_effect=lambda *a,**k:io.BytesIO(('{"rates":{"EGP":'+value+'}}').encode())):
                with self.assertRaises(crypto_pay.ExchangeRateUnavailable):crypto_pay.get_live_usd_egp_rate()

    def test_opening_topup_menu_does_not_need_a_rate(self):
        bot=TelegramUiMixin();bot._show_panel=Mock()
        manager=Mock();manager.get_wallet_balance.return_value=0;manager.get_user_account_id.return_value=31
        with patch('telegram_bot.payment_manager.PAYMENT_MGR',manager), \
             patch('telegram_bot.crypto_pay.get_live_usd_egp_rate',side_effect=AssertionError('No quote requested')):
            bot._show_wallet_topup_menu(31,4)
        self.assertIn('اختر وسيلة الدفع',bot._show_panel.call_args.args[1].text)
        self.assertNotIn('50.00',bot._show_panel.call_args.args[1].text)

    def test_missing_rate_keeps_review_and_does_not_issue_invoice(self):
        bot=SheetWorkflowMixin();bot._show_panel=Mock();bot._persist_sheet_drafts=Mock();bot._ui_lock=threading.RLock()
        bot.authorized_id=9;bot.authorized_ids={9};bot.project_root=None
        draft={'path':Path('fixture.xlsx'),'chat':31,'expires':time.monotonic()+100,'version':(1,2),
            'validation':{'ok':True,'rows':2840},'admin':False,'mapping':{}}
        bot._sheet_drafts={'abcdef012345':draft}
        manager=Mock();manager.get_config.return_value={'sheet_price_egp':30}
        with patch('telegram_bot.sheet_workflow.file_version',return_value=(1,2)), \
             patch('telegram_bot.payment_manager.PAYMENT_MGR',manager), \
             patch('telegram_bot.crypto_pay.calculate_egp_to_usdt',side_effect=crypto_pay.ExchangeRateUnavailable('offline')), \
             patch.object(crypto_pay.CRYPTO_PAY,'create_invoice') as issue:
            bot._sheet_callback('map:abcdef012345:pay',31,4)
        text=bot._show_panel.call_args.args[1].text
        self.assertIn('سعر الصرف غير متاح',text);self.assertNotIn('الملف أو حالة الشيت تغيرت',text)
        self.assertIs(bot._sheet_drafts['abcdef012345'],draft)
        issue.assert_not_called();manager.debit_wallet.assert_not_called()

    def test_missing_rate_does_not_display_transfer_instructions(self):
        bot=TelegramUiMixin();bot._show_panel=Mock()
        with patch('telegram_bot.payment_manager.PAYMENT_MGR'), \
             patch('telegram_bot.crypto_pay.get_live_usd_egp_rate',side_effect=crypto_pay.ExchangeRateUnavailable('offline')):
            bot._show_wallet_topup_detail('binance',31,4)
        self.assertIn('سعر الصرف غير متاح',bot._show_panel.call_args.args[1].text)
        self.assertNotIn('Pay ID',bot._show_panel.call_args.args[1].text)

    def test_fee_button_accepts_decimal_rate_and_reaches_mock_invoice(self):
        bot=SheetWorkflowMixin();bot._show_panel=Mock();bot._persist_sheet_drafts=Mock();bot._ui_lock=threading.RLock()
        bot.authorized_id=9;bot.authorized_ids={9};bot.project_root=None
        bot._sheet_drafts={'abcdef012345':{'path':Path('fixture.xlsx'),'chat':31,'expires':time.monotonic()+100,
            'version':(1,2),'validation':{'ok':True,'rows':2840},'admin':False,'mapping':{},'sheet_name':'XlsxTable'}}
        manager=Mock();manager.get_config.return_value={'sheet_price_egp':30}
        manager.generate_operation_id.return_value='fixture-operation'
        manager.get_wallet_balance.return_value=0;manager.get_user_account_id.return_value=31
        invoice={'invoice_id':7,'pay_url':'https://example.invalid/fixture'}
        with patch('telegram_bot.sheet_workflow.file_version',return_value=(1,2)), \
             patch('telegram_bot.payment_manager.PAYMENT_MGR',manager), \
             patch.object(crypto_pay,'_LAST_RATE',None),patch.object(crypto_pay,'_LAST_RATE_TIME',0), \
             patch('urllib.request.urlopen',return_value=io.BytesIO(b'{"rates":{"EGP":48.6391}}')), \
             patch.object(crypto_pay.CRYPTO_PAY,'create_invoice',return_value=invoice) as issue:
            bot._sheet_callback('map:abcdef012345:pay',31,4)
        issue.assert_called_once()
        self.assertEqual(bot._sheet_drafts['abcdef012345']['invoice_id'],7)
        self.assertNotIn('تعذر إكمال المراجعة',bot._show_panel.call_args.args[1].text)
        manager.debit_wallet.assert_not_called()


class EarningsRegressionTests(unittest.TestCase):
    def setUp(self):
        self.bot=PartnerFinanceMixin();self.bot.current_authorized_ids={9};self.bot._commerce=Mock()
        self.bot._show_panel=Mock();self.bot._commerce_notice=Mock();self.bot._ui_lock=threading.RLock()
        self.data={'gross_service_minor':10000,'partner_commission_minor':3000,'platform_revenue_minor':7000,
            'today_platform_revenue_minor':7000,'awaiting_settlement_orders':1,'partners_total':1,
            'partners':[{'telegram_user_id':31,'referral_code':'<PARTNER>','earned_minor':3000,
                         'available_minor':2500,'reserved_minor':500,'withdrawn_minor':0,'pending_minor':0}]}
        self.bot._commerce.return_value.rpc.return_value=self.data

    def test_admin_sees_platform_share_and_each_partners_balances(self):
        self.bot._admin_earnings(9)
        text=self.bot._show_panel.call_args.args[1].text
        for value in ('حصتك بعد العمولات: 70.00','عمولات الشركاء: 30.00','المتاح: 25.00','المحجوز: 5.00','&lt;PARTNER&gt;'):
            self.assertIn(value,text)
        self.bot._commerce.return_value.rpc.assert_called_once_with('admin_earnings_summary',
            {'p_actor_telegram_id':9,'p_offset':0,'p_limit':8})

    def test_customer_cannot_request_admin_earnings_even_with_forged_callback(self):
        self.bot._partner_finance_callback('pf:admin:0',31,4,{})
        self.bot._commerce.assert_not_called();self.bot._show_panel.assert_not_called()
        self.assertEqual(self.bot._commerce_notice.call_args.args[1],'غير مصرح')

    def test_admin_home_has_earnings_and_customer_filter_removes_admin_link(self):
        panel=views.home({},[],is_admin=True,mode='admin')
        self.assertIn('pf:admin:0',str(panel.rows))
        filtered=views.customer_rows([[views.button('admin','pf:admin:0'),views.button('own','pf:home:0')]])
        self.assertNotIn('pf:admin:',str(filtered));self.assertIn('pf:home:',str(filtered))

    def test_admin_earnings_command_opens_read_only_summary(self):
        self.bot._commerce_command('/earnings',9,True)
        self.assertEqual(self.bot._commerce.return_value.rpc.call_args.args[0],'admin_earnings_summary')


if __name__=='__main__':unittest.main()
