"""Durable Telegram evidence and minimal channel data, never financial writes."""
from copy import deepcopy
import unittest
from unittest.mock import Mock,patch
from telegram_bot.commerce.partner_finance import PartnerFinanceMixin
from telegram_bot.commerce.payout_notifications import payout_notice_panel
from telegram_bot.commerce.client import CommerceUnavailable

WD='da337a0e-c03e-46d6-bb92-dd185a6d5242'
NOTICE='72f4515b-404e-4cf0-b9ac-a7420b993b01'
TOKEN='fa93264f-62d9-44cb-82d4-28c1239c1056'


class PayoutNotificationTests(unittest.TestCase):
    def setUp(self):
        self.bot=PartnerFinanceMixin();self.bot.current_authorized_ids={9}
        self.bot._commerce=Mock();self.client=self.bot._commerce.return_value
        self.notice={'id':NOTICE,'token':TOKEN,'kind':'paid','recipient_kind':'partner','recipient_chat_id':31,
            'payload':{'id':WD,'telegram_user_id':31,'amount_minor':1000,'destination':'private bank account',
                'payout_reference':'bank:FIXTURE','state':'paid','receipt':{'id':NOTICE,'state':'requested',
                    'recipient_telegram_id':31,'paid_actor_id':9}}}
        self.client.rpc.side_effect=lambda name,args=None:deepcopy(self.notice) if name=='claim_payout_notification' else {'saved':True}
        self.bot._api_call=Mock(return_value={'ok':True,'result':{'message_id':100,'chat':{'id':31}}})

    def test_partner_receives_exact_paid_amount_with_owned_confirmation_buttons(self):
        self.assertTrue(self.bot._payout_notification_once())
        payload=self.bot._api_call.call_args.args[1]
        self.assertEqual(payload['chat_id'],31)
        self.assertIn('<code>10.00</code>',payload['text'])
        self.assertIn('تم صرف أرباحك',payload['text'])
        self.assertIn('pf:ack:'+NOTICE,str(payload['reply_markup']))
        self.assertIn('pf:dispute:'+NOTICE,str(payload['reply_markup']))
        self.assertEqual(self.client.rpc.call_args.args[1]['p_state'],'sent')

    def test_channel_notice_omits_bank_destination_and_has_no_receipt_response_buttons(self):
        self.notice.update(recipient_kind='channel',recipient_chat_id=-100123)
        self.bot._api_call.return_value={'ok':True,'result':{'message_id':101,'chat':{'id':-100123}}}
        self.bot._payout_notification_once()
        payload=self.bot._api_call.call_args.args[1]
        self.assertEqual(payload['chat_id'],-100123)
        self.assertNotIn('private bank account',payload['text'])
        self.assertEqual(payload['reply_markup']['inline_keyboard'],[])
        self.assertIn('بانتظار تأكيد الشريك',payload['text'])

    def test_confirmed_and_disputed_are_distinct_from_recorded_payment(self):
        self.notice['payload']['receipt']['responded_at']='2026-10-16T00:00:00Z'
        for kind in ('confirmed','disputed'):
            self.notice['kind']=kind;panel=payout_notice_panel(self.notice)
            self.assertNotIn('pf:ack:',str(panel.rows))
            self.assertIn('أكد الشريك' if kind=='confirmed' else 'اعترض الشريك',panel.text)
            self.assertIn('2026-10-16 03:00:00 بغداد',panel.text)

    def test_timeout_is_uncertain_and_does_not_retry_or_touch_money(self):
        self.bot._api_call.side_effect=TimeoutError('Synthetic timeout')
        self.bot._payout_notification_once()
        self.assertEqual(self.bot._api_call.call_count,1)
        self.assertEqual(self.client.rpc.call_args.args[1]['p_state'],'uncertain')
        self.assertEqual([c.args[0] for c in self.client.rpc.call_args_list],['claim_payout_notification','complete_payout_notification'])

    def test_render_failure_before_send_is_eligible_for_bounded_retry(self):
        with patch('telegram_bot.commerce.payout_notifications.payout_notice_panel',side_effect=ValueError('synthetic bad rendering')):
            self.bot._payout_notification_once()
        self.bot._api_call.assert_not_called()
        self.assertEqual(self.client.rpc.call_args.args[1]['p_state'],'rejected')

    def test_wrong_chat_or_missing_message_id_cannot_claim_delivery(self):
        for result in ({'message_id':12,'chat':{'id':32}},{'chat':{'id':31}}):
            self.bot._api_call.return_value={'ok':True,'result':result}
            self.bot._payout_notification_once()
            self.assertEqual(self.client.rpc.call_args.args[1]['p_state'],'uncertain')

    def test_explicit_rejection_is_saved_for_bounded_retry(self):
        self.bot._api_call.return_value={'ok':False,'error_code':403}
        self.bot._payout_notification_once()
        self.assertEqual(self.client.rpc.call_args.args[1]['p_state'],'rejected')

    def test_empty_queue_does_not_send_any_message(self):
        self.client.rpc.return_value=None;self.client.rpc.side_effect=None
        self.assertFalse(self.bot._payout_notification_once());self.bot._api_call.assert_not_called()

    def test_missing_database_functions_back_off_and_can_recover(self):
        self.bot._configure_payout_channels=Mock(side_effect=CommerceUnavailable('missing',http_status=404,database_code='PGRST202'))
        self.assertEqual([self.bot._payout_notice_cycle() for _ in range(4)],[300,600,900,900])
        self.client.rpc.assert_not_called();self.bot._api_call.assert_not_called()
        self.bot._configure_payout_channels.side_effect=None
        self.bot._payout_notification_once=Mock(return_value=False)
        self.assertEqual(self.bot._payout_notice_cycle(),15)
        self.assertEqual(self.bot._payout_notice_failures,0)

    def test_network_failure_has_bounded_backoff_and_recovers(self):
        self.bot._configure_payout_channels=Mock(side_effect=OSError('offline'))
        self.assertEqual([self.bot._payout_notice_cycle() for _ in range(7)],[15,30,60,120,240,300,300])

    def test_cash_receipt_is_labelled_as_internal_documentation(self):
        self.notice['payload']['payout_reference']='cash:CASH-EXAMPLE'
        self.assertIn('مرجع توثيق نقدي داخلي',payout_notice_panel(self.notice).text)
        self.notice['recipient_kind']='channel'
        self.assertIn('مرجع توثيق نقدي داخلي',payout_notice_panel(self.notice).text)

    def test_only_configured_negative_channel_ids_are_registered(self):
        with patch('telegram_bot.commerce.payout_notifications.settings',return_value={
                'admin_groups':[-100123,-100123,31],'chat_id':-100456}):
            self.bot._configure_payout_channels();self.bot._configure_payout_channels()
        self.client.rpc.assert_called_once_with('configure_payout_notice_channels',{
            'p_actor_telegram_id':9,'p_channels':[-100123,-100456]})

    def test_dedicated_payout_channel_does_not_expand_to_other_admin_groups(self):
        with patch('telegram_bot.commerce.payout_notifications.settings',return_value={
                'payout_channels':[-100789],'admin_groups':[-100123],'chat_id':-100456}):
            self.bot._configure_payout_channels()
        self.client.rpc.assert_called_once_with('configure_payout_notice_channels',{
            'p_actor_telegram_id':9,'p_channels':[-100789]})


if __name__=='__main__':unittest.main()
