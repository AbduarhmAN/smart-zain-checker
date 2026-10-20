"""Synthetic local baseline of the real legacy handler/orchestrator lock path.

This does not claim to reproduce production network or Chromium behavior.
All transport operations are fixtures; no processing workers are launched.
"""
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))


def main():
    fixture=Path(tempfile.mkdtemp(prefix='monolith_baseline_retained_')).resolve();os.chdir(fixture)
    os.environ['ZAIN_SERVICE_MODE']='0'
    def blocked(*_,**__):raise RuntimeError('External network forbidden by baseline')
    socket.socket.connect=blocked;socket.create_connection=blocked
    from openpyxl import Workbook
    from manager.queue import QueueService
    from manager.orchestrator import Orchestrator
    from telegram_bot.bot import TelegramBotRunner
    from services.testing_telegram import UnavailableCommerceFixture
    root=fixture;source=root/'synthetic.xlsx'
    workbook=Workbook(write_only=True);sheet=workbook.create_sheet('Data');sheet.append(['Account','Amount'])
    for index in range(50000):sheet.append([str(1000000000+index),12.34])
    workbook.save(source);workbook.close()
    supervisor=SimpleNamespace(get_all_workers=lambda:[],start_all=lambda **_:None,stop_all=lambda:None,get_status_summary=lambda:[])
    queue=QueueService(root/'.zain-queue.json');engine=Orchestrator(supervisor,queue,root)
    bot=TelegramBotRunner(engine,queue,root,bot_token='isolated-baseline-fixture',authorized_id=9,authorized_ids=[9])
    bot.commerce_bridge=SimpleNamespace(worker=SimpleNamespace(client=UnavailableCommerceFixture()))
    bot._wallet_account_label=lambda _: 'fixture';bot._wallet_display_balance=lambda _:0
    bot._refresh_partner_menu_for_chat=lambda _:None
    replies=[]
    def api(method,data=None,timeout=25):replies.append(method);return {'ok':True,'result':{'message_id':1}}
    bot._api_call=api
    def probe(command):
        started=time.perf_counter()
        bot._handle_update({'message':{'from':{'id':9},'chat':{'id':9,'type':'private'},'text':command}})
        return (time.perf_counter()-started)*1000
    def stats(values):
        ordered=sorted(values)
        return {'samples':len(values),'p50_ms':ordered[int((len(values)-1)*.5)],'p95_ms':ordered[int((len(values)-1)*.95)],'p99_ms':ordered[int((len(values)-1)*.99)],'max_ms':max(values)}
    idle=[probe('/start') for _ in range(25)]
    job=queue.add_job(source,0,'Data',mode='account_only',column_mapping={'lookup_col':'A','amount_col':'B','has_headers':True},total_records=50000)
    result=[];initialization=[]
    def initialize():
        started=time.perf_counter()
        result.append(engine.start_queue(job.id));initialization.append(time.perf_counter()-started)
    thread=threading.Thread(target=initialize,name='BaselineInitialization');thread.start()
    # Wait for the real startup operation to hold the same lock used by /start.
    deadline=time.monotonic()+10;contended=False
    while thread.is_alive() and time.monotonic()<deadline:
        acquired=engine.lock.acquire(False)
        if acquired:engine.lock.release();time.sleep(.005)
        else:contended=True;break
    under_load=[probe('/start')]
    thread.join(timeout=60)
    if thread.is_alive():raise RuntimeError('Baseline initialization did not finish')
    under_load.extend(probe('/start') for _ in range(24))
    engine.cancel_session();bot._executor.shutdown(wait=True)
    control=getattr(bot,'_control_executor',None)
    if control:control.shutdown(wait=True)
    report={'synthetic_data_only':True,'external_network':False,'actual_legacy_handler':True,'processing_workers_launched':False,
        'input_rows':50000,'idle_start':stats(idle),'during_initialization_start':stats(under_load),
        'initialization_seconds':initialization[0],'initialization_succeeded':result==[True],
        'observed_shared_lock_contention':contended,'fixture':str(fixture),
        'limitation':'One startup overlap followed by normal observations; p95 does not represent sustained production Chromium load. No Telegram network latency included.'}
    (ROOT/'docs/four_services/monolith_baseline.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
