import json
from pathlib import Path
import tempfile
import time
import unittest
from datetime import datetime,timezone
from unittest.mock import Mock,patch
import test_commerce_checkout as checkout_fixture
from test_commerce_checkout import TOKEN,PAYMENT
from telegram_bot.commerce.client import CommerceUnavailable
from telegram_bot.commerce.payment_providers import TransferEvidence
from telegram_bot.commerce.payments import PaymentService
from telegram_bot.commerce.provider_routes import ProviderRoutes,TransferRoute,verifier_for

CLAIM='c9cccd12-7284-4d5d-a4f1-788f1ba829cf'


class ManualPaymentTests(unittest.TestCase):
    def setUp(self):
        self.fixture=checkout_fixture.CheckoutTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.bot=self.fixture.bot;self.root=self.fixture.root;self.client=self.fixture.client
        (self.root/'payment_config.json').write_text('{"binance_pay_id":42}',encoding='utf-8')
        self.claim=None;self.approvals=0;self.lose_approval=False
        self.client.rpc.side_effect=self.rpc
        rate=patch('telegram_bot.commerce.manual_payments.asset_egp_rate',return_value=('50','fixture-rate'))
        adapter=patch.object(self.bot,'_checkout_adapter',return_value=Mock(client=Mock()))
        rate.start();adapter.start();self.addCleanup(rate.stop);self.addCleanup(adapter.stop)
        self.fixture.prepare()

    def rpc(self,name,args):
        if name=='submit_payment_claim':
            if self.claim is None:
                request=self.fixture.payment_request
                self.claim={'claim_id':CLAIM,'quote_id':args['p_quote_id'],'telegram_user_id':31,
                    'reference':args['p_reference'],'state':'pending','payment_id':None,
                    'provider':request['provider'],'quote':request['quote'],'receiver':request['receiver'],
                    'route_snapshot':request['route_snapshot']}
            return {'claim_id':CLAIM,'state':self.claim['state']}
        if name=='payment_claim':return dict(self.claim)
        if name=='list_payment_claims':return [dict(self.claim)] if self.claim and self.claim['state']=='pending' else []
        if name=='approve_payment_claim':
            if self.claim['state']!='approved':
                self.claim.update(state='approved',payment_id=PAYMENT);self.approvals+=1
                self.fixture.payment_request['payment_id']=PAYMENT
            if self.lose_approval:
                self.lose_approval=False;raise CommerceUnavailable('Synthetic lost approval acknowledgment')
            return {'payment_id':PAYMENT,'telegram_user_id':31,'replayed':self.approvals==1}
        result=self.fixture.rpc(name,args)
        if name=='create_payment_request':
            self.fixture.payment_request['route_snapshot']=args.get('p_route_snapshot',{})
        return result

    def issue_and_submit(self):
        self.fixture.callback('binance')
        self.fixture.callback('sendref')
        waiting=self.bot._waiting_user_inputs.pop(31)
        self.bot._commerce_text_input(31,'ORDER1',waiting)

    def exchange_record(self):
        return {'transactionId':'TX1','orderId':'ORDER1','orderType':'C2C','currency':'USDT','amount':'1.2',
            'transactionTime':int(datetime.now(timezone.utc).timestamp()*1000),
            'receiverInfo':{'binanceId':'41','accountId':'42'},'fundsDetail':[{'currency':'USDT','amount':'1.2'}]}

    def admin(self,action,actor=9):
        self.bot._handle_callback({'id':'admin-update:1','from':{'id':actor},
            'message':{'chat':{'id':actor},'message_id':4},'data':f'zc:cm:{action}:{CLAIM}'})

    def test_transfer_issue_freezes_receiver_currency_and_amount(self):
        self.fixture.callback('binance')
        draft=self.bot._sheet_drafts[TOKEN]
        self.assertEqual(draft['commerce_funding'],'transfer')
        self.assertEqual(draft['commerce_quote']['asset_amount'],'1.200000')
        self.assertEqual(self.fixture.payment_request['receiver'],'pay:42')
        self.assertEqual(self.fixture.payment_request['route_snapshot']['pay_id'],42)
        self.assertEqual(len(self.bot.queue_service.get_jobs()),0)

    def test_reference_submission_never_credits_or_enqueues(self):
        self.issue_and_submit()
        self.assertEqual(self.claim['state'],'pending');self.assertEqual(self.approvals,0)
        self.assertEqual(len(self.bot.queue_service.get_jobs()),0)

    def test_admin_preview_then_explicit_assignment_allocates_once(self):
        self.issue_and_submit()
        with patch('telegram_bot.crypto_pay.BINANCE_PAY.get_recent_transactions',return_value=[self.exchange_record()]):
            self.admin('review')
            self.assertEqual(self.approvals,0)
            self.assertIn('المرجع وحده',self.bot._show_panel.call_args.args[1].text)
            self.admin('approve');self.admin('approve')
        self.assertEqual(self.approvals,1)
        self.assertEqual(len(self.bot.queue_service.get_jobs()),1)
        reserve=next(call.args[1] for call in self.client.rpc.call_args_list if call.args[0]=='reserve_order')
        self.assertEqual(reserve['p_payment_id'],PAYMENT)

    def test_lost_approval_ack_recovery_never_creates_another_payment(self):
        self.issue_and_submit();self.lose_approval=True
        with patch('telegram_bot.crypto_pay.BINANCE_PAY.get_recent_transactions',return_value=[self.exchange_record()]):
            self.admin('review');self.admin('approve')
            self.assertEqual(len(self.bot.queue_service.get_jobs()),0)
            self.assertEqual(self.bot._recover_commerce_checkouts(),1)
            self.admin('approve')
        self.assertEqual(self.approvals,1);self.assertEqual(len(self.bot.queue_service.get_jobs()),1)

    def test_no_preview_expired_preview_and_another_admin_cannot_confirm(self):
        self.issue_and_submit();self.admin('approve')
        self.assertEqual(self.approvals,0)
        with patch('telegram_bot.crypto_pay.BINANCE_PAY.get_recent_transactions',return_value=[self.exchange_record()]):
            self.admin('review')
        self.bot.authorized_ids.add(18)
        self.admin('approve',actor=18)
        self.assertEqual(self.approvals,0)
        self.bot._commerce_payment_reviews[(9,CLAIM)]['expires']=time.monotonic()-1
        self.admin('approve')
        self.assertEqual(self.approvals,0)

    def test_customer_cannot_read_admin_review(self):
        self.issue_and_submit();self.client.rpc.reset_mock();self.admin('review',actor=31)
        self.client.rpc.assert_not_called()

    def test_wrong_transfer_currency_amount_and_receiver_cannot_be_approved(self):
        self.issue_and_submit()
        for mutation in [{'currency':'USDC'},{'amount':'2'},{'receiverInfo':{}}]:
            record={**self.exchange_record(),**mutation}
            with patch('telegram_bot.crypto_pay.BINANCE_PAY.get_recent_transactions',return_value=[record]):
                self.admin('review');self.admin('approve')
        self.assertEqual(self.approvals,0)

    def test_saved_request_keeps_receiver_after_provider_configuration_changes(self):
        self.fixture.callback('binance')
        route=TransferRoute('binance','binance_pay','USDT','pay:99',{'pay_id':99,'uid':None,'asset':'USDT'})
        ProviderRoutes(self.root).save(route,actor_id=9,reference='settings:2')
        self.fixture.callback('binance')
        self.assertEqual(self.fixture.payment_request['receiver'],'pay:42')

    def test_public_configuration_commands_require_authenticated_admin_and_receipt(self):
        self.bot._commerce_command('/commerce_binance 99',31,False,message_id=1)
        self.assertFalse((self.root/'.commerce/provider_settings.json').exists())
        self.bot._commerce_command('/commerce_binance 99',9,True,message_id=4)
        self.assertEqual(ProviderRoutes(self.root).get('binance').receiver,'pay:99')


