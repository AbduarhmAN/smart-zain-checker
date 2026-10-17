"""Real cancel/export/financial settlement; no customer messages or Zain calls."""
import atexit
import hashlib
import json
from pathlib import Path
import secrets
import threading
from types import SimpleNamespace
from unittest.mock import Mock,patch
from openpyxl import Workbook
import openpyxl.worksheet._writer as worksheet_writer
from run_commerce_database_acceptance import ROOT,DATA,sql
from run_commerce_bridge_acceptance import LocalClient,drain
from manager.event_bus import EventBus
from manager.queue import QueueService
from manager.orchestrator import Orchestrator
from manager.continuous_service import ContinuousQueueService
from telegram_bot.commerce.checkout import snapshot_input
from telegram_bot.commerce.bridge import CommerceBridge
from telegram_bot.commerce.finance import FinanceService,VerifiedReceipt


def retain_xml(writer):
    if writer.out in worksheet_writer.ALL_TEMP_FILES:worksheet_writer.ALL_TEMP_FILES.remove(writer.out)
worksheet_writer.WorksheetWriter.cleanup=retain_xml
atexit.unregister(worksheet_writer._openpyxl_shutdown)


def run_case(database,case,count,partial):
    root=ROOT/'scratch'/database/case;root.mkdir(parents=True,exist_ok=True)
    source=root/'input.xlsx';book=Workbook();book.active.append(['number','amount'])
    for index in range(count):book.active.append(['100000000'+str(index),0])
    book.save(source);book.close()
    source_hash=hashlib.sha256(source.read_bytes()).hexdigest()
    frozen,digest=snapshot_input(root,source)
    client=LocalClient(database);finance=FinanceService(client);owner=887410+(1 if partial else 0)
    finance.confirm_payment(owner,'fund-'+case,VerifiedReceipt('fixture','tx-'+case,5000,'synthetic-approved-funds'))
    rows=[{'row_number':index+2,'check_type':'account'} for index in range(count)]
    result=client.rpc('create_order',{'p_telegram_user_id':owner,'p_request_key':case,
        'p_rows':rows,'p_source':{'file_sha256':digest}});order=result['order_id']
    state=client.rpc('checkout_state',{'p_order_id':order,'p_telegram_user_id':owner})
    reference='native-refund-'+case
    client.rpc('checkout_consent',{'p_order_id':order,'p_telegram_user_id':owner,
        'p_policy_version':state['pricing_snapshot']['policy_version'],'p_reference':reference})
    finance.reserve(order,owner,reference)
    mapping={'lookup_col':'A','amount_col':'B','has_headers':True,'telegram_chat_id':owner,
        'commerce_order_id':order,'commerce_input_sha256':digest,'commerce_rows':rows}
    queue=QueueService(root/'queue.json')
    job=queue.add_job(file_path=frozen,sheet_index=0,sheet_name='Sheet',mode='smart_hybrid',
        amount_target='remaining',column_mapping=mapping,total_records=count)
    other=queue.add_job(file_path=root/'unpaid-other.xlsx',sheet_index=0,sheet_name='Sheet',mode='smart_hybrid',total_records=1)
    client.rpc('bind_checker_job',{'p_order_id':order,'p_job_id':job.id})
    actor=SimpleNamespace(worker_id='fixture-worker',task_scope='both',can_check_accounts=True,
        can_check_services=True,status='ready',ip_group='fixture-route',use_proxy=False,
        current_task=None,assign_task=Mock(),record_outcome=Mock(),is_in_cooldown=lambda:False)
    supervisor=Mock();supervisor.get_all_workers.return_value=[]
    supervisor.get_worker.side_effect=lambda name:actor if name==actor.worker_id else None
    orchestrator=Orchestrator(supervisor,queue,root)
    service=ContinuousQueueService(orchestrator,queue,root,client);orchestrator.continuous_service=service
    service.set_enabled(False)
    bus=EventBus();bridge=CommerceBridge(root,client=client);done=threading.Event();completion=[]
    def captured(event,payload):
        try:completion.append(bridge.capture_completion(payload))
        finally:done.set()
    bus.subscribe('session_completed',captured)
    with (patch('manager.orchestrator.EVENT_BUS',bus),patch('manager.queue.EVENT_BUS',bus),
          patch('urllib.request.urlopen',side_effect=AssertionError('External I/O forbidden'))):
        if partial:
            if not orchestrator.start_queue(job_id=job.id):raise AssertionError('Partial checkpoint fixture did not start')
            lease=orchestrator.lease_next_task_for_worker(actor.worker_id)
            orchestrator.record_task_outcome(lease['task_id'],0,'match','verified-zero',actor.worker_id)
            orchestrator.cancel_session()
        starts_before=supervisor.start_all.call_count
        client.rpc('request_customer_stop',{'p_order_id':order,'p_telegram_user_id':owner,'p_reference':'native-stop-'+case})
        # Deliberately omit local stop metadata: model a crash after SQL commit.
        if service.enabled:raise AssertionError('New checking must be disabled in this scenario')
        if not service.tick():raise AssertionError('Disabled consumer stranded a stopped, funded order')
        if not done.wait(45):raise AssertionError('Cancellation did not reach result capture')
        if not completion:raise AssertionError('Cancelled result was not captured')
        if supervisor.start_all.call_count!=starts_before:raise AssertionError('Cancellation launched browser workers')
        if queue.get_job(other.id).status!='pending':raise AssertionError('Stopped job advanced an unpaid customer')
        if queue.get_job(job.id).cancelled_count!=count-(1 if partial else 0):raise AssertionError('Cancellation lost committed rows')
        drain(bridge)
        held=client.rpc('wallet_state',{'p_telegram_user_id':owner})['customer']
        if held['reserved_minor']!=count:raise AssertionError('Capture settled before actual delivery')
        receipt={'chat':{'id':owner},'message_id':987,'document':{'file_id':'synthetic-stop-'+case}}
        bridge.capture_delivery(completion[0],receipt);drain(bridge)
        charged=1 if partial else 0
        for _ in range(2):
            settled=finance.settle(order)
            if settled['charged_minor']!=charged or settled['refunded_minor']!=count-charged:
                raise AssertionError('Stop refund or replay amount is incorrect')
        wallet=client.rpc('wallet_state',{'p_telegram_user_id':owner})['customer']
        if wallet['reserved_minor']!=0 or wallet['available_minor']!=5000-charged:
            raise AssertionError('Wallet conservation failed after stop')
        if hashlib.sha256(source.read_bytes()).hexdigest()!=source_hash:raise AssertionError('Original customer input changed')
    return {'case':case,'rows':count,'charged_minor':charged,'refunded_minor':count-charged,
            'available_minor':wallet['available_minor'],'workers_launched_during_stop':0,
            'other_order_still_pending':True,'new_checking_enabled':service.enabled}


def main():
    if Path(sql('postgres',statement='show data_directory')[0]).resolve()!=DATA.resolve():
        raise RuntimeError('Unknown database instance; refusing changes')
    database='commerce_stopped_queue_'+secrets.token_hex(6);sql('postgres',statement=f'create database {database}')
    for path in sorted((ROOT/'supabase/migrations').glob('*commerce*.sql')):sql(database,file=path)
    cases=[run_case(database,'unstarted_full_refund',2368,False),run_case(database,'partial_preserved_refund',3,True)]
    proof={'passed':True,'production':False,'database':database,'telegram_delivery':'synthetic; no external messages',
        'cases':cases,'original_files_retained':True,'deletion':'none'}
    path=ROOT/'docs/telegram_commerce'/f'acceptance_{database}.json'
    path.write_text(json.dumps(proof,indent=2),encoding='utf-8');print(json.dumps({'passed':True,'evidence':str(path)}))


if __name__=='__main__':main()
