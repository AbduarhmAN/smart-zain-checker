"""Owner approval for partner discount codes; offline authorization/notice tests."""
from __future__ import annotations

import copy
from decimal import Decimal
import threading
import unittest
from unittest.mock import Mock, patch

from telegram_bot import views
from telegram_bot.commerce.client import CommerceUnavailable
from telegram_bot.commerce.partner_campaign_approvals import PartnerCampaignApprovalsMixin
from telegram_bot.commerce.partner_sheets import PartnerSheetMixin


CAMPAIGN = '9a57e616-e0bc-4cd1-9bcd-6a3a79f117a1'
CODE = 'OFFER_C5CE4ADFC998C1DFFB48B3E2'


class ApprovalHarness(PartnerCampaignApprovalsMixin, PartnerSheetMixin):
    pass


class PartnerCampaignApprovalTests(unittest.TestCase):
    def setUp(self):
        self.network = patch('urllib.request.urlopen', side_effect=AssertionError('External network forbidden'))
        self.network.start(); self.addCleanup(self.network.stop)
        self.bot = ApprovalHarness()
        self.bot.authorized_id = 9
        self.bot.current_authorized_ids = {9, 10}
        self.bot._role_owner = lambda actor: actor == 9
        self.bot._ui_lock = threading.RLock()
        self.bot._waiting_user_inputs = {}
        self.bot._show_panel = Mock()
        self.bot._commerce_notice = Mock()
        self.bot._api_call = Mock()
        self.client = Mock()
        self.bot._commerce = Mock(return_value=self.client)
        self.writes = []
        self.setting = {'required': True, 'reviewer_telegram_id': 9, 'partner_telegram_id': 31}
        self.record = {'campaign_id': CAMPAIGN, 'code': CODE, 'partner_telegram_id': 31,
            'basis_points': 3000, 'state': 'pending', 'reviewer_telegram_id': 9,
            'rules': {'benefit_kind': 'percent', 'benefit_value': 2000, 'eligibility': 'any',
                'check_types': ['account', 'service'], 'max_checks': 4000,
                'max_discount_minor': Decimal('0.001'), 'max_total_discount_minor': Decimal('1.001'),
                'duration_seconds': 259200, 'max_redemptions': 10}}
        self.client.rpc.side_effect = self.rpc

    def rpc(self, name, args=None):
        if name == 'partner_campaign_approval_setting':return copy.deepcopy(self.setting)
        if name == 'configure_partner_campaign_approval':
            self.writes.append((name, copy.deepcopy(args)))
            return {**self.setting, 'required': args['p_required']}
        if name == 'list_partner_campaign_approvals':return [copy.deepcopy(self.record)]
        if name == 'partner_campaign_approval_record':return copy.deepcopy(self.record)
        if name == 'decide_partner_campaign':
            self.writes.append((name, copy.deepcopy(args)))
            return {**copy.deepcopy(self.record), 'state': 'approved' if args['p_approve'] else 'rejected'}
        raise AssertionError('Unexpected approval RPC: ' + name)

    def callback(self, command, actor=9, callback=None):
        return self.bot._partner_campaign_approval_callback(command, actor, 4,
            {'id': 'native-review', 'from': {'id': actor}} if callback is None else callback)

    def panel(self):return self.bot._show_panel.call_args.args[1]

    def test_partner_and_delegated_administrator_cannot_read_or_change_owner_policy_or_decisions(self):
        for actor in (31, 10):
            for command in ('pa:settings:31', 'pa:require:31:1', 'pa:set:31:0', 'pa:list:0',
                            'pa:review:' + CAMPAIGN, 'pa:approve:' + CAMPAIGN, 'pa:reject:' + CAMPAIGN):
                self.callback(command, actor=actor)
        self.client.rpc.assert_not_called()
        self.assertEqual(self.writes, [])

    def test_settings_and_confirmation_are_read_only_until_native_set(self):
        self.callback('pa:settings:31')
        self.client.rpc.assert_any_call('partner_campaign_approval_setting',
            {'p_actor_telegram_id': 9, 'p_partner_telegram_id': 31})
        self.assertIn('pa:require:31:0', str(self.panel().rows))
        self.callback('pa:require:31:0')
        self.assertEqual(self.writes, [])
        self.assertIn('pa:set:31:0', str(self.panel().rows))

    def test_owner_setting_uses_native_receipt_and_partner_specific_boolean(self):
        self.callback('pa:set:31:0')
        self.assertEqual(self.writes, [('configure_partner_campaign_approval', {
            'p_actor_telegram_id': 9, 'p_partner_telegram_id': 31, 'p_required': False,
            'p_reference': 'telegram-partner-campaign-policy:9:native-review'})])
        self.callback('pa:set:31:1')
        self.assertIs(self.writes[-1][1]['p_required'], True)
        self.assertEqual(self.writes[-1][1]['p_partner_telegram_id'], 31)

    def test_setting_and_decision_require_native_callback_receipt(self):
        for receipt in ({}, {'id': ''}, {'id': '  '}):
            for command in ('pa:set:31:1', 'pa:approve:' + CAMPAIGN, 'pa:reject:' + CAMPAIGN):
                self.callback(command, callback=receipt)
        self.assertEqual(self.writes, [])

    def test_malformed_policy_or_campaign_identifiers_do_not_mutate(self):
        for command in ('pa:set:31:yes', 'pa:set:-1:1', 'pa:set:31:2', 'pa:set:31',
                        'pa:approve:not-a-uuid', 'pa:reject:../campaign', 'pa:review:bad'):
            self.callback(command)
        self.assertEqual(self.writes, [])

    def test_pending_list_and_full_review_display_copyable_code_and_exact_contract(self):
        self.callback('pa:list:0')
        self.client.rpc.assert_any_call('list_partner_campaign_approvals', {'p_actor_telegram_id': 9, 'p_offset': 0})
        self.assertIn(CODE, self.panel().text)
        self.assertIn('pa:review:' + CAMPAIGN, str(self.panel().rows))
        self.callback('pa:review:' + CAMPAIGN)
        self.client.rpc.assert_any_call('partner_campaign_approval_record',
            {'p_actor_telegram_id': 9, 'p_campaign_id': CAMPAIGN})
        text, rows = self.panel().text, self.panel().rows
        text = text.replace(',', '').replace('٬', '')
        for value in ('<code>' + CODE + '</code>', '31', '20%', '30%', '70%', '10%', '4000', '0.00001', '0.01001', '3', '10'):
            self.assertIn(value, text)
        self.assertIn('pa:approve:' + CAMPAIGN, str(rows))
        self.assertIn('pa:reject:' + CAMPAIGN, str(rows))
        for row in rows:
            for button in row:self.assertLessEqual(len(button.get('callback_data', '').encode()), 64)

    def test_approved_and_rejected_decisions_use_exact_actor_campaign_and_receipt(self):
        for approve, action in ((True, 'approve'), (False, 'reject')):
            self.callback('pa:' + action + ':' + CAMPAIGN)
            self.assertEqual(self.writes[-1], ('decide_partner_campaign', {
                'p_actor_telegram_id': 9, 'p_campaign_id': CAMPAIGN, 'p_approve': approve,
                'p_reference': 'telegram-partner-campaign-decision:9:native-review'}))

    def test_unknown_or_lost_setting_ack_never_claims_saved_success(self):
        original = self.rpc
        for result in ({}, {'state': 'unknown'}, CommerceUnavailable('Fixture lost acknowledgement')):
            self.bot._show_panel.reset_mock(); self.bot._commerce_notice.reset_mock()
            def rpc(name, args=None, result=result):
                if name != 'configure_partner_campaign_approval':return original(name, args)
                if isinstance(result, Exception):raise result
                return result
            self.client.rpc.side_effect = rpc
            self.callback('pa:set:31:1')
            text = (self.panel().text if self.bot._show_panel.call_args else '') + str(self.bot._commerce_notice.call_args)
            self.assertNotIn('تم حفظ', text)
            self.assertNotIn('حُفظ', text)

    def test_unknown_or_lost_decision_ack_never_claims_approval(self):
        original = self.rpc
        for result in ({}, {'state': 'pending'}, CommerceUnavailable('Fixture lost acknowledgement')):
            self.bot._show_panel.reset_mock(); self.bot._commerce_notice.reset_mock()
            def rpc(name, args=None, result=result):
                if name != 'decide_partner_campaign':return original(name, args)
                if isinstance(result, Exception):raise result
                return result
            self.client.rpc.side_effect = rpc
            self.callback('pa:approve:' + CAMPAIGN)
            text = (self.panel().text if self.bot._show_panel.call_args else '') + str(self.bot._commerce_notice.call_args)
            self.assertNotIn('تمت الموافقة', text)
            self.assertNotIn('تم اعتماد', text)

    def test_missing_durable_campaign_record_cannot_be_decided(self):
        original = self.rpc
        self.client.rpc.side_effect = lambda name, args=None: None if name == 'partner_campaign_approval_record' else original(name, args)
        self.callback('pa:approve:' + CAMPAIGN)
        self.assertEqual(self.writes, [])

    def notice(self, kind, state='pending', recipient=9):
        return {'id': 'fixture-notice', 'token': 'fixture-lease', 'recipient_telegram_id': recipient,
                'payload': {'kind': kind, 'campaign_id': CAMPAIGN, 'code': CODE,
                            'partner_telegram_id': 31, 'state': state}}

    def test_campaign_request_notice_fetches_full_record_for_owner_and_exposes_review(self):
        text, rows = self.bot._partner_campaign_notice(self.notice('campaign_request'))
        self.client.rpc.assert_any_call('partner_campaign_approval_record',
            {'p_actor_telegram_id': 9, 'p_campaign_id': CAMPAIGN})
        self.assertIn('<code>' + CODE + '</code>', text)
        self.assertIn('pa:approve:' + CAMPAIGN, str(rows))
        self.assertIn('pa:reject:' + CAMPAIGN, str(rows))
        self.assertIn('0.00001', text)

    def test_partner_decision_notice_contains_no_owner_privileged_actions(self):
        for state in ('approved', 'rejected'):
            text, rows = self.bot._partner_campaign_notice(self.notice('campaign_decision', state, recipient=31))
            self.assertIn('<code>' + CODE + '</code>', text)
            self.assertNotIn('zc:pa:', str(rows))

    def test_embedded_claim_snapshot_renders_owner_review_without_an_extra_rpc(self):
        notice = self.notice('campaign_request')
        notice['payload']['approval_record'] = copy.deepcopy(self.record)
        self.client.rpc.side_effect = AssertionError('Embedded claim must not issue a second database RPC')
        text, rows = self.bot._partner_campaign_notice(notice)
        self.client.rpc.assert_not_called()
        self.assertIn('<code>' + CODE + '</code>', text)
        self.assertIn('0.00001', text)
        self.assertIn('pa:approve:' + CAMPAIGN, str(rows))

    def test_mismatched_embedded_claim_identity_is_never_rendered_or_sent(self):
        differences = {'campaign_id': '0364662b-eb5b-4915-bc4e-577bd7a72863',
                       'reviewer_telegram_id': 10, 'code': 'OTHER_CODE', 'partner_telegram_id': 32}
        for field, value in differences.items():
            with self.subTest(field=field):
                notice = self.notice('campaign_request')
                notice['payload']['approval_record'] = {**copy.deepcopy(self.record), field: value}
                self.client.rpc.reset_mock(); self.bot._api_call.reset_mock()
                self.client.rpc.side_effect = AssertionError('Invalid embedded identity must not fall back to RPC')
                with self.assertRaises(ValueError):self.bot._partner_campaign_notice(notice)
                self.client.rpc.assert_not_called()
                self.bot._api_call.assert_not_called()

    def test_repeated_final_decision_buttons_show_durable_state_without_another_decision_rpc(self):
        for state in ('approved', 'rejected'):
            for action in ('approve', 'reject'):
                with self.subTest(state=state, action=action):
                    self.record['state'] = state
                    self.bot._show_panel.reset_mock()
                    self.callback('pa:' + action + ':' + CAMPAIGN)
                    self.assertEqual(self.writes, [])
                    self.assertIsNotNone(self.bot._show_panel.call_args)
                    self.assertIn(CODE, self.panel().text)
                    self.assertNotIn('zc:pa:approve:', str(self.panel().rows))
                    self.assertNotIn('zc:pa:reject:', str(self.panel().rows))

    def test_campaign_notice_reuses_durable_claim_and_acknowledges_only_verified_recipient(self):
        record = self.notice('campaign_decision', 'approved', recipient=31)
        calls = []
        def rpc(name, args=None):
            calls.append((name, args))
            if name == 'claim_partner_sheet_notification':return record
            if name == 'complete_partner_sheet_notification':return {'state': args['p_state']}
            return self.rpc(name, args)
        self.client.rpc.side_effect = rpc
        self.bot._api_call.return_value = {'ok': True, 'result': {'message_id': 77, 'chat': {'id': 31}}}
        self.assertTrue(self.bot._partner_sheet_delivery_once())
        self.assertEqual(calls[-1], ('complete_partner_sheet_notification', {
            'p_id': 'fixture-notice', 'p_token': 'fixture-lease', 'p_state': 'sent', 'p_message_id': 77}))
        self.bot._api_call.return_value = {'ok': True, 'result': {'message_id': 78, 'chat': {'id': 99}}}
        self.bot._partner_sheet_delivery_once()
        self.assertEqual(calls[-1][1]['p_state'], 'uncertain')
        self.assertIsNone(calls[-1][1]['p_message_id'])


