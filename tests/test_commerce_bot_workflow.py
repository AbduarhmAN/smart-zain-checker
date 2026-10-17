"""Exercise real message/callback routing without Telegram or file mutation."""
import threading
import time
import unittest
from unittest.mock import Mock,patch
from telegram_bot.bot import TelegramBotRunner
from telegram_bot import views

PARTNER = 'deae426e-dc0c-4524-9977-1501d8d2b8ea'


class BotWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.bot = TelegramBotRunner.__new__(TelegramBotRunner)
        self.bot._ui_lock = threading.RLock()
        self.bot._waiting_user_inputs = {}
        self.bot.authorized_ids = {123}
        self.bot.authorized_id = 123
        self.bot._show_panel = Mock()
        self.bot._reply = Mock()
        self.bot._api_call = Mock(return_value={'ok':True,'result':{'username':'ZainDemoBot'}})
        self.bot._commerce_client = Mock()
        self.bot._commerce_client.rpc.return_value = {'status':'attributed'}
        self.bot._dispatch_ui = Mock()
        self.settings_patch = patch('telegram_bot.bot.telegram_settings',return_value={'authorized_ids':[123]})
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)

    def message(self,text,user=456,chat=None):
        self.bot._handle_update({'message':{'from':{'id':user},
            'chat':{'id':user if chat is None else chat,'type':'private'},'text':text}})

    def click(self,action,user=123,chat=None):
        self.bot._handle_update({'callback_query':{'id':'fixture','from':{'id':user},
            'message':{'chat':{'id':user if chat is None else chat},'message_id':7},'data':'zc:'+action}})

    def wizard(self,mode):
        self.click('cp:terms:'+PARTNER)
        self.message('30',123)
        token = next(iter(self.bot._commerce_partner_drafts))
        self.click('cp:mode:'+token+':'+mode)
        return token

    def test_deep_link_and_manual_command_route_to_same_rpc(self):
        self.message('/start ref_PARTNER1')
        self.assertEqual(self.bot._commerce_client.rpc.call_args.args,
                         ('attribute_referral',{'p_telegram_user_id':456,'p_code':'PARTNER1','p_source':'deep_link'}))
        self.message('/ref partner1')
        self.assertEqual(self.bot._commerce_client.rpc.call_args.args,
                         ('attribute_referral',{'p_telegram_user_id':456,'p_code':'PARTNER1','p_source':'manual'}))

    def test_old_referral_button_redirects_customer_without_exposing_profile(self):
        self.click('referral',456)
        self.bot._commerce_client.rpc.assert_not_called()
        self.bot._dispatch_ui.assert_called_once_with('home',456,7)
        self.assertNotIn(456,self.bot._waiting_user_inputs)

    def test_deep_link_attributes_silently_and_opens_home(self):
        self.message('/start ref_PARTNER1')
        self.bot._show_panel.assert_not_called()
        self.bot._dispatch_ui.assert_called_once_with('home',456,None)

    def test_customer_code_confirmation_hides_internal_source(self):
        self.message('/ref PARTNER1')
        text=self.bot._show_panel.call_args.args[1].text
        self.assertIn('تم قبول الكود',text);self.assertNotIn('الإحالة',text)

    def test_private_identity_and_admin_actions_fail_closed(self):
        self.message('/partner_add 999 ABC',456)
        self.bot._commerce_client.rpc.assert_not_called()
        self.click('cp:terms:'+PARTNER,456)
        self.bot._commerce_client.rpc.assert_not_called()
        self.message('/ref ABC',456,123)
        self.bot._commerce_client.rpc.assert_not_called()

    def test_admin_adds_pending_partner(self):
        self.bot._commerce_client.rpc.return_value = {'partner_id':PARTNER,'code':'ABC','state':'pending'}
        self.message('/partner_add 999 ABC',123)
        self.assertEqual(self.bot._commerce_client.rpc.call_args.args,
                     ('add_partner_with_notice',{'p_actor_telegram_id':123,'p_telegram_user_id':999,'p_code':'ABC'}))

    def test_each_entitlement_mode_needs_review_before_saving(self):
        for mode,value,limit in [('checks','100',100),('duration','14',14*86400),('forever',None,None)]:
            with self.subTest(mode=mode):
                self.bot._commerce_client.rpc.reset_mock()
                token = self.wizard(mode)
                if value:
                    self.click('cp:save:'+token)
                    self.bot._commerce_client.rpc.assert_not_called()
                    self.message(value,123)
                self.click('cp:save:'+token)
                self.assertEqual(self.bot._commerce_client.rpc.call_args.args,
                    ('configure_partner',{'p_partner_id':PARTNER,'p_actor_telegram_id':123,
                        'p_basis_points':3000,'p_mode':mode,'p_limit':limit,'p_activate':True}))

    def test_cancel_invalidates_old_save_button(self):
        token = self.wizard('forever')
        self.bot._commerce_client.rpc.return_value = []
        self.click('cp:list:0')
        self.bot._commerce_client.rpc.reset_mock()
        self.click('cp:save:'+token)
        self.bot._commerce_client.rpc.assert_not_called()

    def test_activating_partner_refreshes_role_menu_after_confirmed_save(self):
        token=self.wizard('forever');self.bot._sync_partner_menus=Mock()
        self.bot._commerce_client.rpc.return_value={'state':'active','partner_id':PARTNER}
        self.click('cp:save:'+token)
        self.bot._sync_partner_menus.assert_called_once_with()

    def test_expired_and_foreign_drafts_cannot_save(self):
        token = self.wizard('forever')
        self.bot._commerce_partner_drafts[token]['expires'] = time.monotonic()-1
        self.click('cp:save:'+token)
        self.bot._commerce_client.rpc.assert_not_called()
        self.bot._commerce_partner_drafts[token]['expires'] = time.monotonic()+300
        self.bot._commerce_partner_drafts[token]['chat'] = 456
        self.click('cp:save:'+token)
        self.bot._commerce_client.rpc.assert_not_called()

    def test_buttons_fit_telegram_callback_limit_and_customer_filter(self):
        token = self.wizard('forever')
        for call in self.bot._show_panel.call_args_list:
            for row in call.args[1].rows:
                for button in row:
                    self.assertLessEqual(len(button['callback_data'].encode('utf-8')),64)
        rows = views.customer_rows([[views.button('ref','referral'),views.button('admin','cp:list:0')]])
        self.assertEqual([b['callback_data'] for r in rows for b in r],['zc:home'])

    def test_start_deep_link_receipt_routes_to_receipt_panel(self):
        wid = 'a84549f8-4f0f-404d-b2e4-48d8928621ce'
        self.bot._commerce_client.rpc.return_value = {
            'id': wid, 'state': 'paid', 'amount_minor': 600,
            'payout_reference': 'cash:test', 'telegram_user_id': 456,
            'receipt': {'id': 'f58e3bc0-f123-4f2e-99fd-f38b6287e78f', 'state': 'requested'}
        }
        self.message(f'/start ack_{wid}', user=456)
        self.assertEqual(self.bot._commerce_client.rpc.call_args.args,
                         ('partner_withdrawal_receipt', {'p_withdrawal_id': wid, 'p_telegram_user_id': 456}))
        self.bot._show_panel.assert_called_once()

    def test_partner_add_resolves_account_id(self):
        self.bot._commerce_client.rpc.return_value = {
            'partner_id': PARTNER, 'code': 'NEWPARTNER', 'state': 'pending'
        }
        with patch('telegram_bot.payment_manager.PAYMENT_MGR.find_user_by_account_id', return_value=(999888, {})):
            self.message('/partner_add 351234839 NEWPARTNER', user=123)
            self.assertEqual(self.bot._commerce_client.rpc.call_args.args,
                             ('add_partner_with_notice', {'p_actor_telegram_id': 123, 'p_telegram_user_id': 999888, 'p_code': 'NEWPARTNER'}))


if __name__ == '__main__': unittest.main()
