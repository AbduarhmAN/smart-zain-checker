"""Central State Orchestrator for Smart Zain Checker.
Coordinates task leasing across isolated workers, progress checkpointing,
audit workbook results export, and real-time EventBus dispatches.
"""
from __future__ import annotations

import collections
from collections import deque
import logging
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from domain.models import CheckError, CheckResult, Customer, Mismatch, ProgressMismatch
from domain.money import is_amount_match, parse_money_to_halalas
from domain.results_exporter import append_error_record, append_mismatch_record, ensure_audit_workbook_exists
from domain.workbook import extract_customer_records
from manager.checkpoint import CheckpointManager
from manager.recovery import input_signature, read_journal, append_journal, restore_records
from manager.event_bus import EVENT_BUS
from manager.queue import QueueService
from manager.progress_timing import ProgressTiming
from workers.supervisor import WorkerSupervisor
from workers.service_transport import (
    SERVICE_INTERVAL, get_ip_service_lock, get_ip_service_retry_after,
    normalize_number, set_ip_service_cooldown,
)

logger = logging.getLogger("Orchestrator")


class Orchestrator:
    def __init__(
        self,
        supervisor: WorkerSupervisor,
        queue_service: QueueService,
        project_root: Path,
    ) -> None:
        self.supervisor = supervisor
        self.queue_service = queue_service
        self.project_root = project_root

        self.lock = threading.RLock()
        self.is_running = False
        self.is_paused = False
        self.is_finalizing = False
        self.is_exporting = False
        self.result_ready = False
        self.reserved_next_job_id: Optional[str] = None

        # Current Job Context
        self.current_job_id: Optional[str] = None
        self.queue_round_job_ids: list[str] = []
        self.workbook_name: str = ""
        self.result_path: Path = project_root / "نتائج فحص زين.xlsx"
        self.checkpoint_manager: Optional[CheckpointManager] = None

        self.customers: List[Customer] = []
        self.completed_indices: Set[int] = set()
        self.mismatches: List[ProgressMismatch] = []
        self.errors_count = 0
        self.matches_count = 0
        self.target_url: str = "https://business.zain.sa/dashboard/quick-pay"

        # Leased tasks: task_id -> {"index": int, "customer": Customer, "worker_id": str, "leased_at": float}
        self.active_leases: Dict[str, dict[str, Any]] = {}
        self.recent_results: List[dict[str, Any]] = []

        # Decoupled Async Excel Writer & Master Records Buffer
        self.result_queue: Optional[Any] = None
        self.writer_thread: Optional[Any] = None
        self.all_completed_records: Dict[int, Dict[str, Any]] = {}

        # Deferred retry queue for blocked service numbers (retried at the very end of session)
        self.deferred_indices: List[int] = []
        self.deferred_attempts: Dict[int, int] = {}
        self.deferred_retry_at: Dict[int, float] = {}
        self.retry_attempts_by_status: Dict[int, dict[str, int]] = {}
        self.reviews_count = 0
        self.not_found_count = 0
        self.max_block_retries = 2
        self.session_token = uuid.uuid4().hex
        self.session_started_at: Optional[float] = None
        self.session_initial_completed = 0
        self.verification_notice = ""
        self.verification_pause_tasks: set[str] = set()
        self.verification_auto_resume = False
        self.blocked_service_routes: dict[str, dict[str, Any]] = {}
        self.telegram_enabled = False
        self.source_workbook_path: Optional[Path] = None
        self.source_sheet_index: int = 0
        self.workbook_stem: str = ""
        self.is_repair_session: bool = False
        self.source_has_headers = True
        self.progress_timing = ProgressTiming()

        # In-Memory Fast Task Queues (O(1) dispatching)
        self._pending_accounts: deque[int] = deque()
        self._pending_services: deque[int] = deque()
        self._task_queues_initialized_for: Optional[tuple[int, int]] = None

    def _is_service_index(self, index: int) -> bool:
        if not (0 <= index < len(self.customers)):
            return False
        if getattr(self, "mode", "") == "account_only":
            return False
        cust = self.customers[index]
        if getattr(cust, "record_type", "") == "wallet":
            return True
        srv = getattr(cust, "service_number", "") or ""
        if srv.lstrip().startswith("2"):
            return True
        look = getattr(cust, "lookup_number", "") or ""
        if look.lstrip().startswith("2"):
            return True
        cont = getattr(cust, "contract", "") or ""
        if cont.lstrip().startswith("2"):
            return True
        return False

    def _init_task_queues_locked(self) -> None:
        self._pending_accounts.clear()
        self._pending_services.clear()
        leased = {data["index"] for data in self.active_leases.values()}
        for i in range(len(self.customers)):
            if i in self.completed_indices or i in leased or i in self.deferred_indices:
                continue
            if self._is_service_index(i):
                self._pending_services.append(i)
            else:
                self._pending_accounts.append(i)
        self._task_queues_initialized_for = (id(self.customers), len(self.customers))

    def _ensure_task_queues_locked(self) -> None:
        needs_init = (
            getattr(self, "_task_queues_initialized_for", None) != (id(self.customers), len(self.customers))
            or (
                len(self._pending_accounts) == 0
                and len(self._pending_services) == 0
                and len(self.customers) > len(self.completed_indices) + len(self.deferred_indices) + len(self.active_leases)
            )
        )
        if needs_init:
            self._init_task_queues_locked()

    def _reset_progress_timing_locked(self) -> None:
        self.progress_timing = ProgressTiming(
            'service' if self._effective_search_number(customer).startswith('2') and getattr(self, "mode", "") != "account_only" else 'account'
            for index, customer in enumerate(self.customers) if index not in self.completed_indices)


    def _is_session_busy_locked(self, allowed_job_id: Optional[str] = None) -> Tuple[bool, str]:
        """Checks if any session, finalization, background writer, or export is active."""
        if self.is_running:
            return True, "يوجد فحص نشط قيد التنفيذ حالياً"
        if getattr(self, "is_finalizing", False):
            if allowed_job_id and allowed_job_id == getattr(self, "reserved_next_job_id", None):
                pass
            else:
                return True, "تجري حالياً معالجة إنهاء وحفظ جلسة سابقة"
        if getattr(self, "is_exporting", False):
            return True, "تجري حالياً عملية تصدير للنتائج على القرص"
        if self.writer_thread and self.writer_thread.is_alive():
            return True, "كاتب النتائج لا يزال نشطاً في الخلفية ويقوم بحفظ البيانات"
        return False, ""

    def start_session_from_config(
        self,
        workbook_path: Path,
        sheet_index: int,
        mapping: dict[str, Any],
        mode: str = "smart_hybrid",
        amount_target: str = "contract",
        target_url: str = "https://business.zain.sa/dashboard/quick-pay",
        job_id: Optional[str] = None,
        result_file: str = "نتائج فحص زين.xlsx",
        force_restart: bool = False,
    ) -> bool:
        """Initializes customers from the Excel sheet and starts the worker pool."""
        with self.lock:
            busy, reason = self._is_session_busy_locked(allowed_job_id=job_id)
            if busy:
                logger.warning(f"Cannot start session: {reason}")
                return False

        from domain.workbook import load_fast_workbook
        wb = load_fast_workbook(workbook_path)
        try:
            ws = wb.worksheets[sheet_index] if 0 <= sheet_index < len(wb.worksheets) else wb.worksheets[0]
            
            from domain.workbook import parse_excel_column, inspect_sheet_schema

            # Auto-detect sheet schema and merge with mapping to always capture status columns
            has_headers = (mapping or {}).get("has_headers", True)
            if not isinstance(has_headers, bool):
                raise ValueError("Invalid has_headers flag")
            self.source_has_headers = has_headers
            self.mawarid_format = bool((mapping or {}).get("mawarid_format", False))
            auto_schema = inspect_sheet_schema(ws, has_headers=has_headers, count_rows=False)
            auto_letters = auto_schema.get("letters") or {}
            auto_indices = auto_schema.get("indices") or {}

            full_mapping = dict(auto_letters)
            full_mapping.update(mapping or {})
            mapping = full_mapping

            lookup_col = parse_excel_column(mapping.get("lookup_col")) or parse_excel_column(mapping.get("service_col")) or 12
            amount_col = parse_excel_column(mapping.get("amount_col")) or 15
            amount_col_2 = parse_excel_column(mapping.get("amount_col_2"))

            service_col = parse_excel_column(mapping.get("service_col"))
            if mode == "account_only":
                service_col = None
            elif mode == "service_only" and service_col:
                lookup_col = service_col
            customer_col = parse_excel_column(mapping.get("customer_col"))
            collector_col = parse_excel_column(mapping.get("collector_col"))
            source_row_col = parse_excel_column(mapping.get("source_row_col")) or auto_indices.get("source_row_col")
            case_status_col = parse_excel_column(mapping.get("case_status_col")) or auto_indices.get("case_status_col")
            main_status_col = parse_excel_column(mapping.get("main_status_col")) or auto_indices.get("main_status_col")
            sub_status_col = parse_excel_column(mapping.get("sub_status_col")) or auto_indices.get("sub_status_col")
            notes_col = parse_excel_column(mapping.get("notes_col")) or auto_indices.get("notes_col")

            extracted_customers = extract_customer_records(
                sheet=ws,
                lookup_col=lookup_col,
                amount_col=amount_col,
                amount_col_2=amount_col_2,
                service_col=service_col,
                customer_col=customer_col,
                collector_col=collector_col,
                source_row_col=source_row_col,
                has_headers=has_headers,
                case_status_col=case_status_col,
                main_status_col=main_status_col,
                sub_status_col=sub_status_col,
                notes_col=notes_col,
                record_type="wallet" if mode == "service_only" else ("account" if mode == "account_only" else "mixed"),
            )
        finally:
            wb.close()

        if not extracted_customers:
            logger.warning(f"No valid rows found in {workbook_path.name}")
            return False

        with self.lock:
            if self.is_running or (getattr(self, 'is_finalizing', False) and job_id != getattr(self, 'reserved_next_job_id', None)):
                logger.warning("Session already active.")
                return False
            self.customers = extracted_customers
            self.mode = mode
            self.amount_target = amount_target
 
            if not job_id and self.queue_service:
                self.queue_round_job_ids = []
                qj = self.queue_service.get_job_by_file_and_sheet(workbook_path.name, sheet_index)
                if qj:
                    job_id = qj.id
                    self.queue_service.mark_job_active(job_id)

            self.current_job_id = job_id
            self.workbook_name = workbook_path.name
            self.workbook_stem = workbook_path.stem
            self.source_workbook_path = workbook_path
            self.source_sheet_index = sheet_index
            self.is_repair_session = False
            self.result_path = self.project_root / result_file
            self.result_ready = False
            self.is_finalizing = False
            self.reserved_next_job_id = None
            self.session_token = uuid.uuid4().hex
            job = self.queue_service.get_job(job_id) if job_id else None
            checkpoint_name = (job.checkpoint_file if job and job.checkpoint_file else f'.checkpoint_{workbook_path.stem}_{sheet_index}.json')
            if force_restart:
                # Preserve previous journals/results; a fresh run uses distinct files.
                checkpoint_name = f'.checkpoint_{workbook_path.stem}_{sheet_index}_{self.session_token}.json'
                self.result_path = self.result_path.with_name(self.result_path.stem+' - '+self.session_token[:8]+'.xlsx')
                if job:
                    job.checkpoint_file = checkpoint_name
                    job.result_file = self.result_path.name
                    self.queue_service._save_to_disk()
            self.checkpoint_manager = CheckpointManager(self.project_root/checkpoint_name)
            journal_stem = checkpoint_name[len('.checkpoint_'):-len('.json')] if (job_id and job_id in checkpoint_name) or force_restart else workbook_path.stem
            self.wal_path = self.project_root/f'.wal_{journal_stem}.jsonl'
            stream_path = self.project_root/f'.stream_{journal_stem}.xlsx'
            self.input_signature = input_signature(self.customers, mapping, mode, amount_target, sheet_index)
            saved = {} if force_restart else self.checkpoint_manager.load()
            if saved.get('input_signature') and saved['input_signature'] != self.input_signature:
                self.start_error = 'تغيرت بيانات الملف أو اختيار الأعمدة. اختر فحصًا جديدًا للاحتفاظ بالنتائج السابقة.'
                return False
            restored = restore_records(self.customers, saved, read_journal(self.wal_path))
            self.completed_indices = set(restored)
            self.mismatches = [ProgressMismatch(sequence_index=i,
                expected_amount=round(float(r['expected_sar'])*100), website_amount=round(float(r['live_sar'])*100))
                for i,r in restored.items() if r.get('status') == 'mismatch' and r.get('live_sar') is not None]
            self.errors_count = sum(r.get('status') == 'error' for r in restored.values())
            self.matches_count = sum(r.get('status') == 'match' for r in restored.values())
            self.start_error = ''
            ensure_audit_workbook_exists(self.result_path)

            self.deferred_indices.clear()
            self.deferred_attempts.clear()
            self.deferred_retry_at.clear()
            self.retry_attempts_by_status.clear()
            self.reviews_count = sum(r.get('status') == 'needs_review' for r in restored.values())
            self.not_found_count = sum(r.get('status') == 'not_found' for r in restored.values())
            if not force_restart:
                self.deferred_indices = [int(i) for i in saved.get("deferred_indices", [])
                                         if 0 <= int(i) < len(self.customers) and int(i) not in self.completed_indices]
                self.deferred_attempts = {int(i): int(n) for i, n in saved.get("deferred_attempts", {}).items()}
                self.retry_attempts_by_status = {int(i): dict(n) for i, n in saved.get("retry_attempts_by_status", {}).items()}
                self.deferred_retry_at = {int(i): time.monotonic() + max(0.0, float(n))
                                          for i, n in saved.get("deferred_retry_delays", {}).items()}
            self.target_url = target_url
            self.is_running = True
            self.is_paused = False
            self.session_started_at = saved.get('started_at') or time.time()
            self.session_initial_completed = len(self.completed_indices)
            self._reset_progress_timing_locked()
            self.verification_notice = ""
            self.verification_pause_tasks.clear()
            self.verification_auto_resume = False
            self.blocked_service_routes.clear()
            self.active_leases.clear()
            self.recent_results.clear()
            self._init_task_queues_locked()

            # Start Decoupled Background Async Writer Thread
            import queue
            from zain_checker.async_pipeline_engine import AsyncExcelWriterThread

            self.result_queue = queue.Queue()
            self.all_completed_records.clear()
            self.all_completed_records.update(restored)
            if self.current_job_id:
                self.queue_service.update_job_progress(self.current_job_id, len(restored), len(self.customers)-len(restored),
                    self.matches_count, len(self.mismatches), self.errors_count)
            self._save_checkpoint_locked(-1)
            self.writer_thread = AsyncExcelWriterThread(
                result_queue=self.result_queue,
                result_path=stream_path,
                wal_path=self.wal_path,
                batch_size=200,
                flush_interval_seconds=15.0,
                wal_already_persisted=True,
            )
            self.writer_thread.start()


        # Start isolated worker actors
        self.supervisor.start_all(target_url=target_url)

        # Launch Direct API background threads for active workers
        for actor in self.supervisor.get_all_workers():
            if getattr(actor, "use_direct_api", False) and actor.status == "ready":
                w_thread = threading.Thread(
                    target=self._run_worker_api_loop,
                    args=(actor.worker_id, self.session_token),
                    name=f"DirectApiWorker-{actor.worker_id}",
                    daemon=True,
                )
                w_thread.start()

        job_owner=next((job.get('column_mapping',{}).get('telegram_chat_id') for job in self.queue_service.get_jobs() if job.get('id')==self.current_job_id),None)
        EVENT_BUS.publish("session_started", {
            "telegram_chat_id":job_owner,
            "workbook": self.workbook_name,
            "target_url": self.target_url,
            "total_records": len(self.customers),
            "previously_completed": len(self.completed_indices),
        })
        return True

    @staticmethod
    def _search_number(customer: Customer) -> str:
        service = normalize_number(customer.service_number)
        lookup = normalize_number(customer.lookup_number)
        if service.startswith("2"):
            return service
        if lookup.startswith("2"):
            return lookup
        return normalize_number(customer.contract or customer.lookup_number)

    def _effective_search_number(self, customer: Customer) -> str:
        mode = getattr(self, "mode", "smart_hybrid")
        if mode == "account_only":
            acc = normalize_number(customer.contract or customer.original_account_number or customer.lookup_number)
            if acc:
                return acc
        elif mode == "service_only":
            srv = normalize_number(customer.service_number or customer.lookup_number)
            if srv:
                return srv
        return self._search_number(customer)

    def _task_payload(self, task_id: str, data: dict) -> dict[str, Any]:
        customer = data["customer"]
        number = self._effective_search_number(customer)
        mode = getattr(self, "mode", "smart_hybrid")
        if mode == "account_only":
            rec_type = "account"
            target_url = self.target_url
        elif mode == "service_only":
            rec_type = "wallet"
            target_url = f"https://app.sa.zain.com/ar/quickpay?account={number}"
        else:
            rec_type = "wallet" if number.startswith("2") else "account"
            target_url = (f"https://app.sa.zain.com/ar/quickpay?account={number}"
                           if number.startswith("2") else self.target_url)
        return {
            "task_id": task_id, "index": data["index"],
            "row_number": customer.row_number, "search_number": number,
            "contract": customer.contract, "service_number": customer.service_number,
            "record_type": rec_type,
            "expected_amount_sar": customer.expected_amount / 100.0,
            "target_url": target_url,
            "worker_id": data["worker_id"],
        }

    def renew_task_lease(self, task_id: str, worker_id: str) -> bool:
        with self.lock:
            lease = self.active_leases.get(task_id)
            if lease is None or lease["worker_id"] != worker_id:
                return False
            lease["leased_at"] = time.monotonic()
            return True

    def lease_next_task_for_worker(self, worker_id: str) -> Optional[dict[str, Any]]:
        """Lease once per row and enforce service exclusion per outbound route."""
        with self.lock:
            if not self.is_running or self.is_paused:
                return None
            actor = self.supervisor.get_worker(worker_id)
            if actor is None or actor.status in ("stopped", "error") or actor.is_in_cooldown():
                return None
            self._ensure_task_queues_locked()
            now = time.monotonic()
            for task_id, data in list(self.active_leases.items()):
                if now - data["leased_at"] <= 120:
                    continue
                # Never recycle a service while its route has active network I/O.
                if self._is_service_index(data["index"]) and get_ip_service_lock(data["ip_group"]).locked():
                    data["leased_at"] = now
                    continue
                self.active_leases.pop(task_id)
                # Return expired uncompleted task to the front of its queue
                exp_idx = data["index"]
                if exp_idx not in self.completed_indices and exp_idx not in self.deferred_indices:
                    if self._is_service_index(exp_idx):
                        self._pending_services.appendleft(exp_idx)
                    else:
                        self._pending_accounts.appendleft(exp_idx)
                previous = self.supervisor.get_worker(data["worker_id"])
                if previous and previous.current_task and previous.current_task.get("task_id") == task_id:
                    previous.current_task = None
                    if previous.status == "processing":
                        previous.status = "ready"
            for task_id, data in self.active_leases.items():
                if data["worker_id"] == worker_id:
                    data["leased_at"] = now
                    return self._task_payload(task_id, data)

            leased = {data["index"] for data in self.active_leases.values()}
            busy = {data["ip_group"] for data in self.active_leases.values()
                    if self._is_service_index(data["index"])}

            task_scope = getattr(actor, "task_scope", "both")
            can_check_acc = getattr(actor, "can_check_accounts", task_scope in ("both", "account"))
            can_check_srv = getattr(actor, "can_check_services", task_scope in ("both", "service"))

            can_service = (
                can_check_srv
                and actor.ip_group not in busy
                and actor.ip_group not in getattr(self, 'blocked_service_routes', {})
                and not get_ip_service_lock(actor.ip_group).locked()
                and get_ip_service_retry_after(actor.ip_group) <= 0
            )

            def _peek_valid(deque_obj: deque[int]) -> Optional[int]:
                while deque_obj:
                    top = deque_obj[0]
                    if top in self.completed_indices or top in leased or top in self.deferred_indices:
                        deque_obj.popleft()
                    else:
                        return top
                return None

            next_srv = _peek_valid(self._pending_services)
            next_acc = _peek_valid(self._pending_accounts)
            has_regular = (next_srv is not None or next_acc is not None)

            candidate = None
            if getattr(actor, "use_proxy", False):
                # Workers with a proxy prioritize service numbers (starts with 2) to leverage proxy
                if can_service and next_srv is not None:
                    candidate = self._pending_services.popleft()
                elif can_check_acc and next_acc is not None:
                    candidate = self._pending_accounts.popleft()
            else:
                if can_check_acc and can_service and next_srv is not None and next_acc is not None:
                    if next_srv < next_acc:
                        candidate = self._pending_services.popleft()
                    else:
                        candidate = self._pending_accounts.popleft()
                elif can_check_acc and next_acc is not None and (not can_service or next_srv is None or next_acc < next_srv):
                    candidate = self._pending_accounts.popleft()
                elif can_service and next_srv is not None:
                    candidate = self._pending_services.popleft()

            if candidate is None and not has_regular:
                candidate = next((i for i in self.deferred_indices
                                  if i not in self.completed_indices and i not in leased
                                  and self.deferred_retry_at.get(i, 0.0) <= now
                                  and (
                                      (self._is_service_index(i) and can_service)
                                      or (not self._is_service_index(i) and can_check_acc)
                                  )), None)

            if candidate is None:
                return None

            is_srv = self._is_service_index(candidate)
            if not can_check_acc and not is_srv:
                logger.error(
                    "CRITICAL SAFETY INTERCEPT: Worker %s (scope=%s) attempted to lease account task %s! Blocked.",
                    worker_id, task_scope, candidate,
                )
                self._pending_accounts.appendleft(candidate)
                return None
            if not can_check_srv and is_srv:
                logger.error(
                    "CRITICAL SAFETY INTERCEPT: Worker %s (scope=%s) attempted to lease service task %s! Blocked.",
                    worker_id, task_scope, candidate,
                )
                self._pending_services.appendleft(candidate)
                return None

            task_id = f"task_{self.session_token}_{candidate}_{uuid.uuid4().hex}"
            data = {"index": candidate, "customer": self.customers[candidate],
                    "worker_id": worker_id, "leased_at": now, "ip_group": actor.ip_group,
                    "phase": 'retry' if not has_regular else 'regular'}
            self.active_leases[task_id] = data
            task = self._task_payload(task_id, data)
            actor.assign_task({"task_id": task_id, "row": task["row_number"],
                               "search_number": task["search_number"],
                               "expected_sar": task["expected_amount_sar"]})
            return task

    def _save_checkpoint_locked(self, last_index: int) -> None:
        if self.checkpoint_manager:
            now = time.monotonic()
            self.checkpoint_manager.save(
                completed_indices=set(self.completed_indices), mismatches=list(self.mismatches),
                last_index=last_index, workbook_name=self.workbook_name,
                extra={"errors_count": self.errors_count, "matches_count": self.matches_count,
                       "reviews_count": self.reviews_count, "not_found_count": self.not_found_count,
                       "mawarid_format": getattr(self, "mawarid_format", False),
                       "input_signature": getattr(self, 'input_signature', None),
                       "started_at": self.session_started_at,
                       "deferred_indices": list(self.deferred_indices),
                       "deferred_attempts": dict(self.deferred_attempts),
                       "retry_attempts_by_status": dict(self.retry_attempts_by_status),
                       "deferred_retry_delays": {i: max(0.0, at - now) for i, at in self.deferred_retry_at.items()}},
            )

    def defer_task_to_end(self, task_id: str, reason: str = "", *,
                          worker_id: Optional[str] = None, retry_after: float = SERVICE_INTERVAL,
                          category: str = "blocked", max_attempts: Optional[int] = None) -> None:
        """Retry at the end; exhausted rejections become review, never row errors."""
        with self.lock:
            lease = self.active_leases.get(task_id)
            if not lease or (worker_id is not None and lease["worker_id"] != worker_id):
                return
            idx = lease["index"]
            if idx in self.completed_indices:
                return
            attempts = self.retry_attempts_by_status.setdefault(idx, {})
            attempts[category] = attempts.get(category, 0) + 1
            self.deferred_attempts[idx] = self.deferred_attempts.get(idx, 0) + 1
            threshold = max_attempts if max_attempts is not None else 3
            if attempts[category] >= threshold:
                self.record_task_outcome(task_id, None, "needs_review", reason, lease["worker_id"])
                return
            self.active_leases.pop(task_id)
            if idx in self.deferred_indices:
                self.deferred_indices.remove(idx)
            self.deferred_indices.append(idx)
            self.deferred_retry_at[idx] = time.monotonic() + max(0.0, retry_after)
            actor = self.supervisor.get_worker(lease["worker_id"])
            if actor:
                actor.current_task = None
                if not actor.is_in_cooldown():
                    actor.status = "ready"
                actor.status_reason = reason
            self._save_checkpoint_locked(idx)
            EVENT_BUS.publish("task_deferred", {
                "row": lease["customer"].row_number, "number": self._effective_search_number(lease["customer"]),
                "reason": reason, "attempt": attempts[category], "category": category,
                "deferred_count": len(self.deferred_indices),
            })

    def record_task_outcome(
        self, task_id: str, live_amount_raw: Any, status: str,
        error_msg: str = "", worker_id: str = "worker_1",
    ) -> None:
        """Commit lease ownership, completion, metrics and the writer entry atomically."""
        with self.lock:
            lease = self.active_leases.get(task_id)
            if not lease or lease["worker_id"] != worker_id or lease["index"] in self.completed_indices:
                return
            before = (self.matches_count, self.errors_count, self.reviews_count, self.not_found_count, len(self.mismatches))
            cust = lease["customer"]
            idx = lease["index"]
            if status not in ("match", "mismatch", "error", "not_found", "needs_review"):
                status = "needs_review"
            live_halalas = parse_money_to_halalas(live_amount_raw) if status in ("match", "mismatch") else None
            if status in ("match", "mismatch") and (live_halalas is None or live_halalas < 0):
                status = "needs_review"
                error_msg = error_msg or "لم يتم الحصول على مبلغ صالح"
                live_halalas = None
            eff_expected = cust.expected_amount
            live_sar = live_halalas / 100.0 if live_halalas is not None else None
            diff_sar = None
            if status in ("match", "mismatch"):
                matched, eff_expected, diff_h = is_amount_match(
                    live_halalas=live_halalas, expected_primary=cust.expected_amount,
                    expected_secondary=cust.expected_amount_2,
                )
                status = "match" if matched else "mismatch"
                diff_sar = 0.0 if matched else diff_h / 100.0
                label = "مسدد بالكامل" if live_sar == 0.0 and eff_expected > 0 else ("تطابق تام" if matched else "فرق رصيد")
                if matched:
                    self.matches_count += 1
                else:
                    self.mismatches.append(ProgressMismatch(sequence_index=idx, expected_amount=eff_expected, website_amount=live_halalas))
            elif status == "error":
                label = "خطأ فحص"
                self.errors_count += 1
            elif status == "not_found":
                label = "غير موجود"
                self.not_found_count += 1
            else:
                label = "تحتاج مراجعة"
                self.reviews_count += 1
            number = self._effective_search_number(cust)
            result = CheckResult(
                row=cust.row_number, record_type="wallet" if number.startswith("2") and getattr(self, "mode", "") != "account_only" else "account",
                lookup_number=number, customer_name=cust.customer_name,
                expected_amount=eff_expected / 100.0, live_amount=live_sar,
                status=status, diff_sar=diff_sar, worker_id=worker_id,
                timestamp=time.strftime("%H:%M:%S"), details=error_msg,
            )
            row_record = {
                "row": cust.row_number,
                "name": cust.customer_name or "عميل غير محدد",
                "national_id": getattr(cust, "national_id", "") or "-",
                "contract": cust.contract or cust.lookup_number or "-",
                "account": cust.original_account_number or cust.lookup_number or "-",
                "service": cust.service_number or "-",
                "customer_phones": getattr(cust, "phones", "") or "-",
                "collector": cust.collector_name or "-",
                "supervisor": getattr(cust, "supervisor_name", "") or "-",
                "branch": getattr(cust, "branch_name", "") or "-",
                "expected_sar": eff_expected / 100.0,
                "live_sar": live_sar,
                "diff_sar": diff_sar,
                "status_label": label,
                "case_status": getattr(cust, "case_status", "") or "-",
                "main_status": cust.main_status or "-",
                "sub_status": cust.sub_status or "-",
                "follow_notes": cust.notes or "-",
                "follow_date": getattr(cust, "followup_date", "") or "-",
                "status": result.status,
                "error": error_msg,
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "mawarid_format": getattr(self, "mawarid_format", False),
                "input_signature": getattr(self, 'input_signature', None),
            }

            if getattr(self, 'wal_path', None):
                try:
                    append_journal(self.wal_path, row_record)
                except OSError:
                    self.matches_count, self.errors_count, self.reviews_count, self.not_found_count, old_mismatches = before
                    self.mismatches[:] = self.mismatches[:old_mismatches]
                    self.is_paused = True
                    self.progress_timing.pause()
                    self.verification_notice = 'تعذر حفظ النتيجة على القرص. أصلح مساحة التخزين ثم استأنف الفحص.'
                    logger.exception('Paused before committing a row: journal write failed')
                    return
            self.all_completed_records[idx] = row_record
            if self.result_queue is not None:
                self.result_queue.put(row_record)
            self.completed_indices.add(idx)
            tracker = getattr(self, 'progress_timing', None)
            if tracker is not None:
                tracker.complete('service' if self._effective_search_number(cust).startswith('2') and getattr(self, "mode", "") != "account_only" else 'account', phase=lease.get('phase', 'regular'))
            self.active_leases.pop(task_id)
            if task_id in self.verification_pause_tasks:
                self.verification_pause_tasks.discard(task_id)
            for route, block in list(getattr(self, 'blocked_service_routes', {}).items()):
                if block['task_id'] == task_id:
                    if status in ('match', 'mismatch', 'not_found'):
                        self.blocked_service_routes.pop(route)
                    else:
                        block['pending'] = False
            self._refresh_verification_notice_locked()
            if idx in self.deferred_indices:
                self.deferred_indices.remove(idx)
            self.deferred_retry_at.pop(idx, None)
            actor = self.supervisor.get_worker(worker_id)
            if actor:
                actor.record_outcome(result.status)
            self.recent_results.insert(0, result.to_dict())
            self.recent_results = self.recent_results[:150]
            now_t = time.time()
            is_batch_checkpoint = (
                len(self.completed_indices) % 100 == 0
                or (now_t - getattr(self, "_last_disk_checkpoint_time", 0)) >= 5.0
                or len(self.completed_indices) >= len(self.customers)
            )
            if is_batch_checkpoint:
                self._last_disk_checkpoint_time = now_t
                self._save_checkpoint_locked(idx)
                if self.current_job_id:
                    self.queue_service.update_job_progress(
                        job_id=self.current_job_id, completed=len(self.completed_indices),
                        remaining=max(0, len(self.customers) - len(self.completed_indices)),
                        matches=self.matches_count, mismatches=len(self.mismatches), errors=self.errors_count,
                    )
        EVENT_BUS.publish("task_completed", result.to_dict())

    def pause_session(self) -> None:
        with self.lock:
            self.is_paused = True
            self.verification_auto_resume = False
            if getattr(self, 'progress_timing', None):
                self.progress_timing.pause()
        EVENT_BUS.publish("session_paused", {})

    def handle_verification_required(self, task_id: str, message: str, worker_id: str) -> None:
        """Stop service leasing on the challenged route; other routes/contracts continue."""
        with self.lock:
            lease = self.active_leases.get(task_id)
            if not lease or lease["worker_id"] != worker_id:
                return
            self.verification_pause_tasks.discard(task_id)
            if not hasattr(self, 'blocked_service_routes'):
                self.blocked_service_routes = {}
            notice = message or "تطلب صفحة زين تحققًا بشريًا"
            self.blocked_service_routes[lease['ip_group']] = {'task_id':task_id, 'worker_id':worker_id, 'message':notice, 'pending':False}
            self._refresh_verification_notice_locked()
            self.defer_task_to_end(task_id, self.verification_notice, worker_id=worker_id,
                                   retry_after=0.0, category="verification_required")
        EVENT_BUS.publish("service_route_verification", {"worker_id":worker_id, "pending":False})

    def notify_pending_verification(self, task_id: str, message: str, worker_id: str) -> None:
        with self.lock:
            lease = self.active_leases.get(task_id)
            if not lease or lease["worker_id"] != worker_id:
                return
            self.verification_pause_tasks.add(task_id)
            if not hasattr(self, 'blocked_service_routes'):
                self.blocked_service_routes = {}
            self.blocked_service_routes[lease['ip_group']] = {'task_id':task_id, 'worker_id':worker_id, 'message':message, 'pending':True}
            self._refresh_verification_notice_locked()
        EVENT_BUS.publish("service_route_verification", {"worker_id":worker_id, "pending":True})

    def _refresh_verification_notice_locked(self) -> None:
        blocks = getattr(self, 'blocked_service_routes', {})
        self.verification_notice = ' · '.join(dict.fromkeys(block['message'] for block in blocks.values()))

    def resume_service_route(self, worker_id: str) -> bool:
        with self.lock:
            actor = self.supervisor.get_worker(worker_id)
            blocks = getattr(self, 'blocked_service_routes', {})
            block = blocks.get(actor.ip_group) if actor else None
            if not block or block['pending']:
                return False  # A live challenge must be answered by the operator.
            blocks.pop(actor.ip_group)
            self._refresh_verification_notice_locked()
            return True

    def resume_session(self) -> None:
        with self.lock:
            self.verification_pause_tasks.clear()
            self.is_paused = False
            if getattr(self, 'progress_timing', None):
                self.progress_timing.resume()
            self._refresh_verification_notice_locked()
        EVENT_BUS.publish("session_resumed", {})

    def cancel_session(self) -> None:
        with self.lock:
            self._save_checkpoint_locked(-1)
            self.is_running = False
            self.is_paused = False
            self.active_leases.clear()
            self.verification_pause_tasks.clear()
            self.verification_auto_resume = False
            getattr(self, 'blocked_service_routes', {}).clear()
            if self.current_job_id:
                job = self.queue_service.get_job(self.current_job_id)
                if job and job.status == 'active':
                    job.status = 'pending'
                    self.queue_service._save_to_disk()
        from workers.stealth_service_engine import cancel_pending_verifications
        cancel_pending_verifications()
        self.supervisor.stop_all()
        if self.writer_thread and self.result_queue:
            self.result_queue.put("STOP_SENTINEL")
            timeout_sec = max(30.0, float(len(self.customers or [])) * 0.005)
            self.writer_thread.join(timeout=timeout_sec)
        EVENT_BUS.publish("session_cancelled", {})

    @staticmethod
    def _is_retryable_transient_error(status: str, msg: Any) -> bool:
        if status in ("blocked", "session_expired", "network_error"):
            return True
        s_msg = str(msg or "")
        msg_lower = s_msg.lower()
        retry_codes = ("403", "429", "433", "500", "502", "503", "504", "408")
        if any(code in msg_lower for code in retry_codes):
            return True
        retry_keywords = (
            "رفض الطلب", "rejected", "حظر", "timeout", "timed out",
            "targetclosed", "المتصفح", "connection", "reset", "closed",
            "econnreset", "econnrefused", "temporary", "مؤقت", "fail"
        )
        if any(kw in msg_lower for kw in retry_keywords):
            return True
        return False

    def _run_worker_api_loop(self, worker_id: str, session_token: Optional[str] = None) -> None:
        """Execute leased tasks, keeping active leases alive and results fenced."""
        actor = self.supervisor.get_worker(worker_id)
        if not actor:
            return
        session_token = session_token or self.session_token
        while self.is_running and self.session_token == session_token:
            if self.is_paused or actor.is_in_cooldown():
                time.sleep(0.5)
                continue
            task = self.lease_next_task_for_worker(worker_id)
            if task is None:
                with self.lock:
                    finalize = (self.is_running and not self.is_finalizing and bool(self.customers)
                                and len(self.completed_indices) >= len(self.customers) and not self.active_leases)
                    if finalize:
                        self.is_finalizing = True
                if finalize:
                    self._finalize_job()
                    break
                time.sleep(0.4)
                continue
            task_id = task["task_id"]
            is_service = str(task["search_number"]).startswith("2") and task.get("record_type") != "account"
            stop_heartbeat = threading.Event() if is_service else None
            heartbeat_thread = None
            if is_service:
                def heartbeat():
                    while not stop_heartbeat.wait(10.0):
                        if not self.renew_task_lease(task_id, worker_id):
                            break
                heartbeat_thread = threading.Thread(target=heartbeat, daemon=True, name=f"LeaseHeartbeat-{worker_id}")
                heartbeat_thread.start()
            try:
                try:
                    actor.on_verification = lambda message: self.notify_pending_verification(task_id, message, worker_id)
                    status, amount, message = actor.execute_task_api(task["search_number"])
                except Exception as exc:
                    status, amount, message = "network_error", None, f"تعذر استكمال الطلب: {type(exc).__name__}"
            finally:
                if stop_heartbeat and heartbeat_thread:
                    stop_heartbeat.set()
                    heartbeat_thread.join(timeout=1.0)
            if self.session_token != session_token:
                break
            if status == "ok":
                self.record_task_outcome(task_id, amount, "match", worker_id=worker_id)
            elif status == "verification_required":
                self.handle_verification_required(task_id, message, worker_id)
            elif status == "not_found":
                self.record_task_outcome(task_id, None, "not_found", message, worker_id)
            elif status == 'network_error' and ('TargetClosed' in str(message) or 'المتصفح' in str(message)):
                actor.status_reason = 'استعادة جلسة المتصفح لهذا المسار'
                self.defer_task_to_end(task_id, message, worker_id=worker_id,
                    retry_after=SERVICE_INTERVAL, category='browser_recovery', max_attempts=getattr(self, "max_block_retries", 3))
            elif status in ("blocked", "session_expired") or self._is_retryable_transient_error(status, message):
                msg_lower = str(message).lower()
                is_blocked = "block" in msg_lower or status == "blocked" or "403" in msg_lower or "رفض الطلب" in str(message)
                effective_status = "blocked" if is_blocked else (status if status in ("blocked", "session_expired", "network_error") else "transient_error")
                if is_blocked:
                    set_ip_service_cooldown(actor.ip_group, 3.0)
                    if not actor.use_proxy:
                        actor.set_cooldown(3.0, "حظر مؤقت - جاري التبريد والانتظار...")
                    else:
                        actor.status_reason = "حظر مؤقت على المسار - جاري الانتظار والتبريد..."
                delay = max(SERVICE_INTERVAL, get_ip_service_retry_after(actor.ip_group), 3.0 if is_blocked else 1.0)
                self.defer_task_to_end(
                    task_id, message, worker_id=worker_id, retry_after=delay,
                    category=effective_status, max_attempts=getattr(self, "max_block_retries", 3)
                )
            elif status in ("unknown_response", "error"):
                self.record_task_outcome(task_id, None, "needs_review", message, worker_id)
            else:
                with self.lock:
                    lease = self.active_leases.get(task_id)
                    if not lease or lease["worker_id"] != worker_id:
                        continue
                    lease["retries"] = lease.get("retries", 0) + 1
                    attempts = lease["retries"]
                if attempts <= 2:
                    if not actor.use_proxy and not task["search_number"].startswith("2"):
                        actor.set_cooldown(2.0, message)
                    time.sleep(0.5)
                    continue
                self.record_task_outcome(task_id, None, "needs_review", message, worker_id)
            if is_service:
                time.sleep(0.35)
            else:
                time.sleep(0.05)

    def _collect_and_validate_records(self, allow_partial: bool = False) -> Tuple[bool, List[Dict[str, Any]], str]:
        """Unifies in-memory records and WAL persistence into a single de-duplicated dataset.
        Prevents duplicates by row identifier, handles trailing WAL truncation,
        strictly rejects mid-file corruption, and ensures completeness against expected customer count and row IDs.
        """
        records_by_row: Dict[str, Dict[str, Any]] = {}

        # 1. Merge from WAL candidates first
        wb_stem = getattr(self, "workbook_stem", "") or (self.source_workbook_path.stem if getattr(self, "source_workbook_path", None) else "")
        wal_corrupted = False
        wal_error_msg = ""

        if wb_stem:
            wal_candidates = [self.wal_path] if getattr(self, 'wal_path', None) else [
                self.project_root / f".wal_repair_{wb_stem}.jsonl",
                self.project_root / f".wal_{wb_stem}.jsonl"]
            for wal_cand in wal_candidates:
                if wal_cand and wal_cand.exists():
                    try:
                        import json
                        with open(wal_cand, "r", encoding="utf-8") as f:
                            raw_lines = [l for l in f.readlines() if l.strip()]

                        num_lines = len(raw_lines)
                        for idx, line in enumerate(raw_lines):
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                r_data = json.loads(line)
                                if isinstance(r_data, dict):
                                    r_key = str(r_data.get("row") or "")
                                    if r_key:
                                        records_by_row[r_key] = r_data
                            except json.JSONDecodeError as jde:
                                is_last_line = (idx == num_lines - 1)
                                if is_last_line:
                                    logger.warning(f"Skipping truncated trailing WAL line {idx + 1} at EOF in {wal_cand}: {jde}")
                                else:
                                    wal_corrupted = True
                                    wal_error_msg = f"فساد وتلف في بنية السطر {idx + 1} في منتصف ملف WAL ({wal_cand})"
                                    logger.error(wal_error_msg)
                                    break
                    except Exception as wal_err:
                        logger.error(f"Error reading WAL file {wal_cand}: {wal_err}")
                        wal_corrupted = True
                        wal_error_msg = f"تعذر قراءة ملف WAL: {wal_err}"

        if wal_corrupted:
            return False, [], f"فشل التحقق: {wal_error_msg}"

        # 2. Overlay RAM records (current active outcomes take priority over older WAL writes)
        for k, rec in self.all_completed_records.items():
            if isinstance(rec, dict):
                r_key = str(rec.get("row") or k)
                records_by_row[r_key] = rec

        merged_list = list(records_by_row.values())

        # 3. Expected Customers & Row Identity Verification
        expected_rows: set[str] = set()
        if getattr(self, "customers", None):
            for c in self.customers:
                r_val = getattr(c, "row_number", None) if hasattr(c, "row_number") else (c.get("row") if isinstance(c, dict) else None)
                if r_val is not None:
                    expected_rows.add(str(r_val))

        expected_total = len(expected_rows) if expected_rows else (len(self.customers) if getattr(self, "customers", None) else 0)

        if not allow_partial:
            if expected_total == 0:
                return False, [], "فشل التحقق: عدد السجلات المتوقعة مجهول أو غير محدد في الجلسة"

            collected_row_keys = set(records_by_row.keys())
            missing_rows = expected_rows - collected_row_keys
            if missing_rows:
                sorted_missing = sorted(list(missing_rows), key=lambda x: int(x) if x.isdigit() else x)
                sample_missing = sorted_missing[:10]
                return False, merged_list, f"نتائج منقوصة: تم جمع {len(merged_list)} سجل من أصل {expected_total} متوقع. الصفوف المفقودة: {sample_missing}"

            if collected_row_keys != expected_rows:
                return False, merged_list, "فشل التحقق: عدم تطابق بين هوية الصفوف المحفوظة والصفوف المتوقعة للمهمة"

        if not merged_list:
            return False, [], "لا توجد سجلات مكتملة صالحة"

        return True, merged_list, f"تم بنجاح جمع وتدقيق {len(merged_list)} سجل"

    def _finalize_job(self) -> None:
        with self.lock:
            self.is_running = False
            self.is_paused = False
        self.supervisor.stop_all()

        # Stop and flush async writer thread
        writer_ok = True
        writer_error_msg = ""
        if self.writer_thread and self.result_queue:
            self.result_queue.put("STOP_SENTINEL")
            timeout_sec = max(60.0, float(len(self.customers or [])) * 0.005)
            try:
                self.writer_thread.join(timeout=timeout_sec)
            except Exception as e:
                logger.warning(f"Writer thread join error: {e}")

            if self.writer_thread.is_alive():
                logger.error(f"Writer thread did not terminate within {timeout_sec:.1f}s timeout; still running in background.")
                writer_ok = False
                writer_error_msg = "انتهت مهلة انتظار كاتب النتائج دون اكتماله وما زال الخيط يعمل بالخلفية"
            elif getattr(self.writer_thread, "fatal_error", None):
                writer_ok = False
                writer_error_msg = f"تعطل كاتب النتائج بخطأ فادح: {self.writer_thread.fatal_error}"
                logger.error(writer_error_msg)
            elif not getattr(self.writer_thread, "completed_successfully", False):
                err = getattr(self.writer_thread, "save_error", "فشل الحفظ النهائي للكاتب")
                logger.error(f"Writer thread did not complete successfully: {err}")
                writer_ok = False
                writer_error_msg = f"فشل كاتب النتائج في الحفظ على القرص: {err}"

        # If writer failed or timed out, do NOT proceed with report generation or source update!
        if not writer_ok:
            if self.current_job_id:
                self.queue_service.mark_job_failed(self.current_job_id, writer_error_msg)
            with self.lock:
                self.is_finalizing = False
                self.reserved_next_job_id = None
            EVENT_BUS.publish("session_failed", {
                "workbook": self.workbook_name,
                "message": f"فشل الفحص: {writer_error_msg}. نتائج الصفوف محفوظة ويمكن إعادة تصديرها دون إعادة الفحص.",
                "writer_ok": False,
                "result_ready": False,
                "source_update_ok": False,
                "result_file": None,
                "updated_source_file": None,
            })
            return

        # Collect and validate all completed records from memory and WAL
        rec_ok, completed_records_list, rec_msg = self._collect_and_validate_records(allow_partial=False)
        if not rec_ok:
            logger.error(f"Record validation failed: {rec_msg}")
            if self.current_job_id:
                self.queue_service.mark_job_failed(self.current_job_id, f"فشل التحقق من السجلات: {rec_msg}")
            with self.lock:
                self.is_finalizing = False
                self.reserved_next_job_id = None
            EVENT_BUS.publish("session_failed", {
                "workbook": self.workbook_name,
                "message": f"فشل التحقق من اكتمال السجلات ({rec_msg}). تم إيقاف الحفظ النهائي ولم يتم تعديل ملف المصدر.",
                "writer_ok": writer_ok,
                "result_ready": False,
                "source_update_ok": False,
                "result_file": None,
                "updated_source_file": None,
            })
            return

        # Build Executive Multi-Tab Deliverable
        actual_result_path = None
        report_ok = False
        report_error_msg = ""
        try:
            from zain_checker.executive_reporter import export_executive_workbook
            if completed_records_list:
                actual_result_path = export_executive_workbook(
                    records=completed_records_list,
                    output_path=self.result_path,
                    is_repair=getattr(self, "is_repair_session", False),
                    mawarid_format=getattr(self, "mawarid_format", False) or any(bool(r.get("mawarid_format")) for r in completed_records_list if isinstance(r, dict)),
                )
                if actual_result_path and Path(actual_result_path).exists():
                    self.result_path = Path(actual_result_path)
                    self.result_ready = True
                    report_ok = True
                    logger.info(f"Executive workbook generated at {self.result_path}")
                    try:
                        import shutil
                        default_path = self.project_root / "نتائج فحص زين.xlsx"
                        if default_path.resolve() != self.result_path.resolve():
                            shutil.copy2(self.result_path, default_path)
                    except Exception as copy_err:
                        logger.debug(f"Default result copy note: {copy_err}")
                else:
                    self.result_ready = False
                    report_ok = False
                    report_error_msg = "لم يتم إنشاء ملف النتائج التنفيذي"
            else:
                self.result_ready = False
                report_ok = False
                report_error_msg = f"لا توجد سجلات مكتملة صالحة لإنشاء التقرير ({rec_msg})"
        except Exception as e:
            logger.error(f"Failed to generate executive deliverable: {e}")
            self.result_ready = False
            report_ok = False
            report_error_msg = f"تعذر إنشاء ملف النتائج: {e}"

        # Update source workbook in-place and create comprehensive updated file
        source_update_required = bool(getattr(self, "source_workbook_path", None) or self.workbook_name)
        updated_source_file = None
        source_update_ok = True
        source_update_msg = ""
        src_wb = getattr(self, "source_workbook_path", None)
        if not src_wb and self.workbook_name:
            src_wb = self.project_root / self.workbook_name

        if source_update_required:
            if not src_wb or not src_wb.exists():
                source_update_ok = False
                source_update_msg = f"ملف المصدر المطلوب تحديثه غير موجود على القرص: {src_wb}"
                logger.error(source_update_msg)
            elif not completed_records_list:
                source_update_ok = False
                source_update_msg = "لا توجد سجلات مكتملة لتحديث ملف المصدر بها"
                logger.error(source_update_msg)
            else:
                try:
                    from zain_checker.sheet_updater import update_source_workbook_after_job
                    ok, updated_path, msg = update_source_workbook_after_job(
                        workbook_path=src_wb,
                        records=completed_records_list,
                        sheet_index=getattr(self, "source_sheet_index", 0),
                        is_repair_session=getattr(self, "is_repair_session", False),
                        has_headers=getattr(self, "source_has_headers", True),
                    )
                    source_update_ok = ok
                    source_update_msg = msg
                    updated_source_file = str(updated_path) if updated_path else None
                    if not ok:
                        logger.error(f"Source file update failed: {msg}")
                    else:
                        logger.info(f"Source file updated: ok={ok}, path={updated_path}, msg={msg}")
                except Exception as e:
                    logger.error(f"Failed to update source file after job: {e}")
                    source_update_ok = False
                    source_update_msg = str(e)

        overall_success = rec_ok and report_ok and writer_ok and source_update_ok
        if not overall_success:
            failure_reasons = []
            if not writer_ok:
                failure_reasons.append(writer_error_msg or "فشل كاتب النتائج أو استمر بعد المهلة")
            if not rec_ok:
                failure_reasons.append(rec_msg or "نتائج الفحص غير مكتملة")
            if not report_ok:
                failure_reasons.append(report_error_msg or "تعذر حفظ ملف تقرير النتائج")
            if not source_update_ok:
                failure_reasons.append(f"تعذر تحديث ملف المصدر: {source_update_msg}")

            combined_reason = "؛ ".join(failure_reasons)
            logger.error(f"Job {self.current_job_id} finalization incomplete: {combined_reason}")

            if self.current_job_id:
                self.queue_service.mark_job_failed(self.current_job_id, combined_reason)

            with self.lock:
                self.is_finalizing = False
                self.reserved_next_job_id = None

            EVENT_BUS.publish("session_failed", {
                "workbook": self.workbook_name,
                "message": f"تعذر إكمال وحفظ كافة المخرجات المطلوبة ({combined_reason}). نتائج الصفوف محفوظة ويمكن إعادة تصديرها دون إعادة فحصها.",
                "writer_ok": writer_ok,
                "result_ready": self.result_ready,
                "source_update_ok": source_update_ok,
                "result_file": str(self.result_path) if self.result_ready else None,
                "updated_source_file": updated_source_file,
            })
            return

        finished_job_id = self.current_job_id
        if finished_job_id:
            self.queue_service.mark_job_completed(finished_job_id, result_file=self.result_path.name)
        round_jobs = self.queue_service.get_jobs()
        queued_remaining = [job for job in round_jobs if job.get("status") == "pending" or
                            (job.get("status") not in {"completed","archived"} and job.get("completed",0) < job.get("total_records",0))]
        round_files = [str(self.project_root / job["result_file"]) for job in round_jobs
                       if job.get("id") in getattr(self,"queue_round_job_ids",[]) and
                       job.get("status") == "completed" and job.get("result_file")]

        EVENT_BUS.publish("session_completed", {
            "workbook": self.workbook_name,
            "total": len(self.customers),
            "matches": self.matches_count,
            "mismatches": len(self.mismatches),
            "errors": self.errors_count,
            "needs_review": self.reviews_count,
            "not_found": self.not_found_count,
            "result_file": str(self.result_path),
            "updated_source_file": updated_source_file,
            "round_pending": len(queued_remaining),
            "round_result_files": round_files,
            "round_deliveries":[{"file":str(self.project_root / job["result_file"]),
                                 "chat_id":job.get("column_mapping",{}).get("telegram_chat_id")}
                                for job in round_jobs if job.get("id") in getattr(self,"queue_round_job_ids",[])
                                and job.get("status")=="completed" and job.get("result_file")],
        })

        # Auto-advance to next pending queue job if present
        next_job = self.queue_service.claim_next_pending_job(exclude_job_id=finished_job_id)
        if next_job:
            self.reserved_next_job_id = next_job.id
            if next_job.id not in self.queue_round_job_ids:
                self.queue_round_job_ids.append(next_job.id)
            logger.info(f"Advancing automatically to queued job: {next_job.filename} [{next_job.sheet_name}] (ID: {next_job.id})")

            def _start_next_job():
                wb_path = self.project_root / next_job.filename
                should_force = False  # A stale queue count must never erase a valid checkpoint.
                try:
                    started = self.start_session_from_config(
                        workbook_path=wb_path,
                        sheet_index=next_job.sheet_index,
                        mapping=next_job.column_mapping,
                        mode=next_job.mode,
                        amount_target=next_job.amount_target,
                        target_url=getattr(next_job, "target_url", "https://business.zain.sa/dashboard/quick-pay"),
                        job_id=next_job.id,
                        result_file=getattr(next_job, "result_file", "نتائج فحص زين.xlsx"),
                        force_restart=should_force,
                    )
                except Exception:
                    logger.exception('Failed to advance to next sheet')
                    started = False
                if not started:
                    with self.lock:
                        self.is_finalizing = False
                        self.reserved_next_job_id = None
                    self.queue_service.mark_job_failed(next_job.id, getattr(self,'start_error',''))

            threading.Thread(target=_start_next_job, daemon=True, name=f"AutoAdvance-{next_job.id}").start()
        else:
            self.is_finalizing = False
            self.reserved_next_job_id = None
            logger.info("All queued audit jobs completed successfully. Queue is empty.")

    def reexport_saved_results(self, output_path: Optional[Path | str] = None, job_id: Optional[str] = None) -> Tuple[bool, Optional[Path], str]:
        """Re-exports the executive deliverable workbook directly from in-memory records and WAL
        without re-running web queries. Updates queue job path and publishes delivery events.
        Does not mark the job completed unless all required outputs are verified.
        """
        with self.lock:
            busy, reason = self._is_session_busy_locked()
            if busy:
                return False, None, f"تعذر إعادة التصدير: {reason}"

            if job_id and self.current_job_id and job_id != self.current_job_id:
                return False, None, f"طلب مرفوض: معرّف المهمة المطلوب ({job_id}) لا يطابق المهمة المحملة حالياً ({self.current_job_id})"

            target_job_id = self.current_job_id or job_id
            self.is_exporting = True

        try:
            ok, records, msg = self._collect_and_validate_records(allow_partial=True)
            if not ok or not records:
                return False, None, f"فشل جمع السجلات المحفوظة: {msg}"

            target_path = Path(output_path) if output_path else self.result_path
            try:
                from zain_checker.executive_reporter import export_executive_workbook
                actual_path = export_executive_workbook(
                    records=records,
                    output_path=target_path,
                    is_repair=getattr(self, "is_repair_session", False),
                    mawarid_format=getattr(self, "mawarid_format", False) or any(bool(r.get("mawarid_format")) for r in records if isinstance(r, dict)),
                )
                self.result_path = Path(actual_path)
                self.result_ready = True

                # Update queue job result file pointer without altering job completion status
                if target_job_id:
                    self.queue_service.update_job_result_file(target_job_id, self.result_path.name)

                EVENT_BUS.publish("session_reexported", {
                    "job_id": target_job_id,
                    "result_file": str(self.result_path),
                    "total_records": len(records),
                })
                return True, self.result_path, f"تمت إعادة تصدير النتائج بنجاح إلى {self.result_path}"
            except Exception as e:
                logger.error(f"Failed to re-export executive deliverable: {e}")
                return False, None, f"فشل إعادة تصدير النتائج: {e}"
        finally:
            with self.lock:
                self.is_exporting = False

    def start_queue(self, job_id: Optional[str] = None) -> bool:
        """Starts processing the first pending job in the sequential queue."""
        with self.lock:
            busy, reason = self._is_session_busy_locked(allowed_job_id=job_id)
            if busy:
                logger.warning(f"Cannot start queue: {reason}")
                return False
            self.queue_round_job_ids = [job["id"] for job in self.queue_service.get_jobs() if job.get("status") == "pending"]

            next_job = self.queue_service.claim_next_pending_job(job_id=job_id) if job_id else self.queue_service.claim_next_pending_job()
            if not next_job:
                logger.warning("No pending jobs found in queue.")
                return False

            wb_path = self.project_root / next_job.filename
            should_force = False
            try:
                started = self.start_session_from_config(
                    workbook_path=wb_path,
                    sheet_index=next_job.sheet_index,
                    mapping=next_job.column_mapping,
                    mode=next_job.mode,
                    amount_target=next_job.amount_target,
                    target_url=getattr(next_job, "target_url", "https://business.zain.sa/dashboard/quick-pay"),
                    job_id=next_job.id,
                    result_file=getattr(next_job, "result_file", "نتائج فحص زين.xlsx"),
                    force_restart=should_force,
                )
            except Exception:
                self.queue_service.mark_job_failed(next_job.id, "تعذر بدء فحص الشيت")
                raise
            if not started:
                self.queue_service.mark_job_failed(next_job.id, "لم تبدأ جلسة الفحص")
            return started

    def get_live_status(self) -> dict[str, Any]:
        """Provides real-time telemetry for the Web UI without locking any inputs."""
        with self.lock:
            total = len(self.customers)
            done = len(self.completed_indices)
            remaining = max(0, total - done)
            matches = self.matches_count
            mismatches = len(self.mismatches)
            errors = self.errors_count
            reviews = self.reviews_count
            not_found = self.not_found_count

            # Calculate positive mismatch halalas (expected > website, customer payments)
            positive_mismatch_halalas = sum(
                (m.expected_amount - m.website_amount)
                for m in self.mismatches
                if (m.expected_amount - m.website_amount) > 0
            )

            table_snapshot = list(self.recent_results)
            active_leases_count = len(self.active_leases)
            deferred_count = len(self.deferred_indices)
            retry_delays = [max(0.0, self.deferred_retry_at.get(i, 0.0) - time.monotonic())
                            for i in self.deferred_indices]
            verification_notice = self.verification_notice
            running = self.is_running or self.is_finalizing
            finalizing = self.is_finalizing
            session_timing = {
                "session_id": self.session_token,
                "started_at": getattr(self, "session_started_at", None),
                "initial_completed": getattr(self, "session_initial_completed", 0),
                "server_time": time.time(),
            }
            tracker = getattr(self, 'progress_timing', None)
            if tracker is not None:
                session_timing.update(tracker.snapshot())
            busy_service_groups = {lease["ip_group"] for lease in self.active_leases.values()
                                   if self._effective_search_number(lease["customer"]).startswith("2") and getattr(self, "mode", "") != "account_only"}
            route_blocks = dict(getattr(self, 'blocked_service_routes', {}))

        workers_info = self.supervisor.get_status_summary()
        for worker in workers_info:
            block = route_blocks.get(worker.get('ip_group'))
            worker['service_verification_required'] = bool(block)
            worker['service_verification_pending'] = bool(block and block['pending'])
            worker['service_retry_seconds'] = round(get_ip_service_retry_after(worker.get('ip_group', '')), 1)
            worker["waiting_for_shared_service_browser"] = bool(
                self.is_running and not self.is_paused and worker["status"] == "ready"
                and worker.get("ip_group") in busy_service_groups)
        from workers.stealth_service_engine import pending_verifications

        return {
            "running": running,
            "finalizing": finalizing,
            "result_ready": self.result_ready,
            "round_pending": any(job.get("status") in {"pending","active"} for job in self.queue_service.get_jobs()),
            "paused": self.is_paused,
            "workbook": self.workbook_name,
            "session_timing": session_timing,
            "verification_notice": verification_notice,
            "telegram_enabled": self.telegram_enabled,
            "verifications": pending_verifications(),
            "kpis": {
                "total": total,
                "completed": done,
                "remaining": remaining,
                "matches": matches,
                "mismatches": mismatches,
                "errors": errors,
                "needs_review": reviews,
                "not_found": not_found,
                "mismatch_total": positive_mismatch_halalas / 100.0,
                "active_leases": active_leases_count,
                "verified": matches + mismatches,
                "deferred": deferred_count,
                "next_retry_seconds": round(min(retry_delays), 1) if retry_delays else 0.0,
            },
            "workers": workers_info,
            "table_rows": table_snapshot,
        }

    def restart_queue_job(self, job_id: str, auto_start: bool = True) -> bool:
        """Fork a new run, preserving the old checkpoint, journal, and results."""
        with self.lock:
            busy, reason = self._is_session_busy_locked()
            if busy:
                return False
            job = self.queue_service.fork_job(job_id)
            if not job:
                return False
            if auto_start:
                self.queue_service.mark_job_active(job.id)
                return self.start_session_from_config(workbook_path=self.project_root/job.filename,
                    sheet_index=job.sheet_index, mapping=job.column_mapping, mode=job.mode,
                    amount_target=job.amount_target, target_url=job.target_url,
                    job_id=job.id, result_file=job.result_file)
        return True

    def restart_all_queue_jobs(self, auto_start: bool = True) -> bool:
        """Create new runs for the current queue, preserving previous files."""
        with self.lock:
            busy, reason = self._is_session_busy_locked()
            if busy:
                logger.warning(f"Cannot restart all jobs: {reason}")
                return False

        jobs = self.queue_service.get_jobs()
        for j_dict in jobs:
            if j_dict['status'] != 'archived':
                self.restart_queue_job(j_dict["id"], auto_start=False)

        if auto_start:
            return self.start_queue()
        return True

    def repair_errors(
        self,
        row: Optional[int] = None,
        workbook_path: Optional[Path] = None,
        sheet_index: int = 0,
    ) -> dict[str, Any]:
        """Repairs/retries rows with errors or needs_review in the current sheet.

        Removes the target error rows from completed_indices, resets their attempt counters,
        and re-enqueues them for immediate worker leasing and checking without restarting matching rows.
        """
        with self.lock:
            busy, reason = self._is_session_busy_locked()
            if busy:
                return {"status": "error", "message": f"لا يمكن بدء صيانة الأخطاء: {reason}"}
            if not self.customers:
                if workbook_path and Path(workbook_path).exists():
                    return self.start_repair_session_from_sheet(
                        workbook_path=Path(workbook_path),
                        sheet_index=sheet_index,
                    )
                return {"status": "error", "message": "لا توجد سجلات محملة في الجلسة الحالية لإجراء الصيانة."}

            # Populate all_completed_records from WAL if empty
            if not self.all_completed_records and self.workbook_name:
                wb_stem = Path(self.workbook_name).stem
                wal_path = self.project_root / f".wal_{wb_stem}.jsonl"
                if wal_path.exists():
                    try:
                        import json
                        with open(wal_path, "r", encoding="utf-8") as f:
                            for line in f:
                                if line.strip():
                                    data = json.loads(line.strip())
                                    r_num = data.get("row")
                                    for i, c in enumerate(self.customers):
                                        if c.row_number == r_num:
                                            self.all_completed_records[i] = data
                                            break
                    except Exception as e:
                        logger.warning(f"Error loading WAL for repair: {e}")

            # Find candidate error records
            target_indices = []
            if row is not None:
                # Specific row requested
                for idx, cust in enumerate(self.customers):
                    if cust.row_number == row:
                        target_indices.append(idx)
                        break
            else:
                # All rows with error or needs_review
                for idx, rec in self.all_completed_records.items():
                    if rec.get("status") in ("error", "needs_review"):
                        target_indices.append(idx)
                for res in self.recent_results:
                    if res.get("status") in ("error", "needs_review"):
                        r_num = res.get("row")
                        for idx, cust in enumerate(self.customers):
                            if cust.row_number == r_num and idx not in target_indices:
                                target_indices.append(idx)

            if not target_indices:
                return {
                    "status": "info",
                    "repaired_count": 0,
                    "message": "لا توجد أخطاء في الشيت الحالي تحتاج إلى صيانة."
                }

            # Reset each target index so it can be re-leased and re-checked
            for idx in target_indices:
                self.completed_indices.discard(idx)
                rec = self.all_completed_records.get(idx)
                if rec:
                    if rec.get("status") == "error":
                        self.errors_count = max(0, self.errors_count - 1)
                    elif rec.get("status") == "needs_review":
                        self.reviews_count = max(0, self.reviews_count - 1)

                # Reset attempt counters
                self.deferred_attempts.pop(idx, None)
                self.retry_attempts_by_status.pop(idx, None)
                self.deferred_retry_at.pop(idx, None)
                if idx in self.deferred_indices:
                    self.deferred_indices.remove(idx)

                # Remove from active leases if lingering
                for tid, ldata in list(self.active_leases.items()):
                    if ldata.get("index") == idx:
                        self.active_leases.pop(tid, None)

                # Re-queue into in-memory queue
                if self._is_service_index(idx):
                    self._pending_services.append(idx)
                else:
                    self._pending_accounts.append(idx)

            # Update queue job progress if in a queue
            if self.current_job_id:
                self.queue_service.update_job_progress(
                    job_id=self.current_job_id,
                    completed=len(self.completed_indices),
                    remaining=max(0, len(self.customers) - len(self.completed_indices)),
                    matches=self.matches_count,
                    mismatches=len(self.mismatches),
                    errors=self.errors_count,
                )

            # If session is NOT running, restart worker threads to process these repaired rows
            self._reset_progress_timing_locked()
            need_start_workers = not self.is_running
            if need_start_workers:
                self.result_ready = False
                self.is_running = True
                self.is_paused = False
                self.is_finalizing = False
                self.session_token = uuid.uuid4().hex
                self.session_started_at = time.time()
                self.session_initial_completed = len(self.completed_indices)

                # Ensure async writer is ready
                if not self.writer_thread or not self.writer_thread.is_alive():
                    import queue as py_queue
                    from zain_checker.async_pipeline_engine import AsyncExcelWriterThread
                    self.result_queue = py_queue.Queue()
                    wb_stem = Path(self.workbook_name).stem if self.workbook_name else "audit"
                    wal_path = getattr(self, 'wal_path', None) or self.project_root / f".wal_{wb_stem}.jsonl"
                    self.wal_path = wal_path
                    stream_path = self.project_root / f".stream_{wb_stem}.xlsx"
                    self.writer_thread = AsyncExcelWriterThread(
                        result_queue=self.result_queue,
                        result_path=stream_path,
                        wal_path=wal_path,
                        batch_size=200,
                        flush_interval_seconds=15.0,
                        wal_already_persisted=True,
                    )
                    self.writer_thread.start()

                # Start supervisor and background worker threads
                self.supervisor.start_all(target_url=getattr(self, "target_url", "https://business.zain.sa/dashboard/quick-pay"))
                for actor in self.supervisor.get_all_workers():
                    if getattr(actor, "use_direct_api", False) and actor.status == "ready":
                        t = threading.Thread(
                            target=self._run_worker_api_loop,
                            args=(actor.worker_id, self.session_token),
                            daemon=True,
                            name=f"WorkerAPI-{actor.worker_id}",
                        )
                        t.start()
            else:
                # If paused, resume
                if self.is_paused:
                    self.resume_session()

            EVENT_BUS.publish("errors_repair_started", {
                "count": len(target_indices),
                "workbook": self.workbook_name,
            })

            return {
                "status": "ok",
                "repaired_count": len(target_indices),
                "message": f"تم بنجاح بدء صيانة {len(target_indices)} سجل من الأخطاء والمهلات!"
            }

    def start_repair_session_from_sheet(
        self,
        workbook_path: Path,
        sheet_index: int = 0,
        target_url: str = "https://business.zain.sa/dashboard/quick-pay",
    ) -> dict[str, Any]:
        """Loads error or results rows from a previously exported workbook and starts worker audit loop."""
        with self.lock:
            busy, reason = self._is_session_busy_locked()
            if busy:
                return {"status": "error", "message": f"لا يمكن بدء فحص صيانة جديد: {reason}"}

            from domain.workbook import load_fast_workbook, inspect_sheet_schema, extract_customer_records

            wb = load_fast_workbook(workbook_path)
            try:
                target_ws = None
                chosen_idx = sheet_index
                if 0 <= chosen_idx < len(wb.worksheets):
                    target_ws = wb.worksheets[chosen_idx]

                # If selected sheet doesn't look like an errors tab, look for one
                if target_ws and "أخطاء" not in target_ws.title and "ملاحظات" not in target_ws.title:
                    for i, s in enumerate(wb.worksheets):
                        if "أخطاء" in s.title or "ملاحظات" in s.title:
                            target_ws = s
                            chosen_idx = i
                            break
                if not target_ws:
                    target_ws = wb.worksheets[0]
                    chosen_idx = 0

                schema = inspect_sheet_schema(target_ws)
                indices = schema["indices"]

                records = extract_customer_records(
                    sheet=target_ws,
                    lookup_col=indices.get("lookup_col") or 3,
                    amount_col=indices.get("amount_col") or 4,
                    amount_col_2=indices.get("amount_col_2"),
                    service_col=indices.get("service_col"),
                    customer_col=indices.get("customer_col"),
                    notes_col=indices.get("notes_col"),
                    source_row_col=indices.get("source_row_col"),
                    main_status_col=indices.get("main_status_col"),
                    sub_status_col=indices.get("sub_status_col"),
                    record_type="mixed",
                )
            finally:
                wb.close()

            if not records:
                return {"status": "error", "message": "لم يتم العثور على سجلات صالحة في الشيت المحدد لإجراء الصيانة."}

            self.customers = records
            self.workbook_name = workbook_path.name
            self.workbook_stem = workbook_path.stem
            self.source_workbook_path = workbook_path
            self.source_sheet_index = chosen_idx
            self.source_has_headers = True
            self.is_repair_session = True
            self.current_job_id = None
            self.result_path = self.project_root / f"نتائج_صيانة_{workbook_path.stem}.xlsx"
            self.session_token = uuid.uuid4().hex

            self.completed_indices = set()
            self.mismatches = []
            self.matches_count = 0
            self.errors_count = len(records)
            self.reviews_count = 0
            self.not_found_count = 0
            self.deferred_indices.clear()
            self.deferred_attempts.clear()
            self.deferred_retry_at.clear()
            self.retry_attempts_by_status.clear()
            self.active_leases.clear()
            self.recent_results.clear()
            self.all_completed_records.clear()
            self.result_ready = False
            self._init_task_queues_locked()

            # Pre-populate recent_results with these records marked as needs_repair
            for c in records:
                self.recent_results.append({
                    "row": c.row_number,
                    "type": c.record_type,
                    "number": c.lookup_number,
                    "name": c.customer_name,
                    "expected_sar": c.expected_amount / 100.0,
                    "live_sar": None,
                    "diff_sar": None,
                    "status": "error",
                    "status_label": "قيد الصيانة والتدقيق",
                    "issue": c.error_or_review_details or "بانتظار فحص العامل",
                    "main_status": c.main_status,
                    "sub_status": c.sub_status,
                })

            self.target_url = target_url
            self.is_running = True
            self.is_paused = False
            self.is_finalizing = False
            self.session_started_at = time.time()
            self.session_initial_completed = len(self.completed_indices)

            # Start background async writer
            self._reset_progress_timing_locked()
            import queue as py_queue
            from zain_checker.async_pipeline_engine import AsyncExcelWriterThread
            self.result_queue = py_queue.Queue()
            wal_path = self.project_root / f".wal_repair_{workbook_path.stem}.jsonl"
            self.wal_path = wal_path
            self.input_signature = None
            stream_path = self.project_root / f".stream_repair_{workbook_path.stem}.xlsx"
            self.writer_thread = AsyncExcelWriterThread(
                result_queue=self.result_queue,
                result_path=stream_path,
                wal_path=wal_path,
                batch_size=20,
                flush_interval_seconds=1.5,
                wal_already_persisted=True,
            )
            self.writer_thread.start()

            # Start isolated workers
            self.supervisor.start_all(target_url=target_url)

            # Start Direct API worker threads
            for actor in self.supervisor.get_all_workers():
                if getattr(actor, "use_direct_api", False) and actor.status == "ready":
                    w_thread = threading.Thread(
                        target=self._run_worker_api_loop,
                        args=(actor.worker_id, self.session_token),
                        name=f"DirectApiWorkerRepair-{actor.worker_id}",
                        daemon=True,
                    )
                    w_thread.start()

            EVENT_BUS.publish("session_started", {
                "workbook": self.workbook_name,
                "target_url": self.target_url,
                "total_records": len(self.customers),
                "is_repair_session": True,
            })

            return {
                "status": "ok",
                "message": f"تم بدء صيانة {len(records)} صف بنجاح.",
                "total_records": len(records),
            }

