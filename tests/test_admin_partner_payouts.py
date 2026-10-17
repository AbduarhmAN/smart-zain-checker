"""Admin-originated payout review, exact money and post-commit notification."""
import copy
from decimal import Decimal
import threading
import time
import unittest
from unittest.mock import Mock
from telegram_bot.commerce.partner_finance import PartnerFinanceMixin
from telegram_bot.commerce.client import CommerceUnavailable

WD='f981217e-879f-425d-a844-c3cecc81aee3'
RECEIPT='35e3f826-0012-4530-901a-990f526e5587'


class AdminPartnerPayoutTests(unittest.TestCase):
    def setUp(self):
        self.bot=PartnerFinanceMixin();self.bot.current_authorized_ids={9}
        self.bot._ui_lock=threading.RLock();self.bot._waiting_user_inputs={}
        self.client=Mock();self.bot._commerce=Mock(return_value=self.client)
        self.bot._show_panel=Mock(return_value={'ok':True});self.bot._commerce_notice=Mock()
        self.writes=[]
        self.record={'id':WD,'telegram_user_id':31,'state':'paid','amount_minor':1000,
            'destination':'<bank>','payout_reference':'bank:TX001','receipt':{
                'id':RECEIPT,'state':'requested','recipient_telegram_id':31,'paid_actor_id':9}}
        self.client.rpc.side_effect=self.rpc

    def rpc(self,name,args):
        if name=='partner_finance':return {'is_partner':True,'available_minor':5000}
        if name=='record_partner_payout':
            self.writes.append((name,copy.deepcopy(args)));return copy.deepcopy(self.record)
        raise AssertionError('Unexpected RPC')

    def callback(self,command,user=9,callback=None):
        self.bot._withdrawal_admin_callback(command,user,4,{'id':'native-click'} if callback is None else callback)

    def issue(self,amount='10'):
        self.callback('wa:paypartner:31')
        for message,text in enumerate((amount,'<bank>','bank TX001'),10):
            waiting=self.bot._waiting_user_inputs.pop(9)
            waiting['_telegram_input_message_id']=message
            self.bot._commerce_text_input(9,text,waiting)
        return next(iter(self.bot._admin_partner_payout_reviews))

    def test_admin_begins_without_partner_request_and_review_does_not_write(self):
        token=self.issue()
        self.assertEqual(self.writes,[])
        panel=self.bot._show_panel.call_args.args[1]
        self.assertIn('&lt;bank&gt;',panel.text);self.assertIn('10.00',panel.text)
        self.assertIn('wa:payconfirm:'+token,str(panel.rows))
        self.assertEqual([call.args[0] for call in self.client.rpc.call_args_list],['partner_finance','partner_finance'])

    def test_cash_payout_requires_no_external_receipt_and_only_writes_after_confirmation(self):
        self.callback('wa:paypartner:31')
        waiting=self.bot._waiting_user_inputs.pop(9)
        self.bot._commerce_text_input(9,'0.00001',waiting)
        waiting=self.bot._waiting_user_inputs[9];token=waiting['payout_token']
        self.assertIn('wa:paycash:'+token,str(self.bot._show_panel.call_args.args[1].rows))
        self.callback('wa:paycash:'+token)
        self.assertEqual(self.writes,[])
        review=self.bot._admin_partner_payout_reviews[token]
        self.assertEqual(review['provider'],'cash');self.assertTrue(review['receipt'].startswith('CASH-'))
        self.assertEqual(review['destination'],'نقدًا باليد')
        self.assertIn('لا يلزم رقم عملية خارجي',self.bot._show_panel.call_args.args[1].text)
        self.record['amount_minor']=Decimal('0.001')
        self.callback('wa:payconfirm:'+token);self.callback('wa:payconfirm:'+token)
        self.assertEqual(self.writes[0],self.writes[1])
        self.assertEqual(self.writes[0][1]['p_amount_minor'],Decimal('0.001'))
        self.assertEqual(self.writes[0][1]['p_provider'],'cash')

    def test_bad_receipt_keeps_previous_fields_and_allows_cash_instead(self):
        self.callback('wa:paypartner:31')
        for text in ('10','bank destination'):
            waiting=self.bot._waiting_user_inputs.pop(9);self.bot._commerce_text_input(9,text,waiting)
        waiting=self.bot._waiting_user_inputs.pop(9)
        self.bot._commerce_text_input(9,'cash',waiting)
        self.assertEqual(self.bot._waiting_user_inputs[9],waiting)
        self.assertIn('نقدًا باليد',self.bot._show_panel.call_args.args[1].text)
        self.callback('wa:paycash:'+waiting['payout_token'])
        self.assertEqual(self.writes,[])

    def test_cash_selection_rejects_foreign_stale_or_forged_buttons(self):
        self.callback('wa:paypartner:31')
        waiting=self.bot._waiting_user_inputs.pop(9);self.bot._commerce_text_input(9,'10',waiting)
        token=self.bot._waiting_user_inputs[9]['payout_token']
        self.callback('wa:paycash:'+token,user=31)
        self.callback('wa:paycash:'+token,callback={})
        self.bot._waiting_user_inputs[9]['expires']=time.monotonic()-1
        self.callback('wa:paycash:'+token)
        self.assertFalse(getattr(self.bot,'_admin_partner_payout_reviews',{}))
        self.assertEqual(self.writes,[])

    def test_confirmation_records_stable_key_and_queues_delivery_without_direct_send(self):
        token=self.issue();self.bot._show_panel.reset_mock()
        self.callback('wa:payconfirm:'+token)
        self.assertEqual(len(self.writes),1)
        args=self.writes[0][1]
        self.assertEqual(args['p_actor_telegram_id'],9);self.assertEqual(args['p_partner_telegram_id'],31)
        self.assertEqual(args['p_proof'],'telegram-admin-payout:9:12')
        self.assertEqual(args['p_key'],'admin-payout:'+token)
        delivery=self.bot._show_panel.call_args_list[0]
        self.assertEqual(delivery.args[0],9)
        self.assertIn('wa:notices:'+WD,str(delivery.args[1].rows))
        self.assertIn('حُفظ صرف الأرباح وإشعارات',delivery.args[1].text)
        self.callback('wa:payconfirm:'+token)
        self.assertEqual(self.writes[0],self.writes[1])

    def test_customer_cannot_start_confirm_or_submit_fake_admin_inputs(self):
        self.callback('wa:paypartner:31',user=31)
        self.assertEqual(self.client.rpc.call_count,0)
        self.bot._commerce_text_input(31,'10',{'action':'commerce_admin_payout_amount','partner':31,'expires':time.monotonic()+100})
        self.assertEqual(self.bot._waiting_user_inputs,{})
        token=self.issue();self.callback('wa:payconfirm:'+token,user=31)
        self.assertEqual(self.writes,[])

    def test_missing_callback_foreign_admin_and_expired_draft_cannot_pay(self):
        token=self.issue();self.callback('wa:payconfirm:'+token,callback={})
        self.bot.current_authorized_ids.add(10)
        self.callback('wa:payconfirm:'+token,user=10)
        self.bot._admin_partner_payout_reviews[token]['expires']=time.monotonic()-1
        self.callback('wa:payconfirm:'+token)
        self.assertEqual(self.writes,[])

    def test_exact_sub_piastre_amount_survives_review_and_rpc(self):
        token=self.issue('0.00001');self.record['amount_minor']=Decimal('0.001')
        self.callback('wa:payconfirm:'+token)
        self.assertEqual(self.writes[0][1]['p_amount_minor'],Decimal('0.001'))
        self.assertIn('0.00001 جنيه',self.bot._show_panel.call_args_list[-2].args[1].text)

    def test_unknown_write_ack_never_claims_payment_or_sends_partner_message(self):
        token=self.issue();self.bot._show_panel.reset_mock()
        self.client.rpc.side_effect=CommerceUnavailable('Lost acknowledgment')
        self.callback('wa:payconfirm:'+token)
        self.bot._show_panel.assert_not_called()
        self.assertIn('تعذر',self.bot._commerce_notice.call_args.args[1])

    def test_recording_does_not_attempt_untracked_partner_sending(self):
        token=self.issue();self.bot._show_panel.reset_mock()
        self.bot._api_call=Mock(side_effect=OSError('Synthetic Telegram outage'))
        self.callback('wa:payconfirm:'+token)
        self.assertEqual(len(self.writes),1)
        panel=self.bot._show_panel.call_args.args[1]
        self.assertIn('حُفظ صرف الأرباح',panel.text)
        self.assertIn('تابع وصول الرسائل',panel.text)
        self.assertIn('wa:requestack:'+WD,str(panel.rows))
        self.bot._api_call.assert_not_called()

    def test_insufficient_balance_never_sends_paid_notification(self):
        token=self.issue();self.record={'state':'insufficient_balance','available_minor':1}
        self.bot._show_panel.reset_mock();self.callback('wa:payconfirm:'+token)
        self.bot._show_panel.assert_not_called()
        self.assertIn('غير كافٍ',self.bot._commerce_notice.call_args.args[1])

    def test_returning_to_admin_earnings_cancels_input_and_old_confirmation(self):
        token=self.issue();self.bot._cancel_admin_partner_payout(9)
        self.callback('wa:payconfirm:'+token)
        self.assertEqual(self.writes,[])

    def test_invalid_money_and_non_partner_do_not_make_financial_writes(self):
        for value in ('0','-1','NaN','Infinity','bad'):
            self.callback('wa:paypartner:31')
            waiting=self.bot._waiting_user_inputs.pop(9)
            self.bot._commerce_text_input(9,value,waiting)
        self.assertEqual(self.writes,[])
        self.client.rpc.return_value={'is_partner':False};self.client.rpc.side_effect=None
        self.callback('wa:paypartner:32')
        self.assertEqual(self.bot._waiting_user_inputs,{})

    def test_confirmed_receipt_is_not_resent_as_waiting_or_debited_again(self):
        token=self.issue();self.record['receipt']['state']='confirmed'
        self.bot._show_panel.reset_mock();self.callback('wa:payconfirm:'+token)
        self.assertEqual(self.bot._show_panel.call_count,1)
        self.assertEqual(self.bot._show_panel.call_args.args[0],9)
        self.assertIn('رد الشريك محفوظ',self.bot._show_panel.call_args.args[1].text)

    def test_admin_earnings_exposes_payout_button_for_each_partner_only_to_admin(self):
        self.client.rpc.side_effect=None
        self.client.rpc.return_value={'gross_service_minor':5000,'partner_commission_minor':1500,
            'platform_revenue_minor':3500,'today_platform_revenue_minor':3500,
            'awaiting_settlement_orders':0,'partners_total':1,'partners':[{
                'telegram_user_id':31,'referral_code':'PARTNER','earned_minor':1500,
                'available_minor':1000,'reserved_minor':0,'withdrawn_minor':500,'pending_minor':0}]}
        self.bot._admin_earnings(9,0,4)
        self.assertIn('wa:paypartner:31',str(self.bot._show_panel.call_args.args[1].rows))
        self.client.rpc.reset_mock();self.bot._admin_earnings(31,0,4)
        self.client.rpc.assert_not_called()


if __name__=='__main__':unittest.main()
