import copy
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from telegram_bot.bot import TelegramBotRunner
from telegram_bot import subscription_policy as policy,views
import test_telegram_maintenance as maintenance_fixture


class PolicyTests(unittest.TestCase):
    def test_all_membership_states_and_restricted_flag(self):
        for status,extra,expected in [
            ('creator',{},'member'),('administrator',{},'member'),('member',{},'member'),
            ('left',{},'missing'),('kicked',{},'missing'),('restricted',{'is_member':True},'member'),
            ('restricted',{'is_member':False},'missing'),('restricted',{},'unavailable'),
            ('restricted',{'is_member':1},'unavailable'),('unknown',{},'unavailable')]:
            with self.subTest(status=status,extra=extra):
                api=Mock(return_value={'ok':True,'result':{'user':{'id':31},'status':status,**extra}})
                self.assertEqual(policy.membership(api,31),expected)
                api.assert_called_once_with('getChatMember',{'chat_id':'@zainchecker','user_id':31},timeout=5)

    def test_api_errors_and_mismatched_identity_fail_closed(self):
        for result in [None,{},True,{'ok':False,'error_code':429},
            {'ok':False,'error_code':403},{'ok':True,'result':True},
            {'ok':True,'result':{'status':'member','user':{'id':32}}},
            {'ok':True,'result':{'status':'member','user':{'id':'31'}}}]:
            with self.subTest(result=result):
                self.assertEqual(policy.membership(Mock(return_value=result),31),'unavailable')
        self.assertEqual(policy.membership(Mock(side_effect=TimeoutError),31),'unavailable')
        for actor in (True,'31',0,-31,None):
            api=Mock();self.assertEqual(policy.membership(api,actor),'unavailable');api.assert_not_called()

    def test_join_check_and_sole_support_buttons_survive_customer_filter(self):
        self.assertEqual(views.customer_rows(policy.rows()),policy.rows())
        self.assertEqual(policy.rows()[0][0]['url'],'https://t.me/zainchecker')
        self.assertEqual(policy.rows()[1][0]['callback_data'],policy.CHECK_CALLBACK)
        self.assertEqual(policy.rows()[2][0]['url'],'https://t.me/zainchecker?direct')


class SubscriptionRoutingTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.bot=TelegramBotRunner.__new__(TelegramBotRunner)
        self.bot.project_root=Path(temp.name);self.bot._ui_lock=threading.RLock()
        self.bot.authorized_ids={9};self.bot.authorized_id=9;self.bot._authorized_override=True
        self.bot._waiting_user_inputs={31:{'action':'commerce_vodafone_proof','request_id':'keep'}}
        self.bot._confirmations={};self.bot._sheet_drafts={'keep':{'owner':31}}
        self.bot._support_handle_message=Mock(return_value=False)
        self.bot._refresh_partner_menu_for_chat=Mock();self.bot._vodafone_handle_media=Mock(return_value=False)
        self.bot._handle_document_upload=Mock();self.bot._handle_user_text_input=Mock()
        self.bot._commerce_command=Mock(return_value=False);self.bot._dispatch_ui=Mock()
        self.bot._vodafone_callback=Mock();self.bot._wallet_callback=Mock()
        self.bot._show_panel=Mock();self.bot._official_support=Mock()
        self.bot._commerce_client=Mock();self.bot.queue_service=Mock()
        self.status='left'
        def api(method,payload,**kwargs):
            if method=='getChatMember':
                return {'ok':True,'result':{'status':self.status,'user':{'id':payload['user_id']}}}
            return {'ok':True}
        self.bot._api_call=Mock(side_effect=api)

    def message(self,**values):
        return {'message':{'chat':{'id':31,'type':'private'},'from':{'id':31},'message_id':17,**values}}

    def callback(self,data,actor=31,chat=31):
        self.bot._handle_update({'callback_query':{'id':'q','from':{'id':actor},'data':data,
            'message':{'chat':{'id':chat,'type':'private'},'message_id':19}}})

    def test_unsubscribed_messages_media_and_files_cannot_consume_payment_input(self):
        waiting=copy.deepcopy(self.bot._waiting_user_inputs);drafts=copy.deepcopy(self.bot._sheet_drafts)
        for values in ({'text':'/start'},{'text':'/wallet'},{'text':'/redeem code'},
            {'text':'01012345678'},{'photo':[{'file_id':'proof'}]},
            {'document':{'file_name':'customers.xlsx','file_id':'file'}}):
            self.bot._handle_update(self.message(**values))
            self.assertEqual(self.bot._show_panel.call_args.args[1].rows,policy.rows())
        self.assertEqual(self.bot._waiting_user_inputs,waiting)
        self.assertEqual(self.bot._sheet_drafts,drafts)
        for method in (self.bot._refresh_partner_menu_for_chat,self.bot._vodafone_handle_media,
                self.bot._handle_document_upload,self.bot._handle_user_text_input,self.bot._commerce_command,
                self.bot._dispatch_ui,self.bot._commerce_client.rpc):method.assert_not_called()

    def test_old_callbacks_cannot_trigger_wallet_or_payment_operations(self):
        for data in ('zc:wallet','zc:vc:phone:order','zc:vc:approve:order','zc:co:pay:draft'):
            self.callback(data,actor=9,chat=9) if 'approve' in data else self.callback(data)
        self.bot._wallet_callback.assert_not_called();self.bot._vodafone_callback.assert_not_called()
        self.bot._commerce_client.rpc.assert_not_called();self.bot._dispatch_ui.assert_not_called()

    def test_verify_must_confirm_membership_and_never_replays_an_old_action(self):
        self.callback(policy.CHECK_CALLBACK)
        self.bot._dispatch_ui.assert_not_called()
        self.status='member';self.callback(policy.CHECK_CALLBACK)
        self.bot._dispatch_ui.assert_called_once_with('home',31,19)
        self.bot._vodafone_callback.assert_not_called();self.bot._wallet_callback.assert_not_called()

    def test_leaving_channel_blocks_next_input_without_stale_positive_cache(self):
        self.status='member';self.callback('zc:vc:list:0')
        self.bot._vodafone_callback.assert_called_once()
        self.status='left';self.callback('zc:vc:list:0')
        self.bot._vodafone_callback.assert_called_once()
        lookups=[c for c in self.bot._api_call.call_args_list if c.args[0]=='getChatMember']
        self.assertEqual(len(lookups),2)

    def test_subscribed_command_reaches_normal_home(self):
        self.status='member';self.bot._handle_update(self.message(text='/start'))
        self.bot._dispatch_ui.assert_called_once_with('home',31)
        self.assertEqual(self.bot._api_call.call_args_list[0].args[0],'getChatMember')
        self.bot._show_panel.assert_not_called()

    def test_support_remains_reachable_without_membership_lookup(self):
        for text in ('/support','/support@ZainCheckerbot'):
            self.bot._handle_update(self.message(text=text))
        for data in ('zc:support','zc:sp:compose:order','zc:vc:complaint:order'):
            self.callback(data)
        self.assertEqual(self.bot._official_support.call_count,5)
        self.assertNotIn('getChatMember',[c.args[0] for c in self.bot._api_call.call_args_list])

    def test_invalid_callback_identity_and_group_messages_cannot_bypass_gate(self):
        self.callback(policy.CHECK_CALLBACK,actor=32,chat=31)
        self.callback('zc:wallet',actor=31,chat=-99)
        self.bot._handle_update({'message':{'chat':{'id':-99,'type':'supergroup'},'from':{'id':31},'text':'/wallet'}})
        self.bot._dispatch_ui.assert_not_called();self.bot._wallet_callback.assert_not_called()
        self.assertNotIn('getChatMember',[c.args[0] for c in self.bot._api_call.call_args_list])

    def test_lookup_outage_blocks_action_and_reports_retry_instead_of_missing_membership(self):
        self.bot._api_call=Mock(return_value={'ok':False,'error_code':429})
        self.callback('zc:vc:phone:order')
        self.bot._vodafone_callback.assert_not_called()
        self.assertIn('تعذّر التحقق',self.bot._show_panel.call_args.args[1].text)


class MaintenanceSubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.fixture=maintenance_fixture.MaintenanceTests();self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.r=self.fixture.r;self.api=self.fixture.api
        self.r.db.execute("update deliveries set status='sent' where kind='broadcast'");self.r.db.commit()
        self.status='left'
        def api(method,payload,**kwargs):
            if method=='getChatMember':
                return {'ok':True,'result':{'status':self.status,'user':{'id':payload['user_id']}}}
            return {'ok':True,'result':{'message_id':8}}
        self.api.call.side_effect=api

    def capture(self,uid=11,text='/start'):
        self.r.capture([{'update_id':uid,'message':{'chat':{'id':31,'type':'private'},'from':{'id':31},'text':text}}])
        self.r.deliver_one()
        return self.api.call.call_args.args[1]

    def test_nonmember_gets_join_panel_but_member_still_gets_maintenance(self):
        payload=self.capture()
        self.assertEqual(payload['reply_markup']['inline_keyboard'],policy.rows())
        self.status='member';payload=self.capture(12)
        self.assertEqual(payload['text'],self.r.text)
        self.assertTrue(all(not k.startswith('_') for k in payload))
        self.assertEqual(self.r.db.execute("select count(*) from deliveries where kind='broadcast'").fetchone()[0],2)

    def test_verify_button_is_checked_and_does_not_enable_normal_bot(self):
        self.status='member'
        self.r.capture([{'update_id':12,'callback_query':{'id':'q','from':{'id':31},'data':policy.CHECK_CALLBACK,
            'message':{'chat':{'id':31,'type':'private'},'message_id':1}}}])
        self.r.deliver_one();self.r.deliver_one()
        self.assertEqual([c.args[0] for c in self.api.call.call_args_list],['answerCallbackQuery','getChatMember','sendMessage'])
        self.assertEqual(self.api.call.call_args.args[1]['text'],self.r.text)

    def test_support_skips_membership_and_spoofed_private_sender_is_ignored(self):
        payload=self.capture(text='/support')
        self.assertIn('الدعم الرسمي',payload['text'])
        self.assertNotIn('getChatMember',[c.args[0] for c in self.api.call.call_args_list])
        self.r.capture([{'update_id':12,'message':{'chat':{'id':31,'type':'private'},'from':{'id':32},'text':'/start'}}])
        self.assertFalse(self.r.deliver_one())

    def test_membership_outage_keeps_services_blocked(self):
        self.api.call.side_effect=lambda method,payload,**kw: (
            {'ok':False,'error_code':'transport'} if method=='getChatMember' else {'ok':True,'result':{}})
        self.assertIn('تعذّر التحقق',self.capture()['text'])


if __name__=='__main__':unittest.main()
