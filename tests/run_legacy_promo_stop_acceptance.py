"""Real legacy free claim, native confirmation, queue, WAL and partial XLSX export."""
import atexit
import hashlib
import json
import secrets
import threading
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from openpyxl import Workbook
import openpyxl.worksheet._writer as worksheet_writer
from run_commerce_database_acceptance import ROOT
sys.path.insert(0,str(ROOT))
from manager.event_bus import EventBus
from manager.queue import QueueService
from manager.orchestrator import Orchestrator
from manager.continuous_service import ContinuousQueueService
from telegram_bot.bot import TelegramBotRunner
from telegram_bot.payment_manager import PaymentManager

def retain_xml(writer):
    if writer.out in worksheet_writer.ALL_TEMP_FILES:worksheet_writer.ALL_TEMP_FILES.remove(writer.out)
worksheet_writer.WorksheetWriter.cleanup=retain_xml
atexit.unregister(worksheet_writer._openpyxl_shutdown)

def run_case(case,partial):
    root=ROOT/'scratch'/('legacy_promo_stop_'+secrets.token_hex(6));root.mkdir(parents=True)
    source=root/'input.xlsx';book=Workbook();book.active.append(['number','amount'])
    for index in range(3):book.active.append(['100000000'+str(index),0])
    book.save(source);book.close();digest=hashlib.sha256(source.read_bytes()).hexdigest()
    token='123456abcdef';owner=31
    manager=PaymentManager(root);manager.add_promo_code('ONE_SHEET','free',10,100)
    assert manager.apply_promo_code('ONE_SHEET',owner,claim_id=token)[0]
    mapping={'lookup_col':'A','amount_col':'B','has_headers':True,'telegram_chat_id':owner,'payment_token':'telegram:31:'+token}
    queue=QueueService(root/'queue.json')
    job=queue.add_job(source,0,'Sheet',total_records=3,column_mapping=mapping,mode='smart_hybrid',amount_target='remaining')
    other=queue.add_job(root/'unpaid-other.xlsx',0,'Sheet',total_records=1)
    manager.record_payment_draft(token,owner,{'path':str(source),'sheet_name':'Sheet','egp_amount':0,
        'promo_code':'ONE_SHEET','promo_applied':True,'promo_discount':100},kind='promo')
    manager.complete_payment_draft(token,job.id)
    tracked=[manager.config_path,manager.pending_path,manager.wallets_path]
    saved={str(path):path.read_bytes() for path in tracked}
    actor=SimpleNamespace(worker_id='fixture-worker',task_scope='both',can_check_accounts=True,
        can_check_services=True,status='ready',ip_group='fixture-route',use_proxy=False,current_task=None,
        assign_task=Mock(),record_outcome=Mock(),is_in_cooldown=lambda:False)
    supervisor=Mock();supervisor.get_all_workers.return_value=[]
    supervisor.get_worker.side_effect=lambda name:actor if name==actor.worker_id else None
    orch=Orchestrator(supervisor,queue,root);client=Mock()
    client.rpc.side_effect=lambda name,args:{'users':0,'expired_minor':0} if name=='expire_promotional_credit' else (_ for _ in ()).throw(AssertionError('Legacy free stop must not invoke financial RPCs'))
    service=ContinuousQueueService(orch,queue,root,client);service.enabled=False;orch.continuous_service=service
    bot=TelegramBotRunner.__new__(TelegramBotRunner);bot.project_root=root;bot._ui_lock=threading.RLock()
    bot.authorized_id=9;bot.authorized_ids={9};bot._authorized_override=True
    bot._sheet_drafts={};bot._waiting_user_inputs={};bot._show_panel=Mock();bot.queue_service=queue;bot.orchestrator=orch
    bot._commerce_client=client;bus=EventBus();done=threading.Event();events=[]
    def completion(event,payload):events.append(payload);done.set()
    bus.subscribe('session_completed',completion)
    with (patch('manager.orchestrator.EVENT_BUS',bus),patch('manager.queue.EVENT_BUS',bus),
          patch('urllib.request.urlopen',side_effect=AssertionError('External I/O forbidden'))):
        if partial:
            assert orch.start_queue(job_id=job.id)
            lease=orch.lease_next_task_for_worker(actor.worker_id)
            orch.record_task_outcome(lease['task_id'],0,'match','verified-zero',actor.worker_id)
        starts=supervisor.start_all.call_count
        bot._history_callback('oh:stop:'+token,owner)
        review=next(iter(bot._customer_stop_reviews))
        bot._history_callback('oh:stopconfirm:'+review,owner,callback={'id':'native-free-stop'})
        if partial:service.tick()
        else:assert service.tick()
        assert done.wait(30),'Stopped legacy request did not export its partial result'
        assert events[0]['telegram_chat_id']==owner and events[0]['customer_stop']
        assert events[0]['cancelled']==3-int(partial)
        assert supervisor.start_all.call_count==starts
        assert queue.get_job(other.id).status=='pending'
        assert Path(events[0]['result_file']).is_file()
        assert all(path.read_bytes()==saved[str(path)] for path in tracked)
        assert not manager.validate_promo_code('ONE_SHEET',owner)[0]
        assert hashlib.sha256(source.read_bytes()).hexdigest()==digest
    return {'case':case,'committed_before_stop':int(partial),'cancelled':3-int(partial),'partial_result_exported':True,
        'sheet_offer_stays_consumed':True,'financial_files_unchanged':True,'other_unpaid_job_not_started':True,
        'browser_workers_launched_for_stop':0,'native_customer_confirmation':True}

def main():
    cases=[run_case('unstarted_free_sheet',False),run_case('partial_free_sheet',True)]
    proof={'passed':True,'production':False,'cases':cases,'telegram_messages':'synthetic','external_checker_requests':False}
    path=ROOT/'docs/telegram_commerce/legacy_promo_stop_acceptance_20261015.json'
    path.write_text(json.dumps(proof,indent=2),encoding='utf-8');print(json.dumps({'passed':True,'evidence':str(path)}))

if __name__=='__main__':main()
