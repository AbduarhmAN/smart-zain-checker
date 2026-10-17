"""Private pre-payment notes bind deposits without trusting pasted TXIDs."""
import copy
import json
import unittest
from unittest.mock import Mock
from telegram_bot.commerce.binance_notes import BinanceNoteService,BinanceNoteMonitor,enabled
from telegram_bot.commerce.payment_providers import BinanceTransferAdapter
from test_commerce_payments import quote,transfer


class BinanceNoteTests(unittest.TestCase):
    def setUp(self):
        self.quote=quote();self.challenge={'quote_id':self.quote.id,'challenge':'ZC'+'a'*32,
            'payer_uid':'999','issued_at':self.quote.observed_at,'expires_at':self.quote.expires_at}
        self.record={**transfer(),'note':self.challenge['challenge']}
        self.history=Mock();self.history.get_recent_transactions.return_value=[self.record]
        self.adapter=BinanceTransferAdapter(self.history,receiver_uid=41,receiver_pay_id=42)
        self.request={'provider':'binance_pay','receiver':'uid:41','quote':self.quote.as_dict()}
        self.client=Mock();self.confirmations=[]
        def rpc(name,args):
            if name=='payment_request':return copy.deepcopy(self.request)
            if name=='binance_note':return copy.deepcopy(self.challenge)
            if name=='issue_binance_note':return copy.deepcopy(self.challenge)
            if name=='confirm_binance_note':
                self.confirmations.append(copy.deepcopy(args));return {'payment_id':'owned-payment'}
            if name=='pending_binance_notes':
                return [] if args['p_after'] else [{**self.challenge,'telegram_user_id':31}]
            raise AssertionError(name)
        self.client.rpc.side_effect=rpc
        self.service=BinanceNoteService(self.client,lambda request:self.adapter)

    def test_exact_private_note_confirms_prebound_quote_not_the_person_pasting_txid(self):
        result,q=self.service.confirm(self.quote.id,31)
        self.assertEqual(q.user_id,31);self.assertEqual(result['payment_id'],'owned-payment')
        proof=json.loads(self.confirmations[0]['p_evidence']['proof_json'])
        self.assertEqual(proof['payment_note'],self.challenge['challenge']);self.assertEqual(proof['payer_uid'],'999')
        self.assertNotIn('p_telegram_user_id',self.confirmations[0])

    def test_another_telegram_account_cannot_claim_the_same_private_note(self):
        with self.assertRaises(ValueError):self.service.confirm(self.quote.id,32)
        self.history.get_recent_transactions.assert_not_called();self.assertEqual(self.confirmations,[])

    def test_raw_txid_with_no_private_note_does_not_credit(self):
        self.record['note']=''
        with self.assertRaises(ValueError):self.service.confirm(self.quote.id,31)
        self.assertEqual(self.confirmations,[])

    def test_wrong_note_modified_nonce_or_note_given_only_in_chat_cannot_credit(self):
        for note in [None,'ZC'+'b'*32,'copied TX1',self.challenge['challenge']+' ',True]:
            self.record['note']=note
            with self.subTest(note=note),self.assertRaises(ValueError):self.service.confirm(self.quote.id,31)
        self.assertEqual(self.confirmations,[])

    def test_wrong_sender_receiver_currency_amount_and_old_time_are_rejected(self):
        original=copy.deepcopy(self.record)
        mutations=[{'payerInfo':{'binanceId':'998'}},{'receiverInfo':{'binanceId':'40','accountId':'42'}},
            {'amount':'1','fundsDetail':[{'currency':'USDT','amount':'1'}]}, {'currency':'USDC'},
            {'transactionTime':1791939500000},{'orderType':'PAY_REFUND'}]
        for values in mutations:
            self.history.get_recent_transactions.return_value=[{**original,**values}]
            with self.subTest(values=values),self.assertRaises(ValueError):self.service.confirm(self.quote.id,31)
        self.assertEqual(self.confirmations,[])

    def test_multiple_real_deposits_with_the_same_note_require_review(self):
        self.history.get_recent_transactions.return_value=[self.record,{**self.record,'transactionId':'TX2','orderId':'ORDER2'}]
        with self.assertRaises(ValueError):self.service.confirm(self.quote.id,31)
        self.assertEqual(self.confirmations,[])

    def test_repeated_identical_provider_record_keeps_one_canonical_receipt(self):
        self.history.get_recent_transactions.return_value=[self.record,copy.deepcopy(self.record)]
        self.service.confirm(self.quote.id,31);self.assertEqual(len(self.confirmations),1)

    def test_challenge_is_issued_only_for_owned_binance_quote_and_canonical_payer(self):
        for uid in ['0999','-1','999\n','9.0','']:
            with self.subTest(uid=uid),self.assertRaises(ValueError):self.service.issue(self.quote.id,31,uid)
        with self.assertRaises(ValueError):self.service.issue(self.quote.id,32,'999')
        self.service.issue(self.quote.id,31,'999')
        self.assertEqual(self.client.rpc.call_args.args[0],'issue_binance_note')

    def test_monitor_uses_one_authenticated_history_read_per_page(self):
        monitor=BinanceNoteMonitor(self.client,lambda _:self.adapter,self.history)
        self.assertEqual(monitor.tick()['confirmed'],1)
        self.history.get_recent_transactions.assert_called_once_with(limit=100)
        self.assertEqual(monitor.tick()['checked'],0)

    def test_absent_activation_proof_never_enables_automatic_note_credits(self):
        from pathlib import Path
        import tempfile
        root=Path(tempfile.mkdtemp(prefix='binance-note-retained-'))
        self.assertFalse(enabled(root))

    def test_live_probe_checks_note_without_creating_or_crediting_a_payment(self):
        from pathlib import Path
        import tempfile,time
        from telegram_bot.commerce.binance_note_probe import prepare,check
        root=Path(tempfile.mkdtemp(prefix='binance-echo-retained-'))
        probe=prepare(root)
        self.history.get_recent_transactions.return_value=[{**self.record,'note':probe['note'],'uid':41,
            'transactionTime':int(time.time()*1000)+1000}]
        result=check(root,probe['probe'],self.history)
        self.assertTrue(result['verified']);self.assertFalse(result['financial_writes'])
        self.assertFalse(result['automatic_credit_enabled']);self.assertFalse(enabled(root))
        self.client.rpc.assert_not_called()

    def test_live_probe_does_not_accept_a_note_on_an_outgoing_payment(self):
        from pathlib import Path
        import tempfile,time
        from telegram_bot.commerce.binance_note_probe import prepare,check
        root=Path(tempfile.mkdtemp(prefix='binance-echo-retained-'));probe=prepare(root)
        self.history.get_recent_transactions.return_value=[{**self.record,'note':probe['note'],'uid':999,
            'transactionTime':int(time.time()*1000)+1000}]
        self.assertFalse(check(root,probe['probe'],self.history)['verified'])
        self.client.rpc.assert_not_called();self.assertFalse(enabled(root))


if __name__=='__main__':unittest.main()
