"""Owner-only native role controls; isolated settings and no live traffic."""
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from telegram_bot.config import settings,save_admin_membership
from telegram_bot.commerce.admin_roles import AdminRolesMixin
from telegram_bot.commerce.client import CommerceUnavailable
from telegram_bot.ui import TelegramUiMixin
from telegram_bot.maintenance_access import MaintenanceAccessMixin
from telegram_bot.subscription import SubscriptionMixin

class RoleBot(AdminRolesMixin,MaintenanceAccessMixin,SubscriptionMixin,TelegramUiMixin):
    @property
    def current_authorized_ids(self):return set(settings()['authorized_ids'])

class AdminRoleTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(tempfile.mkdtemp(prefix='admin-roles-retained-'))
        self.path=self.root/'settings.json'
        self.path.write_text(json.dumps({'authorized_id':9,'authorized_ids':[9,21],'bot_token':'fixture-only',
            'payout_channels':[-100600],'unrelated':'preserve'}),encoding='utf-8')
        p=patch('telegram_bot.config.LOCAL_SETTINGS',self.path);p.start();self.addCleanup(p.stop)
        essential={k:v for k,v in os.environ.items() if k.upper() in {'SYSTEMROOT','WINDIR','PATH','TEMP','TMP','APPDATA','LOCALAPPDATA','USERPROFILE'}}
        p=patch.dict(os.environ,essential,clear=True);p.start();self.addCleanup(p.stop)
        self.bot=RoleBot();self.bot.project_root=self.root;self.bot._ui_lock=threading.RLock()
        self.bot._subscription_status=Mock(return_value='member')
        self.bot._waiting_user_inputs={};self.bot._show_panel=Mock();self.bot._commerce_notice=Mock()
        self.bot._confirmations={}
        self.bot._api_call=Mock(return_value={'ok':True});self.bot._commerce=Mock()
        self.client=self.bot._commerce.return_value
        self.client.rpc.side_effect=lambda name,args:{'state':'configured','active_count':len(args['p_actor_ids'])}

    def click(self,action,actor=9):
        self.bot._handle_callback({'id':'native-owner-click','from':{'id':actor},
            'message':{'message_id':4,'chat':{'id':actor,'type':'private'}},'data':'zc:'+action})

    def grant_review(self,target='31'):
        self.click('am:add:0');waiting=self.bot._waiting_user_inputs.pop(9)
        self.bot._commerce_text_input(9,target,waiting)
        return next(iter(self.bot._admin_role_reviews))

    def test_grant_has_review_and_only_grants_locally_after_backend_acknowledgment(self):
        token=self.grant_review();self.client.rpc.assert_not_called()
        def rpc(name,args):
            self.assertEqual(name,'configure_support_actors');self.assertNotIn(31,settings()['authorized_ids'])
            return {'state':'configured','active_count':3}
        self.client.rpc.side_effect=rpc
        self.click('am:save:'+token)
        self.assertIn(31,settings()['authorized_ids'])
        self.assertEqual(self.bot._get_user_mode(31),'admin')
        self.assertEqual(self.bot._read_role_action()['state'],'done')
        data=json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(data['unrelated'],'preserve');self.assertEqual(data['payout_channels'],[-100600])

    def test_revoke_denies_old_buttons_before_backend_call_and_survives_environment_allowlist(self):
        with patch.dict(os.environ,{'ZAIN_TELEGRAM_AUTHORIZED_IDS':'21'}):
            self.click('am:remove:21');token=next(iter(self.bot._admin_role_reviews))
            def rpc(name,args):
                self.assertNotIn(21,settings()['authorized_ids'])
                return {'state':'configured','active_count':1}
            self.client.rpc.side_effect=rpc;self.click('am:save:'+token)
            self.assertNotIn(21,settings()['authorized_ids'])
            self.assertEqual(self.bot._get_user_mode(21),'customer')
            self.client.rpc.reset_mock();self.click('am:list:0',actor=21)
            self.client.rpc.assert_not_called()
            self.assertTrue(self.bot._api_call.call_args.args[1]['show_alert'])

    def test_secondary_administrator_and_customer_cannot_manage_roles(self):
        self.click('am:list:0',actor=21);self.click('am:add:0',actor=31)
        self.client.rpc.assert_not_called();self.assertEqual(self.bot._waiting_user_inputs,{})
        self.bot._admin_roles_callback('am:remove:21',31,4,{'id':'fake'})
        self.client.rpc.assert_not_called()

    def test_primary_cannot_be_removed_and_invalid_ids_are_not_reviewed(self):
        self.click('am:remove:9');self.client.rpc.assert_not_called()
        self.click('am:add:0');waiting=self.bot._waiting_user_inputs.pop(9)
        for invalid in ('-100600','@someone','0','31.5'):
            self.bot._commerce_text_input(9,invalid,waiting)
        self.assertFalse(getattr(self.bot,'_admin_role_reviews',{}))
        self.assertEqual(settings()['authorized_ids'],[9,21])

    def test_lost_grant_ack_keeps_local_role_ungranted_and_can_resume(self):
        token=self.grant_review();self.client.rpc.side_effect=CommerceUnavailable('synthetic lost ack')
        self.click('am:save:'+token)
        self.assertNotIn(31,settings()['authorized_ids'])
        self.assertEqual(self.bot._read_role_action()['state'],'pending')
        self.client.rpc.side_effect=lambda name,args:{'state':'configured','active_count':len(args['p_actor_ids'])}
        self.click('am:retry:'+token)
        self.assertIn(31,settings()['authorized_ids'])
        self.assertEqual(self.bot._read_role_action()['state'],'done')

    def test_failed_revoke_is_locally_effective_and_recovered_after_restart(self):
        self.click('am:remove:21');token=next(iter(self.bot._admin_role_reviews))
        self.client.rpc.side_effect=CommerceUnavailable('synthetic network failure')
        self.click('am:save:'+token)
        self.assertNotIn(21,settings()['authorized_ids'])
        self.client.rpc.side_effect=lambda name,args:{'state':'configured','active_count':1}
        self.bot._admin_role_reviews={};self.bot._recover_admin_role_action()
        self.assertEqual(self.bot._read_role_action()['state'],'done')
        self.assertNotIn(21,settings()['authorized_ids'])

    def test_expired_cancelled_and_missing_native_confirmations_do_not_write(self):
        token=self.grant_review()
        self.bot._admin_roles_callback('am:save:'+token,9,4,{})
        self.client.rpc.assert_not_called()
        self.bot._admin_role_reviews[token]['expires']=time.time()-1
        self.click('am:save:'+token);self.client.rpc.assert_not_called()
        token=self.grant_review();self.click('am:list:0');self.click('am:save:'+token)
        self.client.rpc.assert_not_called()

    def test_duplicate_confirmation_is_idempotent(self):
        token=self.grant_review();self.click('am:save:'+token);self.click('am:save:'+token)
        self.assertEqual(self.client.rpc.call_count,1)
        self.assertEqual(settings()['authorized_ids'],[9,21,31])

    def test_changed_admin_list_cannot_be_overwritten_by_stale_review(self):
        token=self.grant_review()
        save_admin_membership({9,21},{9,21,41},9)
        self.click('am:save:'+token);self.client.rpc.assert_not_called()
        self.assertEqual(settings()['authorized_ids'],[9,21,41])
        self.click('am:cancel:'+token)
        self.assertEqual(self.bot._read_role_action()['state'],'cancelled')
        self.assertEqual(settings()['authorized_ids'],[9,21,41])
        self.client.rpc.reset_mock()
        self.click('am:retry:'+token);self.click('am:save:'+token)
        self.client.rpc.assert_not_called()
        self.assertEqual(self.bot._read_role_action()['state'],'cancelled')

    def test_primary_and_last_admin_protected_at_storage_layer(self):
        for candidate in (set(),{21}):
            with self.assertRaises(ValueError):save_admin_membership({9,21},candidate,9)
        self.assertEqual(settings()['authorized_ids'],[9,21])

    def test_admin_home_button_and_customer_filter(self):
        from telegram_bot import views
        panel=views.home({},[],is_admin=True,mode='admin')
        self.assertIn('zc:menu:settings',str(panel.rows))
        self.assertNotIn('zc:menu:settings',str(views.customer_rows(panel.rows)))
        self.bot._dispatch_ui('admin_roles',9,4)
        self.assertIn('المديرون والصلاحيات',self.bot._show_panel.call_args.args[1].text)

    def test_role_management_button_is_hidden_from_secondary_administrator(self):
        from telegram_bot import views
        panel=views.admin_menu('settings')
        TelegramUiMixin._show_panel(self.bot,21,panel)
        self.assertNotIn('zc:admin_roles',str(self.bot._api_call.call_args.args[1]['reply_markup']))
        TelegramUiMixin._show_panel(self.bot,9,panel)
        self.assertIn('zc:admin_roles',str(self.bot._api_call.call_args.args[1]['reply_markup']))
