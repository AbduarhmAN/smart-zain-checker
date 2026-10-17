import unittest
from unittest.mock import Mock
from telegram_bot.commerce.finance import FinanceService,VerifiedReceipt,result_rows

ORDER = 'bfc55f1c-9815-4e5d-b1f0-c8d94a375ef3'


class FinanceTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.service = FinanceService(self.client)

    def test_zero_website_amount_is_a_valid_paid_result(self):
        self.assertEqual(result_rows([{'row':2,'status':'mismatch','live_sar':0}]),
                         [{'row_number':2,'state':'succeeded','verified_result':True}])

    def test_review_and_error_are_refundable_without_result_proof(self):
        rows = result_rows([{'row':2,'status':'needs_review','live_sar':None},{'row':3,'status':'error'}])
        self.assertFalse(any(row['verified_result'] for row in rows))

    def test_valid_negative_result_is_paid(self):
        self.assertTrue(result_rows([{'row':2,'status':'not_found'}])[0]['verified_result'])

    def test_pending_deferred_invalid_amount_and_duplicate_rows_rejected(self):
        samples = [[{'row':2,'status':'deferred'}],[{'row':2,'status':'pending'}],
                   [{'row':2,'status':'match','live_sar':None}],[{'row':2,'status':'match','live_sar':'NaN'}],
                   [{'row':2,'status':'match','live_sar':-1}],[{'row':True,'status':'not_found'}],
                   [{'row':2,'status':'error'},{'row':2,'status':'not_found'}]]
        for records in samples:
            with self.subTest(records=records),self.assertRaises(ValueError): result_rows(records)

    def test_receipt_type_currency_and_money_precision_are_explicit(self):
        with self.assertRaises(ValueError): self.service.confirm_payment(5,'key',{'tx_id':'unverified'})
        for amount in [1.5,True,0,-1]:
            with self.subTest(amount=amount),self.assertRaises(ValueError):
                VerifiedReceipt('fixture','tx',amount,'proof')
        with self.assertRaises(ValueError): VerifiedReceipt('fixture','tx',3000,'proof',currency='USDT')

    def test_direct_payment_and_topup_remain_distinct(self):
        receipt = VerifiedReceipt('fixture','tx',3000,'proof')
        self.service.confirm_payment(5,'key',receipt)
        self.assertEqual(self.client.rpc.call_args.args[1]['p_purpose'],'topup')
        self.service.confirm_payment(5,'direct-key',receipt,order_id=ORDER)
        self.assertEqual(self.client.rpc.call_args.args[1]['p_purpose'],'direct_order')

    def test_refund_consent_cannot_be_implicit(self):
        for consent in [None,True,'']:
            with self.subTest(consent=consent),self.assertRaises(ValueError): self.service.reserve(ORDER,5,consent)
        self.client.rpc.assert_not_called()

    def test_delivery_requires_actual_owned_document_receipt(self):
        valid = {'message_id':8,'chat':{'id':5},'document':{'file_id':'fixture-file'}}
        self.service.delivery(ORDER,5,valid,'a'*64)
        self.assertEqual(self.client.rpc.call_args.args[0],'confirm_result_delivery')
        for bad in [{'message_id':8,'chat':{'id':6},'document':{'file_id':'fixture'}},
                    {'message_id':8,'chat':{'id':5}},{'chat':{'id':5},'document':{'file_id':'fixture'}},
                    {'message_id':8,'chat':None,'document':None}]:
            with self.subTest(bad=bad),self.assertRaises(ValueError): self.service.delivery(ORDER,5,bad,'a'*64)


if __name__ == '__main__': unittest.main()
