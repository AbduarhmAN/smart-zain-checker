"""Single owner of the existing queue, checkpoints, leases and API workers."""
from __future__ import annotations
import copy
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from .codec import dumps
from .events import EventStore
from .ipc import RpcClient, ServiceUnavailable


class TelegramProxy:
    def __init__(self,root): self.client=RpcClient('telegram',root); self.notifier=self
    def status(self):
        try: return self.client.call('telegram.status',{},timeout=2)
        except ServiceUnavailable: return {'status':'unavailable','enabled':False,'connected':False}
    def set_enabled(self,enabled): return self.client.call('telegram.set_enabled',{'enabled':enabled},timeout=5)
    def check_connection(self): return self.client.call('telegram.check_connection',{},timeout=15)
    def send_message(self,text,wait=False): return self.client.call('telegram.send_message',{'text':text,'wait':wait},timeout=30)


class EngineApplication:
    retry_safe=('queue.request_customer_stop','engine.apply_stop','event.ack','engine.wake','engine.completed_evidence','queue.review_start')
    def __init__(self,root):
        from manager.queue import QueueService
        from manager.orchestrator import Orchestrator
        from workers.supervisor import WorkerSupervisor
        from manager.event_bus import EVENT_BUS
        self.root=Path(root).resolve(); self.stop_event=threading.Event(); self.bus=EVENT_BUS
        self.queue=QueueService(self.root/'.zain-queue.json')
        self.supervisor=WorkerSupervisor(project_root=self.root)
        self.orchestrator=Orchestrator(self.supervisor,self.queue,self.root)
        self.orchestrator.execution_authorizer=self.authorize_execution
        self.orchestrator.execution_dependencies_ready=lambda:self.files_available
        self.orchestrator.telegram_integration=TelegramProxy(self.root)
        self.events=EventStore(self.root)
        self._events={'session_started','session_completed','session_failed','session_paused','session_resumed','session_cancelled','network_alert','service_route_verification'}
        self.snapshot={'state':{'engine_online':True,'running':False,'kpis':{},'stale':True},'observed_at':None,'session_token':self.orchestrator.session_token,'is_running':False,'current_job_id':None,'queue_round_job_ids':[]}
        self.snapshot_lock=threading.Lock(); self.gateway=None; self.continuous=None; self.snapshot_thread=None
        self.files_available=False;self.dependency_thread=None

    def _on_event(self,event,payload):
        if event in self._events: self.events.publish(event,payload)

    def authorize_execution(self,job_id):
        job=self.queue.get_job(job_id)
        if job is None or self.continuous is None: return False
        return self.continuous.funded(job.to_dict())

    def start(self):
        from manager.continuous_service import ContinuousQueueService
        from telegram_bot.commerce.client import CommerceClient
        self.bus.subscribe('*',self._on_event)
        if os.environ.get('ZAIN_ENGINE_GATEWAY','1')=='1':
            from server.gateway import ServerGateway
            self.gateway=ServerGateway(self.orchestrator,self.queue,self.root/'web_ui',self.root,
                web_port=int(os.environ.get('ZAIN_DASHBOARD_PORT','5050')),
                bridge_port=int(os.environ.get('ZAIN_BRIDGE_PORT','8766')))
            self.gateway.start()
        self.dependency_thread=threading.Thread(target=self._dependencies,name='EngineDependencies',daemon=True);self.dependency_thread.start()
        if os.environ.get('ZAIN_SERVICE_TESTING')!='1':
            self.continuous=ContinuousQueueService(self.orchestrator,self.queue,self.root,CommerceClient())
            self.orchestrator.continuous_service=self.continuous; self.continuous.start()
        self.snapshot_thread=threading.Thread(target=self._snapshot_loop,name='EngineSnapshot',daemon=True); self.snapshot_thread.start()

    def _dependencies(self):
        client=RpcClient('files',self.root)
        while not self.stop_event.is_set():
            try:
                health=client.health(timeout=.5)
                self.files_available=bool(health.get('ready',health.get('state')=='ready'))
                if self.files_available:self.orchestrator.waiting_dependency=None
            except ServiceUnavailable:self.files_available=False
            self.stop_event.wait(1)

    def _snapshot_loop(self):
        while not self.stop_event.is_set():
            try:
                state=self.orchestrator.get_live_status()
                if self.orchestrator.lock.acquire(timeout=.05):
                    try:
                        current={'state':{**state,'engine_online':True,'stale':False},'observed_at':time.time(),'session_token':self.orchestrator.session_token,
                            'is_running':self.orchestrator.is_running,'current_job_id':self.orchestrator.current_job_id,
                            'queue_round_job_ids':list(self.orchestrator.queue_round_job_ids)}
                    finally: self.orchestrator.lock.release()
                    with self.snapshot_lock: self.snapshot=current
            except Exception: pass
            self.stop_event.wait(.25)

    def get_snapshot(self,payload):
        with self.snapshot_lock: value=copy.deepcopy(self.snapshot)
        if value['observed_at'] is None or time.time()-value['observed_at']>2: value['state']['stale']=True
        return value

    def health(self): return {'ready':not self.stop_event.is_set(),'files_available':self.files_available,'scheduler':self.continuous.status() if self.continuous else {'enabled':False}}

    def stop(self):
        self.stop_event.set()
        if self.continuous: self.continuous.stop()
        self.orchestrator.cancel_session()
        self.supervisor.stop_all()
        if self.gateway: self.gateway.stop()
        from workers.account_transport import close_account_sessions
        close_account_sessions()
        self.bus.unsubscribe('*',self._on_event)

    def _path(self,value):
        path=Path(value)
        if not path.is_absolute(): path=self.root/path
        path=path.resolve()
        if not path.is_relative_to(self.root): raise ValueError('Path outside workspace')
        return path

    def add_job(self,payload):
        arguments=dict(payload['arguments']); arguments['file_path']=self._path(arguments['file_path'])
        with self.orchestrator.lock: return self.queue.add_job(**arguments).to_dict()

    def update_mapping(self,payload):
        with self.orchestrator.lock:
            return self.queue.update_pending_mapping(payload['job_id'],**payload['arguments'])

    def remove_job(self,payload):
        with self.orchestrator.lock:
            job=self.queue.get_job(payload['job_id']); expected=payload.get('expected',{})
            if not job or job.status=='active' or self.orchestrator.current_job_id==job.id and self.orchestrator.is_running: return False
            if any(getattr(job,key,None)!=value for key,value in expected.items()): return False
            return self.queue.remove_job(job.id)

    def customer_stop(self,payload):
        # Queue's owner lock is sufficient for recording consent. Never wait
        # behind initialization/export while the customer confirms a stop.
        changed=self.queue.request_customer_stop(payload['job_id'],payload['actor'],payload['reference'])
        if changed and self.continuous:self.continuous.wake()
        return changed

    def apply_stop(self,payload):
        job=self.queue.get_job(payload['job_id'])
        requested=bool(job and job.column_mapping.get('customer_stop'))
        if requested and self.continuous:self.continuous.wake()
        return requested

    def action(self,payload):
        name=payload['action']
        if name not in {'start_queue','pause_session','resume_session','repair_errors','apply_customer_stop'}: raise ValueError('Unknown engine action')
        with self.orchestrator.lock:
            if payload.get('expected_session') not in (None,self.orchestrator.session_token): raise ValueError('Session changed')
            if payload.get('expected_workbook') not in (None,self.orchestrator.workbook_name): raise ValueError('Workbook changed')
            return getattr(self.orchestrator,name)(*payload.get('arguments',[]))

    def review_start(self,payload):
        # Reuse the existing validation contract; parsing is delegated by the
        # domain facade to the independent File service.
        from telegram_bot.ui import TelegramUiMixin
        context=SimpleNamespace(orchestrator=self.orchestrator,queue_service=self.queue,project_root=self.root)
        return TelegramUiMixin._prepare_queue_start(context)

    def start_reviewed(self,payload):
        prepared=self.review_start({})
        if prepared['signature']!=payload['signature'] or dumps(prepared['versions'])!=dumps(payload['versions']): raise ValueError('Queue review expired')
        with self.orchestrator.lock:
            jobs=[job for job in self.queue.get_jobs() if job.get('status')=='pending']
            from domain import decimal_json as json
            if json.dumps(jobs,sort_keys=True,ensure_ascii=False)!=prepared['signature']: raise ValueError('Queue changed')
            return self.orchestrator.start_queue()

    def session_action(self,payload):
        from telegram_bot.ui import TelegramUiMixin
        with self.orchestrator.lock:
            if payload['expected_session']!=self.orchestrator.session_token or payload['expected_workbook']!=self.orchestrator.workbook_name: raise ValueError('Session changed')
            name=payload['action']; state=self.orchestrator.get_live_status()
            problem=TelegramUiMixin._action_problem(name,state)
            if problem: return {'problem':problem}
            if name in {'pause','resume','repair'}:
                result=getattr(self.orchestrator,{'pause':'pause_session','resume':'resume_session','repair':'repair_errors'}[name])()
                return {'problem':'','repair_result':result}
            if name!='results': raise ValueError('Unknown session action')
            reports=[self.root/job['result_file'] for job in self.queue.get_jobs() if job['id'] in self.orchestrator.queue_round_job_ids and job['id']!=self.orchestrator.current_job_id and job['status']=='completed' and job.get('result_file')]
            return {'problem':'','records':list(self.orchestrator.all_completed_records.values()),'other_reports':reports}

    def completed_evidence(self,payload):
        from telegram_bot.commerce.bridge import completed_job_evidence
        known=payload.get('known_orders',[])
        if not isinstance(known,list) or len(known)>100000 or any(not isinstance(item,str) for item in known): raise ValueError('Invalid known orders')
        errors=[]
        rows=list(completed_job_evidence(self.root,self.queue,known_orders=known,on_error=errors.append))
        return {'payloads':rows,'errors':errors}

    def get_job(self,payload):
        with self.orchestrator.lock:
            job=self.queue.get_job(payload['job_id'])
            return job.to_dict() if job else None

    def get_records(self,payload):
        with self.orchestrator.lock:return copy.deepcopy(self.orchestrator.all_completed_records)

    def handlers(self):
        return {'engine.snapshot':self.get_snapshot,'engine.records':self.get_records,
            'queue.get_jobs':lambda _:self.queue.get_jobs(),
            'queue.get_job':self.get_job,
            'queue.add_job':self.add_job,'queue.update_pending_mapping':self.update_mapping,'queue.remove_job':self.remove_job,
            'queue.request_customer_stop':self.customer_stop,'engine.action':self.action,
            'engine.apply_stop':self.apply_stop,
            'engine.wake':lambda _:self.continuous.wake() if self.continuous else None,
            'queue.review_start':self.review_start,'engine.start_reviewed':self.start_reviewed,'engine.session_action':self.session_action,
            'engine.completed_evidence':self.completed_evidence,
            'event.list':self.events.list,'event.ack':self.events.ack}


def build(root): return EngineApplication(root)
