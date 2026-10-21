"""Owned notices, explicit retirement, UTC and native Telegram evidence."""
import copy
import hashlib
import threading
import time
import unittest
from unittest.mock import Mock
from telegram_bot import views
from telegram_bot.commerce.partner_lifecycle import PartnerLifecycleMixin,lifecycle_panel
from telegram_bot.commerce.withdrawal_receipts import _utc_stamp,receipt_panel
from telegram_bot.commerce.payout_notifications import payout_notice_panel
from telegram_bot.commerce.partner_finance import PartnerFinanceMixin

PID='38ee2874-782c-450d-a313-111c9dacb596'
EVENT='2d4d6ddc-bc24-4abf-95a0-d3ddcc33b637'
NOTICE='1ed449a5-dd3f-4bed-aed0-c5150f801602'
TOKEN='40d13c3f-8e83-4ff3-9406-b29dfcf40d9d'


class PartnerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.bot=PartnerLifecycleMixin();self.bot.current_authorized_ids={9};self.bot._ui_lock=threading.RLock()
        self.bot._known_partner_ids={31};self.bot._show_panel=Mock();self.bot._commerce_notice=Mock();self.bot._api_call=Mock()
        self.client=Mock();self.bot._commerce=Mock(return_value=self.client)
        self.body='نشكرك على تعاونك. انتهاء المشاركة مع حفظ المستحقات.\nوقت الإجراء UTC: 2026-10-16 01:02:03 UTC'
        self.event={'id':EVENT,'partner_id':PID,'recipient_telegram_id':31,'actor_telegram_id':9,'kind':'ended',
            'body':self.body,'body_sha256':hashlib.sha256(self.body.encode()).hexdigest(),'acknowledged_at':None,'deliveries':[]}
        self.preview={'partner_id':PID,'telegram_user_id':31,'ended_at':None,'terms_version':'version1','preview':self.body}
        self.writes=[]
        def rpc(name,args=None):
            if name=='partner_end_preview':return copy.deepcopy(self.preview)
            if name=='end_partner':self.writes.append(args);return copy.deepcopy(self.event)
            if name=='partner_lifecycle_event':return copy.deepcopy(self.event)
            if name=='acknowledge_partner_notice':return {**copy.deepcopy(self.event),'acknowledged_at':'2026-10-16T02:03:04Z'}
            raise AssertionError(name)
        self.client.rpc.side_effect=rpc

    def click(self,action,actor=9,callback=None):
        self.bot._partner_lifecycle_callback(action,actor,4,{'id':'native-callback'} if callback is None else callback)

    def test_end_review_does_not_change_role_or_send_untracked_messages(self):
        self.click('pl:remove:'+PID)
        self.assertEqual(self.writes,[]);self.bot._api_call.assert_not_called()
        self.assertIn(self.body,self.bot._show_panel.call_args.args[1].text)
        self.assertIn('pl:endconfirm:'+PID,str(self.bot._show_panel.call_args.args[1].rows))
        self.click('pl:endconfirm:'+PID)
        self.assertEqual(self.writes,[{'p_actor_telegram_id':9,'p_partner_id':PID,'p_expected_terms_version':'version1','p_reference':'telegram-partner-end:native-callback'}])
        self.assertNotIn(31,self.bot._known_partner_ids)
        self.bot._api_call.assert_called_once_with('deleteMyCommands',{'scope':{'type':'chat','chat_id':31}})

    def test_end_removes_ready_menu_and_inflight_partner_prompts_before_notice_delivery(self):
        self.bot._partner_menu_ready={31}
        self.bot._waiting_user_inputs={31:{'action':'commerce_withdraw_amount'}}
        self.bot._withdrawal_reviews={'old':{'user':31}}
        self.bot._campaign_reviews={'old':{'actor':31,'partner':True},'other':{'actor':32,'partner':True}}
        self.bot._api_call.side_effect=TimeoutError('Synthetic unavailable Telegram')
        self.click('pl:remove:'+PID);self.click('pl:endconfirm:'+PID)
        self.assertEqual(len(self.writes),1)
        self.assertNotIn(31,self.bot._partner_menu_ready)
        self.assertNotIn(31,self.bot._waiting_user_inputs)
        self.assertEqual(self.bot._withdrawal_reviews,{})
        self.assertEqual(set(self.bot._campaign_reviews),{'other'})

    def test_foreign_actor_stale_review_and_missing_callback_cannot_end(self):
        self.click('pl:remove:'+PID,31)
        self.client.rpc.assert_not_called()
        self.click('pl:endconfirm:'+PID)
        self.assertEqual(self.writes,[])
        self.click('pl:remove:'+PID);self.click('pl:endconfirm:'+PID,callback={})
        self.assertEqual(self.writes,[])
        self.bot._partner_end_reviews[(9,PID)]['expires']=time.monotonic()-1
        self.click('pl:endconfirm:'+PID)
        self.assertEqual(self.writes,[])

    def test_partner_acknowledges_only_with_native_identity_without_money_commands(self):
        self.click('pl:ack:'+EVENT,31)
        self.client.rpc.assert_called_once_with('acknowledge_partner_notice',{'p_event_id':EVENT,'p_telegram_user_id':31,'p_reference':'telegram-partner-notice-ack:native-callback'})
        self.assertEqual(self.writes,[]);self.assertIn('2026-10-16 02:03:04 UTC',self.bot._show_panel.call_args.args[1].text)

    def test_unexpected_foreign_event_is_not_displayed(self):
        self.click('pl:view:'+EVENT,32)
        self.bot._show_panel.assert_not_called()

    def notice(self,recipient='partner'):
        record={'id':NOTICE,'token':TOKEN,'event_id':EVENT,'event_kind':'ended','kind':'event','recipient_kind':recipient,
            'recipient_chat_id':31 if recipient=='partner' else -100123,'body':self.body,'body_sha256':self.event['body_sha256']}
        self.client.rpc.side_effect=lambda name,args=None:copy.deepcopy(record) if name=='claim_partner_lifecycle_notification' else {'saved':True}
        return record

    def test_sender_uses_exact_frozen_text_and_records_native_utc_evidence(self):
        self.notice()
        native=int(time.time())
        self.bot._api_call.return_value={'ok':True,'result':{'chat':{'id':31},'message_id':77,'date':native}}
        self.bot._partner_lifecycle_notification_once()
        payload=self.bot._api_call.call_args_list[0].args[1]
        self.assertEqual(payload['text'],self.body);self.assertNotIn('parse_mode',payload)
        self.assertIn('pl:ack:'+EVENT,str(payload['reply_markup']))
        proof=self.client.rpc.call_args.args[1]
        self.assertEqual(proof['p_state'],'sent');self.assertEqual(proof['p_telegram_timestamp'],native)

    def test_channel_gets_identical_text_without_partner_ack_button(self):
        self.notice('channel')
        self.bot._api_call.return_value={'ok':True,'result':{'chat':{'id':-100123},'message_id':77,'date':int(time.time())}}
        self.bot._partner_lifecycle_notification_once()
        payload=self.bot._api_call.call_args.args[1]
        self.assertEqual(payload['text'],self.body);self.assertEqual(payload['reply_markup']['inline_keyboard'],[])

    def test_cancel_invalidates_old_end_confirmation(self):
        self.click('pl:remove:'+PID)
        self.bot._commerce_cancel_pending(9)
        self.click('pl:endconfirm:'+PID)
        self.assertEqual(self.writes,[])

    def test_missing_native_date_or_wrong_chat_is_not_claimed_as_sent(self):
        for response in ({'ok':True,'result':{'chat':{'id':31},'message_id':77}},
                         {'ok':True,'result':{'chat':{'id':32},'message_id':77,'date':int(time.time())}},None):
            with self.subTest(response=response):
                self.notice();self.bot._api_call.return_value=response
                self.bot._partner_lifecycle_notification_once()
                self.assertEqual(self.client.rpc.call_args.args[1]['p_state'],'uncertain')

    def test_tampered_text_is_not_sent(self):
        record=self.notice();record['body']='changed'
        self.client.rpc.side_effect=lambda name,args=None:record if name=='claim_partner_lifecycle_notification' else {'saved':True}
        self.bot._partner_lifecycle_notification_once()
        self.bot._api_call.assert_not_called();self.assertEqual(self.client.rpc.call_args.args[1]['p_state'],'rejected')

    def test_native_ack_is_distinct_from_delivery_and_from_financial_release(self):
        event={**self.event,'deliveries':[{'recipient_kind':'partner','state':'sent','telegram_message_id':77,
            'telegram_date':'2026-10-16T04:02:03+03:00','completed_at':'2026-10-16T01:02:04Z'}]}
        panel=lifecycle_panel(event,admin=True)
        self.assertIn('لا يثبت قراءتها',panel.text);self.assertIn('لم يُسجل رد الشريك',panel.text)
        self.assertIn('2026-10-16 01:02:03 UTC',panel.text)

    def test_customer_rows_never_expose_end_or_admin_archive(self):
        rows=[[views.button('view','pl:view:'+EVENT),views.button('ack','pl:ack:'+EVENT),views.button('remove','pl:remove:'+PID),views.button('archive','pl:ended:0')]]
        self.assertEqual(len(views.customer_rows(rows)[0]),2)

    def test_utc_converts_explicit_offsets_and_does_not_invent_missing_times(self):
        self.assertEqual(_utc_stamp('2026-10-16T04:02:03+03:00'),'2026-10-16 01:02:03 UTC')
        self.assertEqual(_utc_stamp(None),'غير مسجل')

    def test_payment_receipt_and_channel_have_payment_and_response_utc_times(self):
        record={'id':EVENT,'amount_minor':1000,'telegram_user_id':31,'paid_at':'2026-10-16T04:02:03+03:00',
            'payout_reference':'bank:ABC','receipt':{'id':NOTICE,'state':'confirmed','paid_actor_id':9,'recipient_telegram_id':31,
            'requested_at':'2026-10-16T01:02:04Z','responded_at':'2026-10-16T01:03:04Z'}}
        for panel in (receipt_panel(record),payout_notice_panel({'payload':record,'recipient_kind':'channel','kind':'confirmed'})):
            self.assertIn('2026-10-16 01:02:03 UTC',panel.text);self.assertIn('2026-10-16 01:03:04 UTC',panel.text)

    def test_ended_partner_keeps_financial_history_without_active_role_buttons(self):
        bot=PartnerFinanceMixin();bot.current_authorized_ids={9};bot._ui_lock=threading.RLock()
        bot._waiting_user_inputs={};bot._known_partner_ids={31};bot._show_panel=Mock();bot._commerce_notice=Mock();bot._commerce=Mock()
        bot._commerce.return_value.rpc.return_value={'is_partner':True,'available_minor':300,'reserved_minor':0,'withdrawn_minor':0,
            'today_earned_minor':300,'pending_commission_minor':0,'clients':1,'paid_checks':1,'collected_minor':1000,
            'awaiting_settlement_orders':0,'partnership_ended_at':'2026-10-16T01:02:03Z','partnership_end_event_id':EVENT}
        bot._partner_finance_callback('pf:home:0',31,4,{})
        panel=bot._show_panel.call_args.args[1]
        self.assertIn('3.00',panel.text);self.assertIn('2026-10-16 01:02:03 UTC',panel.text)
        self.assertIn('pf:list:0',str(panel.rows));self.assertNotIn('pc:home:0',str(panel.rows));self.assertNotIn('po:list:0',str(panel.rows))
        self.assertNotIn(31,bot._known_partner_ids)


    def test_reopen_partner_review_and_confirm(self):
        self.bot._partner_lifecycle_callback(f'pl:reopen:{PID}',9,4,{})
        panel=self.bot._show_panel.call_args.args[1]
        self.assertIn('مراجعة استئناف وإعادة تفعيل الشراكة',panel.text)
        self.assertIn(f'pl:rok:{PID}',str(panel.rows))

        self.client.rpc.side_effect=None
        self.client.rpc.return_value={'partner_id':PID,'telegram_user_id':31,'code':'AZZA','state':'pending','notice_event_id':EVENT}
        self.bot._grant_partner_role=Mock()
        self.bot._sync_partner_menus=Mock()
        self.bot._partner_lifecycle_callback(f'pl:rok:{PID}',9,4,{'id':'cb123'})
        self.client.rpc.assert_called_with('reactivate_partner',{
            'p_actor_telegram_id':9,'p_partner_id':PID,'p_reference':'telegram-partner-reopen:cb123'})
        res_panel=self.bot._show_panel.call_args.args[1]
        self.assertIn('تمت إعادة الشراكة بنجاح',res_panel.text)
        self.assertIn(f'cp:terms:{PID}',str(res_panel.rows))
        self.bot._grant_partner_role.assert_called_with(31)

    def test_reopen_partner_backward_compatibility(self):
        self.client.rpc.side_effect=None
        self.client.rpc.return_value={'partner_id':PID,'telegram_user_id':31,'code':'AZZA','state':'pending','notice_event_id':EVENT}
        self.bot._grant_partner_role=Mock()
        self.bot._sync_partner_menus=Mock()
        self.bot._partner_lifecycle_callback(f'pl:reopenconfirm:{PID}:31',9,4,{'id':'cb123'})
        self.client.rpc.assert_called_with('reactivate_partner',{
            'p_actor_telegram_id':9,'p_partner_id':PID,'p_reference':'telegram-partner-reopen:cb123'})
        self.bot._grant_partner_role.assert_called_with(31)

    def test_lifecycle_panel_shows_reopen_button_for_ended_partner(self):
        event={**self.event,'kind':'ended','partner_id':PID,'recipient_telegram_id':31}
        panel=lifecycle_panel(event,admin=True)
        self.assertIn('إعادة تفعيل الشراكة',str(panel.rows))
        self.assertIn(f'pl:reopen:{PID}',str(panel.rows))
        self.assertNotIn('support',str(panel.rows))
        self.assertNotIn('الدعم',str(panel.rows))


if __name__=='__main__':unittest.main()