class ProviderRouteTests(unittest.TestCase):
    def root(self):return Path(tempfile.mkdtemp(prefix='provider-route-retained-'))

    def test_no_hardcoded_receiver_or_guessed_bsc_contract(self):
        root=self.root();self.assertEqual(ProviderRoutes(root).all(),[])
        (root/'payment_config.json').write_text('{"bep20_contract_info":"***d580d"}',encoding='utf-8')
        self.assertEqual(ProviderRoutes(root).all(),[])

    def test_bad_contract_decimals_and_identity_domain_rejected(self):
        for snapshot in [{'chain_id':56,'asset':'USDT','contract':'masked','decimals':18},
                         {'chain_id':56,'asset':'USDT','contract':'0x'+'d'*40,'decimals':True}]:
            with self.assertRaises(ValueError):TransferRoute('bscusdt','bsc','USDT','0x'+'c'*40,snapshot)
        with self.assertRaises(ValueError):
            TransferRoute('binance','binance_pay','USDT','uid:42',{'pay_id':42,'uid':None})

    def test_bsc_verifier_uses_stored_contract_and_not_current_config(self):
        request={'provider':'bsc','receiver':'0x'+'c'*40,'route_snapshot':
            {'chain_id':56,'asset':'USDC','contract':'0x'+'d'*40,'decimals':18}}
        adapter=verifier_for(request,bsc_rpc=Mock())
        self.assertEqual(adapter.token.contract,'0x'+'d'*40)
        self.assertEqual(adapter.token.decimals,18)

    def test_approval_service_rejects_admin_impersonation_and_wrong_bsc_contract(self):
        service=PaymentService(Mock(),admin_ids=[9])
        with self.assertRaises(ValueError):service.prepare_claim(CLAIM,Mock(),actor_id=31)
        service.client.rpc.assert_not_called()
        from telegram_bot.commerce.payment_quotes import freeze_quote
        quote=freeze_quote(31,3000,'USDT','50','fixture')
        request={'provider':'bsc','receiver':'0x'+'c'*40,'route_snapshot':{'contract':'0x'+'d'*40,'decimals':18}}
        evidence=TransferEvidence('bsc','56:fixture','USDT','0.6',quote.observed_at,request['receiver'],
            json.dumps({'chain_id':56,'contract':'0x'+'e'*40,'decimals':18}))
        with self.assertRaises(ValueError):service.validate_evidence(request,quote,evidence,{'kind':'admin'})


if __name__=='__main__':unittest.main()
