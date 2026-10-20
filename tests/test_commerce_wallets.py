import concurrent.futures
import json
from pathlib import Path
import tempfile
import time
import unittest
from datetime import datetime,timezone
from unittest.mock import Mock,patch
import test_commerce_checkout as checkout_fixture
from telegram_bot.commerce.payment_providers import CryptoInvoiceAdapter
from telegram_bot.commerce.payment_quotes import freeze_quote
from telegram_bot.commerce.topup_store import TopupStore

PAYMENT='92ac8e9f-8d4c-4d47-b939-cef01c2d39c7'


class WalletTests(unittest.TestCase):
    def setUp(self):
        self.fixture=checkout_fixture.CheckoutTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.bot=self.fixture.bot;self.root=self.fixture.root;self.client=self.fixture.client
        self.balance=0;self.requests={};self.cash={};self.cash_calls=0
        self.client.rpc.side_effect=self.rpc
        (self.root/'payment_config.json').write_text('{"binance_pay_id":42}',encoding='utf-8')
        provider=Mock(base_url='https://pay.crypt.bot/api');provider.get_me.return_value={'app_id':7}
        self.adapter=CryptoInvoiceAdapter(provider,app_id=7);self.provider=provider;self.invoices={}
        def create(endpoint,payload):
            self.assertEqual(endpoint,'createInvoice')
            invoice={**payload,'invoice_id':8,'status':'active','created_at':datetime.now(timezone.utc).isoformat(),
                     'bot_invoice_url':'https://t.me/CryptoBot?start=fixture'}
            self.invoices[8]=invoice;return invoice
        provider._call.side_effect=create
        provider.get_invoice.side_effect=lambda invoice_id:dict(self.invoices[invoice_id])
        adapter_patch=patch.object(self.bot,'_checkout_adapter',return_value=self.adapter)
        rate_patch=patch('telegram_bot.commerce.wallets.asset_egp_rate',return_value=('50','fixture-rate'))
        adapter_patch.start();rate_patch.start();self.addCleanup(adapter_patch.stop);self.addCleanup(rate_patch.stop)
        self.rate_mock=rate_patch

    def rpc(self,name,args):
        if name=='register_telegram_user':return {'user_id':'fixture-user'}
        if name=='wallet_state':return {'user_id':'fixture-user','currency':'EGP','is_partner':True,
            'customer':{'available_minor':self.balance,'reserved_minor':0},
            'partner':{'available_minor':900,'reserved_minor':400,'withdrawn_minor':600}}
        if name=='create_payment_request':
            q=args['p_quote'];self.requests.setdefault(q['id'],{'quote':q,'provider':args['p_provider'],
                'receiver':args['p_receiver'],'route_snapshot':args.get('p_route_snapshot',{}),
                'invoice_state':'none','invoice_id':None,'payment_id':None})
            return {'state':'issued'}
        if name=='payment_request':
            record=self.requests[args['p_quote_id']]
            if record['quote']['user_id']!=args['p_telegram_user_id']:raise ValueError('Wrong owner')
            return record
        if name=='begin_payment_invoice':
            record=self.requests[args['p_quote_id']]
            if record['invoice_state']!='none':return {'state':record['invoice_state']}
            record['invoice_state']='started';return {'state':'claimed'}
        if name=='attach_payment_invoice':
            record=self.requests[args['p_quote_id']];record.update(invoice_state='attached',invoice_id=args['p_invoice_id'])
            return {'invoice_id':args['p_invoice_id']}
        if name=='confirm_payment_request':
            record=self.requests[args['p_quote_id']]
            if not record['payment_id']:
                record['payment_id']=PAYMENT;self.balance+=record['quote']['amount_minor']
            return {'payment_id':PAYMENT}
        if name=='submit_payment_claim':return {'claim_id':'c7a8c35c-512a-479b-b1a4-758bcb94be23','state':'pending'}
        if name=='admin_cash_topup':
            self.cash_calls+=1;ref=args['p_receipt_reference'].upper()
            replay=ref in self.cash
            if not replay:self.cash[ref]=args;self.balance+=args['p_amount_minor']
            return {'payment_id':PAYMENT,'replayed':replay}
        raise AssertionError(name)

    def callback(self,action,value='0',user=31):
        self.bot._handle_callback({'id':'callback:1','from':{'id':user},
            'message':{'chat':{'id':user},'message_id':4},'data':f'zc:cw:{action}:{value}'})

    def issue(self,method='cryptopay',amount='60.00',message_id=123):
        self.callback('amount',method)
        waiting=self.bot._waiting_user_inputs.pop(31);waiting['_telegram_input_message_id']=message_id
        self.bot._commerce_text_input(31,amount,waiting)
        return self.bot._topups().recent(31)[0]['quote']['id']

    def test_view_reads_server_and_separates_wallets_without_fx_request(self):
        self.balance=6000
        with patch('telegram_bot.commerce.wallets.asset_egp_rate') as rates:
            self.bot._show_wallet_panel(31,4)
        rates.assert_not_called()
        text=self.bot._show_panel.call_args.args[1].text
        self.assertIn('60.00',text);self.assertIn('9.00',text);self.assertIn('4.00',text);self.assertIn('6.00',text)

    def test_crypto_topup_confirms_once_and_never_creates_checking_job(self):
        qid=self.issue();self.callback('invoice',qid)
        self.invoices[8].update(status='paid',paid_at=datetime.now(timezone.utc).isoformat())
        self.callback('check',qid);self.callback('check',qid)
        self.assertEqual(self.balance,6000)
        self.assertIsNone(self.requests[qid]['quote']['order_id'])
        self.assertEqual(len(self.bot.queue_service.get_jobs()),0)

    def test_same_message_replay_uses_original_quote_without_new_fx_or_amount(self):
        first=self.issue()
        with patch('telegram_bot.commerce.wallets.asset_egp_rate') as rate:
            second=self.issue(message_id=123)
        self.assertEqual(first,second);rate.assert_not_called()
        self.assertEqual(len(self.bot._topups().recent(31)),1)
        self.assertEqual(self.balance,0)

    def test_binance_topup_reference_goes_to_review_without_credit(self):
        qid=self.issue('binance')
        self.callback('reference',qid)
        waiting=self.bot._waiting_user_inputs.pop(31)
        self.bot._commerce_text_input(31,'ORDER1',waiting)
        self.assertEqual(self.balance,0)
        args=next(call.args[1] for call in self.client.rpc.call_args_list if call.args[0]=='submit_payment_claim')
        self.assertEqual(args['p_quote_id'],qid)

    def test_other_customer_cannot_open_or_confirm_wallet_request(self):
        qid=self.issue();self.client.rpc.reset_mock();self.callback('check',qid,user=32)
        self.client.rpc.assert_not_called();self.assertEqual(self.balance,0)

    def test_malformed_and_boolean_identity_callbacks_do_not_read_or_write_wallet(self):
        self.client.rpc.reset_mock()
        self.bot._wallet_callback('cw:list:0',True,4,{})
        self.bot._wallet_callback('cw:check',31,4,{})
        self.bot._cash_topup_callback('ca:approve',9,4,{'id':'invalid'})
        self.client.rpc.assert_not_called()

    def test_store_failure_prevents_external_issue(self):
        self.callback('amount','cryptopay');waiting=self.bot._waiting_user_inputs.pop(31)
        waiting['_telegram_input_message_id']=123;self.client.rpc.reset_mock()
        with patch.object(TopupStore,'put',side_effect=OSError('synthetic persistence failure')):
            self.bot._commerce_text_input(31,'60',waiting)
        self.client.rpc.assert_not_called()

    def test_lost_invoice_creation_response_never_creates_second_invoice(self):
        qid=self.issue();self.provider._call.side_effect=TimeoutError('synthetic lost response')
        self.callback('invoice',qid);self.callback('invoice',qid)
        self.assertEqual(self.provider._call.call_count,1)
        self.assertEqual(self.balance,0)

    def test_invalid_subcent_and_expired_amount_prompt_do_not_issue_money(self):
        for value in ['-1','NaN','0','1.001']:
            self.callback('amount','cryptopay');waiting=self.bot._waiting_user_inputs.pop(31)
            waiting['_telegram_input_message_id']=123
            self.bot._commerce_text_input(31,value,waiting)
        self.assertEqual(self.requests,{})

    def test_admin_cash_needs_review_matching_actor_and_receipt(self):
        with patch('telegram_bot.payment_manager.PAYMENT_MGR.find_user_by_account_id',return_value=None):
            self.bot._commerce_command('/credit_user 31 60 CASH-001',9,True,message_id=77)
        self.assertEqual(self.cash_calls,0)
        token=next(iter(self.bot._cash_topup_reviews))
        self.bot._cash_topup_callback('ca:approve:'+token,31,4,{'id':'unauthorized'})
        self.assertEqual(self.cash_calls,0)
        self.bot._cash_topup_callback('ca:approve:'+token,9,4,{'id':'approved:1'})
        self.bot._cash_topup_callback('ca:approve:'+token,9,4,{'id':'approved:2'})
        self.assertEqual(self.balance,6000)
        self.assertTrue(self.bot._topups().is_owner(31))

    def test_expired_cash_review_and_missing_receipt_do_not_credit(self):
        with patch('telegram_bot.payment_manager.PAYMENT_MGR.find_user_by_account_id',return_value=None):
            self.bot._commerce_command('/credit_user 31 60',9,True,message_id=77)
            self.assertFalse(getattr(self.bot,'_cash_topup_reviews',{}))
            self.bot._commerce_command('/credit_user 31 60 CASH-001',9,True,message_id=77)
        token=next(iter(self.bot._cash_topup_reviews));self.bot._cash_topup_reviews[token]['expires']=time.monotonic()-1
        self.bot._cash_topup_callback('ca:approve:'+token,9,4,{'id':'expired'})
        self.assertEqual(self.cash_calls,0)


class TopupStoreTests(unittest.TestCase):
    def setUp(self):self.root=Path(tempfile.mkdtemp(prefix='topup-store-retained-'))
    def record(self):return {'quote':freeze_quote(31,3000,'USDT','50','fixture').as_dict(),
        'provider':'cryptopay','receiver':'7','route_snapshot':{}}

    def test_concurrent_quote_replay_retains_one_record(self):
        record=self.record();store=TopupStore(self.root)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            ids=list(executor.map(lambda _:TopupStore(self.root).put(record),range(16)))
        self.assertEqual(len(set(ids)),1);self.assertEqual(len(store.recent(31)),1)
        self.assertEqual(store.get(ids[0],31),record)

    def test_restart_foreign_owner_and_changed_quote(self):
        record=self.record();TopupStore(self.root).put(record);qid=record['quote']['id']
        with self.assertRaises(ValueError):TopupStore(self.root).get(qid,32)
        record['receiver']='8'
        with self.assertRaises(ValueError):TopupStore(self.root).put(record)


if __name__=='__main__':unittest.main()
