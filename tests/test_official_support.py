import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch
from telegram_bot.bot import TelegramBotRunner
from telegram_bot import views,official_support


class OfficialSupportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.bot=TelegramBotRunner.__new__(TelegramBotRunner)
        self.bot.project_root=Path(self.temp.name)
        self.bot._ui_lock=threading.RLock();self.bot._waiting_user_inputs={}
        self.bot.authorized_ids={9};self.bot.authorized_id=9;self.bot._authorized_override=True
        self.bot._show_panel=Mock();self.bot._api_call=Mock()
        self.bot._commerce_client=Mock()

    def assert_redirect(self):
        panel=self.bot._show_panel.call_args.args[1]
        self.assertEqual(panel.rows[0],[official_support.support_button()])
        self.assertIn('الوحيدة المعتمدة',panel.text)
        self.bot._commerce_client.rpc.assert_not_called()
        self.bot._api_call.assert_not_called()

    def test_policy_is_fixed_to_channel_direct_messages_not_a_post(self):
        self.assertTrue(official_support.CHANNEL_ONLY)
        self.assertEqual(official_support.SUPPORT_URL,'https://t.me/zainchecker?direct')
        self.assertEqual(official_support.support_button()['text'],'التواصل مع الدعم')

    def test_home_support_button_is_visible_to_customer_and_admin(self):
        for admin,mode in [(False,'customer'),(True,'admin')]:
            panel=views.home({},[],is_admin=admin,mode=mode)
            self.assertIn(official_support.support_button(),[item for row in panel.rows for item in row])
        self.assertEqual(views.customer_rows([[official_support.support_button()]]),[[official_support.support_button()]])

    def test_old_support_command_and_reply_redirect_without_a_ticket_or_send(self):
        for command in ('/support','/support_reply 11111111-1111-1111-1111-111111111111'):
            self.bot._commerce_command(command,31,False)
            self.assert_redirect()
            self.assertEqual(self.bot._waiting_user_inputs,{})

    def test_old_callbacks_cannot_compose_or_send_via_the_bot(self):
        for action in ('compose','send','review','open','closed'):
            self.bot._support_callback('sp:'+action+':11111111-1111-1111-1111-111111111111',31)
            self.assert_redirect()
        self.bot._support_send('11111111-1111-1111-1111-111111111111',9)
        self.assert_redirect()

    def test_old_vodafone_complaint_and_staff_reply_cannot_write_or_send(self):
        for action in ('complaint','reply','send'):
            self.bot._vodafone_callback('vc:'+action+':11111111-1111-1111-1111-111111111111',31,8,{})
            self.assert_redirect()
        for action in ('complaint','reply'):
            self.bot._commerce_text_input(31,'متن رسالة لن ينقل إلى المسار القديم',{
                'action':'commerce_vodafone_'+action,'request_id':'order-ref','message_id':8})
            self.assert_redirect()

    def test_inflight_support_input_and_stored_update_are_redirected(self):
        self.bot._waiting_user_inputs[31]={'action':'commerce_support_order'}
        self.bot._commerce_text_input(31,'order-ref',self.bot._waiting_user_inputs[31])
        self.assertEqual(self.bot._waiting_user_inputs,{})
        update={'_support_event':'old-event','message':{'from':{'id':31},'chat':{'id':31},'text':'old-message'}}
        self.bot._capture_support_arrival(update)
        self.assertTrue(self.bot._support_handle_message(update))
        self.assert_redirect()
        self.assertFalse((self.bot.project_root/'.commerce/support.sqlite').exists())

    def test_arbitrary_or_group_updates_do_not_start_a_legacy_support_worker(self):
        self.assertIsNone(self.bot._existing_support())
        self.assertFalse(self.bot._support_handle_message({'message':{'chat':{'id':31},'from':{'id':31}}}))
        self.assertFalse(self.bot._support_handle_message({'_support_event':'old','message':{'chat':{'id':-1},'from':{'id':31}}}))
        self.bot._api_call.assert_not_called()

    def test_request_reference_is_copyable_and_escaped(self):
        self.bot._official_support(31,order='<bad&ref>')
        self.assertIn('<code>&lt;bad&amp;ref&gt;</code>',self.bot._show_panel.call_args.args[1].text)

    def test_channel_only_policy_preserves_manual_payment_approval(self):
        from test_commerce_vodafone_cash import VodafoneTests
        f=VodafoneTests();f.setUp()
        try:
            with patch('telegram_bot.commerce.vodafone_cash.CHANNEL_ONLY',True):
                key=f.submit();f.prepare_approval(key)
                f.callback('approve',key,9);f.callback('approve',key,9)
                self.assertEqual(f.balance,6050)
                self.assertEqual(f.records[key]['state'],'approved')
                self.assertEqual(f.bot.queue_service.get_jobs(),[])
        finally:f.doCleanups()

if __name__=='__main__':unittest.main()
