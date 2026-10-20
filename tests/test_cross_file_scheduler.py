"""Exercise real leasing and result commits using retained, in-memory fixtures.

No customer data, network checks, browser launches, builds or filesystem cleanup.
"""
import concurrent.futures
from pathlib import Path
import queue
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from domain.models import Customer
from manager.orchestrator import Orchestrator
from manager.scheduler import JobSession, QueueScheduler, create_orchestrator
from workers.actor import WorkerActor


def customers(job, accounts, services):
    return [Customer(row_number=i+2, record_type='wallet' if i >= accounts else 'account',
        lookup_number=('2' if i >= accounts else '1')+str(job).zfill(4)+str(i).zfill(6),
        contract='' if i >= accounts else '1'+str(job).zfill(4)+str(i).zfill(6),
        service_number='2'+str(job).zfill(4)+str(i).zfill(6) if i >= accounts else '',
        expected_amount=job*100, customer_name='LOCAL-FIXTURE-'+str(job)) for i in range(accounts+services)]


class MemoryQueue:
    def __init__(self, sizes):
        self.catalog = {}
        self.jobs = {}
        self.published = {}
        self.lock = threading.RLock()
        for index, (accounts, services) in enumerate(sizes, 1):
            key = 'file-'+str(index)
            self.catalog[key] = customers(index, accounts, services)
            self.jobs[key] = SimpleNamespace(id=key, filename=key+'.xlsx', status='pending',
                sheet_index=0, sheet_name='Sheet1', column_mapping={}, total_records=accounts+services,
                completed=0, remaining=accounts+services, matches=0, mismatches=0, errors=0,
                mode='smart_hybrid', amount_target='contract', target_url='https://example.invalid',
                checkpoint_file='.checkpoint_'+key+'.json', result_file='results-'+key+'.xlsx')

    def get_job(self, key):
        return self.jobs.get(key)

    def get_jobs(self):
        with self.lock:
            return [dict(vars(job)) for job in self.jobs.values()]

    def claim_next_pending_job(self, job_id=None):
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None or job.status != 'pending':
                return None
            job.status = 'active'
            return job

    def update_job_progress(self, job_id, **values):
        with self.lock:
            for key, value in values.items():
                setattr(self.jobs[job_id], key, value)

    def mark_job_active(self, key):
        self.jobs[key].status = 'active'

    def mark_job_completed(self, key, **kwargs):
        self.jobs[key].status = 'completed'

    def mark_job_failed(self, key, message):
        self.jobs[key].status = 'failed'

    def defer_job(self, key):
        self.jobs[key].status = 'pending'


class MemorySession(Orchestrator):
    def start_session_from_config(self, **config):
        job_id = config['job_id']
        if config['mapping'].get('commerce_order_id') and not self.execution_authorizer(job_id):
            raise PermissionError('Unfunded fixture')
        self.current_job_id = job_id
        self.workbook_name = config['workbook_path'].name
        self.mode = config['mode']
        self.amount_target = config['amount_target']
        self.customers = self.queue_service.catalog[job_id]
        self.is_running = True
        self.result_queue = queue.Queue()
        self.input_signature = 'frozen-'+job_id
        self._reset_progress_timing_locked()
        self._init_task_queues_locked()
        return True

    def _finalize_job_impl(self):
        self.is_running = False
        self.queue_service.published[self.current_job_id] = list(self.all_completed_records.values())
        self.queue_service.mark_job_completed(self.current_job_id)
        self.result_ready = True
        self.is_finalizing = False


def fixture(sizes, *, account_workers=3, service_workers=3, shared_service_route=False, **options):
    root = Path(__file__).resolve().parents[1]/'retained-cross-file-fixtures'
    actors = {}
    for scope, count in [('account', account_workers), ('service', service_workers)]:
        for i in range(count):
            key = scope+'-'+str(i)
            actor = WorkerActor(key, key, root/key, root, task_scope=scope,
                use_proxy=scope == 'service', proxy_url='http://route-'+str(0 if shared_service_route else i)+'.invalid:8080' if scope == 'service' else None)
            actor.status = 'ready'
            actors[key] = actor
    supervisor = SimpleNamespace(get_worker=actors.get, get_all_workers=lambda: list(actors.values()),
        get_status_summary=lambda: [a.to_dict() for a in actors.values()], start_all=Mock(), stop_all=Mock())
    jobs = MemoryQueue(sizes)
    scheduler = QueueScheduler(supervisor, jobs, root, session_factory=MemorySession, start_workers=False, **options)
    return scheduler, jobs, actors


