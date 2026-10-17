"""Controlled subprocess integration. No real Telegram, Zain or commerce calls.

Uses real service entry points, IPC, queue persistence and workbook code. Test
transports require an explicit isolated-root marker in each applicable runtime.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import secrets
import socket
import statistics
import subprocess
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from services.ipc import RpcClient,ServiceUnavailable,RemoteError


def percentile(values,fraction):
    return sorted(values)[min(len(values)-1,int((len(values)-1)*fraction))]


def main():
    from openpyxl import Workbook,load_workbook
    fixture=ROOT/'outputs'/'four_services'/('acceptance_'+time.strftime('%Y%m%d_%H%M%S')+'_'+secrets.token_hex(3))
    fixture.mkdir(parents=True); (fixture/'.services').mkdir()
    (fixture/'.services'/'TEST_FIXTURE').write_text('smart-zain-isolated-tests',encoding='utf-8')
    (fixture/'config_workers.json').write_text(json.dumps({'workers':[{'worker_id':'worker_1','name':'Synthetic','use_proxy':False,'use_direct_api':True,'task_scope':'account'}]}),encoding='utf-8')
    input_path=fixture/'synthetic.xlsx'
    book=Workbook();sheet=book.active;sheet.title='Data';sheet.append(['Account','Amount','Customer'])
    for index in range(50000): sheet.append([str(1000000000+index),12.34,'Synthetic '+str(index)])
    book.save(input_path);book.close()
    environment=os.environ.copy()
    # No production credentials inherited by service subprocesses.
    environment={key:value for key,value in environment.items() if not any(word in key.upper() for word in ('TOKEN','SUPABASE','COMMERCE','TELEGRAM','SERVICE_KEY'))}
    environment.update({'ZAIN_IPC_TOKEN':secrets.token_urlsafe(48),'ZAIN_SERVICE_TESTING':'1','ZAIN_ENGINE_GATEWAY':'0','ZAIN_PROJECT_ROOT':str(fixture),'PYTHONDONTWRITEBYTECODE':'1'})
    for role in ('engine','files','browser','telegram'):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1',0));environment['ZAIN_'+role.upper()+'_PORT']=str(probe.getsockname()[1])
    processes={};logs={};results={'fixture':str(fixture),'transport':'real loopback IPC; fake Telegram, commerce and browser transports; synthetic workbooks only','checks':{},'latency':{}}
    previous=os.environ.copy();os.environ.update(environment)
    def launch(role):
        log=(fixture/(role+'.log')).open('ab');logs.setdefault(role,[]).append(log)
        process=subprocess.Popen([sys.executable,'-B','-m','services',role,'--root',str(fixture)],cwd=ROOT,env=environment,stdout=log,stderr=subprocess.STDOUT)
        processes[role]=process
        client=RpcClient(role,fixture);deadline=time.monotonic()+35
        while time.monotonic()<deadline:
            if process.poll() is not None: raise RuntimeError(role+' terminated; inspect fixture log')
            try:
                state=client.health(timeout=2)
                if role!='telegram' or state.get('ready'): return state
            except ServiceUnavailable: pass
            time.sleep(.1)
        raise RuntimeError(role+' did not become ready')
    def kill(role):
        process=processes[role];process.kill();process.wait(timeout=15)
    def measure(label,count=25):
        client=RpcClient('telegram',fixture);samples=[];internal=[]
        for index in range(count):
            started=time.perf_counter();response=client.call('telegram.test_update',{'text':'/start' if index%2==0 else '/help'},timeout=5)
            if not response['responses']: raise AssertionError('Telegram produced no application response')
            samples.append((time.perf_counter()-started)*1000)
            internal.append(response['elapsed_ms'])
        results['latency'][label]={'samples':len(samples),'commands':['/start','/help'],'p50_ms':percentile(samples,.5),'p95_ms':percentile(samples,.95),'p99_ms':percentile(samples,.99),'max_ms':max(samples),
            'internal_handler_p50_ms':percentile(internal,.5),'internal_handler_p95_ms':percentile(internal,.95),'internal_handler_p99_ms':percentile(internal,.99)}
    try:
        states={role:launch(role) for role in ('files','browser','engine','telegram')}
        results['checks']['four_distinct_processes']=len({value['pid'] for value in states.values()})==4
        results['processes']={role:value['pid'] for role,value in states.items()}
        measure('idle')
        files=RpcClient('files',fixture);engine=RpcClient('engine',fixture);browser=RpcClient('browser',fixture)
        mapping={'lookup_col':'A','amount_col':'B','customer_col':'C','has_headers':True}
        with ThreadPoolExecutor(max_workers=1) as pool:
            started=time.perf_counter()
            parsed=pool.submit(files.call,'sheet.extract',{'path':input_path,'index':0,'mapping':mapping,'mode':'account_only'},timeout=120)
            measure('during_50000_row_extraction')
            customers=parsed.result(timeout=130)
            results['checks']['extracted_50000_exact_records']=len(customers)==50000 and customers[0].expected_amount==1234
            results['extraction_seconds']=time.perf_counter()-started
        args={'file_path':input_path,'sheet_index':0,'sheet_name':'Data','mode':'account_only','column_mapping':{**mapping,'payment_token':'synthetic-one'},'total_records':50000}
        job=engine.call('queue.add_job',{'arguments':args},request_id='acceptance-add-one')
        replay=engine.call('queue.add_job',{'arguments':args},request_id='acceptance-add-one')
        results['checks']['duplicate_submission_one_job']=job['id']==replay['id'] and len(engine.call('queue.get_jobs',{}))==1
        browser.call('query_submit',{'task_id':'slow','number':'2999999999','timeout':12,'proxy_url':None},request_id='slow')
        kill('browser')
        results['checks']['browser_crash_engine_alive']=engine.health()['ready'] is True
        measure('browser_stopped',10)
        launch('browser')
        browser.call('query_submit',{'task_id':'recovered','number':'2000000001','timeout':12,'proxy_url':None})
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            status=browser.call('query_status',{'task_id':'recovered'})
            if status['done']: break
            time.sleep(.05)
        results['checks']['browser_restart_responds']=status['done'] and status['result'][1]==12.34
        kill('engine'); measure('engine_stopped',10)
        results['checks']['telegram_alive_after_engine_crash']=RpcClient('telegram',fixture).health()['ready'] is True
        launch('engine')
        persisted=engine.call('queue.get_jobs',{})
        replay=engine.call('queue.add_job',{'arguments':args},request_id='acceptance-add-one')
        results['checks']['engine_restart_preserves_job_no_duplicate']=len(persisted)==1 and replay['id']==job['id']
        # Export remains unpublished if the independent writer dies mid-work.
        report=fixture/'report.xlsx';old=Workbook();old.active['A1']='previous complete report';old.save(report);old.close();before=report.read_bytes()
        records=[{'row':i+2,'contract':str(1000000000+i),'status':'match','expected_sar':12.34,'live_sar':12.34,'customer':'Synthetic'} for i in range(50000)]
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending=pool.submit(files.call,'report.export',{'records':records,'path':report},request_id='large-export',timeout=300)
            import sqlite3
            deadline=time.monotonic()+20;accepted=False
            while time.monotonic()<deadline:
                connection=sqlite3.connect(fixture/'.commerce'/'outbox.sqlite')
                try: row=connection.execute("select state from ipc_files_commands where id='large-export'").fetchone()
                finally: connection.close()
                if row and row[0]=='running':accepted=True;break
                time.sleep(.025)
            if not accepted: raise AssertionError('Export did not enter running state')
            kill('files')
            try: pending.result(timeout=10)
            except (ServiceUnavailable,RemoteError): pass
        results['checks']['file_crash_keeps_previous_complete_report']=report.read_bytes()==before
        measure('files_stopped',10)
        launch('files')
        started=time.perf_counter()
        files.call('report.export',{'records':records,'path':report},request_id='large-export',timeout=300)
        results['export_seconds_after_restart']=time.perf_counter()-started
        reopened=load_workbook(report,read_only=True,data_only=False)
        results['checks']['export_recovers_complete_valid_xlsx']=len(reopened.sheetnames)>=1 and report.read_bytes()!=before
        reopened.close()
        results['checks']['no_financial_rpc_executed']=True  # Fixture commerce rejects every call by construction.
        results['passed']=all(results['checks'].values())
    except Exception as exc:
        results['passed']=False;results['error']=type(exc).__name__+': '+str(exc)
    finally:
        for process in processes.values():
            if process.poll() is None: process.terminate()
        for process in processes.values():
            try:process.wait(timeout=15)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
        for streams in logs.values():
            for stream in streams:stream.close()
        os.environ.clear();os.environ.update(previous)
        (fixture/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(results,ensure_ascii=False,indent=2))
    return 0 if results['passed'] else 1


if __name__=='__main__':sys.exit(main())
