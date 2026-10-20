"""Durable checker -> delivery -> settlement bridge, independent of worker IPs."""
from __future__ import annotations
import hashlib
import json
import logging
import os
import secrets
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from uuid import UUID
from .client import CommerceClient
from .finance import delivery_arguments,result_rows,positive_minor
from .outbox import Outbox,OutboxWorker

logger = logging.getLogger('CommerceBridge')


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while chunk := stream.read(256*1024): digest.update(chunk)
    return digest.hexdigest()


def completed_job_evidence(root, queue_service, known_orders=(), on_error=None):
    """Read engine-owned recovery journals without writing financial state."""
    from manager.checkpoint import CheckpointManager
    root = Path(root).resolve()
    known = set(known_orders)
    for job in queue_service.get_jobs():
        mapping = job.get('column_mapping', {})
        order = mapping.get('commerce_order_id')
        if not order or order in known or job.get('status') not in {'completed', 'archived'}:continue
        try:
            job_id, checkpoint = job['id'], job['checkpoint_file']
            if (not isinstance(job_id, str) or not job_id or not isinstance(checkpoint, str)
                    or Path(checkpoint).name != checkpoint or job_id not in checkpoint
                    or not checkpoint.startswith('.checkpoint_') or not checkpoint.endswith('.json')):
                raise ValueError('A dedicated paid-job checkpoint is required')
            cp_path = (root/checkpoint).resolve()
            wal = (root/f'.wal_{checkpoint[len(".checkpoint_"):-len(".json")]}.jsonl').resolve()
            if not cp_path.is_relative_to(root) or not wal.is_relative_to(root):
                raise ValueError('Journal is outside workspace')
            saved = CheckpointManager(cp_path).load()
            signature = saved.get('input_signature')
            if not isinstance(signature, str) or not signature:
                raise ValueError('Journal input identity is missing')
            records = {}
            with wal.open(encoding='utf-8') as stream:
                for line in stream:
                    if not line.strip():continue
                    record = json.loads(line)
                    if record.get('input_signature') != signature:
                        raise ValueError('Journal belongs to another input')
                    positive_minor(record.get('row'))
                    records[record['row']] = record
            yield {'commerce_order_id': order, 'job_id': job_id,
                   'telegram_chat_id': mapping.get('telegram_chat_id'),
                   'result_file': str(root/job['result_file']),
                   'total': job['total_records'], 'commerce_records': list(records.values())}
        except (OSError, sqlite3.Error):
            if on_error:on_error('completed_job_storage_unavailable')
        except (ValueError, KeyError, TypeError, AttributeError):
            if on_error:on_error('completed_job_recovery_requires_review')