class CrossFileSchedulerTests(unittest.TestCase):
    def admit(self, scheduler, jobs):
        for key in jobs.jobs:
            self.assertTrue(scheduler.start_queue(key))

    def complete(self, scheduler, task, worker):
        amount = str(int(task['job_id'].split('-')[1]))
        scheduler.record_task_outcome(task['task_id'], amount, 'match', worker_id=worker)

    def test_service_workers_pass_later_files_while_first_accounts_continue(self):
        scheduler, jobs, _ = fixture([(1000, 1)]*10+[(1, 100)]*10)
        self.admit(scheduler, jobs)
        for index in range(1, 12):
            task = scheduler.lease_next_task_for_worker('service-0')
            self.assertEqual(task['job_id'], 'file-'+str(index))
            self.complete(scheduler, task, 'service-0')
        self.assertEqual(jobs.jobs['file-1'].completed, 1)
        self.assertEqual(jobs.jobs['file-1'].remaining, 1000)
        self.assertEqual(jobs.jobs['file-11'].completed, 1)
        self.assertEqual(scheduler.lease_next_task_for_worker('account-0')['job_id'], 'file-1')

    def test_accounts_reach_file_19_while_file_15_services_are_unfinished(self):
        scheduler, jobs, _ = fixture([(1, 100)]*20)
        self.admit(scheduler, jobs)
        for index in range(1, 20):
            task = scheduler.lease_next_task_for_worker('account-0')
            self.assertEqual(task['job_id'], 'file-'+str(index))
            self.complete(scheduler, task, 'account-0')
        self.assertEqual(jobs.jobs['file-19'].completed, 1)
        self.assertEqual(jobs.jobs['file-15'].remaining, 100)
        self.assertEqual(scheduler.lease_next_task_for_worker('service-0')['job_id'], 'file-1')

    def test_full_20_file_example_has_exactly_one_result_per_row_and_owner(self):
        scheduler, jobs, actors = fixture([(1000, 1)]*10+[(1, 100)]*10, account_workers=50)
        self.admit(scheduler, jobs)
        total = 0
        seen = set()
        while total < 11020:
            progressed = False
            for worker in actors:
                task = scheduler.lease_next_task_for_worker(worker)
                if task is None:
                    continue
                identity = (task['job_id'], task['row_number'])
                self.assertNotIn(identity, seen)
                seen.add(identity)
                self.complete(scheduler, task, worker)
                total += 1
                progressed = True
            self.assertTrue(progressed)
        for key, session in scheduler._sessions.items():
            engine = session.engine
            self.assertEqual(len(engine.completed_indices), len(jobs.catalog[key]))
            self.assertEqual(len(engine.all_completed_records), len(jobs.catalog[key]))
            self.assertEqual(engine.matches_count, len(jobs.catalog[key]))
            self.assertTrue(all(r['name'] == 'LOCAL-FIXTURE-'+key.split('-')[1] for r in engine.all_completed_records.values()))
        self.assertEqual(total, 10010+1010)
        self.assertEqual(scheduler.supervisor.stop_all.call_count, 0)

    def test_same_row_index_and_stale_or_duplicate_results_never_cross_files(self):
        scheduler, jobs, _ = fixture([(1, 1), (1, 1)])
        self.admit(scheduler, jobs)
        a = scheduler.lease_next_task_for_worker('account-0')
        b = scheduler.lease_next_task_for_worker('account-1')
        self.assertEqual(a['index'], b['index'])
        self.assertNotEqual(a['job_id'], b['job_id'])
        scheduler.record_task_outcome(a['task_id'], '999', 'match', worker_id='account-1')
        self.assertEqual(jobs.jobs['file-1'].completed, 0)
        self.complete(scheduler, b, 'account-1')
        self.complete(scheduler, b, 'account-1')
        self.complete(scheduler, a, 'account-0')
        self.assertEqual(jobs.jobs['file-1'].completed, 1)
        self.assertEqual(jobs.jobs['file-2'].completed, 1)

    def test_same_worker_poll_keeps_lease_and_shared_routes_exclude_other_files(self):
        scheduler, jobs, _ = fixture([(0, 1), (0, 1)], shared_service_route=True)
        self.admit(scheduler, jobs)
        first = scheduler.lease_next_task_for_worker('service-0')
        self.assertEqual(scheduler.lease_next_task_for_worker('service-0')['task_id'], first['task_id'])
        self.assertIsNone(scheduler.lease_next_task_for_worker('service-1'))
        self.complete(scheduler, first, 'service-0')
        self.assertEqual(scheduler.lease_next_task_for_worker('service-1')['job_id'], 'file-2')

    def test_due_deferred_service_in_old_file_precedes_later_regular_service(self):
        scheduler, jobs, _ = fixture([(10, 1), (1, 1)])
        self.admit(scheduler, jobs)
        first = scheduler.lease_next_task_for_worker('service-0')
        scheduler.defer_task_to_end(first['task_id'], 'temporary fixture', worker_id='service-0', retry_after=100, category='network_error')
        later = scheduler.lease_next_task_for_worker('service-0')
        self.assertEqual(later['job_id'], 'file-2')
        self.complete(scheduler, later, 'service-0')
        old = scheduler._sessions['file-1'].engine
        old.deferred_retry_at[first['index']] = 0
        recovered = scheduler.lease_next_task_for_worker('service-0')
        self.assertEqual(recovered['job_id'], 'file-1')
        self.assertEqual(old.active_leases[recovered['task_id']]['phase'], 'retry')

    def test_manual_route_block_applies_across_files_but_healthy_route_continues(self):
        scheduler, jobs, actors = fixture([(0, 1), (0, 1)])
        self.admit(scheduler, jobs)
        old = scheduler._sessions['file-1'].engine
        route = actors['service-0'].ip_group
        old.blocked_service_routes[route] = {'pending': True, 'automatic_recovery': False, 'worker_id': 'service-0', 'task_id': 'old'}
        self.assertIsNone(scheduler.lease_next_task_for_worker('service-0'))
        self.assertIsNotNone(scheduler.lease_next_task_for_worker('service-1'))

    def test_stopping_one_customer_preserves_its_inflight_row_and_other_customers(self):
        scheduler, jobs, _ = fixture([(2, 1), (2, 1)])
        self.admit(scheduler, jobs)
        inflight = scheduler.lease_next_task_for_worker('account-0')
        jobs.jobs['file-1'].column_mapping['customer_stop'] = {'reference': 'owner-confirmed'}
        self.assertTrue(scheduler.apply_customer_stop('file-1'))
        first = scheduler._sessions['file-1'].engine
        self.assertIn(inflight['task_id'], first.active_leases)
        self.assertEqual(sum(r['status'] == 'cancelled' for r in first.all_completed_records.values()), 2)
        later = scheduler.lease_next_task_for_worker('account-1')
        self.assertEqual(later['job_id'], 'file-2')
        self.complete(scheduler, inflight, 'account-0')
        self.assertEqual(jobs.jobs['file-1'].completed, 3)
        self.assertEqual(scheduler.supervisor.stop_all.call_count, 0)

    def test_busy_file_lock_does_not_block_a_different_file(self):
        scheduler, jobs, _ = fixture([(2, 0), (2, 0)])
        self.admit(scheduler, jobs)
        old = scheduler._sessions['file-1'].engine
        acquired = threading.Event()
        release = threading.Event()
        def hold():
            with old.lock:
                acquired.set()
                release.wait(3)
        thread = threading.Thread(target=hold)
        thread.start()
        acquired.wait(1)
        try:
            started = time.monotonic()
            task = scheduler.lease_next_task_for_worker('account-0')
            self.assertLess(time.monotonic()-started, .5)
            self.assertEqual(task['job_id'], 'file-2')
        finally:
            release.set()
            thread.join(3)

    def test_unfunded_customer_is_never_claimed_and_admission_has_a_memory_limit(self):
        scheduler, jobs, _ = fixture([(1, 1), (1, 1)], max_active_jobs=1)
        jobs.jobs['file-1'].column_mapping['commerce_order_id'] = 'unfunded-fixture'
        scheduler.execution_authorizer = Mock(return_value=False)
        self.assertFalse(scheduler.start_queue('file-1'))
        self.assertEqual(jobs.jobs['file-1'].status, 'pending')
        scheduler.execution_authorizer.return_value = True
        self.assertTrue(scheduler.start_queue('file-1'))
        self.assertFalse(scheduler.start_queue('file-2'))
        self.assertEqual(jobs.jobs['file-2'].status, 'pending')

    def test_global_pause_stops_both_types_and_resume_restores_them(self):
        scheduler, jobs, _ = fixture([(1, 1), (1, 1)])
        self.admit(scheduler, jobs)
        scheduler.pause_session()
        self.assertIsNone(scheduler.lease_next_task_for_worker('account-0'))
        self.assertIsNone(scheduler.lease_next_task_for_worker('service-0'))
        scheduler.resume_session()
        self.assertIsNotNone(scheduler.lease_next_task_for_worker('account-0'))
        self.assertIsNotNone(scheduler.lease_next_task_for_worker('service-0'))

    def test_concurrent_workers_preserve_exactly_once_completion(self):
        scheduler, jobs, actors = fixture([(10, 5)]*20, account_workers=10)
        self.admit(scheduler, jobs)
        def run(worker):
            for _ in range(1000):
                task = scheduler.lease_next_task_for_worker(worker)
                if task is None:
                    break
                self.complete(scheduler, task, worker)
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(actors)) as pool:
            list(pool.map(run, actors))
        self.assertEqual(sum(j.completed for j in jobs.jobs.values()), 300)
        self.assertEqual(sum(len(s.engine.all_completed_records) for s in scheduler._sessions.values()), 300)

    def test_finishing_one_file_does_not_stop_other_workers_or_merge_reports(self):
        scheduler, jobs, _ = fixture([(1, 0), (2, 0)])
        self.admit(scheduler, jobs)
        task = scheduler.lease_next_task_for_worker('account-0')
        self.complete(scheduler, task, 'account-0')
        engine = scheduler._sessions['file-1'].engine
        engine.is_finalizing = True
        scheduler.finalize_session(engine)
        self.assertNotIn('file-1', scheduler.active_job_ids)
        self.assertEqual(jobs.jobs['file-1'].status, 'completed')
        self.assertEqual(len(jobs.published['file-1']), 1)
        self.assertEqual(scheduler.lease_next_task_for_worker('account-0')['job_id'], 'file-2')
        scheduler.supervisor.stop_all.assert_not_called()

    def test_legacy_fallback_and_shared_pool_start_only_once(self):
        with patch.dict('os.environ', {'ZAIN_CROSS_FILE_SCHEDULING': '0'}):
            obj = create_orchestrator(SimpleNamespace(), MemoryQueue([]), Path('.'))
            self.assertIsInstance(obj, Orchestrator)
        scheduler, _, _ = fixture([])
        scheduler._start_workers = True
        scheduler.supervisor.get_all_workers = lambda: []
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: scheduler.ensure_workers(), range(8)))
        scheduler.supervisor.start_all.assert_called_once()

    def test_shared_api_threads_process_each_file_without_restarting_pool(self):
        scheduler, jobs, actors = fixture([(3, 1), (3, 1)])
        self.admit(scheduler, jobs)
        for actor in actors.values():
            actor.use_direct_api = True
            actor.execute_task_api = Mock(return_value=('ok', '1', 'local fixture'))
        scheduler._start_workers = True
        try:
            scheduler.ensure_workers()
            deadline = time.monotonic()+10
            while sum(j.completed for j in jobs.jobs.values()) < 8 and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertEqual(sum(j.completed for j in jobs.jobs.values()), 8)
            self.assertEqual(sum(a.execute_task_api.call_count for a in actors.values()), 8)
            scheduler.supervisor.start_all.assert_called_once()
            scheduler.supervisor.stop_all.assert_not_called()
        finally:
            scheduler._pool_stop.set()
            scheduler._worker_wake.set()
            for thread in scheduler._worker_threads.values():
                thread.join(3)
                self.assertFalse(thread.is_alive())

    def test_cancelled_lease_never_calls_lookup_api(self):
        scheduler, jobs, actors = fixture([(1, 0)])
        self.admit(scheduler, jobs)
        task = scheduler.lease_next_task_for_worker('account-0')
        engine = scheduler._sessions['file-1'].engine
        actors['account-0'].execute_task_api = Mock()
        engine.active_leases.clear()
        self.assertFalse(engine.execute_leased_task_api('account-0', task, engine.session_token))
        actors['account-0'].execute_task_api.assert_not_called()

    def test_publication_failure_is_visible_resumable_and_other_files_continue(self):
        from telegram_bot.views import controls, state_label
        scheduler, jobs, _ = fixture([(1, 0), (2, 0)])
        self.admit(scheduler, jobs)
        self.complete(scheduler, scheduler.lease_next_task_for_worker('account-0'), 'account-0')
        engine = scheduler._sessions['file-1'].engine
        engine.is_finalizing = True
        with patch.object(engine, '_finalize_job_impl', side_effect=OSError('local fixture disk error')):
            scheduler.finalize_session(engine)
        self.assertTrue(engine.is_paused)
        state = scheduler.get_live_status()
        self.assertFalse(state['paused'])
        self.assertEqual(state['scheduling']['paused_files'], 1)
        self.assertIn('بعض الملفات', state_label(state))
        self.assertIn('استئناف الملفات', str(controls(state)))
        from telegram_bot.ui import TelegramUiMixin
        self.assertEqual(TelegramUiMixin._action_problem('resume', state), '')
        self.assertEqual(scheduler.lease_next_task_for_worker('account-0')['job_id'], 'file-2')
        scheduler.resume_session()
        self.assertFalse(engine.is_paused)
        engine.is_finalizing = True
        scheduler.finalize_session(engine)
        self.assertEqual(jobs.jobs['file-1'].status, 'completed')

    def test_colliding_checkpoint_or_report_is_not_admitted(self):
        scheduler, jobs, _ = fixture([(1, 0), (1, 0)])
        self.assertTrue(scheduler.start_queue('file-1'))
        jobs.jobs['file-2'].checkpoint_file = jobs.jobs['file-1'].checkpoint_file
        self.assertFalse(scheduler.start_queue('file-2'))
        self.assertEqual(jobs.jobs['file-2'].status, 'pending')

    def test_continuous_consumer_admits_funded_second_file_while_first_runs(self):
        from manager.continuous_service import ContinuousQueueService
        scheduler, jobs, _ = fixture([(2, 0), (2, 0)])
        scheduler.execution_authorizer = Mock(return_value=True)
        for job in jobs.jobs.values():
            job.column_mapping['commerce_order_id'] = 'fixture-funded'
        client = Mock()
        service = ContinuousQueueService(scheduler, jobs, scheduler.project_root, client)
        service.funded = Mock(return_value=True)
        self.assertTrue(service.tick())
        self.assertTrue(scheduler.is_running)
        self.assertTrue(service.tick())
        self.assertEqual(scheduler.active_job_ids, ('file-1', 'file-2'))
        client.rpc.assert_called_once_with('expire_promotional_credit', {'p_limit': 100})

    def test_manually_started_local_round_admits_next_file_before_first_finishes(self):
        scheduler, jobs, _ = fixture([(1000, 1), (1, 100)])
        self.assertTrue(scheduler.start_queue())
        scheduler.maintain_sessions()
        self.assertEqual(scheduler.active_job_ids, ('file-1', 'file-2'))
        service = scheduler.lease_next_task_for_worker('service-0')
        self.complete(scheduler, service, 'service-0')
        self.assertEqual(scheduler.lease_next_task_for_worker('service-0')['job_id'], 'file-2')
        self.assertEqual(jobs.jobs['file-1'].remaining, 1000)

    def test_local_round_does_not_approve_uploads_added_after_start(self):
        scheduler, jobs, _ = fixture([(1, 0), (1, 0), (1, 0)])
        jobs.jobs['file-3'].status = 'not_uploaded_yet'
        self.assertTrue(scheduler.start_queue())
        jobs.jobs['file-3'].status = 'pending'
        first = scheduler._sessions['file-1'].engine
        self.complete(scheduler, scheduler.lease_next_task_for_worker('account-0'), 'account-0')
        first.is_finalizing = True
        scheduler.finalize_session(first)
        scheduler.maintain_sessions()
        self.assertEqual(scheduler.active_job_ids, ('file-2',))
        self.assertEqual(jobs.jobs['file-3'].status, 'pending')

    def test_route_requiring_operator_in_second_file_is_visible_in_status(self):
        scheduler, jobs, actors = fixture([(1, 1), (1, 1)])
        self.admit(scheduler, jobs)
        other = scheduler._sessions['file-2'].engine
        other.blocked_service_routes[actors['service-0'].ip_group] = {
            'pending': False, 'automatic_recovery': False, 'worker_id': 'service-0', 'task_id': 'fixture'}
        worker = next(w for w in scheduler.get_live_status()['workers'] if w['worker_id'] == 'service-0')
        self.assertTrue(worker['service_verification_required'])
        self.assertTrue(scheduler.resume_service_route('service-0'))


if __name__ == '__main__':
    unittest.main()
