"""Offline authority, routing and report coverage; no financial writes or live APIs."""
import copy
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock

from telegram_bot.bot import TelegramBotRunner
from telegram_bot import views
from telegram_bot.commerce.client import CommerceUnavailable

ORDER = '20000000-0000-0000-0000-000000000001'


class CustomerActivityTests(unittest.TestCase):
    def setUp(self):
        self.bot = TelegramBotRunner.__new__(TelegramBotRunner)
        self.bot.authorized_id = 9
        self.bot.authorized_ids = {9, 21}
        self.bot._authorized_override = True
        self.bot.project_root = Path.cwd()
        self.bot._ui_lock = threading.RLock()
        self.bot._waiting_user_inputs = {}
        self.bot._confirmations = {}
        self.bot._commerce_cancel_pending = Mock()
        self.bot._show_panel = Mock()
        self.bot._api_call = Mock(return_value={'ok': True})
        self.bot._commerce_client = Mock()
        self.bot.orchestrator = Mock()
        self.page = {'kind': 'codes', 'customer_telegram_id': None, 'offset': 0, 'limit': 6,
                     'total': 8, 'customer_count': 8, 'cash_credit_minor': 1234,
                     'promotional_credit_minor': 250, 'free_account_checks': 2, 'free_service_checks': 1,
                     'items': [{'telegram_user_id': 31, 'telegram_username': '<b>customer</b>',
                                'event_count': 2, 'last_activity_at': '2026-10-19T10:00:00+00:00',
                                'cash_credit_minor': 1234, 'promotional_credit_minor': 250,
                                'free_account_checks': 2, 'free_service_checks': 1}]}
        self.bot._commerce_client.rpc.side_effect = lambda *_: copy.deepcopy(self.page)

    def panel(self):
        return self.bot._show_panel.call_args.args[1]

    def callback(self, route, user=9, chat=None):
        self.bot._handle_callback({'id': 'activity-click', 'from': {'id': user},
            'message': {'chat': {'id': user if chat is None else chat}, 'message_id': 7},
            'data': 'zc:' + route})

    def test_admin_menu_reaches_both_reports_without_financial_calls(self):
        self.callback('menu:customers')
        buttons = str(self.panel().rows)
        self.assertIn('cr:list:codes:0', buttons)
        self.assertIn('cr:list:free:0', buttons)
        self.bot._commerce_client.rpc.assert_not_called()

    def test_customer_and_partner_cannot_open_report_or_forge_details(self):
        for route in ('menu:customers', 'cr:list:codes:0', 'cr:list:free:0', 'cr:detail:codes:32:0'):
            self.callback(route, user=31)
            self.assertTrue(self.bot._api_call.call_args.args[1]['show_alert'])
        self.bot._commerce_client.rpc.assert_not_called()

    def test_different_sender_and_group_destination_cannot_read_report(self):
        for user, chat in ((31, 9), (9, -123)):
            self.callback('cr:list:codes:0', user=user, chat=chat)
            self.assertTrue(self.bot._api_call.call_args.args[1]['show_alert'])
        self.bot._commerce_client.rpc.assert_not_called()

    def test_direct_dispatch_checks_current_administrator(self):
        self.bot._dispatch_ui('cr:list:codes:0', 31, 7)
        self.bot._commerce_client.rpc.assert_not_called()
        self.assertIn('تعذر عرض', self.panel().text)

    def test_code_list_passes_actor_and_current_local_admin_exclusions(self):
        self.callback('cr:list:codes:0')
        self.bot._commerce_client.rpc.assert_called_once_with('admin_customer_activity', {
            'p_actor_telegram_id': 9, 'p_kind': 'codes', 'p_offset': 0, 'p_limit': 6,
            'p_customer_telegram_id': None, 'p_excluded_admin_ids': [9, 21]})
        self.assertIn('12.34', self.panel().text)
        self.assertIn('2.50', self.panel().text)
        self.assertIn('ليست الرصيد المتبقي', self.panel().text)
        self.assertIn('&lt;b&gt;customer&lt;/b&gt;', self.panel().text)
        self.assertIn('13:00', self.panel().text)
        self.assertIn('cr:detail:codes:31:0', str(self.panel().rows))
        self.assertIn('cr:list:codes:6', str(self.panel().rows))
        self.bot.orchestrator.get_live_status.assert_not_called()

    def test_free_details_show_zero_price_full_discount_and_promotional_sources(self):
        self.page.update(kind='free', customer_telegram_id=31, total=1, items=[{
            'telegram_user_id': 31, 'event_id': ORDER, 'occurred_at': '2026-10-19T10:00:00Z',
            'free_account_checks': 2, 'free_service_checks': 1, 'zero_price_checks': 1,
            'full_discount_checks': 1, 'promotional_checks': 1}])
        self.callback('cr:detail:free:31:0')
        text = self.panel().text
        for term in ('سعر صفر: 1', 'خصم كامل: 1', 'رصيد ترويجي كامل: 1', ORDER):
            self.assertIn(term, text)
        self.assertIn('oh:cinspect:' + ORDER, str(self.panel().rows))
        self.assertIn('cr:list:free:0', str(self.panel().rows))

    def test_code_details_show_each_code_and_credited_amounts(self):
        self.page.update(customer_telegram_id=31, total=1, items=[{
            'telegram_user_id': 31, 'code': 'CASH_31', 'occurred_at': '2026-10-19T10:00:00Z',
            'cash_credit_minor': 1234, 'promotional_credit_minor': 0}])
        self.callback('cr:detail:codes:31:0')
        self.assertIn('<code>CASH_31</code>', self.panel().text)
        self.assertIn('12.34', self.panel().text)

    def test_backend_unavailable_is_not_reported_as_zero_customers(self):
        self.bot._commerce_client.rpc.side_effect = CommerceUnavailable('Unavailable')
        self.callback('cr:list:free:0')
        self.assertIn('تعذر عرض', self.panel().text)
        self.assertNotIn('عدد العملاء: 0', self.panel().text)

    def test_foreign_customer_detail_response_is_not_displayed(self):
        self.page.update(customer_telegram_id=31)
        self.page['items'][0]['telegram_user_id'] = 32
        self.callback('cr:detail:codes:31:0')
        self.assertIn('تعذر عرض', self.panel().text)
        self.assertNotIn('customer', self.panel().text)

    def test_local_admin_in_response_is_not_displayed(self):
        self.page['items'][0]['telegram_user_id'] = 21
        self.callback('cr:list:codes:0')
        self.assertIn('تعذر عرض', self.panel().text)

    def test_malformed_routes_do_not_call_rpc(self):
        for route in ('cr:list:codes:-1', 'cr:list:fake:0', 'cr:detail:free:0:0',
                      'cr:list:codes:1000001', 'cr:wrong:codes:0', 'cr:detail:free:31'):
            self.bot._customer_activity(route, 9, 7)
        self.bot._commerce_client.rpc.assert_not_called()

    def test_report_buttons_have_valid_length_and_are_hidden_in_customer_panels(self):
        self.callback('cr:list:codes:0')
        for row in self.panel().rows:
            for button in row:
                self.assertLessEqual(len(button['callback_data'].encode()), 64)
        rows = views.customer_rows(views.admin_menu('customers').rows)
        self.assertNotIn('zc:cr:', str(rows))
        self.assertNotIn('zc:menu:', str(rows))

    def test_empty_page_retains_navigation_and_global_totals(self):
        self.page.update(items=[], total=0, customer_count=0, cash_credit_minor=0, promotional_credit_minor=0)
        self.callback('cr:list:codes:0')
        self.assertIn('لا توجد نتائج', self.panel().text)
        self.assertIn('cr:list:codes:0', str(self.panel().rows))

    def test_revoked_administrator_cannot_use_an_old_report_button(self):
        self.bot.authorized_ids = set()
        self.bot.authorized_id = 0
        self.callback('cr:list:free:0')
        self.bot._commerce_client.rpc.assert_not_called()
        self.assertTrue(self.bot._api_call.call_args.args[1]['show_alert'])


if __name__ == '__main__':
    unittest.main()