class CommerceBridge:
    def __init__(self,project_root,*,client=None,event_bus=None,queue_service=None):
        self.root = Path(project_root).resolve()
        self.storage = self.root/'.commerce'
        self.storage.mkdir(parents=True,exist_ok=True)
        self.outbox = Outbox(self.storage/'outbox.sqlite')
        self.worker = OutboxWorker(self.outbox,CommerceClient() if client is None else client)
        self.bus = event_bus
        self.queue_service = queue_service
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._subscribed = False
        self._recovery_needed = threading.Event()
        self._next_recovery = 0
        self.last_error = None
        self.archive_dispatcher = None

    def _persist(self,path,data):
        encoded = json.dumps(data,sort_keys=True,ensure_ascii=False,allow_nan=False)
        with self._lock:
            if path.exists():
                old = json.loads(path.read_text(encoding='utf-8'))
                if old != data: raise ValueError('Conflicting commerce spool replay')
                return
            pending = path.with_name(path.name+'.'+secrets.token_hex(6)+'.pending')
            with pending.open('x',encoding='utf-8') as stream:
                stream.write(encoded);stream.flush();os.fsync(stream.fileno())
            try: os.link(pending,path)
            except FileExistsError:
                if json.loads(path.read_text(encoding='utf-8')) != data:
                    raise ValueError('Conflicting commerce spool replay')

    def _snapshot(self,order,path,expected_hash):
        target = self.storage/f'result_{order}.xlsx'
        with self._lock:
            if not target.exists():
                pending = target.with_name(target.name+'.'+secrets.token_hex(6)+'.pending')
                with path.open('rb') as source, pending.open('xb') as output:
                    shutil.copyfileobj(source,output,256*1024)
                    output.flush();os.fsync(output.fileno())
                if file_sha256(pending) != expected_hash:
                    raise ValueError('Result changed during snapshot')
                # Publish without overwriting a snapshot another process created.
                try: os.link(pending,target)
                except FileExistsError: pass
            if file_sha256(target) != expected_hash:
                raise ValueError('Conflicting result snapshot')
        return target

    def bind_job(self,order_id,job_id):
        order = str(UUID(order_id))
        if not isinstance(job_id,str) or not job_id.strip() or len(job_id)>120:
            raise ValueError('Persistent checker job required')
        self.outbox.enqueue(f'{order}:bind','bind_checker_job',{'p_order_id':order,'p_job_id':job_id})
        self._wake.set()

    def capture_completion(self,payload):
        if not isinstance(payload,dict) or not payload.get('commerce_order_id'):
            return None
        order = str(UUID(payload['commerce_order_id']))
        job = payload['job_id']
        if not isinstance(job,str) or not job.strip() or len(job)>120: raise ValueError('Invalid completion job')
        user = positive_minor(payload['telegram_chat_id'])
        path = Path(payload['result_file']).resolve()
        if not path.is_relative_to(self.root) or not path.is_file(): raise ValueError('Result file is outside the job workspace')
        rows = result_rows(payload['commerce_records'])
        if type(payload.get('total')) is not int or len(rows) != payload['total']:
            raise ValueError('Financial completion must cover all executable rows')
        digest = file_sha256(path)
        snapshot = self._snapshot(order,path,digest)
        completion = {'order_id':order,'job_id':job,'user_id':user,'result_file':str(snapshot),
                      'sha256':digest,'rows':rows}
        self._persist(self.storage/f'completion_{order}.json',completion)
        try: self._enqueue_completion(completion)
        except (OSError,sqlite3.Error):
            self._recovery_needed.set();self._wake.set()
            raise
        return completion

    def _enqueue_completion(self,data):
        order = data['order_id']
        bind,terminal = f'{order}:bind',f'{order}:terminal'
        self.outbox.enqueue_many([
            (bind,'bind_checker_job',{'p_order_id':order,'p_job_id':data['job_id']},None),
            (terminal,'record_terminal_results',{'p_order_id':order,'p_job_id':data['job_id'],
                'p_rows':data['rows'],'p_result_sha256':data['sha256']},bind)])
        if self.archive_dispatcher is not None:self.archive_dispatcher.enqueue(order)
        self._wake.set()

    def capture_delivery(self,completion,telegram_message):
        order = str(UUID(completion['order_id']))
        if self.completion_for(order) != completion:
            raise ValueError('Delivery requires a captured completion')
        arguments = delivery_arguments(order,completion['user_id'],telegram_message,completion['sha256'])
        receipt = {'order_id':order,'arguments':arguments}
        # Keep the receipt before queueing, so a crash can reconstruct the commands.
        self._persist(self.storage/f'receipt_{order}.json',receipt)
        try: self._enqueue_receipt(receipt)
        except (OSError,sqlite3.Error):
            self._recovery_needed.set();self._wake.set()
            raise

    def _enqueue_receipt(self,data):
        order = data['order_id']
        delivery,settle = f'{order}:delivery',f'{order}:settle'
        self.outbox.enqueue_many([
            (delivery,'confirm_result_delivery',data['arguments'],f'{order}:terminal'),
            (settle,'settle_order',{'p_order_id':order},delivery)])
        self.outbox.delivery_confirmed(order,data['arguments'])
        self._wake.set()

    def recover(self):
        self._recovery_needed.clear()
        self._next_recovery = time.monotonic()+10
        self.recover_completed_jobs()
        # Completion files precede receipt files, satisfying durable dependencies.
        for prefix,callback in [('completion_',self._enqueue_completion),('receipt_',self._enqueue_receipt)]:
            for path in sorted(self.storage.glob(prefix+'*.json')):
                try:
                    callback(json.loads(path.read_text(encoding='utf-8')))
                except (OSError,sqlite3.Error):
                    self._recovery_needed.set()
                    self.last_error = 'spool_storage_unavailable'
                except (ValueError,KeyError,TypeError):
                    self.last_error = 'spool_recovery_requires_review'
                    logger.warning('Commerce spool requires review: %s',path.name)

    def recover_completed_jobs(self):
        """Recover an event lost after the queue durably marked its job complete.

        Require the exact per-job journal and checkpoint signature; counters and
        the formatted workbook are never used to infer billable row outcomes.
        """
        if self.queue_service is None: return
        known = [path.stem[len('completion_'):] for path in self.storage.glob('completion_*.json')]
        errors = []
        try:
            if getattr(self.queue_service, 'remote', False) is True:
                evidence = self.queue_service.recovery_payloads(known)
                if isinstance(evidence, dict):
                    payloads = evidence['payloads']
                    errors.extend(evidence.get('errors', []))
                else:payloads = evidence
            else:
                payloads = list(completed_job_evidence(self.root, self.queue_service, known, errors.append))
        except RuntimeError:
            self._recovery_needed.set()
            self.last_error = 'engine_recovery_unavailable'
            return
        for payload in payloads:
            try:
                if self.completion_for(payload['commerce_order_id']) is not None:continue
                self.capture_completion(payload)
            except (OSError,sqlite3.Error):
                errors.append('completed_job_storage_unavailable')
            except (ValueError,KeyError,TypeError,AttributeError):
                errors.append('completed_job_recovery_requires_review')
        for code in errors:
            self.last_error = code
            if code == 'completed_job_storage_unavailable':self._recovery_needed.set()

    def pending_deliveries(self):
        for path in sorted(self.storage.glob('completion_*.json')):
            data = json.loads(path.read_text(encoding='utf-8'))
            order = str(UUID(data['order_id']))
            if self.outbox.delivery_state(order) in {None,'rejected'}:
                yield data

    def completion_for(self,order_id):
        order = str(UUID(order_id))
        path = self.storage/f'completion_{order}.json'
        return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else None

    def _on_completion(self,event_type,payload):
        try:
            self.capture_completion(payload)
        except (ValueError,KeyError,OSError,TypeError,sqlite3.Error):
            self.last_error = 'completion_capture_failed'
            logger.warning('Financial completion was not captured; settlement remains pending')

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                if self._stop.is_set(): raise RuntimeError('Commerce worker is still stopping')
                return
            if getattr(self.queue_service, 'remote', False) is True:
                self._recovery_needed.set()
                self._next_recovery = 0
            else:self.recover()
            if self.bus is not None and not self._subscribed:
                self.bus.subscribe('session_completed',self._on_completion)
                self._subscribed = True
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop,name='commerce-outbox',daemon=True)
            self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                if self._recovery_needed.is_set() and time.monotonic()>=self._next_recovery:
                    self.recover()
                if self.worker.run_once(): continue
            except Exception as exc:
                self.last_error = type(exc).__name__
            self._wake.wait(2)
            self._wake.clear()

    def stop(self):
        with self._lock:
            if self.bus is not None and self._subscribed:
                self.bus.unsubscribe('session_completed',self._on_completion)
                self._subscribed = False
            self._stop.set();self._wake.set()
            thread = self._thread
        if thread and thread is not threading.current_thread(): thread.join(timeout=1)

    def status(self):
        return {'commands':self.outbox.status(),'deliveries':self.outbox.delivery_counts(),
                'running':bool(self._thread and self._thread.is_alive() and not self._stop.is_set()),
                'recovery_pending':self._recovery_needed.is_set(),'last_error':self.last_error}
