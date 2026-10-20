"""Compact menus retain routes, state controls and role boundaries."""
import unittest
import test_telegram_ui as fixture
from telegram_bot import views

def actions(panel):return {b['callback_data'][3:] for row in panel.rows for b in row if 'callback_data' in b}

class MenuNavigationTests(unittest.TestCase):
    def test_admin_home_is_compact_and_all_management_actions_are_reachable(self):
        panel=views.home({},[],is_admin=True,mode='admin')
        self.assertEqual(len(panel.rows),6)
        self.assertTrue(all(len(row)<=3 for row in panel.rows))
        reachable=actions(panel)
        for section in ('money','partners','offers','settings'):
            reachable|=actions(views.admin_menu(section))
        reachable|=actions(views.admin_menu('customers'))
        reachable|=actions(views.operations({},[]))
        required={'upload','status','queue:0','cp:list:0','payment_reviews','oh:admin:0','prices',
            'partner_sheet_settings','withdrawals','pf:admin:0','campaigns','cash_codes','admin_roles',
            'archive','support_inbox','help','wallet','switch_mode:customer','ps:list:0','pl:ended:0'}
        self.assertTrue(required<=reachable,required-reachable)

    def test_controls_still_follow_actual_execution_state(self):
        for state in ({'running':True},{'running':True,'paused':True},
                {'kpis':{'total':5,'completed':5,'errors':1}}, {'running':True,'verifications':['pending']}):
            expected={b['callback_data'][3:] for row in views.controls(state) for b in row}
            panel=views.home(state,[],is_admin=True,mode='admin')
            self.assertTrue(expected<=actions(panel))
            self.assertTrue(all(len(row)<=3 for row in panel.rows))
            self.assertLessEqual(len(panel.rows),8)

    def test_customer_menu_has_only_owned_customer_actions_and_short_rows(self):
        panel=views.home({},[],is_admin=False,mode='customer')
        self.assertEqual(len(panel.rows),5)
        self.assertEqual(actions(panel),{'upload','drafts:0','oh:list:0','wallet','archive','status','help'})
        self.assertEqual(panel.rows,views.customer_rows(panel.rows))
        self.assertTrue(all(len(row)<=2 for row in panel.rows))

    def test_each_section_has_home_navigation_and_valid_callback_lengths(self):
        for section in ('money','partners','offers','settings','customers'):
            panel=views.admin_menu(section)
            self.assertIn('home',actions(panel))
            self.assertLessEqual(len(panel.rows),5)
            for row in panel.rows:
                self.assertLessEqual(len(row),2)
                for button in row:
                    if 'callback_data' in button:self.assertLessEqual(len(button['callback_data'].encode()),64)
                    else:self.assertEqual(button['url'],'https://t.me/zainchecker?direct')

    def test_real_callback_routes_sections_without_backend_or_queue_actions(self):
        f=fixture.TelegramUiTests();f.setUp();self.addCleanup(f.doCleanups)
        f.bot._commerce_client.rpc.reset_mock()
        f.callback('menu:money')
        payload=f.bot._api_call.call_args.args[1]
        self.assertIn('الطلبات والمال',payload['text'])
        self.assertIn('zc:pf:admin:0',str(payload['reply_markup']))
        f.bot._commerce_client.rpc.assert_not_called()
        f.orchestrator.start_session.assert_not_called()

    def test_customer_cannot_open_admin_sections_even_with_old_callback(self):
        f=fixture.TelegramUiTests();f.setUp();self.addCleanup(f.doCleanups)
        f.callback('menu:settings',user=456,chat=456)
        self.assertTrue(f.bot._api_call.call_args.args[1]['show_alert'])
        f.bot._commerce_client.rpc.assert_not_called()
        f.bot._dispatch_ui('menu:settings',456,4)
        self.assertIn('غير مصرح',f.bot._api_call.call_args.args[1]['text'])

    def test_partner_shortcuts_remain_own_role_routes(self):
        row=views.partner_home_rows()
        self.assertEqual(len(row),3)
        self.assertEqual({b['callback_data'] for b in row},{'zc:pf:home:0','zc:po:list:0','zc:pc:home:0'})

    def test_operations_only_offer_start_for_an_idle_online_queue(self):
        pending=[{'status':'pending'}]
        self.assertIn('start_queue',actions(views.operations({},pending)))
        for state in ({'running':True},{'engine_online':False},{'stale':True},{'finalizing':True}):
            self.assertNotIn('start_queue',actions(views.operations(state,pending)))
        self.assertNotIn('start_queue',actions(views.operations({},pending+[{'status':'active'}])))
        self.assertNotIn('start_queue',actions(views.operations({},[])))

    def test_operations_preserve_pause_resume_and_hide_controls_when_status_unknown(self):
        self.assertIn('pause',actions(views.operations({'running':True},[])))
        self.assertIn('resume',actions(views.operations({'running':True,'paused':True},[])))
        for state in ({'running':True,'engine_online':False},{'running':True,'stale':True}):
            self.assertFalse({'pause','resume','start_queue','repair','results'} & actions(views.operations(state,[])))

    def test_partner_status_callback_queries_only_the_clicking_users_orders(self):
        from unittest.mock import Mock
        f=fixture.TelegramUiTests();f.setUp();self.addCleanup(f.doCleanups)
        f.bot._known_partner_ids={456}
        f.bot._uses_commerce_wallet=Mock(return_value=True)
        f.bot._commerce_client.rpc.side_effect=lambda name,args: {'items':[],'total':0}
        f.callback('status',user=456,chat=456)
        f.bot._commerce_client.rpc.assert_called_once_with('customer_order_history',
            {'p_actor_telegram_id':456,'p_offset':0,'p_limit':8,'p_admin':False})
        f.orchestrator.get_live_status.assert_not_called()
        f.queue.get_jobs.assert_not_called()
        self.assertIn('طلباتي',f.bot._api_call.call_args.args[1]['text'])

    def test_customer_status_command_and_persistent_button_reach_own_history(self):
        from unittest.mock import Mock
        f=fixture.TelegramUiTests();f.setUp();self.addCleanup(f.doCleanups)
        f.bot._refresh_partner_menu_for_chat=Mock()
        f.bot._history_callback=Mock()
        for text in ('/status','📊 حالة فحوصاتي'):
            f.bot._handle_update({'message':{'from':{'id':456},'chat':{'id':456},'text':text}})
            f.bot._history_callback.assert_called_with('oh:list:0',456,None)
        f.orchestrator.get_live_status.assert_not_called()

    def test_operations_callback_is_admin_only_and_reads_no_commerce_data(self):
        f=fixture.TelegramUiTests();f.setUp();self.addCleanup(f.doCleanups)
        f.callback('menu:operations',user=456,chat=456)
        self.assertTrue(f.bot._api_call.call_args.args[1]['show_alert'])
        f.orchestrator.get_live_status.assert_not_called()
        f.callback('menu:operations')
        self.assertIn('إدارة التشغيل',f.bot._api_call.call_args.args[1]['text'])
        f.bot._commerce_client.rpc.assert_not_called()
        f.orchestrator.pause_session.assert_not_called()

    def test_status_does_not_allow_a_different_sender_or_group_chat(self):
        from unittest.mock import Mock
        f=fixture.TelegramUiTests();f.setUp();self.addCleanup(f.doCleanups)
        f.bot._history_callback=Mock()
        for user,chat in ((456,123),(456,-123)):
            f.callback('status',user=user,chat=chat)
            self.assertTrue(f.bot._api_call.call_args.args[1]['show_alert'])
        f.bot._history_callback.assert_not_called()

    def test_admin_in_customer_mode_sees_only_their_own_status(self):
        from unittest.mock import Mock
        f=fixture.TelegramUiTests();f.setUp();self.addCleanup(f.doCleanups)
        f.bot._user_modes={'123':'customer'}
        f.bot._history_callback=Mock()
        f.callback('status')
        f.bot._history_callback.assert_called_once_with('oh:list:0',123,7)
        f.orchestrator.get_live_status.assert_not_called()

    def test_customer_status_does_not_grant_global_operations(self):
        f=fixture.TelegramUiTests();f.setUp();self.addCleanup(f.doCleanups)
        for command in ('pause','resume','queue:0','results','repair','start_queue','menu:operations'):
            f.callback(command,user=456,chat=456)
            self.assertTrue(f.bot._api_call.call_args.args[1]['show_alert'])
        f.orchestrator.get_live_status.assert_not_called()
        f.orchestrator.pause_session.assert_not_called()
        f.orchestrator.resume_session.assert_not_called()

    def test_partner_command_menu_includes_own_status_even_when_sheet_access_is_disabled(self):
        f=fixture.TelegramUiTests();f.setUp();self.addCleanup(f.doCleanups)
        f.bot._partner_sheet_access_flags={456:False}
        f.bot._set_partner_menu(456,chat_known=True)
        payload=f.bot._api_call.call_args.args[1]
        commands={item['command'] for item in payload['commands']}
        self.assertEqual(payload['scope'],{'type':'chat','chat_id':456})
        self.assertIn('status',commands)
        self.assertNotIn('partner_checks',commands)
        self.assertFalse({'control','queue','pause','resume'} & commands)

    def test_control_command_opens_operations_for_admin(self):
        from unittest.mock import Mock
        f=fixture.TelegramUiTests();f.setUp();self.addCleanup(f.doCleanups)
        f.bot._refresh_partner_menu_for_chat=Mock()
        f.bot._dispatch_ui=Mock()
        f.bot._handle_update({'message':{'from':{'id':123},'chat':{'id':123},'text':'/control'}})
        f.bot._dispatch_ui.assert_called_once_with('menu:operations',123)
