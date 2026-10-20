"""Telegram-facing engine interfaces; no queue or orchestrator memory is shared.

``lock`` is a snapshot scope for legacy read rendering only. Mutations use
engine-owned validation; it is explicitly not a distributed mutex.
"""
from __future__ import annotations

from pathlib import Path
import threading
import time

from services.ipc import RpcClient, ServiceUnavailable


class JobSnapshot(dict):
    def __getattr__(self, name):
        try:return self[name]
        except KeyError:raise AttributeError(name) from None

    def to_dict(self):return dict(self)


class SnapshotScope:
    def __init__(self, engine):self.engine = engine
    def __enter__(self):
        self.engine.refresh()
        return self
    def __exit__(self, *_):pass


class RemoteQueue:
    remote = True
    def __init__(self, client, engine=None):self.client, self.engine = client, engine

    def get_jobs(self):
        if self.engine is not None and (time.monotonic() - getattr(self.engine, '_cached_at', 0) < 2.0):
            return self.cached_jobs()
        jobs = self.client.call('queue.get_jobs', {}, timeout=2)
        if self.engine is not None:
            with self.engine._snapshot_lock:
                self.engine._jobs = jobs
                self.engine._cached_at = time.monotonic()
        return jobs

    def cached_jobs(self):
        if self.engine is None:return []
        with self.engine._snapshot_lock:return list(self.engine._jobs)

    def recovery_payloads(self, known_orders):
        return self.client.call('engine.completed_evidence', {'known_orders': list(known_orders)}, timeout=30)

    def get_job(self, job_id):
        value = self.client.call('queue.get_job', {'job_id': job_id}, timeout=2)
        return JobSnapshot(value) if value is not None else None

    def add_job(self, **arguments):
        if 'file_path' in arguments:arguments['file_path'] = str(arguments['file_path'])
        return JobSnapshot(self.client.call('queue.add_job', {'arguments': arguments}, timeout=10))

    def update_pending_mapping(self, job_id, **arguments):
        return self.client.call('queue.update_pending_mapping', {'job_id': job_id, 'arguments': arguments}, timeout=10)

    def remove_job(self, job_id, *, expected=None):
        return self.client.call('queue.remove_job', {'job_id': job_id, 'expected': expected or {}}, timeout=10)

    def request_customer_stop(self, job_id, actor, reference):
        return self.client.call('queue.request_customer_stop',
                                {'job_id': job_id, 'actor': actor, 'reference': reference},
                                request_id='customer-stop:' + reference, timeout=10)


class RemoteEngine:
    remote = True
    def __init__(self, root, *, client=None):
        self.client = client or RpcClient(service='engine', root=Path(root))
        self.lock = SnapshotScope(self)
        self._snapshot_lock = threading.Lock()
        self._cached = {'state': {'engine_online': False, 'stale': True}, 'session_token': None,
                        'is_running': False, 'current_job_id': None, 'queue_round_job_ids': []}
        self._cached_at = 0
        self._jobs = []
        self._stop = threading.Event()
        self._wake_event = threading.Event()
        self._observer = None
        self.telegram_enabled = False
        self.continuous_service = self

    def refresh(self):
        with self._snapshot_lock:
            return {**self._cached, 'state': dict(self._cached['state'])}

    def observe(self):
        try:
            current = self.client.call('engine.snapshot', {}, timeout=.5)
            jobs = self.client.call('queue.get_jobs', {}, timeout=1)
            current.setdefault('state', {})['engine_online'] = True
            with self._snapshot_lock:
                self._cached = current
                self._jobs = jobs
                self._cached_at = time.monotonic()
        except ServiceUnavailable:
            with self._snapshot_lock:
                self._cached['state']['engine_online'] = False
                self._cached['state']['stale'] = True
                self._cached_at = time.monotonic()

    def wake(self):
        self._wake_event.set()
        try:
            return self.client.call('engine.wake', {}, timeout=2)
        except Exception:
            return None

    def start(self):
        if self._observer and self._observer.is_alive():return
        self._stop.clear()
        self._wake_event.clear()
        def run():
            while not self._stop.is_set():
                try:self.observe()
                except (RuntimeError, ValueError, KeyError, TypeError):
                    with self._snapshot_lock:
                        self._cached['state']['engine_online'] = False
                        self._cached['state']['stale'] = True
                self._wake_event.wait(2.0)
                self._wake_event.clear()
        self._observer = threading.Thread(target=run, name='EngineStateObserver', daemon=True)
        self._observer.start()

    def stop(self):
        self._stop.set()
        self._wake_event.set()
        if self._observer:self._observer.join(timeout=2)

    def get_live_status(self):return dict(self.refresh()['state'])

    @property
    def session_token(self):return self.refresh().get('session_token')
    @property
    def is_running(self):return self.refresh().get('is_running', False)
    @property
    def current_job_id(self):return self.refresh().get('current_job_id')
    @property
    def queue_round_job_ids(self):return self.refresh().get('queue_round_job_ids', [])
    @property
    def all_completed_records(self):return self.client.call('engine.records', {}, timeout=10)

    def _action(self, action, *arguments):
        result = self.client.call('engine.action', {'action': action, 'arguments': list(arguments)}, timeout=15)
        self._cached_at = 0
        return result

    def start_queue(self, job_id=None):return self._action('start_queue', job_id)
    def pause_session(self):return self._action('pause_session')
    def resume_session(self):return self._action('resume_session')
    def repair_errors(self):return self._action('repair_errors')
    def apply_customer_stop(self, job_id):return self.client.call('engine.apply_stop',{'job_id':job_id},timeout=3)
    def wake(self):return self.client.call('engine.wake', {}, timeout=2)
    def review_queue_start(self):return self.client.call('queue.review_start', {}, timeout=120)
    def start_reviewed(self, signature, versions):
        return self.client.call('engine.start_reviewed', {'signature': signature, 'versions': versions}, timeout=120)
    def session_action(self, action, session, workbook):
        return self.client.call('engine.session_action', {'action': action,
            'expected_session': session, 'expected_workbook': workbook}, timeout=30)
