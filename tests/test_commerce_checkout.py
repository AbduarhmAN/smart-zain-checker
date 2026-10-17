import copy
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch
from openpyxl import Workbook
from manager.queue import QueueService
from manager.orchestrator import Orchestrator
from telegram_bot.bot import TelegramBotRunner
from telegram_bot.commerce.bridge import CommerceBridge, file_sha256
from telegram_bot.commerce.client import CommerceUnavailable
from telegram_bot.commerce.payment_providers import CryptoInvoiceAdapter
from telegram_bot.commerce.rates import asset_egp_rate
from telegram_bot.sheet_workflow import file_version

TOKEN = 'abcdef012345'
ORDER = '5086d622-0941-4454-9db4-9294af1cbb5b'
PAYMENT = '0364662b-eb5b-4915-bc4e-577bd7a72863'


class CheckoutTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='commerce-checkout-retained-'))
        self.original = self.root/'fixture.xlsx'
        book = Workbook()
        for row in [['رقم','مبلغ'],['1000200989',100],['1000200989',100],['2000000001',50]]:
            book.active.append(row)
        book.save(self.original); book.close()
        self.bot = TelegramBotRunner.__new__(TelegramBotRunner)
        self.bot.project_root = self.root
        self.bot._ui_lock = threading.RLock()
        self.bot._waiting_user_inputs = {}
        self.bot._sheet_drafts = {}
        self.bot.authorized_id = 9
        self.bot.authorized_ids = {9}
        self.bot._authorized_override = True
        self.bot._show_panel = Mock()
        self.bot._api_call = Mock()
        self.bot.orchestrator = SimpleNamespace(lock=threading.RLock())
        self.bot.queue_service = QueueService(self.root/'.sheet_queue.json')
        self.bot._restore_sheet_drafts()
        self.bot._sheet_drafts[TOKEN] = {'path':self.original,'chat':31,'expires':time.monotonic()+1800,
            'version':file_version(self.original),'sheet_index':0,'sheet_name':'Sheet','sheets':['Sheet'],
            'columns':[],'mapping':{'lookup_col':'A','amount_col':'B','has_headers':True},
            'metadata':{},'mode':'smart_hybrid','amount_target':'remaining','job_id':None,
            'validation':{'ok':True,'rows':3,'problems':[]}}
        self.state = None
        self.payment_request = None
        self.lose_reserve = False
        self.client = Mock()
        self.client.rpc.side_effect = self.rpc
        self.bot._commerce_client = self.client
        self.bot.commerce_bridge = CommerceBridge(self.root,client=self.client)
        self.env = patch.dict(os.environ, {'ZAIN_COMMERCE_CHECKOUT':'1'})
        self.env.start(); self.addCleanup(self.env.stop)
        self.legacy = patch.object(self.bot,'_commerce_legacy_payment',return_value=False)
        self.legacy.start(); self.addCleanup(self.legacy.stop)
        self.network = patch('urllib.request.urlopen',side_effect=AssertionError('Live network forbidden'))
        self.network.start(); self.addCleanup(self.network.stop)

    def rpc(self, name, args):
        if name == 'create_order':
            if self.state is None:
                self.state = {'order_id':ORDER,'quoted_minor':6000,'requested_checks':2,
                    'pricing_snapshot':{'policy_version':1},'source_snapshot':args['p_source'],
                    'order_state':'draft','payment_state':'unpaid','reservation_state':None,
                    'refund_consent_reference':None}
            return {'order_id':ORDER}
        if name == 'checkout_state':
            if args['p_telegram_user_id']!=31: raise ValueError('Wrong owner')
            return copy.deepcopy(self.state)
        if name == 'checkout_consent':
            self.state['refund_consent_reference'] = self.state['refund_consent_reference'] or args['p_reference']
            return {'reference':self.state['refund_consent_reference']}
        if name == 'reserve_order':
            self.state.update(order_state='confirmed',payment_state='reserved',reservation_state='held')
            if self.lose_reserve:
                self.lose_reserve = False
                raise CommerceUnavailable('Synthetic lost acknowledgment')
            return {'state':'held'}
        if name == 'create_payment_request':
            if self.payment_request is None:
                self.payment_request = {'quote':args['p_quote'],'provider':args['p_provider'],
                    'receiver':args['p_receiver'],'invoice_id':None,'invoice_state':'none'}
            return {'state':'issued'}
        if name == 'payment_request': return self.payment_request
        if name == 'begin_payment_invoice':
            state = self.payment_request['invoice_state']
            self.payment_request['invoice_state'] = 'started'
            return {'state':'claimed' if state=='none' else state}
        if name == 'attach_payment_invoice':
            self.payment_request.update(invoice_id=args['p_invoice_id'],invoice_state='attached')
            return {'invoice_id':args['p_invoice_id']}
        if name == 'confirm_payment_request': return {'payment_id':PAYMENT}
        raise AssertionError(name)

    def prepare(self): self.bot._commerce_start_checkout(TOKEN,31,4)
    def callback(self, action, callback_id='update:1', user=31):
        self.bot._handle_callback({'id':callback_id,'from':{'id':user},
            'message':{'chat':{'id':user},'message_id':4},'data':f'zc:co:{action}:{TOKEN}'})

    def test_executable_rows_price_and_explicit_consent_before_spend(self):
        self.prepare()
        self.assertEqual(self.state['requested_checks'],2)
        self.assertEqual(len(self.bot.queue_service.get_jobs()),0)
        panel = self.bot._show_panel.call_args.args[1]
        self.assertIn('60.00',panel.text)
        self.assertIn('توافق',panel.text)
        self.assertFalse(any(call.args[0]=='reserve_order' for call in self.client.rpc.call_args_list))
        self.callback('wallet')
        self.assertEqual(len(self.bot.queue_service.get_jobs()),1)
        self.assertIn('telegram:31:update:1',self.state['refund_consent_reference'])
        job = self.bot.queue_service.get_jobs()[0]
        self.assertEqual(job['column_mapping']['commerce_order_id'],ORDER)
        self.assertTrue(job['filename'].startswith('.commerce/inputs/'))
        self.assertEqual(job['total_records'],2)
        self.assertIsNone(job['pricing'])
        self.assertEqual(job['column_mapping']['commerce_quoted_minor'],6000)
        self.assertEqual(file_sha256(self.root/job['filename']),self.state['source_snapshot']['file_sha256'])

    def test_lost_reserve_ack_then_retry_enqueues_once_without_second_spend(self):
        self.prepare(); self.lose_reserve = True
        self.callback('wallet')
        self.assertEqual(len(self.bot.queue_service.get_jobs()),0)
        self.callback('wallet','update:2'); self.callback('wallet','update:3')
        self.assertEqual(len(self.bot.queue_service.get_jobs()),1)
        names = [call.args[0] for call in self.client.rpc.call_args_list]
        self.assertEqual(names.count('reserve_order'),1)
        command = self.bot.commerce_bridge.outbox.lease()
        self.assertEqual(command['method'],'bind_checker_job')

    def test_partner_pending_sheet_blocks_all_stale_payment_callbacks_until_owner_approval(self):
        original=self.rpc
        def partner_rpc(name,args):
            result=original(name,args)
            if name=='create_order':
                self.state.update(partner_sheet={'state':'pending'},quoted_minor=0)
            return result
        self.client.rpc.side_effect=partner_rpc
        self.prepare()
        for action in ('wallet','invoice','check'):
            self.callback(action)
        self.assertFalse(any(c.args[0] in {'checkout_consent','reserve_order','create_payment_request'} for c in self.client.rpc.call_args_list))
        self.assertEqual(self.bot.queue_service.get_jobs(),[])
        self.state.update(partner_sheet={'state':'approved'},quoted_minor=1500,
                          pricing_snapshot={'policy_version':1,'partner_cost_sheet':True})
        self.bot._commerce_start_checkout(TOKEN,31,4)
        self.assertIn('15.00',self.bot._show_panel.call_args.args[1].text)
        self.callback('wallet','partner-consent')
        self.assertEqual(len(self.bot.queue_service.get_jobs()),1)
        self.assertEqual(self.bot.queue_service.get_jobs()[0]['column_mapping']['commerce_quoted_minor'],1500)

    def test_restart_recovers_held_checkout_and_expired_original_review(self):
        self.prepare(); self.lose_reserve = True
        self.callback('wallet')
        saved = json.loads((self.root/'.zain-telegram-drafts.json').read_text(encoding='utf-8'))
        saved['drafts'][TOKEN]['expires_at'] = 1
        (self.root/'.zain-telegram-drafts.json').write_text(json.dumps(saved),encoding='utf-8')
        self.bot._sheet_drafts = {}
        self.bot._restore_sheet_drafts()
        self.assertEqual(self.bot._recover_commerce_checkouts(),1)
        self.assertEqual(len(self.bot.queue_service.get_jobs()),1)
        self.assertEqual(self.bot._recover_commerce_checkouts(),0)
        self.assertEqual(len(self.bot.queue_service.get_jobs()),1)

    def test_other_customer_cannot_accept_or_read_checkout(self):
        self.prepare(); self.client.rpc.reset_mock()
        self.callback('wallet',user=32)
        self.client.rpc.assert_not_called()
        self.assertEqual(len(self.bot.queue_service.get_jobs()),0)

    def test_changed_mapping_and_tampered_snapshot_cannot_be_queued(self):
        self.prepare()
        draft = self.bot._sheet_drafts[TOKEN]
        draft['mapping']['amount_col']='A'
        self.callback('wallet')
        self.assertEqual(len(self.bot.queue_service.get_jobs()),0)
        draft['mapping']['amount_col']='B'
        Path(draft['commerce_input']).write_bytes(b'tampered')
        self.callback('wallet')
        self.assertEqual(len(self.bot.queue_service.get_jobs()),0)

    def test_persistence_failure_prevents_external_order_even_on_retry(self):
        with patch.object(self.bot,'_persist_sheet_drafts',return_value=False):
            self.prepare(); self.prepare()
        self.client.rpc.assert_not_called()

    def test_legacy_payment_buttons_and_text_cannot_debit_new_order(self):
        self.prepare()
        with patch('telegram_bot.payment_manager.PAYMENT_MGR.debit_wallet') as debit:
            self.bot._handle_payment_method_callback(f'pay:wallet:{TOKEN}',31,4,{'id':'old'})
            self.bot._handle_crypto_payment_check(f'paycheck:8:{TOKEN}',31,4)
            self.bot._handle_user_text_input(31,'fixture','old-transfer',{'action':'tx_id','token':TOKEN,'message_id':4})
            debit.assert_not_called()
        self.assertEqual(len(self.bot.queue_service.get_jobs()),0)

    def test_funded_queue_mapping_and_fork_are_protected(self):
        self.prepare(); self.callback('wallet')
        job = self.bot.queue_service.get_jobs()[0]
        mapping = copy.deepcopy(job['column_mapping']); mapping['amount_col']='A'
        with self.assertRaises(ValueError):
            self.bot.queue_service.update_pending_mapping(job['id'],column_mapping=mapping,
                mode=job['mode'],amount_target=job['amount_target'],total_records=2)
        with self.assertRaises(ValueError): self.bot.queue_service.fork_job(job['id'])
        self.assertFalse(self.bot.queue_service.remove_job(job['id']))
        with self.assertRaises(ValueError):
            self.bot.queue_service.update_job_pricing(job['id'],{'mode':'sheet','unit_price':'1'})

    def test_engine_rejects_rows_different_from_paid_input_before_start(self):
        self.prepare(); self.callback('wallet')
        job = self.bot.queue_service.get_jobs()[0]
        engine = Orchestrator.__new__(Orchestrator)
        engine.lock = threading.RLock(); engine.is_running=False; engine.project_root=self.root
        mapping = copy.deepcopy(job['column_mapping']); mapping['commerce_rows']=[]
        with self.assertRaises(ValueError):
            engine.start_session_from_config(self.root/job['filename'],0,mapping,
                mode='smart_hybrid',amount_target='remaining')

    def test_invoice_uses_frozen_quote_then_allocates_direct_payment(self):
        self.prepare()
        provider = Mock(base_url='https://pay.crypt.bot/api')
        provider.get_me.return_value={'app_id':7}
        adapter = CryptoInvoiceAdapter(provider,app_id=7)
        emitted = {}
        def create(endpoint, payload):
            self.assertEqual(endpoint,'createInvoice')
            emitted.update(payload,invoice_id=8,status='active',created_at=datetime.now(timezone.utc).isoformat(),
                           bot_invoice_url='https://t.me/CryptoBot?start=fixture')
            return emitted
        provider._call.side_effect=create
        provider.get_invoice.side_effect=lambda invoice_id:dict(emitted)
        with patch.object(self.bot,'_checkout_adapter',return_value=adapter),\
             patch('telegram_bot.commerce.checkout.asset_egp_rate',return_value=('50.1','fixture-rate')):
            self.callback('invoice')
            self.assertEqual(len(self.bot.queue_service.get_jobs()),0)
            self.assertEqual(emitted['amount'],'1.197605')
            self.assertTrue(emitted['payload'].startswith('zain-commerce:'))
            emitted.update(status='paid',paid_at=datetime.now(timezone.utc).isoformat())
            self.callback('check')
        reserve = next(call.args[1] for call in self.client.rpc.call_args_list if call.args[0]=='reserve_order')
        self.assertEqual(reserve['p_payment_id'],PAYMENT)
        self.assertEqual(len(self.bot.queue_service.get_jobs()),1)


