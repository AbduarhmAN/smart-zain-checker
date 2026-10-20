"""Cash-code cancellation through the actual Telegram callback routing, offline."""
import unittest
from uuid import uuid4

import test_commerce_cash_codes as fixtures
from telegram_bot.commerce.client import CommerceUnavailable


class CashCodeCancellationTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.CashCodeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.bot=self.fixture.bot
        self.record={'code_id':str(uuid4()),'code':'PAID_CASH','active':True,
            'credit_minor':3000,'max_redemptions':6,'redemptions':1,
            'cash_funded_minor':18000,'cash_redeemed_minor':3000,
            'expires_at':None,'receipt_reference':'CASH-001'}
        self.calls=[]
        self.bot._commerce_client.rpc.side_effect=self.rpc

    def rpc(self,name,args):
        self.calls.append((name,args))
        if name=='list_cash_credit_codes':return [dict(self.record)]
        if name=='cash_credit_code_record':return dict(self.record)
        if name=='cancel_cash_credit_code':
            state='cancelled' if self.record['active'] else 'already_cancelled'
            self.record['active']=False
            return {'state':state,'code':self.record['code'],'unredeemed_minor':15000}
        raise AssertionError(name)

    def callback(self,action,user=9):
        self.fixture.callback('cg:'+action+':'+self.record['code_id'],user=user)

    def panel(self):return self.bot._show_panel.call_args.args[1]

    def test_list_has_state_and_cancellation_for_active_codes_including_exhausted(self):
        self.record['redemptions']=6
        self.fixture.callback('cg:list:0')
        self.assertIn('الحالة: نشط',self.panel().text)
        self.assertIn('cg:cancel:'+self.record['code_id'],str(self.panel().rows))
        for row in self.panel().rows:
            for item in row:
                self.assertLessEqual(len(('zc:'+item['callback_data']).encode('utf-8')),64)

    def test_cancel_opens_specific_review_without_mutation(self):
        self.callback('cancel')
        self.assertEqual([name for name,args in self.calls],['cash_credit_code_record'])
        self.assertIn('PAID_CASH',self.panel().text)
        self.assertIn('150.00',self.panel().text)
        self.assertIn('1 / 6',self.panel().text)
        self.assertIn('دون رد نقدي تلقائي',self.panel().text)
        self.assertIn('cg:cancelok:'+self.record['code_id'],str(self.panel().rows))

    def test_back_does_not_cancel(self):
        self.callback('cancel');self.fixture.callback('cg:list:0')
        self.assertTrue(self.record['active'])
        self.assertNotIn('cancel_cash_credit_code',[name for name,args in self.calls])

    def test_confirmation_uses_authenticated_actor_code_and_callback_receipt(self):
        self.callback('cancelok')
        self.assertEqual(self.calls,[('cancel_cash_credit_code',{'p_actor_telegram_id':9,
            'p_code_id':self.record['code_id'],'p_reference':'telegram-cash-code-cancel:9:fixture-callback'})])
        self.assertIn('تم إلغاء الكود',self.panel().text)
        self.assertIn('الأرصدة المشحونة سابقًا محفوظة',self.panel().text)

    def test_repeat_is_reported_and_inactive_code_has_no_cancel_button(self):
        self.callback('cancelok');self.callback('cancelok')
        self.assertIn('ملغى بالفعل',self.panel().text)
        self.fixture.callback('cg:list:0')
        self.assertIn('ملغى / متوقف',self.panel().text)
        self.assertNotIn('cg:cancel:',str(self.panel().rows))
        self.callback('cancel')
        self.assertIn('متوقف بالفعل',self.panel().text)
        self.assertNotIn('cg:cancelok:',str(self.panel().rows))

    def test_customer_cannot_review_or_cancel_even_with_forged_callback(self):
        self.callback('cancel',user=31);self.callback('cancelok',user=31)
        self.bot._cash_code_callback('cg:cancelok:'+self.record['code_id'],31,7,{'id':'forged'})
        self.assertEqual(self.calls,[])
        self.assertTrue(self.record['active'])

    def test_invalid_id_or_missing_receipt_cannot_mutate(self):
        self.fixture.callback('cg:cancelok:bad-id')
        self.bot._cash_code_callback('cg:cancelok:'+self.record['code_id'],9,7,{})
        self.assertEqual(self.calls,[])

    def test_missing_code_does_not_offer_confirmation_or_claim_success(self):
        self.bot._commerce_client.rpc.side_effect=lambda name,args:None if name=='cash_credit_code_record' else {'state':'invalid_code'}
        self.callback('cancel')
        self.assertIn('غير موجود',self.panel().text)
        self.assertNotIn('cg:cancelok:',str(self.panel().rows))
        self.callback('cancelok')
        self.assertIn('غير موجود',self.panel().text)

    def test_lost_acknowledgement_directs_admin_to_check_status(self):
        def lost_ack(name,args):
            result=self.rpc(name,args)
            raise CommerceUnavailable('Synthetic committed response lost')
        self.bot._commerce_client.rpc.side_effect=lost_ack
        self.callback('cancelok')
        self.assertFalse(self.record['active'])
        self.assertIn('لم نتأكد من إلغاء',self.panel().text)
        self.bot._commerce_client.rpc.side_effect=self.rpc
        self.callback('cancelok')
        self.assertIn('ملغى بالفعل',self.panel().text)


if __name__=='__main__':unittest.main()
