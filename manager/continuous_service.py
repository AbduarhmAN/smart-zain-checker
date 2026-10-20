"""Persistent single-consumer queue; only funded, consented customer orders run."""
import json
import os
from pathlib import Path
import secrets
import threading
import time
from uuid import UUID


class ContinuousQueueService:
    def __init__(self,orchestrator,queue_service,root,client):
        self.orchestrator=orchestrator;self.queue=queue_service;self.root=Path(root);self.client=client
        self.path=self.root/'.commerce/runtime-policy.json';self._stop=threading.Event();self._wake=threading.Event()
        self._thread=None;self.last_error=None;self.last_started=None;self._next_expiry=0;self._next_start=0
        policy=self.policy();self.enabled=policy.get('continuous_enabled',True) is True
        if getattr(orchestrator,'cross_file_enabled',False) is True and getattr(orchestrator,'execution_authorizer',None) is None:
            def authorize(job_id):
                job=self.queue.get_job(job_id)
                return job is not None and self.funded(job.to_dict())
            orchestrator.execution_authorizer=authorize

    def policy(self):
        if not self.path.exists():return {}
        value=json.loads(self.path.read_text(encoding='utf-8'))
        if not isinstance(value,dict):raise ValueError('Invalid runtime policy')
        return value

    def set_enabled(self,enabled):
        if type(enabled) is not bool:raise ValueError('Boolean service control required')
        value=self.policy();value['continuous_enabled']=enabled
        self.path.parent.mkdir(parents=True,exist_ok=True)
        pending=self.path.with_name(self.path.name+'.'+secrets.token_hex(6)+'.pending')
        with pending.open('x',encoding='utf-8') as stream:
            json.dump(value,stream);stream.flush();os.fsync(stream.fileno())
        os.replace(pending,self.path);self.enabled=enabled;self.wake()

    def funded(self,job):
        mapping=job.get('column_mapping',{});order=mapping.get('commerce_order_id');owner=mapping.get('telegram_chat_id')
        if not order or type(owner) is not int or owner<=0:return False
        state=self.client.rpc('checkout_state',{'p_order_id':str(UUID(order)),'p_telegram_user_id':owner})
        if state.get('partner_sheet') and state['partner_sheet'].get('state')!='approved':return False
        valid=(state.get('order_id')==order and state.get('payment_state')=='reserved' and state.get('reservation_state')=='held'
               and bool(state.get('refund_consent_reference')) and state.get('requested_checks')==job['total_records'])
        job['_verified_customer_stop']=valid and bool(state.get('customer_stop_reference'))
        if mapping.get('customer_stop') and not job['_verified_customer_stop']:return False
        if valid and state.get('customer_stop_reference') and not mapping.get('customer_stop'):
            self.queue.request_customer_stop(job['id'],owner,state['customer_stop_reference'])
        if valid and not job['_verified_customer_stop']:
            # checkout_state deliberately permits suspended owners to recover
            # stops/refunds. Normal checking needs the active-owner endpoint.
            detail=self.client.rpc('customer_order_detail',{'p_actor_telegram_id':owner,
                'p_order_id':str(UUID(order)),'p_admin':False})
            valid=(isinstance(detail,dict) and detail.get('order_id')==order
                   and detail.get('owner_telegram_id')==owner and detail.get('checker_job_id')==job['id'])
        return valid

    def tick(self):
        # Private owner access does not reopen the existing customer queue.
        if (self.root/'.maintenance/enabled').exists():return False
        if time.monotonic()>=self._next_expiry:
            self._next_expiry=time.monotonic()+60
            try:self.client.rpc('expire_promotional_credit',{'p_limit':100})
            except Exception:self.last_error='promotional_expiry_unavailable'
        cross_file=getattr(self.orchestrator,'cross_file_enabled',False) is True
        if cross_file:
            self.orchestrator.maintain_sessions()
            if self.orchestrator.is_paused:return False
        else:
            with self.orchestrator.lock:
                if self.orchestrator.is_running:
                    self.orchestrator._cancel_unleased_customer_rows_locked()
                    if (not self.orchestrator.is_finalizing and self.orchestrator.customers
                        and len(self.orchestrator.completed_indices)==len(self.orchestrator.customers) and not self.orchestrator.active_leases):
                        self.orchestrator.is_finalizing=True
                        threading.Thread(target=self.orchestrator._finalize_job,name='ContinuousFinalize',daemon=True).start()
                    return False
                if self.orchestrator.is_finalizing or self.orchestrator.is_paused or time.monotonic()<self._next_start:return False
            self._next_start=time.monotonic()+3
        for job in self.queue.get_jobs():
            if job.get('status')!='pending':continue
            try:
                from manager.legacy_customer_stop import accepted_stopped_legacy
                if not job.get('column_mapping',{}).get('commerce_order_id'):
                    if not accepted_stopped_legacy(self.root,job):continue
                    started=self.orchestrator.start_queue(job_id=job['id'])
                    if started:self.last_started=job['id'];self.last_error=None
                    else:self.last_error='legacy_stop_start_failed'
                    return started
                if not self.funded(job):continue
                # Turning off new checking must never strand an owner's stop
                # and wallet refund. This path starts the cancellation/export
                # pipeline; a stopped job never launches browser workers.
                if not self.enabled and not job.get('_verified_customer_stop'):continue
                started=self.orchestrator.start_queue(job_id=job['id'])
                if started:self.last_started=job['id'];self.last_error=None
                else:self.last_error='capacity_or_file_initialization_wait' if cross_file else 'funded_job_start_failed'
                if not started and cross_file:continue
                return started
            except Exception as exc:
                from services.ipc import ServiceUnavailable
                if isinstance(exc,ServiceUnavailable):
                    self.last_error='file_service_unavailable'
                    continue
                self.last_error='funded_job_requires_review'
                continue
        return False

    def wake(self):self._wake.set()

    def _loop(self):
        while not self._stop.is_set():
            try:self.tick()
            except Exception:self.last_error='continuous_service_requires_review'
            self._wake.wait(1);self._wake.clear()

    def start(self):
        if self._thread and self._thread.is_alive():return
        self._stop.clear();self._thread=threading.Thread(target=self._loop,name='ContinuousCustomerQueue',daemon=True);self._thread.start()

    def stop(self):
        self._stop.set();self.wake()
        if self._thread and self._thread is not threading.current_thread():self._thread.join(timeout=2)

    def status(self):
        return {'enabled':self.enabled,'running':bool(self._thread and self._thread.is_alive()),
                'last_started':self.last_started,'last_error':self.last_error}
