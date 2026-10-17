"""Offline Telegram cash-code reviews, retained fixtures, no workbook cleanup."""
import os
import json
from pathlib import Path
import tempfile
import threading
import time
from decimal import Decimal
import unittest
from unittest.mock import Mock,patch
from telegram_bot.bot import TelegramBotRunner
from telegram_bot.ui import TelegramUiMixin
from telegram_bot import views
from telegram_bot.commerce.client import CommerceUnavailable


class CashCodeTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(tempfile.mkdtemp(prefix='cash-code-retained-'))
        self.bot=TelegramBotRunner.__new__(TelegramBotRunner)
        self.bot.project_root=self.root;self.bot._ui_lock=threading.RLock()
        self.bot._waiting_user_inputs={};self.bot._sheet_drafts={};self.bot._confirmations={}
        self.bot.authorized_id=9;self.bot.authorized_ids={9};self.bot._authorized_override=True
        self.bot._show_panel=Mock();self.bot._api_call=Mock();self.bot._commerce_client=Mock()
        self.client=self.bot._commerce_client;self.client.rpc.side_effect=self.rpc
        self.calls=[]
        self.env=patch.dict(os.environ,{'ZAIN_COMMERCE_CHECKOUT':'1'})
        self.env.start();self.addCleanup(self.env.stop)

    def rpc(self,name,args):
        self.calls.append((name,args))
        if name=='list_cash_credit_codes':return []
        if name=='issue_cash_credit_code':return {'code':args['p_code'],'replayed':False}
        if name=='apply_customer_code':return {'kind':'cash_credit','state':'redeemed','credit_minor':3001}
        if name=='register_telegram_user':return {}
        raise AssertionError(name)

    def callback(self,command,user=9):
        self.bot._handle_callback({'id':'fixture-callback','from':{'id':user},'message':{
            'chat':{'id':user},'message_id':7},'data':'zc:'+command})

    def text(self,text,user=9):
        waiting=self.bot._waiting_user_inputs.pop(user)
        waiting['_telegram_input_message_id']=11
        self.bot._commerce_text_input(user,text,waiting)

    def prepare(self,*,owner='0',count='2',credit='30.01'):
        self.callback('cg:new:0');token=next(reversed(self.bot._cash_code_reviews))
        for value in ['PAID_CASH',credit,count,'CASH-RECEIPT-001',owner,'15']:self.text(value)
        return token

    def test_generated_code_and_calculated_funding_do_not_issue_before_review(self):
        self.callback('cg:new:0');token=next(iter(self.bot._cash_code_reviews))
        self.callback('cg:generate:'+token)
        code=self.bot._cash_code_reviews[token]['values']['code']
        self.assertRegex(code,r'^CASH_[0-9A-F]{24}$')
        self.text('30.01');self.text('2')
        panel=self.bot._show_panel.call_args.args[1]
        self.assertIn('60.02',panel.text);self.assertIn('ليس هدية',panel.text)
        self.assertEqual(self.bot._cash_code_reviews[token]['values']['funded'],6002)
        self.assertEqual(self.bot._waiting_user_inputs[9]['field'],'receipt')
        self.assertNotIn('cg:funded:',str(panel.rows))
        for value in ['CASH-AUTO-001','0','15']:self.text(value)
        self.assertEqual(self.calls,[])
        self.callback('cg:save:'+token)
        self.assertEqual(self.calls[0][1]['p_funded_minor'],6002)

    def test_generation_cannot_be_used_by_customer_or_at_other_field(self):
        self.callback('cg:new:0');token=next(iter(self.bot._cash_code_reviews))
        self.callback('cg:generate:'+token,user=31)
        self.assertEqual(self.bot._cash_code_reviews[token]['step'],0)
        self.callback('cg:generate:'+token)
        before=dict(self.bot._cash_code_reviews[token]['values'])
        self.callback('cg:generate:'+token)
        self.assertEqual(self.bot._cash_code_reviews[token]['values'],before)
        self.assertEqual(self.calls,[])

    def test_cash_rejection_explains_conflict_and_keeps_same_receipt(self):
        token=self.prepare()
        self.client.rpc.side_effect=CommerceUnavailable('Commerce RPC HTTP 400',reason='cash_conflict',database_code='22023',http_status=400)
        self.callback('cg:save:'+token)
        panel=self.bot._show_panel.call_args.args[1]
        self.assertIn('الإيصال مستخدم',panel.text)
        self.assertEqual(self.bot._cash_code_reviews[token]['arguments']['p_receipt_reference'],'CASH-RECEIPT-001')
        self.assertNotIn('تعذر التأكد من حالة المحفظة',panel.text)

    def test_permission_denial_preserves_review_and_does_not_claim_connection_loss(self):
        token=self.prepare()
        self.client.rpc.side_effect=CommerceUnavailable('Commerce RPC HTTP 403',
            reason='permission_denied',database_code='42501',http_status=403)
        self.callback('cg:save:'+token)
        panel=self.bot._show_panel.call_args.args[1]
        self.assertIn('صلاحيات الخدمة',panel.text)
        self.assertNotIn('مشكلة الاتصال',panel.text)
        self.assertEqual(self.bot._cash_code_reviews[token]['arguments']['p_receipt_reference'],'CASH-RECEIPT-001')

    def test_review_before_issue_and_matching_admin_only(self):
        token=self.prepare();self.assertEqual(self.calls,[])
        panel=self.bot._show_panel.call_args.args[1]
        self.assertIn('60.02',panel.text);self.assertIn('30.01',panel.text)
        self.callback('cg:save:'+token,user=31);self.assertEqual(self.calls,[])
        self.callback('cg:save:'+token)
        self.assertEqual(self.calls[0][0],'issue_cash_credit_code')
        self.assertEqual(self.calls[0][1]['p_credit_minor'],3001)
        self.assertEqual(self.calls[0][1]['p_funded_minor'],6002)

    def test_callback_replay_preserves_expiry_and_approval_receipt(self):
        token=self.prepare();self.callback('cg:save:'+token);self.callback('cg:save:'+token)
        self.assertEqual(self.calls[0],self.calls[1])

    def test_total_is_derived_and_no_second_amount_is_requested(self):
        self.callback('cg:new:0')
        for value in ['PAID_CASH','10','10']:self.text(value)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.bot._waiting_user_inputs[9]['field'],'receipt')
        for value in ['CASH-ONCE-001','0','0']:self.text(value)
        token=next(iter(self.bot._cash_code_reviews))
        self.assertEqual(self.calls,[])
        self.assertIn('100.00',self.bot._show_panel.call_args.args[1].text)
        self.callback('cg:save:'+token)
        self.assertEqual(self.calls[0][1]['p_funded_minor'],10000)

    def test_expired_review_and_cancelled_review_cannot_issue(self):
        token=self.prepare();self.bot._cash_code_reviews[token]['expires']=0
        self.callback('cg:save:'+token);self.assertEqual(self.calls,[])
        token=self.prepare();self.bot._commerce_cancel_pending(9)
        self.callback('cg:save:'+token);self.assertEqual(self.calls,[])

    def test_bound_code_is_explicit_and_only_one_redemption(self):
        token=self.prepare(owner='31',count='1')
        self.callback('cg:save:'+token)
        self.assertEqual(self.calls[0][1]['p_bound_telegram_id'],31)

    def test_large_duration_is_rejected_before_advancing_field(self):
        self.callback('cg:new:0')
        for value in ['PAID_CASH','30','1','CASH-001','0',str(9223372036854775807)]:self.text(value)
        self.assertEqual(self.bot._waiting_user_inputs[9]['field'],'days')
        self.text('0');self.assertEqual(self.calls,[])
        token=next(iter(self.bot._cash_code_reviews));self.callback('cg:save:'+token)
        self.assertIsNone(self.calls[0][1]['p_expires_at'])

    def test_tiny_credit_and_funding_are_exact(self):
        token=self.prepare(credit='0.00001',count='3')
        self.callback('cg:save:'+token)
        self.assertEqual(self.calls[0][1]['p_credit_minor'],Decimal('0.001'))
        self.assertEqual(self.calls[0][1]['p_funded_minor'],Decimal('0.003'))

    def test_whole_decimal_count_is_accepted_but_fractional_count_is_not(self):
        self.callback('cg:new:0')
        for value in ['PAID_CASH','10','2.5']:self.text(value)
        self.assertEqual(self.bot._waiting_user_inputs[9]['field'],'count')
        self.assertNotIn('count',next(iter(self.bot._cash_code_reviews.values()))['values'])
        self.text('١٠.٠٠')
        self.assertEqual(self.bot._waiting_user_inputs[9]['field'],'receipt')
        self.assertEqual(next(iter(self.bot._cash_code_reviews.values()))['values']['funded'],10000)

    def test_obsolete_funding_confirmation_does_not_advance_or_issue(self):
        self.callback('cg:new:0');token=next(iter(self.bot._cash_code_reviews))
        self.callback('cg:funded:'+token)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.bot._cash_code_reviews[token]['step'],0)

    def test_excessive_derived_funding_is_rejected_without_storing_count(self):
        self.callback('cg:new:0')
        for value in ['PAID_CASH','92233720368547758.07','2']:self.text(value)
        self.assertEqual(self.bot._waiting_user_inputs[9]['field'],'count')
        self.assertNotIn('count',next(iter(self.bot._cash_code_reviews.values()))['values'])

    def test_customer_code_receipt_is_tied_to_actual_message(self):
        self.callback('cc:redeem:0',user=31);self.text('paid_cash',user=31)
        name,args=self.calls[0]
        self.assertEqual(name,'apply_customer_code');self.assertEqual(args['p_telegram_user_id'],31)
        self.assertEqual(args['p_reference'],'telegram-code:31:11');self.assertEqual(args['p_code'],'PAID_CASH')
        self.assertTrue(self.bot._topups().is_owner(31))

    def test_customer_admin_buttons_are_blocked_and_private_identity_required(self):
        self.callback('cg:list:0',user=31);self.assertEqual(self.calls,[])
        self.bot._handle_callback({'id':'x','from':{'id':31},'message':{'chat':{'id':32},'message_id':7},'data':'zc:cc:redeem:0'})
        self.assertNotIn(32,self.bot._waiting_user_inputs)

    def test_legacy_code_button_works_without_global_checkout_flag(self):
        with patch.dict(os.environ,{'ZAIN_COMMERCE_CHECKOUT':'','ZAIN_COMMERCE_PILOT_IDS':''}):
            self.callback('wallet:redeem',user=31)
            self.assertEqual(self.bot._waiting_user_inputs[31]['action'],'commerce_customer_code')
            self.text('PAID_CASH',user=31)
            self.assertEqual(self.calls[0][0],'apply_customer_code')
            self.assertTrue(self.bot._commerce_checkout_enabled(31))

    def test_inflight_legacy_prompt_uses_owned_new_code_rpc(self):
        with patch('telegram_bot.payment_manager.PAYMENT_MGR.redeem_code') as old:
            self.bot._handle_user_text_input(31,'fixture','PAID_CASH',
                {'action':'redeem','message_id':7,'_telegram_input_message_id':11})
            old.assert_not_called()
        self.assertEqual(self.calls[0][1]['p_reference'],'telegram-code:31:11')

    def test_invalid_code_does_not_switch_customer_wallet(self):
        self.client.rpc.side_effect=lambda name,args:{'kind':'unknown','state':'invalid_code'}
        with patch.dict(os.environ,{'ZAIN_COMMERCE_CHECKOUT':'','ZAIN_COMMERCE_PILOT_IDS':''}):
            self.callback('wallet:redeem',user=31);self.text('INVALID_CODE',user=31)
            self.assertFalse(self.bot._topups().is_owner(31))
            self.assertFalse(self.bot._commerce_checkout_enabled(31))

    def test_legacy_balance_remains_visible_and_unchanged_after_code(self):
        path=self.root/'user_wallets.json';path.write_text(json.dumps({'31':{'balance':385.24}}),encoding='utf-8')
        original=path.read_bytes()
        self.callback('wallet:redeem',user=31)
        self.assertIn('رصيدك السابق محفوظ',self.bot._show_panel.call_args.args[1].text)
        self.text('PAID_CASH',user=31)
        self.client.rpc.side_effect=lambda name,args:({} if name=='register_telegram_user' else
            {'customer':{'available_minor':5000,'reserved_minor':0}})
        self.bot._show_wallet_panel(31,7)
        text=self.bot._show_panel.call_args.args[1].text
        self.assertIn('50.00',text);self.assertIn('385.24',text)
        self.assertEqual(path.read_bytes(),original)

    def test_enrolled_customer_cannot_spend_or_credit_legacy_balance(self):
        from telegram_bot.payment_manager import PaymentManager,PaymentPersistenceError
        manager=PaymentManager(self.root)
        manager.credit_wallet(31,100,'fixture')
        manager.credit_wallet(32,100,'fixture')
        self.bot._topups().mark_owner(31)
        with self.assertRaises(PaymentPersistenceError):manager.debit_wallet(31,1)
        with self.assertRaises(PaymentPersistenceError):manager.credit_wallet(31,1)
        self.assertEqual(manager.get_wallet_balance(31),100)
        self.assertTrue(manager.debit_wallet(32,1)[0])

    def test_real_panel_keeps_customer_code_button_and_hides_issuance(self):
        panel=views.Panel('fixture',[[views.button('إدخال كود','cc:redeem:0')],[views.button('إصدار','cg:new:0')]])
        TelegramUiMixin._show_panel(self.bot,31,panel)
        rows=self.bot._api_call.call_args.args[1]['reply_markup']['inline_keyboard']
        self.assertEqual([item['callback_data'] for row in rows for item in row],['zc:cc:redeem:0'])


if __name__=='__main__':unittest.main()
