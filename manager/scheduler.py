"""FIFO by available check type across isolated file sessions and one worker pool.

An Orchestrator still owns each file's rows, leases, checkpoint, WAL, writer,
result and settlement event. This coordinator never combines their row indices.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import threading
import time
import uuid

from manager.orchestrator import Orchestrator

logger = logging.getLogger('QueueScheduler')


@dataclass(frozen=True)
class JobSession:
    job_id: str
    engine: Orchestrator


class QueueScheduler:
    cross_file_enabled = True

    def __init__(self, supervisor, queue_service, project_root, *,
                 session_factory=Orchestrator, start_workers=True,
                 max_active_jobs=32, max_active_rows=200000):
        self.supervisor = supervisor
        self.queue_service = queue_service
        self.project_root = Path(project_root)
        self.lock = threading.RLock()
        self._admission_lock = threading.RLock()
        self._export_lock = threading.Lock()
        self._factory = session_factory
        self._sessions = {}
        self._loading = set()
        self._local_round_ids = []
        self._worker_jobs = {}
        self._last = session_factory(supervisor, queue_service, self.project_root)
        self._last.shared_worker_pool = self
        self._paused = False
        self._pool_started = False
        self._pool_ready = False
        self._pool_stop = threading.Event()
        self._worker_threads = {}
        self._worker_wake = threading.Event()
        self._start_workers = start_workers
        self._max_active_jobs = max_active_jobs
        self._max_active_rows = max_active_rows
        self._epoch = uuid.uuid4().hex
        self._started_at = None
        self.queue_round_job_ids = []
        self.telegram_enabled = False
        self.start_error = ''
        self.waiting_dependency = None

    def __setattr__(self, name, value):
        owned = {'supervisor','queue_service','project_root','lock','queue_round_job_ids',
                 'telegram_enabled','start_error','waiting_dependency'}
        last = self.__dict__.get('_last')
        if (last is not None and not name.startswith('_') and name not in owned
                and name in last.__dict__ and not isinstance(getattr(type(self),name,None),property)):
            setattr(self._focus(), name, value)
            return
        object.__setattr__(self, name, value)

    def _engines(self):
        with self.lock:
            return [session.engine for key, session in self._sessions.items() if key not in self._loading]

    def _focus(self):
        with self.lock:
            ready = [s.engine for key, s in self._sessions.items() if key not in self._loading]
            return ready[0] if ready else self._last

    def __getattr__(self, name):
        # Legacy administrative rendering shows the oldest active file. All
        # task, stop, admission and lifecycle mutations have explicit routes.
        if name.startswith('__'):
            raise AttributeError(name)
        return getattr(self._focus(), name)

    @property
    def session_token(self):
        return self._epoch

    @property
    def current_job_id(self):
        return self._focus().current_job_id

    @current_job_id.setter
    def current_job_id(self, value):
        if self._sessions:
            raise RuntimeError('Active file identity cannot be replaced')
        self._last.current_job_id = value

    @property
    def waiting_dependency(self):
        return self.__dict__.get('_waiting_dependency') or getattr(self._focus(),'waiting_dependency',None)

    @waiting_dependency.setter
    def waiting_dependency(self, value):
        self._waiting_dependency = value

    @property
    def active_job_ids(self):
        with self.lock:
            return tuple(self._sessions)

    @property
    def is_running(self):
        with self.lock:
            return bool(self._loading) or any(s.engine.is_running for s in self._sessions.values()) or (not self._sessions and self._last.is_running)

    @is_running.setter
    def is_running(self, value):
        if self._sessions:
            raise RuntimeError('Use the scheduler lifecycle for active files')
        self._last.is_running = value

    @property
    def is_finalizing(self):
        engines = self._engines()
        return (any(e.is_finalizing for e in engines) and not any(e.is_running for e in engines)) if engines else self._last.is_finalizing

    @is_finalizing.setter
    def is_finalizing(self, value):
        if self._sessions:
            raise RuntimeError('Use per-file finalization for active files')
        self._last.is_finalizing = value

    @property
    def is_paused(self):
        return self._paused

    @property
    def active_leases(self):
        leases = {}
        for engine in self._engines():
            with engine.lock:
                leases.update(engine.active_leases)
        return leases

    def _capacity(self, job):
        with self.lock:
            if len(self._sessions) >= self._max_active_jobs:
                return False
            loaded = sum(len(s.engine.customers) for s in self._sessions.values())
            # A single large file can run alone; concurrent residency is bounded.
            return not self._sessions or loaded + max(0, job.total_records) <= self._max_active_rows

    def _owns_paths(self, job):
        with self.lock:
            for session in self._sessions.values():
                other = self.queue_service.get_job(session.job_id)
                if other and ((job.checkpoint_file and other.checkpoint_file == job.checkpoint_file)
                              or (job.result_file and other.result_file == job.result_file)):
                    return False
        return True

    def start_queue(self, job_id=None):
        with self._admission_lock:
            if self._paused or (self.project_root/'.maintenance/enabled').exists():
                return False
            ready = getattr(self, 'execution_dependencies_ready', None)
            if ready is not None and not ready():
                from services.ipc import ServiceUnavailable
                raise ServiceUnavailable('File service temporarily unavailable')
            if job_id is None:
                candidate = next((j for j in self.queue_service.get_jobs() if j.get('status') == 'pending'), None)
                if candidate is None:
                    return False
                job_id = candidate['id']
            job = self.queue_service.get_job(job_id)
            with self.lock:
                if job_id in self._sessions:
                    return False
            if job is None or job.status != 'pending' or not self._capacity(job) or not self._owns_paths(job):
                return False
            if job.column_mapping.get('commerce_order_id'):
                authorizer = getattr(self, 'execution_authorizer', None)
                if authorizer is None or not authorizer(job_id):
                    self.start_error = 'تعذر التحقق من اعتماد طلب العميل وتمويله.'
                    return False
            elif (not job.column_mapping.get('customer_stop') and not self._sessions
                  and job_id not in self._local_round_ids):
                # A manual start approves the current local round, matching
                # the old auto-advance scope. Later uploads need another start.
                self._local_round_ids = [j['id'] for j in self.queue_service.get_jobs()
                    if j.get('status') == 'pending'
                    and not j.get('column_mapping', {}).get('commerce_order_id')
                    and not j.get('column_mapping', {}).get('customer_stop')]
                self.queue_round_job_ids = list(self._local_round_ids)
            claimed = self.queue_service.claim_next_pending_job(job_id=job_id)
            if claimed is None:
                return False
            try:
                started = self.start_session_from_config(
                    workbook_path=self.project_root/job.filename, sheet_index=job.sheet_index,
                    mapping=job.column_mapping, mode=job.mode, amount_target=job.amount_target,
                    target_url=job.target_url, job_id=job.id, result_file=job.result_file)
            except Exception as exc:
                self.queue_service.defer_job(job_id)
                from services.ipc import ServiceUnavailable
                if isinstance(exc, ServiceUnavailable):
                    self.waiting_dependency = 'files'
                raise
            if not started:
                self.queue_service.mark_job_failed(job_id, self.start_error or 'لم تبدأ جلسة الفحص')
            return started

    def start_session_from_config(self, workbook_path, sheet_index, mapping, mode='smart_hybrid',
                                  amount_target='contract', target_url='https://business.zain.sa/dashboard/quick-pay',
                                  job_id=None, result_file='نتائج فحص زين.xlsx', force_restart=False):
        with self._admission_lock:
            if self._paused or (self.project_root/'.maintenance/enabled').exists():
                return False
            if job_id is None:
                # Give even a manually opened file unique durable output paths.
                job = self.queue_service.add_job(file_path=Path(workbook_path), sheet_index=sheet_index,
                    sheet_name='Sheet'+str(sheet_index+1), mode=mode, amount_target=amount_target,
                    target_url=target_url, column_mapping=mapping, total_records=0)
                return self.start_queue(job.id)
            job = self.queue_service.get_job(job_id)
            with self.lock:
                if job_id in self._sessions or (job is not None and not self._capacity(job)):
                    return False
                engine = self._factory(self.supervisor, self.queue_service, self.project_root)
                engine.shared_worker_pool = self
                engine.service_route_unavailable = lambda route: self.service_route_unavailable(route)
                engine.execution_authorizer = getattr(self, 'execution_authorizer', None)
                engine.execution_dependencies_ready = getattr(self, 'execution_dependencies_ready', None)
                engine.telegram_enabled = self.telegram_enabled
                engine.telegram_integration = getattr(self, 'telegram_integration', None)
                engine.continuous_service = getattr(self, 'continuous_service', None)
                engine.queue_round_job_ids = [job_id]
                self._sessions[job_id] = JobSession(job_id, engine)
                self._loading.add(job_id)
                if not self._started_at:
                    self._started_at = time.time()
            started = False
            try:
                if (mapping or {}).get('commerce_order_id') and engine.execution_authorizer is None:
                    raise PermissionError('Funded customer execution could not be verified')
                started = engine.start_session_from_config(workbook_path=Path(workbook_path),
                    sheet_index=sheet_index, mapping=dict(mapping or {}), mode=mode,
                    amount_target=amount_target, target_url=target_url, job_id=job_id,
                    result_file=result_file, force_restart=force_restart)
                self.start_error = getattr(engine, 'start_error', '')
                if started and job_id not in self.queue_round_job_ids:
                    self.queue_round_job_ids.append(job_id)
                return started
            finally:
                with self.lock:
                    self._loading.discard(job_id)
                    if not started:
                        self._sessions.pop(job_id, None)
                if not started and engine.writer_thread is not None and engine.result_queue is not None:
                    engine.result_queue.put('STOP_SENTINEL')
                    engine.writer_thread.join(timeout=10)
                self._worker_wake.set()

    def service_route_unavailable(self, route):
        # Called while the coordinator's dispatch lock is held. Reservations
        # across ALL files count, even before the network lock is acquired.
        for engine in self._engines():
            if route in engine.blocked_service_routes:
                return True
            for lease in tuple(engine.active_leases.values()):
                if lease['ip_group'] == route and engine._is_service_index(lease['index']):
                    return True
        return False

    def _owner(self, task_id):
        for engine in self._engines():
            if task_id in engine.active_leases:
                return engine
        return None

    def lease_next_task_for_worker(self, worker_id):
        with self.lock:
            if self._paused or self._pool_stop.is_set() or (self.project_root/'.maintenance/enabled').exists():
                return None
            if self._start_workers and not self._pool_ready:
                return None
            engines = self._engines()
            for engine in engines:
                if engine.lock.acquire(blocking=False):
                    try:
                        engine._release_due_service_recoveries_locked()
                    finally:
                        engine.lock.release()
            assigned = self._worker_jobs.get(worker_id)
            if assigned is not None:
                engine, task_id = assigned
                lease = engine.active_leases.get(task_id)
                if lease is not None and lease['worker_id'] == worker_id:
                    if not engine.lock.acquire(blocking=False):
                        return None
                    try:
                        task = engine.lease_next_task_for_worker(worker_id)
                        return self._annotate(task, engine) if task else None
                    finally:
                        engine.lock.release()
                self._worker_jobs.pop(worker_id, None)
            for engine in engines:
                if not engine.is_running or engine.is_paused or engine.is_finalizing:
                    continue
                if not engine.lock.acquire(blocking=False):
                    continue
                try:
                    if engine._customer_stop_locked():
                        continue
                    task = engine.lease_next_task_for_worker(worker_id)
                    if task is not None:
                        self._worker_jobs[worker_id] = (engine, task['task_id'])
                        actor = self.supervisor.get_worker(worker_id)
                        if actor is not None and actor.current_task is not None:
                            actor.current_task.update(job_id=engine.current_job_id, workbook=engine.workbook_name)
                        return self._annotate(task, engine)
                finally:
                    engine.lock.release()
            return None

    @staticmethod
    def _annotate(task, engine):
        return {**task, 'job_id': engine.current_job_id, 'session_id': engine.session_token,
                'workbook': engine.workbook_name, 'total_records': len(engine.customers)}

    def _route(self, method, task_id, *args, **kwargs):
        with self.lock:
            engine = self._owner(task_id)
        if engine is None:
            return False
        return getattr(engine, method)(task_id, *args, **kwargs)

    def renew_task_lease(self, task_id, worker_id):
        return self._route('renew_task_lease', task_id, worker_id)

    def record_task_outcome(self, task_id, live_amount_raw, status, error_msg='', worker_id='worker_1'):
        return self._route('record_task_outcome', task_id, live_amount_raw, status, error_msg, worker_id)

    def defer_task_to_end(self, task_id, reason='', **kwargs):
        return self._route('defer_task_to_end', task_id, reason, **kwargs)

    def notify_pending_verification(self, task_id, message, worker_id):
        return self._route('notify_pending_verification', task_id, message, worker_id)

    def handle_verification_required(self, task_id, message, worker_id):
        return self._route('handle_verification_required', task_id, message, worker_id)

    def request_worker_start(self):
        if self._start_workers:
            threading.Thread(target=self.ensure_workers, daemon=True, name='SharedPoolStart').start()

    def ensure_workers(self):
        with self.lock:
            if self._pool_started or not self._start_workers:
                return
            if any(thread.is_alive() for thread in self._worker_threads.values()):
                return  # The continuous consumer retries after the old generation drains.
            self._pool_started = True
            self._pool_stop = threading.Event()
            generation = self._pool_stop
        try:
            self.supervisor.start_all()
            self._pool_ready = True
            for actor in self.supervisor.get_all_workers():
                if getattr(actor, 'use_direct_api', False):
                    thread = threading.Thread(target=self._run_worker, args=(actor.worker_id, generation),
                        daemon=True, name='SharedWorker-'+actor.worker_id)
                    self._worker_threads[actor.worker_id] = thread
                    thread.start()
        except Exception:
            with self.lock:
                self._pool_started = False
                self._pool_ready = False
                generation.set()
            raise

    def _run_worker(self, worker_id, generation):
        while not generation.is_set():
            try:
                task = self.lease_next_task_for_worker(worker_id)
                if task is None:
                    self._worker_wake.wait(.5 if self.is_running else 2.0)
                    self._worker_wake.clear()
                    continue
                with self.lock:
                    engine = self._owner(task['task_id'])
                if engine is not None:
                    engine.execute_leased_task_api(worker_id, task, task['session_id'])
            except Exception:
                logger.exception('Shared worker iteration failed for %s', worker_id)
                generation.wait(.5)

    def maintain_sessions(self):
        if self.is_running and not self._paused and not self._pool_ready:
            self.request_worker_start()
        for engine in self._engines():
            with engine.lock:
                if engine.is_running:
                    engine._cancel_unleased_customer_rows_locked()
                terminal = (engine.is_running and not engine.is_finalizing and bool(engine.customers)
                    and not engine.is_paused and len(engine.completed_indices) == len(engine.customers) and not engine.active_leases)
                if terminal:
                    engine.is_finalizing = True
                    threading.Thread(target=engine._finalize_job, daemon=True,
                        name='Finalize-'+str(engine.current_job_id)).start()
            if not engine.is_running and not engine.is_finalizing:
                self._retire(engine)
        if not self._paused:
            for job_id in tuple(self._local_round_ids):
                job = self.queue_service.get_job(job_id)
                if job is not None and job.status == 'pending' and self.start_queue(job_id):
                    break

    def _retire(self, engine):
        with self.lock:
            key = next((key for key, session in self._sessions.items() if session.engine is engine), None)
            if key not in self._loading and self._sessions.get(key, None) is not None and self._sessions[key].engine is engine:
                self._sessions.pop(key)
                self._last = engine
                for worker, assigned in tuple(self._worker_jobs.items()):
                    if assigned[0] is engine:
                        self._worker_jobs.pop(worker, None)

    def finalize_session(self, engine):
        # Serialize disk publication, not network checking. Shared source
        # workbooks cannot be overwritten by two concurrent finalizers.
        try:
            with self._export_lock:
                return engine._finalize_job_impl()
        except Exception:
            logger.exception('File publication paused for %s', engine.current_job_id)
            with engine.lock:
                engine.is_running = True
                engine.is_finalizing = False
                engine.is_paused = True
                engine.verification_notice = 'تعذر نشر نتيجة هذا الملف؛ السجلات محفوظة. راجع التخزين ثم استأنف النشر.'
        finally:
            if not engine.is_running and not engine.is_finalizing:
                self._retire(engine)
            continuous = getattr(self, 'continuous_service', None)
            if continuous is not None:
                continuous.wake()
            self._worker_wake.set()

    def apply_customer_stop(self, job_id):
        with self.lock:
            session = self._sessions.get(job_id)
        if session is None:
            return False
        changed = session.engine.apply_customer_stop(job_id)
        self._worker_wake.set()
        return changed

    def resume_service_route(self, worker_id):
        changed = False
        for engine in self._engines():
            changed = engine.resume_service_route(worker_id) or changed
        self._worker_wake.set()
        return changed

    def pause_session(self):
        self._paused = True
        for engine in self._engines():
            engine.pause_session()

    def resume_session(self):
        for engine in self._engines():
            engine.resume_session()
        self._paused = False
        self._worker_wake.set()

    def cancel_session(self):
        with self._admission_lock:
            self._paused = True
            self._pool_stop.set()
            self._worker_wake.set()
            for engine in self._engines():
                engine.cancel_session()
                engine.session_token = uuid.uuid4().hex
                self._retire(engine)
            self.supervisor.stop_all()
            from workers.stealth_service_engine import cancel_pending_verifications
            cancel_pending_verifications()
            with self.lock:
                self._pool_started = False
                self._pool_ready = False
                self._worker_jobs.clear()
                self._epoch = uuid.uuid4().hex

    def restart_queue_job(self, job_id, auto_start=True):
        if self.is_running or self.is_finalizing:
            return False
        job = self.queue_service.fork_job(job_id)
        if job is None:
            return False
        self._paused = False
        return self.start_queue(job.id) if auto_start else True

    def restart_all_queue_jobs(self, auto_start=True):
        if self.is_running or self.is_finalizing:
            return False
        for job in self.queue_service.get_jobs():
            if job['status'] != 'archived':
                self.restart_queue_job(job['id'], auto_start=False)
        return self.start_queue() if auto_start else True

    def repair_errors(self, *args, **kwargs):
        if self.is_running or self.is_finalizing:
            return {'status': 'error', 'message': 'انتظر اكتمال الملفات الجارية قبل بدء إعادة الفحص.'}
        engine = self._last
        engine.shared_worker_pool = self
        key = engine.current_job_id
        if key is None:
            if kwargs.get('workbook_path') is not None:
                return self.start_repair_session_from_sheet(workbook_path=kwargs['workbook_path'],
                    sheet_index=kwargs.get('sheet_index', 0))
            return engine.repair_errors(*args, **kwargs)
        with self.lock:
            self._sessions[key] = JobSession(key, engine)
        result = engine.repair_errors(*args, **kwargs)
        if engine.is_running:
            self.queue_service.mark_job_active(key)
            self._paused = False
            self._worker_wake.set()
        else:
            self._retire(engine)
        return result

    def start_repair_session_from_sheet(self, **kwargs):
        if self.is_running or self.is_finalizing:
            return {'status': 'error', 'message': 'انتظر اكتمال الملفات الجارية قبل بدء الصيانة.'}
        engine = self._factory(self.supervisor, self.queue_service, self.project_root)
        engine.shared_worker_pool = self
        engine.service_route_unavailable = self.service_route_unavailable
        key = 'repair-'+uuid.uuid4().hex
        engine.current_job_id = key
        with self.lock:
            self._sessions[key] = JobSession(key, engine)
        result = engine.start_repair_session_from_sheet(**kwargs)
        if not engine.is_running:
            self._retire(engine)
        self._worker_wake.set()
        return result

    def get_live_status(self):
        focus = self._focus()
        state = focus.get_live_status()
        jobs = []
        route_blocks = {}
        busy_routes = set()
        notices = []
        for engine in self._engines():
            with engine.lock:
                route_blocks.update(engine.blocked_service_routes)
                busy_routes.update(lease['ip_group'] for lease in engine.active_leases.values()
                                   if engine._is_service_index(lease['index']))
                if engine.verification_notice:
                    notices.append(engine.workbook_name + ': ' + engine.verification_notice)
                pending_accounts = pending_services = 0
                for i in range(len(engine.customers)):
                    if i not in engine.completed_indices:
                        if engine._is_service_index(i):
                            pending_services += 1
                        else:
                            pending_accounts += 1
                jobs.append({'job_id': engine.current_job_id, 'workbook': engine.workbook_name,
                    'total': len(engine.customers), 'completed': len(engine.completed_indices),
                    'account_remaining': pending_accounts, 'service_remaining': pending_services,
                    'active_leases': len(engine.active_leases), 'finalizing': engine.is_finalizing,
                    'paused': engine.is_paused})
        state.update(running=self.is_running or self.is_finalizing, finalizing=self.is_finalizing,
            paused=self._paused, telegram_enabled=self.telegram_enabled,
            scheduling={'mode': 'cross_file_by_type', 'active_files': len(jobs),
                'paused_files': sum(bool(j['paused']) for j in jobs),
                'loading_files': len(self._loading), 'jobs': jobs,
                'max_active_files': self._max_active_jobs, 'max_active_rows': self._max_active_rows})
        state['session_timing']['session_id'] = self._epoch
        for worker in state.get('workers', []):
            block = route_blocks.get(worker.get('ip_group'))
            automatic = bool(block and block.get('automatic_recovery'))
            worker['service_verification_required'] = bool(block and not automatic)
            worker['service_verification_pending'] = bool(block and block.get('pending') and not automatic)
            worker['service_recovery_pending'] = automatic
            if automatic:
                worker['service_retry_seconds'] = round(max(worker.get('service_retry_seconds', 0),
                    block.get('retry_at', 0)-time.monotonic(), 0), 1)
            worker['waiting_for_shared_service_browser'] = bool(self.is_running and not self._paused
                and worker.get('status') == 'ready' and worker.get('ip_group') in busy_routes)
        if notices:
            state['verification_notice'] = '\n'.join(notices)
        # Existing table and KPI detail remain scoped to the named focus file;
        # aggregate scheduler counters are explicit rather than mixing results.
        state['scheduling']['active_leases'] = sum(j['active_leases'] for j in jobs)
        return state


def create_orchestrator(supervisor, queue_service, project_root):
    """A configuration fallback retains the previous single-file engine."""
    import os
    if os.environ.get('ZAIN_CROSS_FILE_SCHEDULING', '1') == '0':
        return Orchestrator(supervisor, queue_service, project_root)
    return QueueScheduler(supervisor, queue_service, project_root)
