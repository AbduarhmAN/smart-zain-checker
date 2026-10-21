import unittest
from unittest.mock import Mock
from uuid import uuid4

import test_commerce_cash_codes as fixtures
from telegram_bot import views
from telegram_bot.ui import TelegramUiMixin

PARTNER=str(uuid4())


class PartnerSheetAccessTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.CashCodeTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.bot=self.fixture.bot;self.calls=[];self.enabled=True
        self.record={'partner_id':PARTNER,'id':PARTNER,'telegram_user_id':31,'code':'PARTNER31','state':'active','enabled':True}
        self.bot._commerce_client.rpc.side_effect=self.rpc
        self.bot._api_call.return_value={'ok':True}

    def rpc(self,name,args):
        self.calls.append((name,args))
        if name=='partner_sheet_access':return dict(self.record,enabled=self.enabled)
        if name=='configure_partner_sheet_access':
            self.enabled=args['p_enabled'];return dict(self.record,enabled=self.enabled)
        if name=='partner_sheet_context':return {'is_partner':True,'partner_sheet_enabled':self.enabled}
        if name=='list_partners':return [dict(self.record,partner_sheet_enabled=self.enabled)]
        if name=='ended_partners':return []
        return self.fixture.rpc(name,args)

    def callback(self,action,user=9):self.fixture.callback('ps:'+action+':'+PARTNER,user=user)
    def panel(self):return self.bot._show_panel.call_args.args[1]

    def test_admin_can_review_and_disable_only_selected_partner(self):
        self.callback('access');self.assertIn('مسموح',self.panel().text)
        self.assertEqual(self.calls[0][0],'partner_sheet_access')
        self.callback('off');self.assertFalse(self.enabled)
        call=next(args for name,args in self.calls if name=='configure_partner_sheet_access')
        self.assertEqual(call,{'p_actor_telegram_id':9,'p_partner_id':PARTNER,'p_enabled':False,
            'p_reference':'telegram-sheet-access:9:fixture-callback'})
        self.assertIn('مخفي ومتوقف',self.panel().text)
        self.assertIn('ps:on:'+PARTNER,str(self.panel().rows))

    def test_enable_restores_native_command_and_keeps_other_partner_commands(self):
        self.callback('off')
        commands=next(call.args[1]['commands'] for call in reversed(self.bot._api_call.call_args_list) if call.args[0]=='setMyCommands')
        self.assertNotIn('partner_checks',[x['command'] for x in commands])
        self.assertIn('earnings',[x['command'] for x in commands])
        self.assertIn('partner_campaigns',[x['command'] for x in commands])
        self.callback('on')
        commands=next(call.args[1]['commands'] for call in reversed(self.bot._api_call.call_args_list) if call.args[0]=='setMyCommands')
        self.assertIn('partner_checks',[x['command'] for x in commands])

    def test_customer_cannot_read_or_change_permissions(self):
        for action in ['access','off','on']:self.callback(action,user=31)
        self.bot._partner_sheet_admin('ps:off:'+PARTNER,31,7,{'id':'forged'})
        self.assertEqual(self.calls,[]);self.assertTrue(self.enabled)

    def test_missing_receipt_or_bad_partner_id_cannot_change_setting(self):
        self.bot._partner_sheet_admin('ps:off:'+PARTNER,9,7,{})
        self.fixture.callback('ps:off:bad-id')
        self.assertEqual(self.calls,[])

    def test_stale_partner_button_and_direct_command_are_blocked(self):
        self.enabled=False
        self.fixture.callback('pc:home:0',user=31)
        self.assertIn('متوقف',self.panel().text)
        self.assertNotIn('إرسال شيت للفحص',str(self.panel().rows))
        self.bot._commerce_command('/partner_checks',31,False,message_id=7)
        self.assertIn('متوقف',self.panel().text)
        self.assertTrue(self.bot._has_active_partner_role(31))

    def test_actual_panel_hides_price_button_and_keeps_earnings_and_existing_order(self):
        self.enabled=False
        panel=views.Panel('fixture',[[views.button('فحص الشريك','pc:home:0'),views.button('أرباحي','pf:home:0')],
            [views.button('طلبي','pc:open:'+str(uuid4()))]])
        TelegramUiMixin._show_panel(self.bot,31,panel)
        rows=self.bot._api_call.call_args.args[1]['reply_markup']['inline_keyboard']
        actions=[item['callback_data'] for row in rows for item in row]
        self.assertNotIn('zc:pc:home:0',actions);self.assertIn('zc:pf:home:0',actions)
        self.assertTrue(any(item.startswith('zc:pc:open:') for item in actions))

    def test_enabled_panel_keeps_button_and_context_failure_hides_it(self):
        panel=views.Panel('fixture',[[views.button('فحص الشريك','pc:home:0')]])
        TelegramUiMixin._show_panel(self.bot,31,panel)
        self.assertIn('zc:pc:home:0',str(self.bot._api_call.call_args.args[1]))
        self.bot._commerce_client.rpc.side_effect=OSError('Synthetic unavailable context')
        TelegramUiMixin._show_panel(self.bot,31,panel)
        self.assertNotIn('zc:pc:home:0',str(self.bot._api_call.call_args.args[1]))

    def test_unfunded_old_sheet_is_blocked_but_funded_sheet_navigation_remains(self):
        state={'partner_sheet':{'state':'approved'},'partner_sheet_access_blocked':True}
        self.assertTrue(self.bot._partner_sheet_blocked(state,'fixture',31,7))
        self.assertIn('متوقف',self.panel().text)
        state['partner_sheet_access_blocked']=False
        self.assertFalse(self.bot._partner_sheet_blocked(state,'fixture',31,7))

    def test_partner_list_exposes_admin_access_control_with_valid_callback_lengths(self):
        self.bot._commerce_bot_name='fixture_bot'
        self.fixture.callback('cp:list:0')
        self.assertIn('cp:profile:'+PARTNER,str(self.panel().rows))
        self.assertNotIn('ps:access:'+PARTNER,str(self.panel().rows))
        self.fixture.callback('cp:profile:'+PARTNER)
        self.assertIn('ps:access:'+PARTNER,str(self.panel().rows))
        for row in self.panel().rows:
            for item in row:
                if 'callback_data' in item:self.assertLessEqual(len(item['callback_data'].encode()),64)

    def test_queued_price_notice_is_not_sent_after_disabling(self):
        self.enabled=False
        notice={'id':str(uuid4()),'token':str(uuid4()),'recipient_telegram_id':31,
            'payload':{'kind':'price','amount_minor':1000,'version':1}}
        original=self.rpc
        def rpc(name,args=None):
            if name=='claim_partner_sheet_notification':return notice
            if name=='complete_partner_sheet_notification':return {'saved':True}
            return original(name,args)
        self.bot._commerce_client.rpc.side_effect=rpc
        self.assertTrue(self.bot._partner_sheet_delivery_once())
        self.bot._api_call.assert_not_called()
        self.bot._commerce_client.rpc.assert_called_with('complete_partner_sheet_notification',{
            'p_id':notice['id'],'p_token':notice['token'],'p_state':'rejected','p_message_id':None})


if __name__=='__main__':unittest.main()
