import json
import unittest
from unittest.mock import Mock,patch
from telegram_bot.maintenance_access import owner_allowed,active
import test_telegram_subscription as subscription_fixture
import test_customer_commerce_runtime as runtime_fixture


class OwnerMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.fixture=subscription_fixture.SubscriptionRoutingTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.bot=self.fixture.bot;self.root=self.bot.project_root
        self.bot._bot_fingerprint='test-bot-fingerprint'
        self.state=self.root/'.maintenance';self.state.mkdir()
        (self.state/'enabled').touch()
        (self.state/'announcement.txt').write_text('**صيانة**\nالخدمات متوقفة.',encoding='utf-8')
        self.policy={'version':1,'owner_id':9,'bot':'test-bot-fingerprint'}
        self.save()

    def save(self):
        (self.state/'owner-only.json').write_text(json.dumps(self.policy))

    def message(self,actor=9,**values):
        return {'message':{'from':{'id':actor},'chat':{'id':actor,'type':'private'},'message_id':3,**values}}

    def test_only_bound_primary_owner_reaches_normal_command_routing(self):
        self.fixture.status='member'
        self.bot._handle_update(self.message(text='/start'))
        self.bot._dispatch_ui.assert_called_once_with('home',9)
        self.bot._dispatch_ui.reset_mock()
        self.bot.authorized_ids.add(31)
        self.bot._handle_update(self.message(31,text='/start'))
        self.bot._dispatch_ui.assert_not_called()
        self.assertIn('صيانة',self.bot._api_call.call_args.args[1]['text'])

    def test_other_members_files_evidence_and_commands_cannot_touch_workflows(self):
        self.fixture.status='member'
        for content in ({'text':'/wallet'},{'text':'/credit_user 31 50'},
            {'document':{'file_name':'customers.xlsx'}},{'photo':[{'file_id':'proof'}]}):
            self.bot._handle_update(self.message(31,**content))
        self.bot._commerce_command.assert_not_called();self.bot._vodafone_handle_media.assert_not_called()
        self.bot._handle_document_upload.assert_not_called();self.bot._handle_user_text_input.assert_not_called()
        self.assertIn(31,self.bot._waiting_user_inputs)

    def test_direct_old_callback_from_other_admin_stays_in_maintenance(self):
        self.bot.authorized_ids.add(31);self.fixture.status='member'
        self.bot._handle_callback({'id':'old','from':{'id':31},'data':'zc:vc:approve:payment',
            'message':{'chat':{'id':31,'type':'private'},'message_id':7}})
        self.bot._vodafone_callback.assert_not_called();self.bot._commerce_client.rpc.assert_not_called()
        self.assertIn('صيانة',self.bot._api_call.call_args.args[1]['text'])

    def test_owner_callback_keeps_subscription_requirement(self):
        self.fixture.status='member';self.fixture.callback('zc:vc:list:0',actor=9,chat=9)
        self.bot._vodafone_callback.assert_called_once()
        self.fixture.status='left';self.fixture.callback('zc:vc:list:0',actor=9,chat=9)
        self.bot._vodafone_callback.assert_called_once()

    def test_nonmember_gets_join_gate_and_support_remains_available(self):
        self.bot._handle_update(self.message(31,text='/start'))
        self.assertIn('الاشتراك',self.bot._api_call.call_args.args[1]['text'])
        self.bot._handle_update(self.message(31,text='/support'))
        self.assertIn('الدعم الرسمي',self.bot._api_call.call_args.args[1]['text'])

    def test_broken_missing_or_wrong_bot_policy_grants_nobody_access(self):
        for candidate in ({'version':1,'owner_id':9,'bot':'another-bot'},
                          {'version':1,'owner_id':31,'bot':'test-bot-fingerprint'},
                          {'version':1,'owner_id':True,'bot':'test-bot-fingerprint'},None):
            self.policy=candidate;self.save()
            self.assertFalse(owner_allowed(self.root,9,9,self.bot._bot_fingerprint))
            self.bot._handle_update(self.message(text='/start'))
        (self.state/'owner-only.json').write_text('broken-json')
        self.assertFalse(owner_allowed(self.root,9,9,self.bot._bot_fingerprint))
        (self.state/'owner-only.json').unlink()
        self.assertFalse(owner_allowed(self.root,9,9,self.bot._bot_fingerprint))
        self.bot._dispatch_ui.assert_not_called()

    def test_spoofed_actor_group_and_bot_sender_cannot_reach_owner_workflow(self):
        for message in ({'from':{'id':9},'chat':{'id':31,'type':'private'},'text':'/start'},
            {'from':{'id':9},'chat':{'id':-31,'type':'supergroup'},'text':'/start'},
            {'from':{'id':9,'is_bot':True},'chat':{'id':9,'type':'private'},'text':'/start'}):
            self.bot._handle_update({'message':message})
        self.bot._dispatch_ui.assert_not_called();self.bot._api_call.assert_not_called()

    def test_missing_announcement_has_safe_maintenance_response(self):
        self.fixture.status='member';(self.state/'announcement.txt').unlink()
        self.bot._handle_update(self.message(31,text='/start'))
        self.assertIn('الصيانة',self.bot._api_call.call_args.args[1]['text'])

    def test_without_marker_existing_subscription_policy_handles_normal_customer(self):
        (self.state/'enabled').unlink();self.assertFalse(active(self.root))
        self.fixture.status='member';self.bot._handle_update(self.message(31,text='/start'))
        self.bot._dispatch_ui.assert_called_once_with('home',31)


class MaintenanceQueueTests(unittest.TestCase):
    def test_maintenance_holds_pending_queue_before_any_mutating_rpc(self):
        f=runtime_fixture.CustomerRuntimeTests();f.setUp();self.addCleanup(f.doCleanups)
        f.job();f.orch.start_queue=Mock(return_value=True)
        state=f.root/'.maintenance';state.mkdir();(state/'enabled').touch()
        for _ in range(2):self.assertFalse(f.service.tick())
        f.orch.start_queue.assert_not_called();f.client.rpc.assert_not_called()

    def test_private_startup_holds_payment_recovery_and_archive_uploads(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from telegram_bot.integration import TelegramIntegration
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'.maintenance').mkdir();(root/'.maintenance/enabled').touch()
            notifier=Mock(bot_token='synthetic-token',chat_id=9)
            runner=Mock(project_root=root,authorized_id=9)
            with patch('telegram_bot.notifier.TelegramNotifier',return_value=notifier),\
                 patch('telegram_bot.bot.TelegramBotRunner',return_value=runner),\
                 patch('telegram_bot.commerce.bridge.CommerceBridge') as bridge,\
                 patch('telegram_bot.commerce.archive.ArchiveDispatcher') as archive:
                integration=TelegramIntegration(SimpleNamespace(telegram_enabled=False),Mock(),root)
                integration.set_enabled(True)
                runner.start.assert_called_once()
                archive.return_value.start.assert_not_called()
                self.assertFalse(integration.checkout_recovery.is_alive())
                notifier.recover_commerce_deliveries.assert_not_called()
                bridge.return_value.start.assert_called_once()


if __name__=='__main__':unittest.main()
