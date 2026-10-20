"""Browser service's explicit task boundary; no queue, wallet or workbook writes."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
import time

from workers.service_transport import connection_group, normalize_number


class BrowserApplication:
    retry_safe = ('query_submit', 'query_cancel', 'verification_cancel')

    def __init__(self, root: Path, *, transport=None):
        self.root = Path(root).resolve()
        self.transport = transport
        self.lock = threading.RLock()
        self.tasks = {}
        self.native_workers = {}
        self.native_worker_locks = {}
        self.stopping = False
        self.limit = max(1, min(64, int(os.getenv('ZAIN_BROWSER_TASK_LIMIT', '16'))))
        self.concurrency = max(1, min(16, int(os.getenv('ZAIN_BROWSER_CONCURRENCY', '4'))))
        self.executor = ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix='BrowserRPC')
        from services.browser_store import BrowserTaskStore
        self.store = BrowserTaskStore(self.root)
        self.wake = threading.Event()
        try:
            data = json.loads((self.root / 'config_workers.json').read_text(encoding='utf-8'))
            self.configs = {str(c['worker_id']): c for c in data.get('workers', [])}
        except FileNotFoundError:
            self.configs = {}
        self.allowed_proxies = {str(c.get('proxy_url') or c.get('proxy')) for c in self.configs.values()
                                if c.get('proxy_url') or c.get('proxy')}
        self.scheduler = threading.Thread(target=self._schedule, daemon=True, name='BrowserTaskScheduler')
        self.scheduler.start()

    def _browser(self):
        if self.transport is None:
            from workers import stealth_service_engine
            self.transport = stealth_service_engine
        return self.transport

    @staticmethod
    def _fields(payload, allowed):
        if not isinstance(payload, dict) or set(payload) - set(allowed):
            raise ValueError('Invalid browser request fields')

    @staticmethod
    def _id(value):
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', value):
            raise ValueError('Invalid browser task identifier')
        return value

    def submit(self, p):
        self._fields(p, ('task_id', 'number', 'timeout', 'proxy_url', 'allow_verification'))
        task_id = self._id(p.get('task_id'))
        number = normalize_number(p.get('number'))
        if not re.fullmatch(r'2[0-9]{0,63}', number):
            raise ValueError('Browser service only accepts service numbers')
        timeout = float(p.get('timeout', 12))
        if not math.isfinite(timeout) or not 1 <= timeout <= 120:
            raise ValueError('Browser timeout outside permitted range')
        proxy = p.get('proxy_url')
        if proxy is not None and proxy not in self.allowed_proxies:
            raise ValueError('Browser route is not configured')
        connection_group(proxy)
        fingerprint = hashlib.sha256(json.dumps([number, timeout, proxy, bool(p.get('allow_verification'))], ensure_ascii=True).encode()).hexdigest()
        with self.lock:
            from services.ipc import RetryLater
            from services.resources import admit
            if self.stopping or (not self.store.get(task_id) and not admit('browser')):
                raise RetryLater(delay=3.0)
            self.store.submit(task_id, fingerprint, {'number': number, 'timeout': timeout, 'proxy_url': proxy,
                              'allow_verification': bool(p.get('allow_verification'))}, self.limit)
        self.wake.set()
        return {'task_id': task_id, 'accepted': True}

    def _schedule(self):
        from services.resources import admit
        while not self.stopping:
            record = None
            try:
                with self.lock:
                    available = len(self.tasks) < self.concurrency
                if available and admit('browser'):
                    record = self.store.claim()
                    if record:
                        task = {'verification': None, 'cancel': threading.Event()}
                        with self.lock:
                            self.tasks[record['id']] = task
                        self.executor.submit(self._run_task, record, task)
                        continue
            except Exception:
                # Do not discard an accepted job on temporary local-storage,
                # resource-reading or executor failure. It remains durable.
                if record:
                    try:
                        self.store.defer(record['id'])
                    except Exception:
                        pass
            self.wake.wait(.2)
            self.wake.clear()

    def _run_task(self, record, task):
        task_id = record['id']
        p = json.loads(record['payload'])
        try:
            def verification(message):
                with self.lock:
                    task['verification'] = str(message)
            try:
                if task['cancel'].is_set():
                    result = ('network_error', None, 'أُلغيت مهمة المتصفح قبل البدء')
                else:
                    result = self._browser().query_service_stealth(p['number'], timeout_seconds=p['timeout'], proxy_url=p['proxy_url'],
                              on_verification=verification if p.get('allow_verification') else None,
                              cancel_event=task['cancel'])
                if not isinstance(result, (tuple, list)) or len(result) != 3:
                    raise ValueError('Invalid browser result')
                amount = result[1]
                if amount is not None and (isinstance(amount, bool) or not math.isfinite(float(amount)) or float(amount) < 0):
                    raise ValueError('Invalid browser amount')
            except Exception as exc:
                from services.ipc import RetryLater
                if isinstance(exc, RetryLater):
                    self.store.defer(task_id)
                    return
                result = ('network_error', None, 'تعذر تنفيذ مهمة المتصفح (' + type(exc).__name__ + ')')
            if self.stopping:
                self.store.defer(task_id)
            else:
                self.store.finish(task_id, result)
        finally:
            with self.lock:
                self.tasks.pop(task_id, None)
            self.wake.set()

    def status(self, p):
        self._fields(p, ('task_id',))
        with self.lock:
            task_id = self._id(p.get('task_id'))
            record = self.store.get(task_id)
            if record is None:
                return {'done': False, 'result': None, 'verification': None, 'state': 'awaiting_admission'}
            return {'done': record['state'] in ('succeeded', 'cancelled'),
                    'result': json.loads(record['result']) if record['result'] else None,
                    'verification': self.tasks.get(task_id, {}).get('verification'), 'state': record['state']}

    def cancel(self, p):
        self._fields(p, ('task_id',))
        with self.lock:
            task_id = self._id(p.get('task_id'))
            self.store.cancel(task_id)
            task = self.tasks.get(task_id)
            if task:
                task['cancel'].set()
        self.wake.set()
        return {'cancel_requested': True}

    def native_worker(self, p, operation):
        self._fields(p, ('worker_id',))
        worker_id = self._id(p.get('worker_id'))
        if worker_id not in self.configs:
            raise ValueError('Unknown configured browser worker')
        with self.lock:
            actor = self.native_workers.get(worker_id)
            if actor is None:
                from workers.actor import WorkerActor
                c = self.configs[worker_id]
                actor = WorkerActor(worker_id, c.get('name', worker_id), self.root / ('.zain-profile-' + worker_id),
                                    self.root / 'chrome_extension', use_proxy=bool(c.get('use_proxy')),
                                    proxy_url=c.get('proxy_url') or c.get('proxy'), use_direct_api=False,
                                    task_scope=c.get('task_scope', 'both'))
                self.native_workers[worker_id] = actor
            worker_lock = self.native_worker_locks.setdefault(worker_id, threading.Lock())
        with worker_lock:
            if operation == 'worker_launch':
                from services.resources import admit
                from services.ipc import RetryLater
                if not admit('browser'):
                    raise RetryLater(delay=3.0)
                native_limit = max(1, min(32, int(os.getenv('ZAIN_BROWSER_ROUTE_LIMIT', '4'))))
                if not actor.is_alive() and sum(w.is_alive() for w in self.native_workers.values()) >= native_limit:
                    raise RetryLater(delay=3.0)
                return {'ok': actor.launch(), 'pid': actor.pid}
            if operation == 'worker_stop':
                return {'ok': actor.terminate()}
            return {'alive': actor.is_alive(), 'pid': actor.pid}

    def close(self):
        with self.lock:
            self.stopping = True
            for task in self.tasks.values():
                task['cancel'].set()
        self.wake.set()
        self.scheduler.join(timeout=5.0)
        self._browser().stop_service_browsers()
        for actor in list(self.native_workers.values()):
            actor.terminate()
        self.executor.shutdown(wait=True, cancel_futures=True)

    def start(self):
        pass  # Chrome is created lazily only for an explicit browser task.

    def stop(self):
        self.close()

    def health(self):
        with self.lock:
            status = {'service': 'browser', 'pid': os.getpid(), 'ready': not self.stopping,
                      'tasks_active': len(self.tasks), 'task_limit': self.limit,
                      'task_states': self.store.counts()}
        try:
            import psutil
            process = psutil.Process()
            children = process.children(recursive=True)
            status['child_processes'] = len(children)
            status['rss_bytes'] = process.memory_info().rss + sum(child.memory_info().rss for child in children if child.is_running())
            status['threads'] = process.num_threads()
        except Exception:
            status['rss_bytes'] = None
            if Path('/proc/self/status').exists():
                try:
                    # Follow only this process's descendants, without scanning
                    # or taking authority over the user's other browser windows.
                    pending, seen, rss = [os.getpid()], set(), 0
                    while pending and len(seen) < 512:
                        pid = pending.pop()
                        if pid in seen:
                            continue
                        seen.add(pid)
                        try:
                            text = Path(f'/proc/{pid}/status').read_text()
                            values = {line.split(':', 1)[0]: line.split(':', 1)[1].strip() for line in text.splitlines() if ':' in line}
                            rss += int(values.get('VmRSS', '0').split()[0]) * 1024
                            pending.extend(int(value) for value in Path(f'/proc/{pid}/task/{pid}/children').read_text().split())
                            if pid == os.getpid():
                                status['threads'] = int(values.get('Threads', '0'))
                        except (FileNotFoundError, ProcessLookupError):
                            continue
                    status.update(rss_bytes=rss, child_processes=max(0, len(seen) - 1))
                except (OSError, ValueError):
                    pass
        return status

    def handlers(self):
        def empty(p):
            self._fields(p, ())
        def verification_list(p):
            empty(p)
            return self._browser().pending_verifications()
        def image(p):
            self._fields(p, ('id',))
            raw = self._browser().verification_image(self._id(p.get('id')))
            return {'image': base64.b64encode(raw).decode('ascii') if raw else None}
        def answer(p):
            self._fields(p, ('id', 'answer'))
            return self._browser().submit_verification(self._id(p.get('id')), p.get('answer'))
        def cancel_verification(p):
            empty(p)
            self._browser().cancel_pending_verifications()
            return True
        def health(p):
            empty(p)
            return self.health()
        return {'health': health, 'query_submit': self.submit, 'query_status': self.status, 'query_cancel': self.cancel,
                'verification_list': verification_list, 'verification_image': image,
                'verification_submit': answer, 'verification_cancel': cancel_verification,
                **{op: lambda p, op=op: self.native_worker(p, op) for op in ('worker_launch', 'worker_stop', 'worker_status')}}


_application = None


def build(root):
    transport = None
    if os.getenv('ZAIN_SERVICE_TESTING') == '1':
        from services.testing_browser import TestBrowserTransport,OfflineChromiumTransport
        transport = (OfflineChromiumTransport if os.getenv('ZAIN_TEST_REAL_CHROMIUM')=='1' else TestBrowserTransport)(root)
    return BrowserApplication(root, transport=transport)


def handlers(root):
    global _application
    _application = build(root)
    return _application.handlers()


def shutdown():
    if _application:
        _application.close()
