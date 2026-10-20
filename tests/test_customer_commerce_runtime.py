"""Offline customer queue, draining, limits and reviewed prices; retain fixtures."""
import copy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
from uuid import uuid4
from openpyxl import Workbook
from domain.models import Customer
from manager.continuous_service import ContinuousQueueService
from manager.orchestrator import Orchestrator
from manager.queue import QueueService
from manager.recovery import input_signature
from telegram_bot.bot import TelegramBotRunner
from telegram_bot.sheet_workflow import customer_sheet_limit
from telegram_bot.commerce.finance import result_rows
from telegram_bot.commerce.order_input import quote_rows


class CustomerRuntimeTests(unittest.TestCase):
    def test_stopped_legacy_free_checkout_drains_even_when_consumer_disabled(self):
        import json
        token='123456abcdef'
        job=self.queue.add_job(self.root/'free.xlsx',0,'Sheet',total_records=2,column_mapping={
            'telegram_chat_id':31,'payment_token':'telegram:31:'+token,
            'customer_stop':{'actor':31,'reference':'telegram-customer-stop:31:native-confirm'}})
        (self.root/'pending_payments.json').write_text(json.dumps({token:{'user_id':'31','status':'fulfilled','kind':'promo','job_id':job.id}}),encoding='utf-8')
        self.service.enabled=False;self.orch.start_queue=Mock(return_value=True)
        self.assertTrue(self.service.tick());self.orch.start_queue.assert_called_once_with(job_id=job.id)

    def test_legacy_stop_receipt_cannot_start_unaccepted_or_other_customers_job(self):
        import json
        token='123456abcdef'
        job=self.queue.add_job(self.root/'free.xlsx',0,'Sheet',total_records=2,column_mapping={
            'telegram_chat_id':31,'payment_token':'telegram:31:'+token,
            'customer_stop':{'actor':31,'reference':'telegram-customer-stop:31:native-confirm'}})
        self.orch.start_queue=Mock(return_value=True)
        for status,owner in [('ready','31'),('fulfilled','32')]:
            with self.subTest(status=status,owner=owner):
                (self.root/'pending_payments.json').write_text(json.dumps({token:{'user_id':owner,'status':status,'job_id':job.id}}),encoding='utf-8')
                self.service._next_start=0
                self.assertFalse(self.service.tick());self.orch.start_queue.assert_not_called()

    def setUp(self):
        self.root=Path(tempfile.mkdtemp(prefix='customer-runtime-retained-'))
        self.queue=QueueService(self.root/'queue.json')
        self.actor=SimpleNamespace(worker_id='worker-1',task_scope='both',can_check_accounts=True,can_check_services=True,
            status='ready',ip_group='fixture-route',use_proxy=False,current_task=None,assign_task=Mock(),record_outcome=Mock(),is_in_cooldown=lambda:False)
        self.supervisor=Mock();self.supervisor.get_worker.side_effect=lambda name:self.actor if name=='worker-1' else None
        self.supervisor.get_all_workers.return_value=[self.actor]
        self.orch=Orchestrator(self.supervisor,self.queue,self.root)
        self.client=Mock();self.orders={}
        def rpc(name,args):
            if name=='expire_promotional_credit':return {'users':0,'expired_minor':0}
            if name=='checkout_state':return copy.deepcopy(self.orders[args['p_order_id']])
            if name=='customer_order_detail':
                state=self.orders[args['p_order_id']]
                if state.get('suspended'):raise ValueError('Active registered customer required')
                return {key:state[key] for key in ('order_id','owner_telegram_id','checker_job_id')}
            raise AssertionError(name)
        self.client.rpc.side_effect=rpc
        self.service=ContinuousQueueService(self.orch,self.queue,self.root,self.client)
        self.orch.continuous_service=self.service
        self.network=patch('urllib.request.urlopen',side_effect=AssertionError('External I/O forbidden'))
        self.network.start();self.addCleanup(self.network.stop)

    def job(self,owner=31,count=3,funded=True):
        order=str(uuid4())
        mapping={'lookup_col':'A','amount_col':'B','has_headers':True,'telegram_chat_id':owner,
                 'payment_token':'fixture:'+order,'commerce_order_id':order,'commerce_input_sha256':'a'*64}
        job=self.queue.add_job(file_path=self.root/'.commerce/inputs'/('a'*64+'.xlsx'),sheet_index=0,sheet_name='Sheet',mode='smart_hybrid',
            amount_target='remaining',column_mapping=mapping,total_records=count)
        self.orders[order]={'order_id':order,'payment_state':'reserved' if funded else 'unpaid',
            'reservation_state':'held' if funded else None,'refund_consent_reference':'native-consent' if funded else None,'requested_checks':count,
            'owner_telegram_id':owner,'checker_job_id':job.id}
        return job

    def activate(self,job):
        self.orch.current_job_id=job.id;self.queue.mark_job_active(job.id);self.orch.is_running=True
        self.orch.customers=[Customer(row_number=n+2,record_type='account',lookup_number='100000000'+str(n),
            contract='100000000'+str(n),expected_amount=0) for n in range(job.total_records)]
        self.orch.mode='smart_hybrid';self.orch.amount_target='remaining';self.orch.session_token='fixture-session'
        self.orch._init_task_queues_locked();self.orch._save_checkpoint_locked=Mock()
        self.orch.wal_path=self.root/'journal.jsonl';self.orch.input_signature='fixture-input'

    def test_customer_stop_preserves_inflight_lease_and_commits_only_unleased_rows(self):
        job=self.job();self.activate(job)
        lease=self.orch.lease_next_task_for_worker('worker-1');other=self.job(owner=32,count=1)
        self.queue.request_customer_stop(job.id,31,'native-stop-receipt');self.orch.apply_customer_stop(job.id)
        self.assertEqual(self.orch.completed_indices,{1,2});self.assertIn(lease['task_id'],self.orch.active_leases)
        self.assertTrue(all(r['status']=='cancelled' for r in self.orch.all_completed_records.values()))
        self.assertEqual(self.queue.get_job(other.id).status,'pending')
        self.assertIsNone(self.orch.lease_next_task_for_worker('another-worker'))
        self.orch.record_task_outcome(lease['task_id'],0,'match','verified-zero','worker-1')
        self.assertEqual(self.orch.completed_indices,{0,1,2});self.assertEqual(self.orch.matches_count,1)
        self.assertEqual(result_rows(list(self.orch.all_completed_records.values())),
            [{'row_number':2,'state':'succeeded','verified_result':True},{'row_number':3,'state':'cancelled','verified_result':False},
             {'row_number':4,'state':'cancelled','verified_result':False}])

    def test_other_customer_cannot_stop_a_job(self):
        job=self.job()
        before=self.queue.storage_path.read_bytes()
        with self.assertRaises(ValueError):self.queue.request_customer_stop(job.id,32,'forged')
        self.assertEqual(before,self.queue.storage_path.read_bytes())

    def test_stop_checkpoint_signature_preserves_previously_completed_work(self):
        job=self.job();self.activate(job);mapping=job.column_mapping
        original=input_signature(self.orch.customers,mapping,'smart_hybrid','remaining',0)
        self.queue.request_customer_stop(job.id,31,'native-stop')
        self.assertEqual(original,input_signature(self.orch.customers,job.column_mapping,'smart_hybrid','remaining',0))

    def test_stop_persistence_failure_rolls_back_the_request(self):
        job=self.job()
        with patch.object(self.queue,'_save_to_disk',side_effect=OSError('fixture storage failure')):
            with self.assertRaises(OSError):self.queue.request_customer_stop(job.id,31,'native-stop')
        self.assertNotIn('customer_stop',job.column_mapping)

    def test_regular_worker_cannot_forge_cancelled_outcomes(self):
        job=self.job();self.activate(job);lease=self.orch.lease_next_task_for_worker('worker-1')
        self.orch.record_task_outcome(lease['task_id'],None,'cancelled','forged','worker-1')
        self.assertEqual(self.orch.completed_indices,set())

    def test_continuous_queue_processes_ten_customers_in_order_without_payment_calls(self):
        jobs=[self.job(owner=100+n,count=1) for n in range(10)]
        started=[]
        def start(job_id):
            started.append(job_id);self.orch.is_running=True;self.queue.mark_job_active(job_id);return True
        self.orch.start_queue=Mock(side_effect=start);self.service.enabled=True
        for job in jobs:
            self.service._next_start=0
            self.assertTrue(self.service.tick());self.assertEqual(started[-1],job.id)
            self.assertFalse(self.service.tick());self.queue.mark_job_completed(job.id);self.orch.is_running=False
        self.assertEqual(started,[j.id for j in jobs])
        self.assertTrue(all(call.args[0] in {'checkout_state','customer_order_detail','expire_promotional_credit'} for call in self.client.rpc.call_args_list))

    def test_continuous_queue_skips_unpaid_and_unconsented_orders(self):
        unpaid=self.job(funded=False);unconsented=self.job(owner=32);funded=self.job(owner=33)
        self.orders[unconsented.column_mapping['commerce_order_id']]['refund_consent_reference']=None
        self.orch.start_queue=Mock(return_value=True);self.service.enabled=True
        self.assertTrue(self.service.tick());self.orch.start_queue.assert_called_once_with(job_id=funded.id)

    def test_disabled_service_does_not_start_queue(self):
        self.service.set_enabled(False)
        self.job();self.orch.start_queue=Mock();self.assertFalse(self.service.tick());self.orch.start_queue.assert_not_called()

    def test_stopped_funded_job_is_processed_while_new_checking_is_disabled(self):
        self.service.set_enabled(False)
        normal=self.job();stopped=self.job(owner=32)
        self.orders[stopped.column_mapping['commerce_order_id']]['customer_stop_reference']='native-stop'
        self.orch.start_queue=Mock(return_value=True)
        self.assertFalse(self.service.enabled)
        self.assertTrue(self.service.tick())
        self.orch.start_queue.assert_called_once_with(job_id=stopped.id)
        self.assertEqual(normal.status,'pending')
        self.assertEqual(stopped.column_mapping['customer_stop']['reference'],'native-stop')

    def test_unpaid_or_unconsented_stops_never_enter_settlement_pipeline(self):
        unpaid=self.job(funded=False);unconsented=self.job(owner=32)
        for job in [unpaid,unconsented]:self.orders[job.column_mapping['commerce_order_id']]['customer_stop_reference']='native-stop'
        self.orders[unconsented.column_mapping['commerce_order_id']]['refund_consent_reference']=None
        self.orch.start_queue=Mock()
        self.assertFalse(self.service.tick());self.orch.start_queue.assert_not_called()

    def test_local_only_stop_cannot_fake_server_owner_confirmation(self):
        job=self.job();self.queue.request_customer_stop(job.id,31,'local-only')
        self.orch.start_queue=Mock();self.service.enabled=True
        self.assertFalse(self.service.tick());self.orch.start_queue.assert_not_called()

    def test_unrelated_unavailable_order_does_not_strand_a_stopped_job(self):
        broken=self.job();stopped=self.job(owner=32)
        self.orders[stopped.column_mapping['commerce_order_id']]['customer_stop_reference']='native-stop'
        original=self.client.rpc.side_effect
        def rpc(name,args):
            if name=='checkout_state' and args['p_order_id']==broken.column_mapping['commerce_order_id']:raise OSError('fixture connection failure')
            return original(name,args)
        self.client.rpc.side_effect=rpc;self.orch.start_queue=Mock(return_value=True)
        self.assertTrue(self.service.tick());self.orch.start_queue.assert_called_once_with(job_id=stopped.id)

    def test_pending_stop_does_not_interrupt_another_customers_active_lease(self):
        active=self.job();self.activate(active);lease=self.orch.lease_next_task_for_worker('worker-1')
        stopped=self.job(owner=32);self.orders[stopped.column_mapping['commerce_order_id']]['customer_stop_reference']='native-stop'
        self.orch.start_queue=Mock()
        self.assertFalse(self.service.tick());self.orch.start_queue.assert_not_called()
        self.assertIn(lease['task_id'],self.orch.active_leases)

    def test_service_enabled_state_survives_restart(self):
        self.service.set_enabled(True)
        restored=ContinuousQueueService(self.orch,self.queue,self.root,self.client)
        self.assertTrue(restored.enabled)

    def test_eligible_customer_starts_automatically_without_a_policy_file(self):
        job=self.job();self.orch.start_queue=Mock(return_value=True)
        self.assertTrue(self.service.enabled)
        self.assertTrue(self.service.tick())
        self.orch.start_queue.assert_called_once_with(job_id=job.id)

    def test_explicit_disabled_state_survives_restart(self):
        self.service.set_enabled(False)
        restored=ContinuousQueueService(self.orch,self.queue,self.root,self.client)
        self.assertFalse(restored.enabled)

    def test_suspended_customer_is_skipped_without_blocking_the_next_customer(self):
        blocked=self.job();eligible=self.job(owner=32)
        self.orders[blocked.column_mapping['commerce_order_id']]['suspended']=True
        self.orch.start_queue=Mock(return_value=True)
        self.assertTrue(self.service.tick())
        self.orch.start_queue.assert_called_once_with(job_id=eligible.id)
        self.assertEqual(blocked.status,'pending')

    def test_wrong_owner_or_job_binding_never_starts_checking(self):
        job=self.job();state=self.orders[job.column_mapping['commerce_order_id']]
        self.orch.start_queue=Mock()
        for key,value in [('owner_telegram_id',999),('checker_job_id','another-job')]:
            with self.subTest(field=key):
                original=state[key];state[key]=value;self.service._next_start=0
                self.assertFalse(self.service.tick());self.orch.start_queue.assert_not_called()
                state[key]=original

    def test_paused_or_finalizing_session_does_not_start_another_customer(self):
        self.job();self.orch.start_queue=Mock()
        for flag in ('is_paused','is_finalizing'):
            with self.subTest(flag=flag):
                setattr(self.orch,flag,True)
                self.assertFalse(self.service.tick());self.orch.start_queue.assert_not_called()
                setattr(self.orch,flag,False)

    def test_local_round_advances_without_claiming_an_unfunded_customer(self):
        finished=self.queue.add_job(self.root/'first.xlsx',0,'Sheet',total_records=1)
        customer=self.job(funded=False)
        following=self.queue.add_job(self.root/'second.xlsx',0,'Sheet',total_records=1)
        self.orch.current_job_id=finished.id;self.orch.result_ready=True;self.orch.is_finalizing=True
        self.orch.start_session_from_config=Mock(return_value=True)
        with patch('manager.orchestrator.threading.Thread',side_effect=lambda target,**kw:SimpleNamespace(start=target)):
            self.orch._finalize_job()
        self.assertEqual(self.orch.start_session_from_config.call_args.kwargs['job_id'],following.id)
        self.assertEqual(customer.status,'pending')

    def test_finishing_local_round_wakes_customer_consumer(self):
        finished=self.queue.add_job(self.root/'first.xlsx',0,'Sheet',total_records=1)
        customer=self.job()
        self.orch.current_job_id=finished.id;self.orch.result_ready=True;self.orch.is_finalizing=True
        self.service.wake=Mock()
        self.orch._finalize_job()
        self.service.wake.assert_called_once()
        self.assertFalse(self.orch.is_finalizing)
        self.assertEqual(customer.status,'pending')

    def test_server_stop_ack_recovery_preserves_the_customer_request(self):
        job=self.job();self.orders[job.column_mapping['commerce_order_id']]['customer_stop_reference']='server-recorded-stop'
        self.assertTrue(self.service.funded(job.to_dict()))
        self.assertEqual(job.column_mapping['customer_stop']['reference'],'server-recorded-stop')

    def test_customer_limit_has_clear_boundary_and_counts_populated_duplicate_rows(self):
        for count,ok in [(4000,True),(4001,False)]:
            draft={'validation':{'ok':True,'rows':count,'problems':[]},'admin':False}
            self.assertEqual(customer_sheet_limit(draft)['ok'],ok)
        draft={'validation':{'ok':True,'rows':4001,'problems':[]},'admin':True}
        self.assertTrue(customer_sheet_limit(draft)['ok'])

    def test_leading_zero_service_number_is_priced_as_service(self):
        book=Workbook();book.active.append(['number','amount']);book.active.append(['02000000001',0])
        self.assertEqual(quote_rows(book.active,{'lookup_col':'A','amount_col':'B','has_headers':True}),[{'row_number':2,'check_type':'service'}])
        book.close()


