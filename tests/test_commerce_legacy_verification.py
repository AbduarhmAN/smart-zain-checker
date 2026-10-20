import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from telegram_bot.commerce.legacy_verification import probe


class LegacyVerificationTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(tempfile.mkdtemp(prefix='legacy-provider-retained-'))
        (self.root/'user_wallets.json').write_text('{"31":{"account_id":123456789,"balance":0,"history":[]}}')
        self.payments={'old-token':{'user_id':'31','status':'ready','kind':'invoice',
            'draft_info':{'invoice_id':8,'usdt_amount':0.6,'egp_amount':30}}}
        self.save()
        self.client=Mock(base_url='https://pay.crypt.bot/api');self.client.get_me.return_value={'app_id':7}
        self.invoice={'invoice_id':8,'status':'active','amount':'0.6','asset':'USDT','currency_type':'crypto','payload':'old-token'}
        self.client.get_invoice.return_value=self.invoice

    def save(self): (self.root/'pending_payments.json').write_text(json.dumps(self.payments),encoding='utf-8')

    def test_active_paid_expired_and_missing_invoice_never_mutate_sources(self):
        source=(self.root/'pending_payments.json').read_bytes()
        for status in ['active','paid','expired']:
            self.invoice['status']=status
            report=probe(self.root,self.client)
            self.assertFalse(report['writes'])
            self.assertEqual(report['observations'][0]['provider_status'],status)
            self.assertEqual((self.root/'pending_payments.json').read_bytes(),source)
        self.client.get_invoice.return_value=None
        self.assertIn('unavailable',probe(self.root,self.client)['observations'][0]['observation'])

    def test_wrong_payload_or_asset_never_proves_payment(self):
        for mutation in [{'payload':'another'},{'asset':'USDC'},{'amount':'NaN'}]:
            self.client.get_invoice.return_value={**self.invoice,**mutation,'status':'paid'}
            observation=probe(self.root,self.client)['observations'][0]
            self.assertFalse(observation['binding_matches'])
            self.assertEqual(observation['financial_decision'],'review_required')

    def test_stored_invoice_does_not_override_declared_binance_funding(self):
        self.payments['old-token'].update(status='fulfilled',method='Binance Pay',tx_id='ORDER1',amount_usdt=.6)
        self.save();adapter=Mock();adapter.verify.side_effect=ValueError('not found')
        observation=probe(self.root,self.client,binance_adapter=adapter)['observations'][0]
        self.assertFalse(observation['declared_transfer_verified'])
        self.assertEqual(observation['provider_status'],'active')
        self.assertIn('binance',observation['financial_decision'])

    def test_testnet_or_unavailable_app_is_rejected(self):
        self.client.base_url='https://testnet-pay.crypt.bot/api'
        with self.assertRaises(ValueError):probe(self.root,self.client)
        self.client.base_url='https://pay.crypt.bot/api';self.client.get_me.return_value=None
        with self.assertRaises(ValueError):probe(self.root,self.client)


if __name__=='__main__':unittest.main()
