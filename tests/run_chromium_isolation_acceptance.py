"""Real installed Chromium crash, isolated service processes, local HTML only."""
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))


def main():
    import psutil  # Existing development-only measurement dependency.
    from services.ipc import RpcClient,ServiceUnavailable
    candidates=[Path('C:/Program Files/Google/Chrome/Application/chrome.exe'),Path('C:/Program Files (x86)/Google/Chrome/Application/chrome.exe')]
    executable=next((path for path in candidates if path.is_file()),None)
    if executable is None:raise RuntimeError('An existing installed Chrome is required; this test installs nothing')
    root=ROOT/'outputs/four_services'/('chromium_'+time.strftime('%Y%m%d_%H%M%S')+'_'+secrets.token_hex(3))
    (root/'.services').mkdir(parents=True)
    (root/'.services/TEST_FIXTURE').write_text('smart-zain-isolated-tests')
    (root/'config_workers.json').write_text(json.dumps({'workers':[{'worker_id':'fixture','use_direct_api':True,'use_proxy':False,'task_scope':'account'}]}))
    environment={key:value for key,value in os.environ.items() if not any(word in key.upper() for word in ('TOKEN','SUPABASE','COMMERCE','TELEGRAM','SERVICE_KEY'))}
    environment.update(ZAIN_IPC_TOKEN=secrets.token_urlsafe(48),ZAIN_SERVICE_TESTING='1',ZAIN_TEST_REAL_CHROMIUM='1',
        ZAIN_TEST_CHROME_EXECUTABLE=str(executable),ZAIN_ENGINE_GATEWAY='0',ZAIN_PROJECT_ROOT=str(root))
    for role in ('engine','telegram','files','browser'):
        with socket.socket() as probe:probe.bind(('127.0.0.1',0));environment['ZAIN_'+role.upper()+'_PORT']=str(probe.getsockname()[1])
    previous=os.environ.copy();os.environ.update(environment)
    processes={};logs=[];report={'real_chromium':True,'external_page_requests':False,'production_modified':False,'fixture':str(root),'checks':{}}
    def launch(role):
        stream=(root/(role+'.log')).open('ab');logs.append(stream)
        process=subprocess.Popen([sys.executable,'-B','-m','services',role,'--root',str(root)],cwd=ROOT,env=environment,stdout=stream,stderr=subprocess.STDOUT)
        processes[role]=process;deadline=time.monotonic()+35
        while time.monotonic()<deadline:
            if process.poll() is not None:raise RuntimeError('Fixture '+role+' exited; inspect retained log')
            try:
                state=RpcClient(role,root).health()
                if role!='telegram' or state.get('ready'):return
            except ServiceUnavailable:pass
            time.sleep(.1)
        raise RuntimeError(role+' readiness timeout')
    try:
        for role in ('files','engine','browser','telegram'):launch(role)
        browser=RpcClient('browser',root);telegram=RpcClient('telegram',root);engine=RpcClient('engine',root)
        browser.call('query_submit',{'task_id':'crash-real-chromium','number':'2999999999','timeout':12},request_id='real-submit')
        deadline=time.monotonic()+25
        while not (root/'.services/chromium-ready').exists():
            if time.monotonic()>deadline:raise RuntimeError('Chromium did not open local fixture page')
            time.sleep(.05)
        owner=psutil.Process(processes['browser'].pid)
        owned=[child for child in owner.children(recursive=True) if 'chrome' in child.name().lower()]
        if not owned:raise AssertionError('No owned real Chromium child observed')
        samples=[]
        for index in range(12):
            response=telegram.call('telegram.test_update',{'text':'/start' if index%2==0 else '/help'},timeout=5)
            if not response['responses']:raise AssertionError('No Telegram response while Chromium was busy')
            samples.append(response['elapsed_ms'])
        report['chromium_children_before_crash']=len(owned)
        report['browser_health_while_active']=browser.health()
        # Only descendants of this newly created test Browser service qualify.
        selected=min(owned,key=lambda child:child.create_time())
        if selected.pid not in {child.pid for child in owner.children(recursive=True)}:raise AssertionError('Lost process ownership')
        selected.kill();selected.wait(timeout=10)
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            terminal=browser.call('query_status',{'task_id':'crash-real-chromium'})
            if terminal['done']:break
            time.sleep(.05)
        report['checks']['chromium_crash_reported_as_retryable_transport_error']=terminal['done'] and terminal['result'][0]=='network_error' and 'المتصفح' in terminal['result'][2]
        report['checks']['browser_service_remains_alive']=processes['browser'].poll() is None
        report['checks']['engine_remains_alive']=engine.health()['ready']
        report['checks']['telegram_remains_alive']=telegram.health()['ready']
        ordered=sorted(samples)
        report['telegram_internal_latency_ms']={'samples':len(samples),'p50':ordered[int((len(samples)-1)*.5)],'p95':ordered[int((len(samples)-1)*.95)],'max':max(samples)}
        browser.call('query_submit',{'task_id':'recovery-real-chromium','number':'2000000001','timeout':12})
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            restored=browser.call('query_status',{'task_id':'recovery-real-chromium'})
            if restored['done']:break
            time.sleep(.1)
        report['checks']['new_browser_task_recovers']=restored['done'] and restored['result'][0]=='ok' and restored['result'][1]==12.34
        # Telegram's lifecycle is independent of the still-running engine.
        original_engine=processes['engine'].pid
        processes['telegram'].kill();processes['telegram'].wait(timeout=10);launch('telegram')
        report['checks']['telegram_restart_does_not_restart_engine']=engine.health()['pid']==original_engine
        report['passed']=all(report['checks'].values())
        report['limitations']=['Windows installed Chromium/local HTML, not Linux Chromium or production Zain responses.',
            'Manual process kill tests owned Chromium failure; Linux cgroup teardown is covered separately.']
    except Exception as exc:report['passed']=False;report['error']=type(exc).__name__+': '+str(exc)
    finally:
        # Close/kill only this fixture's services and their recorded descendants.
        descendants=[]
        for process in processes.values():
            try:descendants.extend(psutil.Process(process.pid).children(recursive=True))
            except psutil.Error:pass
            if process.poll() is None:process.terminate()
        for process in processes.values():
            try:process.wait(timeout=15)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
        for child in descendants:
            try:
                if child.is_running():child.kill()
            except psutil.Error:pass
        for stream in logs:stream.close()
        os.environ.clear();os.environ.update(previous)
        (root/'results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        (ROOT/'docs/four_services/chromium_isolation_results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2));return 0 if report['passed'] else 1


if __name__=='__main__':sys.exit(main())
