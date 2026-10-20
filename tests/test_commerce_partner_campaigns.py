import time
import unittest
from unittest.mock import Mock
import test_commerce_campaigns as campaign_fixture
from telegram_bot import views
CAMPAIGN=campaign_fixture.CAMPAIGN

class PartnerCampaignTests(unittest.TestCase):
    def setUp(self):
        self.fixture=campaign_fixture.CampaignTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.bot=self.fixture.bot;self.client=self.fixture.client
        self.context={'is_partner':True,'state':'active','basis_points':3000,'policy':{'version':1,'rules':{
            'max_percent_basis_points':5000,'max_checks_per_user':4000,'max_discount_minor_per_user':10000,
            'max_total_discount_minor':100000,'max_duration_seconds':259200,'max_wallet_credit_minor':10000,'max_redemptions':100}}}
        self.writes=[];self.client.rpc.side_effect=self.rpc
    def rpc(self,name,args):
        if name=='ended_partners':return []
        if name=='partner_campaign_context':return self.context
        if name=='campaign_policy':return self.context['policy']
        if name=='list_partner_campaigns':return []
        if name in {'create_partner_campaign','pause_partner_campaign'}:
            self.writes.append((name,args));return {'campaign_id':CAMPAIGN,'replayed':bool(len(self.writes)>1)}
        return self.fixture.rpc(name,args)
    def callback(self,command,user=31):self.fixture.callback(command,user=user)
    def text(self,value,user=31):self.fixture.text(value,user=user)
    def offer(self,discount='20'):
        self.callback('po:new:0');token=next(iter(self.bot._campaign_reviews))
        self.text(discount);self.callback('po:set:'+token+':any');self.callback('po:set:'+token+':both')
        for value in ['100','100','1000','3','5','SHARE20']:self.text(value)
        return token
    def test_partner_campaign_code_is_copyable_inline(self):
        original=self.rpc
        record={'id':CAMPAIGN,'name':'campaign','code':'SHARE20','state':'active',
            'activations':1,'used_discount_minor':0,'held_discount_minor':0}
        self.client.rpc.side_effect=lambda name,args:[record] if name=='list_partner_campaigns' else original(name,args)
        self.callback('po:list:0')
        self.assertIn('<code>SHARE20</code> · active · 1 عميل',self.bot._show_panel.call_args.args[1].text)

    def test_zero_balance_partner_can_review_and_save_from_current_order_share(self):
        token=self.offer();panel=self.bot._show_panel.call_args.args[1]
        self.assertIn('المتبقي لك: 10%',panel.text);self.assertIn('لا يحتاج أرباحًا سابقة',panel.text)
        self.assertTrue(all(b['callback_data'].startswith('zc:po:') for row in panel.rows for b in row))
        self.assertEqual(self.writes,[])
        self.callback('po:save:'+token);self.callback('po:save:'+token)
        self.assertEqual(self.writes[0][0],'create_partner_campaign')
        args=self.writes[0][1];self.assertEqual(args['p_telegram_user_id'],31);self.assertEqual(args['p_rules']['benefit_value'],2000)
        self.assertEqual(self.writes[1][1],args)
        self.assertFalse(any(call.args[0] in {'request_withdrawal','wallet_state','create_platform_campaign'} for call in self.client.rpc.call_args_list))
    def test_over_share_is_explained_and_never_submitted(self):
        token=self.offer('31');self.callback('po:save:'+token)
        self.assertEqual(self.writes,[]);self.assertIn('حصتك المصرح بها',self.bot._show_panel.call_args.args[1].text)
        self.assertFalse(any(b['callback_data'].startswith('zc:oa:') for row in self.bot._show_panel.call_args.args[1].rows for b in row))
    def test_nonpartner_cannot_enter_wizard_or_change_policy(self):
        self.context={'is_partner':False}
        for command in ['po:new:0','po:limits:0','po:list:0','po:pause:'+CAMPAIGN]:self.callback(command)
        self.assertFalse(getattr(self.bot,'_campaign_reviews',{}));self.assertEqual(self.writes,[])
    def test_partner_cannot_use_admin_callback_or_another_review(self):
        token=self.offer();self.callback('oa:save:'+token);self.callback('po:save:'+token,user=32)
        self.assertEqual(self.writes,[])
    def test_partner_cannot_select_platform_credit_kind(self):
        self.callback('po:new:0');token=next(iter(self.bot._campaign_reviews))
        self.callback('po:set:'+token+':wallet_credit')
        self.assertEqual(self.bot._campaign_reviews[token]['values']['benefit_kind'],'percent')
        self.assertEqual(self.writes,[])
    def test_partner_menu_exposes_only_own_list_and_approved_limits(self):
        self.callback('po:list:0');panel=self.bot._show_panel.call_args.args[1]
        self.assertIn('30%',panel.text);self.assertIn('حدود الإدارة',panel.text)
        self.client.rpc.assert_any_call('list_partner_campaigns',{'p_offset':0,'p_telegram_user_id':31})
        self.assertTrue(any(b['callback_data']=='zc:po:new:0' for row in views.customer_rows(panel.rows) for b in row))
    def test_pause_uses_authenticated_partner_and_not_admin_rpc(self):
        self.callback('po:pause:'+CAMPAIGN)
        self.assertEqual(self.writes[0][0],'pause_partner_campaign');self.assertEqual(self.writes[0][1]['p_telegram_user_id'],31)
    def test_expired_review_does_not_submit(self):
        token=self.offer();self.bot._campaign_reviews[token]['expires']=time.monotonic()-1;self.callback('po:save:'+token)
        self.assertEqual(self.writes,[])
    def test_native_partner_menu_is_scoped_to_partner_chat_only(self):
        self.client.rpc.side_effect=lambda name,args:[{'telegram_user_id':31,'state':'active'}] if name=='list_partners' else self.rpc(name,args)
        self.bot._api_call=Mock(return_value={'ok':True})
        self.bot._sync_partner_menus()
        self.assertEqual(self.bot._known_partner_ids,{31})
        self.assertEqual(self.bot._api_call.call_count,2)
        self.assertEqual(self.bot._api_call.call_args_list[0].args,('getChat',{'chat_id':31}))
        method,payload=self.bot._api_call.call_args.args
        self.assertEqual(method,'setMyCommands');self.assertEqual(payload['scope'],{'type':'chat','chat_id':31})
        self.assertIn('partner_campaigns',[item['command'] for item in payload['commands']])

    def test_unreachable_partner_menu_is_deferred_until_private_chat_is_known(self):
        self.client.rpc.side_effect=lambda name,args:([{'telegram_user_id':31,'state':'active'}] if name=='list_partners' else
            {'is_partner':True} if name=='partner_sheet_context' else self.rpc(name,args))
        self.bot._api_call=Mock(return_value={'ok':False,'error_code':400,'description':'Bad Request: chat not found'})
        self.bot._sync_partner_menus()
        self.bot._api_call.assert_called_once_with('getChat',{'chat_id':31})
        self.assertEqual(self.bot._known_partner_ids,{31})
        self.bot._api_call.reset_mock();self.bot._api_call.return_value={'ok':True}
        self.bot._refresh_partner_menu_for_chat(31)
        self.assertEqual(self.bot._api_call.call_args.args[0],'setMyCommands')
        self.bot._refresh_partner_menu_for_chat(31)
        self.assertEqual(self.bot._api_call.call_count,1)
        self.bot._refresh_partner_menu_for_chat(32)
        self.assertEqual(self.bot._api_call.call_count,1)

    def test_suspended_partner_cannot_use_old_campaign_buttons_or_input_review(self):
        token=self.offer()
        self.context['state']='suspended'
        self.bot._known_partner_ids={31}
        self.callback('po:save:'+token)
        self.assertEqual(self.writes,[])
        self.assertNotIn(token,self.bot._campaign_reviews)
        self.assertNotIn(31,self.bot._known_partner_ids)
        self.client.rpc.reset_mock()
        for action in ('list:0','new:0','pause:'+CAMPAIGN):self.callback('po:'+action)
        names=[call.args[0] for call in self.client.rpc.call_args_list]
        self.assertEqual(set(names),{'partner_campaign_context'})
    def test_customer_checkout_discloses_hold_and_estimate_without_partner_share(self):
        fixture=self.fixture.fixture;fixture.prepare()
        fixture.state['pricing_snapshot'].update(partner_future_offer=True,discount_total_minor=2000,base_total_minor=10000,
            estimated_final_minor=8000,applied_campaign={'name':'SAVE20','discounted_checks':2})
        fixture.state['quoted_minor']=10000
        from test_commerce_checkout import TOKEN
        fixture.bot._commerce_start_checkout(TOKEN,31,4)
        text=fixture.bot._show_panel.call_args.args[1].text
        self.assertIn('100.00',text);self.assertIn('80.00',text);self.assertIn('مؤقتًا',text)
        self.assertNotIn('الإحالة',text);self.assertNotIn('حصة الشريك',text)

if __name__=='__main__':unittest.main()