class RateTests(unittest.TestCase):
    def fixture(self,updated,base='USD'):
        return io.BytesIO(json.dumps({'result':'success','base_code':base,'time_last_update_unix':updated,
                                     'rates':{'EGP':50.123456}}).encode())

    def test_observed_asset_rate_does_not_assume_stablecoin_peg(self):
        now = datetime.now(timezone.utc)
        provider=Mock()
        provider._call.return_value=[{'source':'USDT','target':'USD','is_valid':True,'is_crypto':True,'rate':'0.99'}]
        rate,source=asset_egp_rate(provider,'USDT',now=now,
            opener=Mock(return_value=self.fixture(int(now.timestamp()))))
        self.assertEqual(Decimal(rate),Decimal('0.99')*Decimal('50.123456'))
        self.assertIn('open.er-api.com',source)

    def test_stale_future_wrong_base_and_invalid_asset_rates_fail(self):
        now=datetime.now(timezone.utc); stamp=int(now.timestamp())
        provider=Mock()
        provider._call.return_value=[{'source':'USDT','target':'USD','is_valid':True,'is_crypto':True,'rate':'1'}]
        for updated,base in [(stamp-172801,'USD'),(stamp+60,'USD'),(stamp,'EUR')]:
            with self.subTest(updated=updated,base=base),self.assertRaises(ValueError):
                asset_egp_rate(provider,'USDT',now=now,opener=Mock(return_value=self.fixture(updated,base)))
        provider._call.return_value[0]['is_valid']=False
        with self.assertRaises(ValueError): asset_egp_rate(provider,'USDT',opener=Mock())


if __name__=='__main__': unittest.main()
