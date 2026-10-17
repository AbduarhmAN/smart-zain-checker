"""Compact menus retain routes, state controls and role boundaries."""
import unittest
import test_telegram_ui as fixture
from telegram_bot import views

def actions(panel):return {b['callback_data'][3:] for row in panel.rows for b in row if 'callback_data' in b}

class MenuNavigationTests(unittest.TestCase):
    def test_admin_home_is_compact_and_all_management_actions_are_reachable(self):
        panel=views.home({},[],is_admin=True,mode='admin')
        self.assertEqual(len(panel.rows),5)
        self.assertTrue(all(len(row)<=3 for row in panel.rows))
        reachable=actions(panel)
        for section in ('money','partners','offers','settings'):
            reachable|=actions(views.admin_menu(section))
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
            self.assertLessEqual(len(panel.rows),7)

    def test_customer_menu_has_only_owned_customer_actions_and_short_rows(self):
        panel=views.home({},[],is_admin=False,mode='customer')
        self.assertEqual(len(panel.rows),4)
        self.assertEqual(actions(panel),{'upload','drafts:0','oh:list:0','wallet','archive','support','help'})
        self.assertEqual(panel.rows,views.customer_rows(panel.rows))
        self.assertTrue(all(len(row)<=2 for row in panel.rows))

    def test_each_section_has_home_navigation_and_valid_callback_lengths(self):
        for section in ('money','partners','offers','settings'):
            panel=views.admin_menu(section)
            self.assertIn('home',actions(panel))
            self.assertLessEqual(len(panel.rows),4)
            for row in panel.rows:
                self.assertLessEqual(len(row),2)
                for button in row:self.assertLessEqual(len(button['callback_data'].encode()),64)

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
