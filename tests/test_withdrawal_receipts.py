"""Recipient acknowledgements never claim a transfer or make financial writes."""
import copy
import threading
import unittest
from unittest.mock import Mock
from telegram_bot.commerce.partner_finance import PartnerFinanceMixin
from telegram_bot.commerce.withdrawal_receipts import receipt_panel
from telegram_bot import views

WD='fe30d117-a23a-4f35-8e69-f2ac9dca418a'
RECEIPT='06d54ab9-21a7-4db8-913f-3f387f750b3b'


class WithdrawalReceiptTests(unittest.TestCase):
    def setUp(self):
        self.bot=PartnerFinanceMixin();self.bot.current_authorized_ids={9};self.bot._ui_lock=threading.RLock()
        self.bot._commerce=Mock();self.bot._show_panel=Mock(return_value={'ok':True});self.bot._commerce_notice=Mock()
        self.record={'id':WD,'telegram_user_id':31,'state':'paid','amount_minor':1000,'destination':'<bank>',
            'payout_reference':'bank:TX001','paid_at':'2026-10-15T00:10:00Z',
            'receipt':{'id':RECEIPT,'withdrawal_id':WD,'state':'requested','paid_actor_id':9,
                       'recipient_telegram_id':31,'responded_at':None}}
        self.bot._commerce.return_value.rpc.return_value=copy.deepcopy(self.record)

    def test_admin_explicit_request_sends_amount_and_reference_to_the_actual_partner(self):
        self.bot._withdrawal_admin_callback('wa:requestack:'+WD,9,4,{'id':'native-admin-click'})
        self.bot._commerce.return_value.rpc.assert_called_once_with('prepare_withdrawal_receipt',{
            'p_withdrawal_id':WD,'p_actor_telegram_id':9,'p_reference':'telegram-receipt-request:native-admin-click'})
        delivery=self.bot._show_panel.call_args_list[0]
        self.assertEqual(delivery.args[0],31)
        text=delivery.args[1].text
        for part in ('10.00 جنيه','bank:TX001','&lt;bank&gt;','بانتظار تأكيد الشريك','2026-10-15 03:10:00 بغداد'):
            self.assertIn(part,text)
        self.assertNotIn('أكد الشريك الاستلام',text)

    def test_customer_cannot_send_an_administrative_receipt_request(self):
        self.bot._withdrawal_admin_callback('wa:requestack:'+WD,31,4,{'id':'foreign'})
        self.bot._commerce.assert_not_called();self.bot._show_panel.assert_not_called()

    def test_missing_admin_callback_never_prepares_or_sends(self):
        self.bot._withdrawal_admin_callback('wa:requestack:'+WD,9,4,{})
        self.bot._commerce.assert_not_called();self.bot._show_panel.assert_not_called()

    def test_recipient_native_confirmation_uses_authenticated_identity(self):
        self.bot._partner_finance_callback('pf:ack:'+RECEIPT,31,4,{'id':'native-recipient-click'})
        self.bot._commerce.return_value.rpc.assert_called_once_with('respond_withdrawal_receipt',{
            'p_receipt_id':RECEIPT,'p_telegram_user_id':31,'p_accept':True,
            'p_reference':'telegram-receipt-response:native-recipient-click'})

    def test_dispute_is_a_recorded_response_and_never_a_refund(self):
        self.bot._partner_finance_callback('pf:dispute:'+RECEIPT,31,4,{'id':'native-dispute'})
        call=self.bot._commerce.return_value.rpc.call_args
        self.assertEqual(call.args[0],'respond_withdrawal_receipt');self.assertFalse(call.args[1]['p_accept'])
        self.assertEqual(self.bot._commerce.return_value.rpc.call_count,1)

    def test_confirmation_without_native_click_does_not_reach_backend(self):
        self.bot._partner_finance_callback('pf:ack:'+RECEIPT,31,4,{})
        self.bot._commerce.assert_not_called();self.bot._show_panel.assert_not_called()

    def test_unexpected_owner_response_is_not_displayed(self):
        self.bot._commerce.return_value.rpc.return_value={**self.record,'telegram_user_id':32}
        self.bot._partner_finance_callback('pf:receipt:'+WD,31,4,{})
        self.bot._show_panel.assert_not_called()

    def test_failed_telegram_send_is_not_reported_as_delivered(self):
        self.bot._show_panel.side_effect=[{'ok':False},{'ok':True}]
        self.bot._withdrawal_admin_callback('wa:requestack:'+WD,9,4,{'id':'native'})
        self.assertIn('لم نتأكد من وصول الرسالة',self.bot._show_panel.call_args.args[1].text)

    def test_final_receipt_displays_evidence_and_cannot_be_reanswered_from_the_panel(self):
        record=copy.deepcopy(self.record);record['receipt'].update(state='confirmed',responded_at='2026-10-15T00:15:00Z')
        panel=receipt_panel(record)
        self.assertIn('أكد الشريك الاستلام',panel.text);self.assertIn(RECEIPT,panel.text)
        self.assertIn('2026-10-15 03:15:00 بغداد',panel.text)
        self.assertNotIn('pf:ack:',str(panel.rows));self.assertNotIn('pf:dispute:',str(panel.rows))

    def test_native_customer_renderer_keeps_receipt_buttons_and_callback_sizes_are_valid(self):
        rows=views.customer_rows(receipt_panel(self.record).rows)
        actions=[item['callback_data'] for row in rows for item in row]
        self.assertTrue(any('pf:ack:' in action for action in actions))
        self.assertTrue(any('pf:dispute:' in action for action in actions))
        self.assertTrue(all(len(action.encode('utf-8'))<=64 for action in actions))


if __name__=='__main__':unittest.main()