class PartnerCampaignPendingWizardTests(unittest.TestCase):
    def setUp(self):
        from test_commerce_partner_campaigns import PartnerCampaignTests
        self.fixture = PartnerCampaignTests()
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.bot, self.client = self.fixture.bot, self.fixture.client
        self.fixture.context['approval_required'] = True
        original = self.fixture.rpc
        def rpc(name, args):
            result = original(name, args)
            return {**result, 'approval_state': 'pending', 'state': 'pending_approval'} if name == 'create_partner_campaign' else result
        self.client.rpc.side_effect = rpc

    def test_partner_wizard_submits_for_review_without_claiming_activation(self):
        token = self.fixture.offer()
        panel = self.bot._show_panel.call_args.args[1]
        self.assertIn('إرسال للموافقة', str(panel.rows))
        self.assertNotIn('إنشاء وتفعيل', str(panel.rows))
        self.fixture.callback('po:save:' + token)
        text = self.bot._show_panel.call_args.args[1].text
        self.assertIn('موافقة', text)
        self.assertIn('بانتظار', text)
        self.assertNotIn('تم تفعيل', text)
        self.assertNotIn('أُنشئ العرض وتفعّل', text)
        self.assertFalse(any(call.args[0] == 'activate_campaign' for call in self.client.rpc.call_args_list))

    def test_approval_callback_is_native_owner_only_and_cannot_collide_with_pricing_prefix(self):
        self.bot._role_owner = lambda actor: actor == 9
        self.bot._partner_campaign_approval_callback = Mock()
        self.fixture.fixture.callback('pa:settings:31', user=9)
        self.bot._partner_campaign_approval_callback.assert_called_once()
        self.assertEqual(self.bot._partner_campaign_approval_callback.call_args.args[:3], ('pa:settings:31', 9, 4))
        self.bot._partner_campaign_approval_callback.reset_mock()
        self.fixture.fixture.callback('pa:settings:31', user=31)
        self.bot._partner_campaign_approval_callback.assert_not_called()
        self.assertFalse(any(button.get('callback_data', '').startswith('zc:pa:')
            for row in views.customer_rows([[views.button('سياسة موافقة الأكواد', 'pa:settings:31')]]) for button in row))


if __name__ == '__main__':unittest.main()
