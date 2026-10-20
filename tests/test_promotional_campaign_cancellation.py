import unittest
from unittest.mock import Mock

import test_commerce_campaigns as fixtures
from telegram_bot.commerce.client import CommerceUnavailable


class PromotionalCancellationTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.CampaignTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.bot=self.fixture.bot;self.client=self.fixture.client;self.calls=[]
        self.record={'campaign_id':fixtures.CAMPAIGN,'id':fixtures.CAMPAIGN,'code':'PROMO100','name':'PROMO100',
            'state':'active','cancelled':False,'funder_kind':'platform','benefit_kind':'wallet_credit',
            'available_minor':9000,'reserved_minor':1000,'reclaimed_minor':0,
            'activations':1,'used_discount_minor':10000,'held_discount_minor':0}
        self.client.rpc.side_effect=self.rpc

    def rpc(self,name,args):
        self.calls.append((name,args))
        if name=='list_campaigns':return [dict(self.record)]
        if name=='promotion_cancellation_record':return dict(self.record)
        if name=='cancel_promotional_campaign':
            self.record.update(state='ended',cancelled=True,promotion_cancelled_at='fixture-date')
            return dict(self.record)
        if name=='reclaim_cancelled_promotion':
            self.record['reclaimed_minor']+=self.record['available_minor'];self.record['available_minor']=0
            self.record['reclaimed_promotional_minor']=self.record['reclaimed_minor']
            return dict(self.record)
        return self.fixture.rpc(name,args)

    def callback(self,action,user=9,prefix='oa'):
        self.fixture.callback(prefix+':'+action+':'+fixtures.CAMPAIGN,user=user)

    def panel(self):return self.bot._show_panel.call_args.args[1]

    def test_list_exposes_reclaim_for_active_and_paused_wallet_grants_only(self):
        for state in ['active','paused']:
            self.record['state']=state;self.fixture.callback('oa:list:0')
            self.assertIn('oa:cancelpromo:',str(self.panel().rows))
        self.record['benefit_kind']='percent';self.fixture.callback('oa:list:0')
        self.assertNotIn('oa:cancelpromo:',str(self.panel().rows))
        self.record.update(benefit_kind='wallet_credit',funder_kind='partner');self.fixture.callback('oa:list:0')
        self.assertNotIn('oa:cancelpromo:',str(self.panel().rows))

    def test_review_shows_specific_grant_available_and_protected_held_money(self):
        self.callback('cancelpromo')
        self.assertEqual([name for name,args in self.calls],['promotion_cancellation_record'])
        self.assertIn('PROMO100',self.panel().text);self.assertIn('90.00',self.panel().text)
        self.assertIn('10.00',self.panel().text);self.assertIn('النقد المدفوع محفوظ',self.panel().text)
        for row in self.panel().rows:
            for button in row:self.assertLessEqual(len(button['callback_data'].encode()),64)

    def test_confirmation_commits_cancellation_before_bounded_reclaim(self):
        self.callback('cancelpromook')
        self.assertEqual([name for name,args in self.calls],['cancel_promotional_campaign','reclaim_cancelled_promotion'])
        self.assertEqual(self.calls[0][1],{'p_actor_telegram_id':9,'p_campaign_id':fixtures.CAMPAIGN,
            'p_reference':'telegram-promotion-cancel:9:fixture-callback'})
        self.assertIn('90.00',self.panel().text);self.assertIn('10.00',self.panel().text)
        self.callback('cancelpromook');self.assertEqual(self.record['reclaimed_minor'],9000)

    def test_back_does_not_cancel_or_reclaim(self):
        self.callback('cancelpromo');self.fixture.callback('oa:list:0')
        self.assertFalse(self.record['cancelled']);self.assertEqual(self.record['reclaimed_minor'],0)

    def test_customer_and_partner_cannot_cancel_platform_grants(self):
        self.callback('cancelpromo',user=31);self.callback('cancelpromook',user=31)
        self.assertEqual(self.calls,[])
        original=self.rpc
        self.client.rpc.side_effect=lambda name,args:({'is_partner':True,'state':'active'} if name=='partner_campaign_context' else original(name,args))
        self.callback('cancelpromook',user=31,prefix='po')
        self.assertNotIn('cancel_promotional_campaign',[name for name,args in self.calls])

    def test_bad_id_and_missing_confirmation_receipt_do_not_mutate(self):
        self.fixture.callback('oa:cancelpromook:bad-id')
        self.bot._campaign_admin_callback('oa:cancelpromook:'+fixtures.CAMPAIGN,9,4,{})
        self.assertEqual(self.calls,[])

    def test_reclaim_failure_keeps_cancellation_and_reports_automatic_processing(self):
        def temporary(name,args):
            if name=='reclaim_cancelled_promotion':raise CommerceUnavailable('Synthetic reclaim reply lost')
            return self.rpc(name,args)
        self.client.rpc.side_effect=temporary;self.callback('cancelpromook')
        self.assertTrue(self.record['cancelled'])
        self.assertIn('أُلغيت الحملة',self.panel().text);self.assertIn('تلقائيًا',self.panel().text)
        self.client.rpc.side_effect=self.rpc;self.callback('cancelpromook')
        self.assertEqual(self.record['reclaimed_minor'],9000)

    def test_lost_cancel_response_requires_state_check_before_claiming_success(self):
        def temporary(name,args):
            result=self.rpc(name,args)
            if name=='cancel_promotional_campaign':raise CommerceUnavailable('Synthetic committed cancellation reply lost')
            return result
        self.client.rpc.side_effect=temporary;self.callback('cancelpromook')
        self.assertTrue(self.record['cancelled']);self.assertNotIn('<b>أُلغيت الحملة</b>',self.panel().text)
        self.client.rpc.side_effect=self.rpc;self.callback('cancelpromook')
        self.assertEqual(self.record['reclaimed_minor'],9000)

    def test_cancelled_list_shows_reclaimed_amount_and_hides_cancel(self):
        self.callback('cancelpromook');self.fixture.callback('oa:list:0')
        self.assertIn('ملغاة',self.panel().text);self.assertIn('90.00',self.panel().text)
        self.assertNotIn('oa:cancelpromo:',str(self.panel().rows))


if __name__=='__main__':unittest.main()
