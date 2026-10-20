import threading
import unittest
from unittest.mock import Mock
from telegram_bot.commerce.partner_roles import active_partner,PartnerRoleMixin
from telegram_bot.commerce.partner_finance import PartnerFinanceMixin

class PartnerRoleRevocationTests(unittest.TestCase):
    def setUp(self):
        self.bot=PartnerRoleMixin();self.bot._ui_lock=threading.RLock()
        self.bot.current_authorized_ids={9};self.bot._known_partner_ids={31}
        self.bot._partner_menu_ready={31};self.bot._waiting_user_inputs={31:{'action':'customer_upload'}}
        self.bot._commerce=Mock();self.bot._api_call=Mock()

    def test_retained_record_is_not_an_active_role(self):
        for profile in ({'is_partner':False},{'is_partner':True,'partnership_active':False},
            {'is_partner':True,'state':'suspended'},{'is_partner':True,'partnership_ended_at':'2026-10-16'}):
            self.assertFalse(active_partner(profile))
        self.assertTrue(active_partner({'is_partner':True,'partnership_active':True}))

    def test_cached_role_cannot_override_current_database_state(self):
        self.bot._commerce.return_value.rpc.return_value={'is_partner':False}
        self.assertFalse(self.bot._has_active_partner_role(31))
        self.assertNotIn(31,self.bot._known_partner_ids)
        self.assertNotIn(31,self.bot._partner_menu_ready)
        self.assertEqual(self.bot._waiting_user_inputs[31],{'action':'customer_upload'})

    def test_role_verification_network_failure_denies_features(self):
        self.bot._commerce.return_value.rpc.side_effect=OSError('offline')
        self.assertFalse(self.bot._has_active_partner_role(31))

    def test_ending_partner_role_does_not_remove_separate_admin_menu(self):
        self.bot._reset_customer_menu(9)
        self.bot._api_call.assert_not_called()

    def test_customer_menu_failure_is_retained_for_retry(self):
        self.bot._api_call.side_effect=TimeoutError('offline')
        with self.assertRaises(TimeoutError):self.bot._reset_customer_menu(31)
        self.assertIn(31,self.bot._customer_menu_pending)
        self.bot._api_call.side_effect=None;self.bot._api_call.return_value={'ok':True}
        self.bot._reset_customer_menu(31)
        self.assertNotIn(31,self.bot._customer_menu_pending)

    def test_restart_reconciles_persisted_ended_partner_commands_without_cached_role(self):
        bot=PartnerFinanceMixin();bot.current_authorized_ids={9};bot._ui_lock=threading.RLock()
        bot._waiting_user_inputs={};bot._known_partner_ids=set();bot._api_call=Mock(return_value={'ok':True})
        bot._commerce=Mock()
        bot._commerce.return_value.rpc.side_effect=lambda name,args: [] if name=='list_partners' else [{'telegram_user_id':31}]
        bot._sync_partner_menus()
        bot._api_call.assert_called_once_with('deleteMyCommands',{'scope':{'type':'chat','chat_id':31}})
        self.assertEqual(bot._known_partner_ids,set())

    def test_menu_sync_does_not_grant_role_to_suspended_or_unconfigured_partners(self):
        bot=PartnerFinanceMixin();bot.current_authorized_ids={9};bot._ui_lock=threading.RLock()
        bot._waiting_user_inputs={};bot._api_call=Mock();bot._commerce=Mock()
        records=[{'telegram_user_id':31,'state':'suspended'},{'telegram_user_id':32,'state':'draft'},
            {'telegram_user_id':33,'state':'active'}]
        bot._commerce.return_value.rpc.side_effect=lambda name,args:records if name=='list_partners' else []
        bot._sync_partner_menus()
        self.assertEqual(bot._known_partner_ids,{33})
        self.assertEqual(bot._api_call.call_args_list[0].args,('getChat',{'chat_id':33}))