class CustomerPricingUiTests(unittest.TestCase):
    def setUp(self):
        self.bot=TelegramBotRunner.__new__(TelegramBotRunner);self.bot._ui_lock=threading.RLock()
        self.bot._waiting_user_inputs={};self.bot._show_panel=Mock();self.bot._authorized_override=True
        self.bot.authorized_ids={9};self.bot.authorized_id=9;self.client=Mock();self.bot._commerce_client=self.client
        self.client.rpc.return_value={'base_minor':1,'service_excess_minor':1,'service_threshold':500}

    def value(self,text):
        waiting=self.bot._waiting_user_inputs.pop(9);self.bot._commerce_text_input(9,text,waiting)

    def test_customer_cannot_access_pricing_write_or_review(self):
        self.bot._pricing_callback('pr:edit:0',31);self.client.rpc.assert_not_called()

    def test_prices_require_review_and_native_same_actor_confirmation(self):
        self.bot._pricing_callback('pr:edit:0',9,4);self.value('0.01');self.value('0.05')
        token=next(iter(self.bot._price_reviews));self.client.rpc.assert_not_called()
        self.bot._pricing_callback('pr:save:'+token,31,4,{'id':'foreign'});self.client.rpc.assert_not_called()
        self.bot._pricing_callback('pr:save:'+token,9,4,{'id':'native'})
        write=self.client.rpc.call_args_list[0]
        self.assertEqual(write.args[0],'configure_customer_pricing')
        self.assertEqual(write.args[1]['p_rules'],{'base_minor':1,'service_excess_minor':5,'service_threshold':500})

    def test_price_subcent_input_does_not_advance_review(self):
        self.bot._pricing_callback('pr:edit:0',9,4);self.value('0.001')
        token=next(iter(self.bot._price_reviews));self.assertEqual(self.bot._price_reviews[token]['step'],0)
        self.client.rpc.assert_not_called()

if __name__=='__main__':unittest.main()
