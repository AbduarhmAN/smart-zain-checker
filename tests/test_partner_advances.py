"""Explicit advance review and truthful signed partner balances; no network."""
import copy
from decimal import Decimal
import threading
import time
import unittest
from unittest.mock import Mock
from telegram_bot.commerce.partner_finance import PartnerFinanceMixin
from telegram_bot.commerce.withdrawal_receipts import receipt_panel
from telegram_bot.commerce.payout_notifications import payout_notice_panel
from telegram_bot.commerce.partner_balances import partner_balance_text
from telegram_bot.commerce.client import CommerceUnavailable
from telegram_bot.commerce.wallets import WalletPaymentsMixin

WD='35d3ce42-9634-48d2-810e-cd4e3da0de9b'
RECEIPT='5181ab2f-d35f-4dec-bdaa-2ff838760549'


class PartnerAdvanceTests(unittest.TestCase):
    def setUp(self):
        self.bot=PartnerFinanceMixin();self.bot.current_authorized_ids={9};self.bot._ui_lock=threading.RLock()
        self.bot._waiting_user_inputs={};self.bot._show_panel=Mock();self.bot._commerce_notice=Mock()
        self.bot._api_call=Mock();self.client=Mock();self.bot._commerce=Mock(return_value=self.client)
        self.profile={'is_partner':True,'available_minor':700,'withdrawable_minor':700,'account_balance_minor':700,
            'advance_outstanding_minor':0,'reserved_minor':0,'withdrawn_minor':0,'today_earned_minor':700,
            'pending_commission_minor':0,'clients':1,'paid_checks':1,'collected_minor':1000,'awaiting_settlement_orders':0}
        self.record={'id':WD,'telegram_user_id':31,'state':'paid','amount_minor':1000,'destination':'fixture bank',
            'payout_reference':'bank:TX10','receipt':{'id':RECEIPT,'state':'requested','recipient_telegram_id':31,'paid_actor_id':9},
            'advance':{'amount_minor':300,'earned_paid_minor':700,'available_before_minor':700,
                'available_after_minor':-300,'account_after_minor':-300,'reason':'دفعة مقدمة على أرباح الشريك القادمة'}}
        self.writes=[];self.client.rpc.side_effect=self.rpc

    def rpc(self,name,args):
        if name=='partner_finance':return copy.deepcopy(self.profile)
        if name in {'record_partner_advance_payout','record_partner_payout'}:
            self.writes.append((name,copy.deepcopy(args)));return copy.deepcopy(self.record)
        raise AssertionError(name)

    def issue(self,amount='10'):
        self.bot._withdrawal_admin_callback('wa:paypartner:31',9,4,{'id':'native-start'})
        for number,text in enumerate((amount,'fixture bank','bank TX10'),10):
            waiting=self.bot._waiting_user_inputs.pop(9);waiting['_telegram_input_message_id']=number
            self.bot._commerce_text_input(9,text,waiting)
        return next(iter(self.bot._admin_partner_payout_reviews))

    def confirm(self,token,actor=9,callback=None):
        self.bot._withdrawal_admin_callback('wa:payconfirm:'+token,actor,4,{'id':'native-confirm'} if callback is None else callback)

    def test_seven_earned_ten_paid_review_shows_three_advance_and_negative_balance(self):
        token=self.issue()
        self.assertEqual(self.writes,[])
        text=self.bot._show_panel.call_args.args[1].text
        for value in ('10.00 جنيه','7.00 جنيه','3.00 جنيه','-3.00 جنيه','دفعة مقدمة','السبب:'):
            self.assertIn(value,text)
        self.assertIn('تسجيل الزيادة دفعة مقدمة',str(self.bot._show_panel.call_args.args[1].rows))
        self.confirm(token)
        name,args=self.writes[0]
        self.assertEqual(name,'record_partner_advance_payout')
        self.assertEqual(args['p_expected_available_minor'],700);self.assertEqual(args['p_amount_minor'],1000)
        self.assertEqual(args['p_reason'],'دفعة مقدمة على أرباح الشريك القادمة')
        self.bot._api_call.assert_not_called()

    def test_stable_confirmation_retry_never_changes_reviewed_money_or_reason(self):
        token=self.issue();self.confirm(token);self.confirm(token)
        self.assertEqual(self.writes[0],self.writes[1])

    def test_existing_negative_balance_adds_only_new_payout_to_advance(self):
        self.profile.update(available_minor=-300,withdrawable_minor=0,account_balance_minor=-300,advance_outstanding_minor=300)
        token=self.issue('1')
        review=self.bot._admin_partner_payout_reviews[token]
        self.assertEqual(review['advance_minor'],100);self.assertEqual(review['available_after'],-400)
        self.assertIn('-4.00',self.bot._show_panel.call_args.args[1].text)

    def test_foreign_or_unauthenticated_click_cannot_record_advance(self):
        token=self.issue()
        self.confirm(token,actor=31);self.confirm(token,callback={})
        self.assertEqual(self.writes,[])

    def test_changed_balance_failure_does_not_claim_new_payout(self):
        token=self.issue()
        self.bot._show_panel.reset_mock()
        self.client.rpc.side_effect=CommerceUnavailable('Rejected changed review',reason='partner_balance_changed')
        self.confirm(token)
        self.bot._show_panel.assert_not_called();self.assertEqual(self.writes,[])

    def test_unknown_write_response_keeps_same_review_for_recovery(self):
        token=self.issue()
        self.client.rpc.side_effect=CommerceUnavailable('Lost acknowledgement')
        self.confirm(token)
        self.assertIn(token,self.bot._admin_partner_payout_reviews)

    def test_native_receipt_keeps_the_original_advance_snapshot_on_later_replay(self):
        self.profile.update(available_minor=-300,withdrawable_minor=0,account_balance_minor=-300,advance_outstanding_minor=300)
        token=self.issue('10')
        self.record['replayed']=True
        self.confirm(token)
        panel=self.bot._show_panel.call_args.args[1]
        self.assertIn('مسجّل سابقًا',panel.text);self.assertIn('-3.00',panel.text)

    def test_partner_and_channel_receipts_show_reason_and_signed_balance(self):
        partner=receipt_panel(self.record)
        channel=payout_notice_panel({'payload':self.record,'kind':'paid','recipient_kind':'channel'})
        for panel in (partner,channel):
            self.assertIn('الزيادة المسجلة دفعة مقدمة: 3.00',panel.text)
            self.assertIn('رصيد الحساب بعد هذا الصرف: <b>-3.00',panel.text)
            self.assertIn('السبب: دفعة مقدمة',panel.text)
        self.assertNotIn('fixture bank',channel.text)

    def test_negative_account_has_zero_withdrawable_and_an_explanation(self):
        self.profile.update(available_minor=-300,withdrawable_minor=0,account_balance_minor=-300,
            advance_outstanding_minor=300,outstanding_advances=[{'reason':'سلفة <سبب>','outstanding_minor':300}])
        self.bot._partner_finance_callback('pf:home:0',31,4,{})
        text=self.bot._show_panel.call_args.args[1].text
        self.assertIn('رصيد الحساب: <b>-3.00',text)
        self.assertIn('المتاح للسحب: <b>0.00',text);self.assertIn('سلفة &lt;سبب&gt;',text)

    def test_account_includes_holds_and_does_not_offer_them_for_spending(self):
        profile={'available_minor':-300,'withdrawable_minor':0,'account_balance_minor':200,'advance_outstanding_minor':300}
        text=partner_balance_text(profile)
        self.assertIn('رصيد الحساب: <b>2.00',text);self.assertIn('المتاح للسحب: <b>0.00',text)

    def test_wallet_panel_shows_negative_partner_account_alongside_unchanged_customer_funds(self):
        partner={**self.profile,'available_minor':-300,'withdrawable_minor':0,'account_balance_minor':-300,
                 'advance_outstanding_minor':300}
        self.bot._uses_commerce_wallet=Mock(return_value=True)
        self.bot._wallet_state=Mock(return_value={'is_partner':True,'customer':{'available_minor':500,'reserved_minor':200},'partner':partner})
        self.bot._legacy_balance_pending_minor=Mock(return_value=0)
        WalletPaymentsMixin._show_wallet_panel(self.bot,31,4)
        text=self.bot._show_panel.call_args.args[1].text
        self.assertIn('رصيد الحساب: <b>-3.00',text);self.assertIn('المتاح: <b>5.00',text)
        self.assertIn('المحجوز للطلبات: 2.00',text)

    def test_tiny_advance_preserves_subcent_precision(self):
        self.profile.update(available_minor=Decimal('0.0009'),withdrawable_minor=Decimal('0.0009'),account_balance_minor=Decimal('0.0009'))
        token=self.issue('0.00001')
        review=self.bot._admin_partner_payout_reviews[token]
        self.assertEqual(review['advance_minor'],Decimal('0.0001'));self.assertEqual(review['available_after'],Decimal('-0.0001'))


if __name__=='__main__':unittest.main()
