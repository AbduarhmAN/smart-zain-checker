import time
import unittest
from unittest.mock import Mock,patch
import test_commerce_checkout as checkout_fixture
from telegram_bot import views
from telegram_bot.ui import TelegramUiMixin
from telegram_bot.commerce.campaigns import value_input
from telegram_bot.commerce.topup_store import TopupStore
from telegram_bot.commerce.client import CommerceUnavailable

CAMPAIGN='9a57e616-e0bc-4cd1-9bcd-6a3a79f117a1'


class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.fixture=checkout_fixture.CheckoutTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.bot=self.fixture.bot;self.client=self.fixture.client;self.policy={'version':1,'rules':{}}
        self.writes=[];self.client.rpc.side_effect=self.rpc

    def rpc(self,name,args):
        if name=='campaign_policy':return self.policy
        if name=='partner_campaign_context':return {'is_partner':False}
        if name in {'list_campaigns','customer_campaigns'}:return []
        if name in {'configure_campaign_policy','create_platform_campaign','activate_campaign','pause_campaign'}:
            self.writes.append((name,args))
            return {'campaign_id':CAMPAIGN,'state':'activated','wallet_credit_minor':3000}
        if name=='cancel_campaign_checkout':
            self.fixture.state['order_state']='cancelled';self.writes.append((name,args));return {'state':'cancelled'}
        return self.fixture.rpc(name,args)

    def callback(self,command,user=9):
        self.bot._handle_callback({'id':'fixture-callback','from':{'id':user},
            'message':{'chat':{'id':user},'message_id':4},'data':'zc:'+command})

    def text(self,value,user=9):
        waiting=self.bot._waiting_user_inputs.pop(user);waiting['_telegram_input_message_id']=123
        self.bot._commerce_text_input(user,value,waiting)

    def offer(self,kind='percent',code='HELLO',eligibility='new'):
        self.callback('oa:new:0');token=next(reversed(self.bot._campaign_reviews))
        self.callback('oa:set:'+token+':'+kind)
        if kind!='free_checks':self.text('10')
        self.callback('oa:set:'+token+':'+eligibility)
        if kind!='wallet_credit':self.callback('oa:set:'+token+':both');self.text('2')
        for value in ['100','1000','3','5',code]:self.text(value)
        return token

    def test_value_input_exact_minor_percent_and_days(self):
        self.assertEqual(value_input('30.01','money'),3001);self.assertEqual(value_input('33.33','percent'),3333)
        self.assertEqual(value_input('3','days'),259200)
        for text,kind in [('0','int'),('-1','money'),('1.001','money'),('NaN','percent'),('100.01','percent'),('1.5','days')]:
            with self.subTest(text=text),self.assertRaises(ValueError):value_input(text,kind)

    def test_offer_list_marks_actual_code_inline_without_copy_buttons(self):
        original=self.rpc
        record={'id':CAMPAIGN,'name':'A different campaign name','code':'OFFER_C5CE4ADF',
            'state':'active','activations':1,'used_discount_minor':0,'held_discount_minor':0}
        self.client.rpc.side_effect=lambda name,args:[record] if name=='list_campaigns' else original(name,args)
        self.callback('oa:list:0')
        panel=self.bot._show_panel.call_args.args[1]
        self.assertIn('<code>OFFER_C5CE4ADF</code> · active · 1 عميل',panel.text)
        self.assertNotIn('<pre>',views.copyable_message(panel.text))
        self.assertFalse(any('copy' in button.get('callback_data','') for row in panel.rows for button in row))

    def test_offer_review_code_is_inline_copyable(self):
        self.offer(code='OFFER_EXAMPLE')
        self.assertIn('<code>OFFER_EXAMPLE</code>',self.bot._show_panel.call_args.args[1].text)

    def test_actual_small_policy_explained_before_any_campaign_write(self):
        self.policy={'version':2,'rules':{'max_redemptions':30,'max_checks_per_user':1,'max_duration_seconds':259200,
            'max_wallet_credit_minor':3000,'max_percent_basis_points':3000,'max_total_discount_minor':3000,
            'max_discount_minor_per_user':3000}}
        token=self.offer();saved=dict(self.bot._campaign_reviews[token]['values'])
        self.callback('oa:save:'+token)
        self.assertEqual(self.writes,[])
        panel=self.bot._show_panel.call_args.args[1]
        self.assertIn('تشييكات العميل',panel.text);self.assertIn('الحد الحالي 1',panel.text)
        self.assertIn('ميزانية الحملة',panel.text)
        self.assertEqual(self.bot._campaign_reviews[token]['values'],saved)

    def test_fixing_policy_returns_to_existing_offer_without_retyping(self):
        self.policy={'version':2,'rules':{'max_checks_per_user':1}}
        token=self.offer();saved=dict(self.bot._campaign_reviews[token]['values'])
        self.callback('oa:save:'+token);self.callback('oa:limits:'+token)
        limit_token=next(reversed(self.bot._campaign_reviews))
        self.assertNotEqual(limit_token,token)
        for value in ['50','10','100','1000','3','30','10']:self.text(value)
        self.callback('oa:save:'+limit_token)
        self.assertEqual(self.writes[0][0],'configure_campaign_policy')
        self.assertEqual(self.bot._campaign_reviews[token]['values'],saved)
        self.policy={'version':3,'rules':self.bot._campaign_reviews[limit_token]['values']}
        self.callback('oa:save:'+token)
        self.assertEqual(self.writes[1][0],'create_platform_campaign')

    def test_lost_ack_retry_keeps_values_even_if_policy_changes(self):
        token=self.offer();original=self.client.rpc.side_effect
        attempted=[]
        def lost_once(name,args):
            if name=='create_platform_campaign':
                attempted.append(args)
                if len(attempted)==1:raise CommerceUnavailable('Synthetic lost acknowledgement')
            return original(name,args)
        self.client.rpc.side_effect=lost_once
        self.callback('oa:save:'+token)
        self.policy={'version':9,'rules':{'max_checks_per_user':1}}
        self.callback('oa:save:'+token)
        self.assertEqual(len(attempted),2);self.assertEqual(attempted[0],attempted[1])

    def test_invalid_admin_input_can_be_corrected_at_same_field(self):
        self.callback('oa:limits:0');token=next(iter(self.bot._campaign_reviews))
        self.text('NaN')
        self.assertIn(9,self.bot._waiting_user_inputs)
        self.assertEqual(self.bot._campaign_reviews[token]['step'],0)
        self.text('50');self.assertEqual(self.bot._campaign_reviews[token]['values']['max_percent_basis_points'],5000)

    def test_offer_code_can_be_generated_only_at_code_prompt(self):
        self.callback('oa:new:0');token=next(reversed(self.bot._campaign_reviews))
        self.callback('oa:set:'+token+':wallet_credit');self.text('10');self.callback('oa:set:'+token+':any')
        for value in ['100','1000','3','5']:self.text(value)
        self.callback('oa:generate:'+token,user=31)
        self.assertNotIn('code',self.bot._campaign_reviews[token]['values'])
        self.callback('oa:generate:'+token)
        self.assertRegex(self.bot._campaign_reviews[token]['values']['code'],r'^OFFER_[0-9A-F]{24}$')
        self.assertTrue(self.bot._campaign_reviews[token]['ready']);self.assertEqual(self.writes,[])

    def test_guided_policy_requires_review_and_same_admin_confirmation(self):
        self.callback('oa:limits:0');token=next(iter(self.bot._campaign_reviews))
        for value in ['50','10','100','1000','3','30','10']:self.text(value)
        self.assertEqual(self.writes,[]);self.callback('oa:save:'+token,user=31);self.assertEqual(self.writes,[])
        self.callback('oa:save:'+token)
        name,args=self.writes[0];self.assertEqual(name,'configure_campaign_policy')
        self.assertEqual(args['p_rules']['max_percent_basis_points'],5000)
        self.assertEqual(args['p_rules']['max_discount_minor_per_user'],10000)
        self.assertEqual(args['p_rules']['max_duration_seconds'],259200)

    def test_guided_offer_review_creates_immutable_key_on_replay(self):
        token=self.offer();self.assertEqual(self.writes,[])
        panel=self.bot._show_panel.call_args.args[1];self.assertIn('10%',panel.text);self.assertIn('HELLO',panel.text)
        self.callback('oa:save:'+token);self.callback('oa:save:'+token)
        self.assertEqual(self.writes[0],self.writes[1]);rules=self.writes[0][1]['p_rules']
        self.assertEqual(len(rules),9);self.assertEqual(rules['max_checks'],2);self.assertEqual(rules['check_types'],['account','service'])

    def test_wallet_grant_wizard_skips_irrelevant_check_types_and_count(self):
        token=self.offer('wallet_credit');rules=self.bot._campaign_reviews[token]['values']
        self.assertEqual(rules['max_checks'],1);self.assertEqual(rules['benefit_value'],1000)
        self.callback('oa:save:'+token);self.assertEqual(self.writes[0][0],'create_platform_campaign')

    def test_free_offer_wizard_has_no_arbitrary_price(self):
        token=self.offer('free_checks');self.callback('oa:save:'+token)
        self.assertEqual(self.writes[0][1]['p_rules']['benefit_value'],1)

    def test_expired_or_missing_authenticated_callback_cannot_create_offer(self):
        token=self.offer();self.bot._campaign_admin_callback('oa:save:'+token,9,4,{})
        self.bot._campaign_reviews[token]['expires']=time.monotonic()-1
        self.callback('oa:save:'+token);self.assertEqual(self.writes,[])

    def test_customer_cannot_configure_create_pause_or_list_admin_campaigns(self):
        self.client.rpc.reset_mock()
        for command in ['oa:list:0','oa:limits:0','oa:new:0','oa:pause:'+CAMPAIGN]:self.callback(command,user=31)
        self.bot._commerce_command('/campaigns',31,False)
        self.client.rpc.assert_called_once_with('partner_campaign_context',{'p_telegram_user_id':31})
        self.assertEqual(self.writes,[])

    def test_offer_activation_marks_wallet_owner_before_credit_and_has_native_message_reference(self):
        self.callback('of:activate:0',user=31);self.text('hello',user=31)
        self.assertTrue(self.bot._topups().is_owner(31))
        self.assertEqual(self.writes[0],('activate_campaign',{'p_telegram_user_id':31,'p_code':'HELLO','p_reference':'telegram-offer:31:123'}))

    def test_owner_checkpoint_failure_prevents_credit(self):
        with patch.object(TopupStore,'mark_owner',side_effect=OSError('synthetic disk failure')):
            self.bot._activate_offer(31,'HELLO',4,'fixture')
        self.assertEqual(self.writes,[])

    def test_direct_offer_command_requires_original_message_id(self):
        self.bot._commerce_command('/offer HELLO',31,False)
        self.assertEqual(self.writes,[])
        self.bot._commerce_command('/offer HELLO',31,False,message_id=90)
        self.assertEqual(self.writes[0][1]['p_reference'],'telegram-offer:31:90')

    def test_creation_requires_saved_administration_limits(self):
        self.policy=None;self.callback('oa:new:0')
        self.assertFalse(hasattr(self.bot,'_campaign_reviews'));self.assertEqual(self.writes,[])

    def test_real_renderer_preserves_customer_offers_and_hides_administration(self):
        self.bot._show_panel=TelegramUiMixin._show_panel.__get__(self.bot,type(self.bot));self.bot._api_call=Mock(return_value={'ok':True})
        self.bot._show_panel(31,views.Panel('test',[[views.button('offers','of:list:0'),views.button('admin','oa:list:0')]]),4)
        rows=self.bot._api_call.call_args.args[1]['reply_markup']['inline_keyboard']
        self.assertEqual([button['callback_data'] for row in rows for button in row],['zc:of:list:0'])

    def test_free_checkout_shows_discount_and_wallet_confirmation_without_invoice(self):
        self.fixture.prepare();self.fixture.state['quoted_minor']=0
        self.fixture.state['pricing_snapshot'].update(base_total_minor=6000,discount_total_minor=6000,
            applied_campaign={'name':'FREE','discounted_checks':2})
        self.fixture.prepare();panel=self.bot._show_panel.call_args.args[1]
        self.assertIn('FREE',panel.text);actions=[button.get('callback_data','') for row in panel.rows for button in row]
        self.assertFalse(any('invoice' in action or 'transfer' in action for action in actions))
        self.fixture.callback('invoice');self.assertEqual(len(self.bot.queue_service.get_jobs()),0)
        self.fixture.callback('wallet');self.assertEqual(len(self.bot.queue_service.get_jobs()),1)

    def test_cancelled_quote_cannot_enqueue(self):
        self.fixture.prepare();self.fixture.callback('cancel');self.fixture.callback('wallet')
        self.assertEqual(len(self.bot.queue_service.get_jobs()),0)
        self.assertEqual(self.writes[0][0],'cancel_campaign_checkout')

    def test_returning_to_offer_list_cancels_code_input(self):
        self.callback('of:activate:0',user=31);self.callback('of:list:0',user=31)
        self.assertNotIn(31,self.bot._waiting_user_inputs)

    def test_cancelling_administration_review_disables_old_save_button(self):
        token=self.offer();self.callback('oa:list:0');self.callback('oa:save:'+token)
        self.assertEqual(self.writes,[])


if __name__=='__main__':unittest.main()
