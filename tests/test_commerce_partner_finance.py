import time
import unittest
from unittest.mock import Mock
import test_commerce_checkout as checkout_fixture
from telegram_bot import views
from telegram_bot.ui import TelegramUiMixin
from telegram_bot.commerce.client import CommerceUnavailable

WD='21c2fb27-bce6-4c1b-841d-c08ee8839459'


class PartnerFinanceTests(unittest.TestCase):
    def setUp(self):
        self.fixture=checkout_fixture.CheckoutTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.bot=self.fixture.bot;self.client=self.fixture.client
        self.profile={'is_partner':True,'referral_code':'<CODE>','today_earned_minor':900,
            'available_minor':4500,'reserved_minor':1000,'withdrawn_minor':2000,
            'pending_commission_minor':0,'clients':3,'paid_checks':4,'collected_minor':12000,
            'awaiting_settlement_orders':2}
        self.record={'id':WD,'telegram_user_id':31,'amount_minor':1000,'destination':'<bank>', 'state':'requested'}
        self.writes=[];self.client.rpc.side_effect=self.rpc

    def rpc(self,name,args):
        if name=='partner_finance':return self.profile
        if name=='partner_withdrawals':return [self.record]
        if name=='withdrawal_record':return self.record
        if name=='list_withdrawals':return [self.record]
        self.writes.append((name,args))
        if name=='request_withdrawal':return {'withdrawal_id':WD,'state':'requested'}
        return {'state':'paid' if name=='complete_withdrawal' else 'approved'}

    def callback(self,command,user=31):
        self.bot._handle_callback({'id':'callback:1','from':{'id':user},
            'message':{'chat':{'id':user},'message_id':4},'data':'zc:'+command})

    def issue(self):
        self.callback('pf:withdraw:0')
        self.bot._commerce_text_input(31,'10.00',self.bot._waiting_user_inputs.pop(31))
        self.bot._commerce_text_input(31,'<bank>',self.bot._waiting_user_inputs.pop(31))
        return next(iter(self.bot._withdrawal_reviews))

    def test_partner_dashboard_escapes_code_and_uses_own_identity(self):
        self.callback('pf:home:0')
        self.assertEqual(self.client.rpc.call_args.args,('partner_finance',{'p_telegram_user_id':31}))
        text=self.bot._show_panel.call_args.args[1].text
        self.assertIn('&lt;CODE&gt;',text);self.assertIn('45.00',text);self.assertIn('120.00',text)

    def test_admin_summary_routes_through_real_callback_without_user_profile_links(self):
        self.client.rpc.side_effect=None
        self.client.rpc.return_value={'gross_service_minor':5000,'partner_commission_minor':1500,
            'platform_revenue_minor':3500,'today_platform_revenue_minor':3500,
            'awaiting_settlement_orders':0,'partners_total':1,'partners':[{
                'telegram_user_id':31,'referral_code':'PARTNER','earned_minor':1500,
                'available_minor':1000,'reserved_minor':0,'withdrawn_minor':500,'pending_minor':0}]}
        self.callback('pf:admin:0',user=9)
        panel=self.bot._show_panel.call_args.args[1]
        self.assertIn('حصتك بعد العمولات',panel.text)
        self.assertEqual(panel.rows[0][0]['callback_data'],'zc:pf:partner:31')
        self.assertEqual(panel.rows[0][1]['callback_data'],'zc:wa:paypartner:31')
        self.assertFalse(any('url' in item for row in panel.rows for item in row))

    def test_partner_details_are_admin_only_and_read_only(self):
        self.callback('pf:partner:31',user=9)
        self.client.rpc.assert_called_once_with('partner_finance',{'p_telegram_user_id':31})
        panel=self.bot._show_panel.call_args.args[1]
        self.assertIn('&lt;CODE&gt;',panel.text)
        self.assertIn('zc:wa:paypartner:31',str(panel.rows))
        self.assertEqual(self.writes,[])
        self.client.rpc.reset_mock()
        self.callback('pf:partner:32',user=31)
        self.client.rpc.assert_not_called()

    def test_partner_can_never_render_another_partner_details_button(self):
        rows=[[views.button('partner','pf:partner:32'),views.button('own','pf:home:0')]]
        self.assertEqual(views.customer_rows(rows),[[rows[0][1]]])

    def test_partner_status_never_grants_admin_callbacks_or_commands(self):
        self.callback('pf:home:0')
        self.assertIn(31,self.bot._known_partner_ids)
        self.assertNotIn(31,self.bot.current_authorized_ids)
        self.client.rpc.reset_mock()
        for command in ['pf:admin:0','cp:list:0','ps:settings:0','ps:list:0',
                        'ps:approve:'+WD,'pr:home:0','oa:list:0','cg:list:0',
                        'wa:list:0','cm:list:0','queue:0','pause','results']:
            with self.subTest(command=command):
                self.callback(command)
        self.assertTrue(self.bot._commerce_command('/partner_price',31,False))
        self.assertTrue(self.bot._commerce_command('/withdrawals',31,False))
        self.client.rpc.assert_not_called()
        self.assertEqual(self.writes,[])
        self.assertNotIn(31,self.bot.current_authorized_ids)

    def test_partner_sheet_buttons_route_to_own_price_and_drafts_only(self):
        self.client.rpc.side_effect=lambda name,args: (
            {'is_partner':True,'price':{'amount_minor':1500,'version':1}}
            if name=='partner_sheet_context' and args=={'p_telegram_user_id':31}
            else self.fail('Unexpected partner RPC'))
        self.bot._sheet_drafts={'own':{'chat':31,'commerce_order_id':WD,'sheet_name':'mine'},
                                'foreign':{'chat':32,'commerce_order_id':WD,'sheet_name':'other'}}
        self.callback('pc:home:0')
        self.client.rpc.assert_called_once_with('partner_sheet_context',{'p_telegram_user_id':31})
        panel=self.bot._show_panel.call_args.args[1]
        actions=[b['callback_data'] for row in panel.rows for b in row]
        self.assertIn('zc:co:review:own',actions)
        self.assertNotIn('zc:co:review:foreign',actions)
        self.assertIn('15.00',panel.text)
        self.assertFalse(any(a.startswith(('zc:ps:','zc:pr:','zc:pf:admin:')) for a in actions))

    def test_review_does_not_hold_until_confirmation_and_reuses_key(self):
        token=self.issue();self.assertEqual(self.writes,[])
        self.assertIn('&lt;bank&gt;',self.bot._show_panel.call_args.args[1].text)
        self.callback('pf:confirm:'+token);self.callback('pf:confirm:'+token)
        self.assertEqual(len(self.writes),2);self.assertEqual(self.writes[0],self.writes[1])
        self.assertEqual(self.writes[0][1]['p_amount_minor'],1000)

    def test_foreign_or_expired_review_cannot_hold(self):
        token=self.issue();self.callback('pf:confirm:'+token,user=32)
        self.bot._withdrawal_reviews[token]['expires']=time.monotonic()-1
        self.callback('pf:confirm:'+token);self.assertEqual(self.writes,[])

    def test_invalid_amount_and_destination_do_not_hold(self):
        for amount in ['0','-1','1.001','NaN']:
            self.callback('pf:withdraw:0')
            self.bot._commerce_text_input(31,amount,self.bot._waiting_user_inputs.pop(31))
        self.assertEqual(self.writes,[])

    def test_customer_cannot_list_approve_reject_or_pay(self):
        self.client.rpc.reset_mock()
        for command in ['wa:list:0','wa:review:'+WD,'wa:approve:'+WD,'wa:reject:'+WD,'wa:paid:'+WD,
                        'wa:paypartner:31','wa:payconfirm:'+WD]:
            self.callback(command)
        self.assertEqual(self.writes,[]);self.client.rpc.assert_not_called()
        self.assertTrue(self.bot._commerce_command('/withdrawals',31,False))
        self.client.rpc.assert_not_called()

    def test_admin_payout_routes_through_actual_bot_without_a_partner_request(self):
        original_rpc=self.rpc
        receipt={'id':WD,'state':'requested','recipient_telegram_id':31,'paid_actor_id':9}
        self.client.rpc.side_effect=lambda name,args: (
            {**self.record,'state':'paid','payout_reference':'bank:TX001','receipt':receipt}
            if name=='record_partner_payout' else original_rpc(name,args))
        self.callback('wa:paypartner:31',user=9)
        for message,text in enumerate(('10','<bank>','bank TX001'),20):
            waiting=self.bot._waiting_user_inputs.pop(9);waiting['_telegram_input_message_id']=message
            self.bot._commerce_text_input(9,text,waiting)
        token=next(iter(self.bot._admin_partner_payout_reviews))
        self.bot._show_panel.reset_mock();self.bot._show_panel.return_value={'ok':True}
        self.callback('wa:payconfirm:'+token,user=9)
        self.assertEqual(self.bot._show_panel.call_args_list[0].args[0],9)
        self.assertIn('حُفظ صرف الأرباح وإشعارات',self.bot._show_panel.call_args_list[0].args[1].text)
        names=[call.args[0] for call in self.client.rpc.call_args_list]
        self.assertIn('record_partner_payout',names);self.assertNotIn('request_withdrawal',names)

    def test_admin_decision_requires_authenticated_callback(self):
        self.bot._withdrawal_admin_callback('wa:approve:'+WD,9,4,{})
        self.assertEqual(self.writes,[])
        self.callback('wa:approve:'+WD,user=9)
        self.assertEqual(self.writes[0][1]['p_actor_telegram_id'],9)

    def test_payout_requires_approved_request_preview_actor_and_confirmation(self):
        self.record['state']='approved';self.callback('wa:receipt:'+WD,user=9)
        waiting=self.bot._waiting_user_inputs.pop(9);waiting['_telegram_input_message_id']=7
        self.bot._commerce_text_input(9,'bank TX001',waiting);self.assertEqual(self.writes,[])
        token=next(iter(self.bot._payout_reviews))
        self.callback('wa:paid:'+token,user=31);self.assertEqual(self.writes,[])
        self.callback('wa:paid:'+token,user=9);self.callback('wa:paid:'+token,user=9)
        self.assertEqual(self.writes[0],self.writes[1]);self.assertEqual(self.writes[0][0],'complete_withdrawal')
        self.assertEqual(self.writes[0][1]['p_proof'],'telegram-payout:9:7')

    def test_expired_and_unapproved_payout_do_not_record_transfer(self):
        self.callback('wa:receipt:'+WD,user=9);self.assertEqual(self.bot._waiting_user_inputs,{})
        self.bot._payout_reviews={WD:{'actor':9,'expires':time.monotonic()-1}}
        self.callback('wa:paid:'+WD,user=9);self.assertEqual(self.writes,[])

    def test_backend_failure_never_reports_confirmed_money(self):
        token=self.issue();self.client.rpc.side_effect=CommerceUnavailable('synthetic')
        self.callback('pf:confirm:'+token)
        self.assertIn('تعذر',self.bot._show_panel.call_args.args[1].text)

    def test_real_renderer_preserves_commerce_buttons_and_filters_admin_controls(self):
        self.bot._show_panel=TelegramUiMixin._show_panel.__get__(self.bot,type(self.bot))
        self.bot._api_call=Mock(return_value={'ok':True})
        rows=[[views.button('order','co:review:abc'),views.button('topup','cw:topup:0'),views.button('earnings','pf:home:0')],
            [views.button('claims','cm:list:0'),views.button('cash','ca:approve:abc'),views.button('withdrawals','wa:list:0')]]
        self.bot._show_panel(31,views.Panel('test',rows),4)
        actual=self.bot._api_call.call_args.args[1]['reply_markup']['inline_keyboard']
        self.assertEqual([button['callback_data'] for row in actual for button in row],
            ['zc:co:review:abc','zc:cw:topup:0','zc:pf:home:0'])

    def test_commands_route_and_non_partner_receives_no_withdrawal_prompt(self):
        self.profile={'is_partner':False}
        for command in ['/earnings','/partner_wallet','/withdraw']:
            self.assertTrue(self.bot._commerce_command(command,31,False))
        self.assertEqual(self.bot._waiting_user_inputs,{})

    def test_withdrawal_history_is_bound_to_current_user_and_escapes_destination(self):
        self.callback('pf:list:0')
        self.assertEqual(self.client.rpc.call_args.args,('partner_withdrawals',{'p_telegram_user_id':31,'p_offset':0}))
        self.assertIn('&lt;bank&gt;',self.bot._show_panel.call_args.args[1].text)

    def test_returning_to_dashboard_cancels_withdrawal_input(self):
        self.callback('pf:withdraw:0');self.assertIn(31,self.bot._waiting_user_inputs)
        self.callback('pf:home:0');self.assertNotIn(31,self.bot._waiting_user_inputs)

    def test_inactive_historical_profile_never_restores_partner_role_or_withdrawal(self):
        token=self.issue()
        self.profile.update(partnership_active=False,partnership_state='suspended',partnership_ended_at='2026-10-16T00:00:00Z')
        self.bot._known_partner_ids={31};self.bot._partner_menu_ready={31}
        self.callback('pf:confirm:'+token)
        self.assertEqual(self.writes,[])
        self.assertNotIn(31,self.bot._known_partner_ids)
        self.assertNotIn(31,self.bot._partner_menu_ready)
        self.assertNotIn(token,self.bot._withdrawal_reviews)
        self.callback('pf:withdraw:0')
        self.assertNotIn(31,self.bot._waiting_user_inputs)
        self.callback('pf:home:0')
        panel=self.bot._show_panel.call_args.args[1]
        self.assertIn('سجل المستحقات السابقة',panel.text)
        self.assertNotIn('pf:withdraw:',str(panel.rows))
        self.assertNotIn('pc:home:',str(panel.rows));self.assertNotIn('po:list:',str(panel.rows))

    def test_inflight_withdrawal_input_after_retirement_does_not_create_review(self):
        self.callback('pf:withdraw:0')
        waiting=self.bot._waiting_user_inputs.pop(31)
        self.profile['partnership_active']=False
        self.bot._commerce_text_input(31,'10',waiting)
        self.assertEqual(self.writes,[])
        self.assertNotIn(31,self.bot._waiting_user_inputs)

    def test_malformed_callback_or_boolean_user_cannot_access_earnings(self):
        self.client.rpc.reset_mock()
        self.bot._partner_finance_callback('pf:home',31,4,{})
        self.bot._partner_finance_callback('pf:home:0',True,4,{})
        self.client.rpc.assert_not_called()


if __name__=='__main__':unittest.main()
