import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from telegram_bot.commerce.legacy_wallets import reconcile,save_preview,minor


class LegacyWalletTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(tempfile.mkdtemp(prefix='legacy-wallet-retained-'))
        self.wallets={'31':{'account_id':123456789,'balance':20.01,'history':[
            {'type':'credit','amount':30.01,'balance':30.01,'idempotency_key':'topup:1'},
            {'type':'debit','amount':10,'balance':20.01,'idempotency_key':'debit:1'}]}}
        self.pending={}
        self.save()

    def save(self):
        (self.root/'user_wallets.json').write_text(json.dumps(self.wallets),encoding='utf-8')
        (self.root/'pending_payments.json').write_text(json.dumps(self.pending),encoding='utf-8')

    def test_exact_reconciled_plan_preserves_balance_and_sources(self):
        before=(self.root/'user_wallets.json').read_bytes()
        plan=reconcile(self.root)
        self.assertEqual(plan['total_available_minor'],2001)
        self.assertTrue(plan['cutover_ready'])
        self.assertFalse(plan['supabase_balances_changed'])
        saved=save_preview(self.root)
        self.assertTrue(Path(saved['plan']).exists())
        self.assertEqual((self.root/'user_wallets.json').read_bytes(),before)

    def test_missing_duplicate_or_noncanonical_id_is_not_importable(self):
        self.wallets['32']={**self.wallets['31']};self.save()
        self.assertEqual(len(reconcile(self.root)['conflicts']),1)
        self.wallets={'031':self.wallets['31']};self.save()
        self.assertFalse(reconcile(self.root)['cutover_ready'])

    def test_history_mismatch_unknown_journal_and_duplicate_key_are_blocked(self):
        for mutation in ['balance','type','key']:
            value=self.wallets['31']
            if mutation=='balance':value['balance']=20.02
            elif mutation=='type':value['balance']=20.01;value['history'][0]['type']='unknown'
            else:value['history'][0]['type']='credit';value['history'][1]['idempotency_key']='topup:1'
            self.save()
            with self.subTest(mutation=mutation):
                self.assertFalse(reconcile(self.root)['cutover_ready'])
                self.assertEqual(len(reconcile(self.root)['conflicts']),1)

    def test_unresolved_payment_blocks_cutover_without_mutating_its_state(self):
        self.pending={'old':{'status':'pending','user_id':'31'}};self.save()
        plan=reconcile(self.root)
        self.assertFalse(plan['cutover_ready']);self.assertEqual(len(plan['unresolved_payments']),1)
        self.assertEqual(json.loads((self.root/'pending_payments.json').read_text())['old']['status'],'pending')

    def test_fulfillment_is_a_review_candidate_and_not_proof_of_paid_completion(self):
        self.pending={'old':{'status':'fulfilled','user_id':'31','job_id':'job-fixture'}};self.save()
        self.assertEqual(len(reconcile(self.root)['paid_service_candidates']),1)

    def test_source_drift_during_reconciliation_is_rejected(self):
        original=Path.read_bytes;counts={}
        def read(path):
            counts[path]=counts.get(path,0)+1
            raw=original(path)
            return raw+b' ' if path.name=='user_wallets.json' and counts[path]>1 else raw
        with patch('pathlib.Path.read_bytes',new=read),self.assertRaises(ValueError):reconcile(self.root)

    def test_bool_float_subcent_nonfinite_and_huge_money_rejected(self):
        for value in [True,1.2,'0.001','NaN','Infinity','-1','1e100000']:
            with self.subTest(value=value),self.assertRaises(ValueError):minor(value)
        self.assertEqual(minor('0.0100'),1)


if __name__=='__main__':unittest.main()
