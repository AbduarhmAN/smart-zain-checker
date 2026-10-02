"""Central State Orchestrator for Smart Zain Checker.
Coordinates task leasing across isolated workers, progress checkpointing,
audit workbook results export, and real-time EventBus dispatches.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from domain.models import CheckError, CheckResult, Customer, Mismatch, ProgressMismatch
from domain.money import is_amount_match, parse_money_to_halalas
from domain.results_exporter import append_error_record, append_mismatch_record, ensure_audit_workbook_exists
from domain.workbook import extract_customer_records
from manager.checkpoint import CheckpointManager
from manager.event_bus import EVENT_BUS
from manager.queue import QueueService
from workers.supervisor import WorkerSupervisor

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

        # Current Job Context
        self.current_job_id: Optional[str] = None
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
            if self.is_running:
                logger.warning("Session already active.")
                return False

            from openpyxl import load_workbook
            wb = load_workbook(workbook_path, read_only=True, data_only=True)
            try:
                ws = wb.worksheets[sheet_index] if 0 <= sheet_index < len(wb.worksheets) else wb.worksheets[0]
                
                from domain.workbook import parse_excel_column, inspect_sheet_schema

                # Auto-detect sheet schema and merge with mapping to always capture status columns
                auto_schema = inspect_sheet_schema(ws)
                auto_letters = auto_schema.get("letters") or {}
                auto_indices = auto_schema.get("indices") or {}

                full_mapping = dict(auto_letters)
                full_mapping.update(mapping or {})
                mapping = full_mapping

                lookup_col = parse_excel_column(mapping.get("lookup_col", 12)) or 12
                amount_col = parse_excel_column(mapping.get("amount_col", 15)) or 15
                amount_col_2 = parse_excel_column(mapping.get("amount_col_2", 43)) or 43

                service_col = parse_excel_column(mapping.get("service_col"))
                customer_col = parse_excel_column(mapping.get("customer_col"))
                collector_col = parse_excel_column(mapping.get("collector_col"))
                case_status_col = parse_excel_column(mapping.get("case_status_col")) or auto_indices.get("case_status_col")
                main_status_col = parse_excel_column(mapping.get("main_status_col")) or auto_indices.get("main_status_col")
                sub_status_col = parse_excel_column(mapping.get("sub_status_col")) or auto_indices.get("sub_status_col")
                notes_col = parse_excel_column(mapping.get("notes_col")) or auto_indices.get("notes_col")

                self.customers = extract_customer_records(
                    sheet=ws,
                    lookup_col=lookup_col,
                    amount_col=amount_col,
                    amount_col_2=amount_col_2,
                    service_col=service_col,
                    customer_col=customer_col,
                    collector_col=collector_col,
                    case_status_col=case_status_col,
                    main_status_col=main_status_col,
                    sub_status_col=sub_status_col,
                    notes_col=notes_col,
                    record_type="wallet" if mode == "service_only" else ("account" if mode == "account_only" else "mixed"),
                )
            finally:
                wb.close()

            if not self.customers:
                logger.warning(f"No valid rows found in {workbook_path.name}")
                return False
 
            if not job_id and self.queue_service:
                qj = self.queue_service.get_job_by_file_and_sheet(workbook_path.name, sheet_index)
                if qj:
                    job_id = qj.id
                    self.queue_service.mark_job_active(job_id)

            self.current_job_id = job_id
            self.workbook_name = workbook_path.name
            self.result_path = self.project_root / result_file
            self.is_finalizing = False
            ensure_audit_workbook_exists(self.result_path)

            chk_path = self.project_root / f".checkpoint_{workbook_path.stem}_{sheet_index}.json"
            self.checkpoint_manager = CheckpointManager(chk_path)
            
            if force_restart:
                self.checkpoint_manager.reset()
                wal_path = self.project_root / f".wal_{workbook_path.stem}.jsonl"
                stream_path = self.project_root / f".stream_{workbook_path.stem}.xlsx"
                if wal_path.exists():
                    wal_path.unlink(missing_ok=True)
                if stream_path.exists():
                    stream_path.unlink(missing_ok=True)
                self.completed_indices = set()
                self.mismatches = []
                self.matches_count = 0
                self.errors_count = 0
                if self.queue_service:
                    if job_id:
                        self.queue_service.update_job_progress(job_id, 0, len(self.customers), 0, 0, 0)
                    else:
                        qj = self.queue_service.get_job_by_file_and_sheet(workbook_path.name, sheet_index)
                        if qj:
                            self.queue_service.update_job_progress(qj.id, 0, len(self.customers), 0, 0, 0)
                logger.info(f"Force restart enabled: Cleared previous data for {workbook_path.name}")
            else:
                # Load previous checkpoint
                saved = self.checkpoint_manager.load()
                self.completed_indices = set(saved.get("completed_indices", []))
                self.mismatches = [ProgressMismatch(**m) for m in saved.get("mismatches", [])]
                self.errors_count = int(saved.get("errors_count", 0))
                self.matches_count = int(saved.get("matches_count", max(0, len(self.completed_indices) - len(self.mismatches) - self.errors_count)))

            self.deferred_indices.clear()
            self.deferred_attempts.clear()
            self.target_url = target_url
            self.is_running = True
            self.is_paused = False
            self.active_leases.clear()
            self.recent_results.clear()

            # Start Decoupled Background Async Writer Thread
            import queue
            from zain_checker.async_pipeline_engine import AsyncExcelWriterThread

            self.result_queue = queue.Queue()
            self.all_completed_records.clear()
            wal_path = self.project_root / f".wal_{workbook_path.stem}.jsonl"
            stream_path = self.project_root / f".stream_{workbook_path.stem}.xlsx"
            self.writer_thread = AsyncExcelWriterThread(
                result_queue=self.result_queue,
                result_path=stream_path,
                wal_path=wal_path,
                batch_size=50,
                flush_interval_seconds=2.0,
            )
            self.writer_thread.start()


        # Start isolated worker actors
        self.supervisor.start_all(target_url=target_url)

        # Launch Direct API background threads for active workers
        for actor in self.supervisor.get_all_workers():
            if getattr(actor, "use_direct_api", False) and actor.status == "ready":
                w_thread = threading.Thread(
                    target=self._run_worker_api_loop,
                    args=(actor.worker_id,),
                    name=f"DirectApiWorker-{actor.worker_id}",
                    daemon=True,
                )
                w_thread.start()

        EVENT_BUS.publish("session_started", {
            "workbook": self.workbook_name,
            "target_url": self.target_url,
            "total_records": len(self.customers),
            "previously_completed": len(self.completed_indices),
        })
        return True

    def lease_next_task_for_worker(self, worker_id: str) -> Optional[dict[str, Any]]:
        """Leases the next uncompleted customer record to the requesting worker."""
        with self.lock:
            if not self.is_running or self.is_paused:
                return None

            now = time.time()
            # Clean expired leases (older than 120s)
            expired = [t_id for t_id, data in self.active_leases.items() if now - data["leased_at"] > 120]
            for exp_id in expired:
                del self.active_leases[exp_id]

            # 1. If this worker already has an active task in progress, re-issue it (do not skip ahead)
            for active_task_id, active_data in self.active_leases.items():
                if active_data.get("worker_id") == worker_id:
                    cust = active_data["customer"]
                    return {
                        "task_id": active_task_id,
                        "index": active_data["index"],
                        "row_number": cust.row_number,
                        "search_number": cust.lookup_number,
                        "contract": cust.contract,
                        "service_number": cust.service_number,
                        "record_type": cust.record_type,
                        "expected_amount_sar": cust.expected_amount / 100.0,
                        "target_url": getattr(self, "target_url", "https://business.zain.sa/dashboard/quick-pay"),
                        "worker_id": worker_id,
                    }

            already_leased_indices = {data["index"] for data in self.active_leases.values()}

            current_actor = self.supervisor.get_worker(worker_id)
            current_ip_group = current_actor.ip_group if current_actor else "unknown"

            # Identify IP groups that currently hold an active lease on a 2xxx service number
            busy_service_ip_groups: set[str] = set()
            for active_task_id, active_data in self.active_leases.items():
                other_w_id = active_data.get("worker_id")
                other_actor = self.supervisor.get_worker(other_w_id)
                if other_actor:
                    other_cust = active_data.get("customer")
                    if other_cust:
                        is_other_service = (
                            str(getattr(other_cust, "service_number", "")).startswith("2")
                            or str(getattr(other_cust, "lookup_number", "")).startswith("2")
                        )
                        if is_other_service:
                            busy_service_ip_groups.add(other_actor.ip_group)

            candidate_idx = None

            # Phase 1: Regular candidates (skip completed, already leased, and deferred)
            for idx, cust in enumerate(self.customers):
                if idx in self.completed_indices or idx in already_leased_indices or idx in self.deferred_indices:
                    continue

                is_candidate_service = (
                    str(getattr(cust, "service_number", "")).startswith("2")
                    or str(getattr(cust, "lookup_number", "")).startswith("2")
                )

                # Strict Rule: Two workers sharing the same IP MUST NOT query a 2xxx service number concurrently!
                if is_candidate_service and (current_ip_group in busy_service_ip_groups):
                    continue

                candidate_idx = idx
                break

            # Phase 2: Deferred candidates (retried at the very end of the queue!)
            if candidate_idx is None and self.deferred_indices:
                # Check if all normal records are either completed or currently in flight
                has_pending_regular = any(
                    i for i in range(len(self.customers))
                    if i not in self.completed_indices and i not in self.deferred_indices and i not in already_leased_indices
                )
                if not has_pending_regular:
                    # We are at the end of the queue: retry deferred records!
                    for def_idx in list(self.deferred_indices):
                        if def_idx in self.completed_indices or def_idx in already_leased_indices:
                            continue
                        cust = self.customers[def_idx]
                        is_candidate_service = (
                            str(getattr(cust, "service_number", "")).startswith("2")
                            or str(getattr(cust, "lookup_number", "")).startswith("2")
                        )
                        if is_candidate_service and (current_ip_group in busy_service_ip_groups):
                            continue

                        candidate_idx = def_idx
                        break

            if candidate_idx is not None:
                idx = candidate_idx
                cust = self.customers[idx]
                task_id = f"task_{idx}_{int(now)}"
                self.active_leases[task_id] = {
                    "index": idx,
                    "customer": cust,
                    "worker_id": worker_id,
                    "leased_at": now,
                }

                actor = self.supervisor.get_worker(worker_id)
                if actor:
                    actor.assign_task({
                        "row": cust.row_number,
                        "search_number": cust.lookup_number,
                        "expected_sar": cust.expected_amount / 100.0,
                    })

                # Determine whether to search by account or service in Zain portal
                return {
                    "task_id": task_id,
                    "index": idx,
                    "row_number": cust.row_number,
                    "search_number": cust.lookup_number,
                    "contract": cust.contract,
                    "service_number": cust.service_number,
                    "record_type": cust.record_type,
                    "expected_amount_sar": cust.expected_amount / 100.0,
                    "target_url": getattr(self, "target_url", "https://business.zain.sa/dashboard/quick-pay"),
                    "worker_id": worker_id,
                }

            if current_actor and (current_ip_group in busy_service_ip_groups) and current_actor.current_task is None:
                current_actor.status = "ready"
                current_actor.status_reason = "وضع الاستعداد الذكي: حماية الـ IP المشترك من الحظر (انتظار انتهاء استعلام 2xx)"

            return None

    def defer_task_to_end(self, task_id: str, reason: str = "") -> None:
        """Defers a blocked task so it is retried at the very end of the session, not in the error list."""
        with self.lock:
            lease = self.active_leases.pop(task_id, None)
            if not lease:
                return

            idx = lease["index"]
            cust = lease["customer"]
            attempts = self.deferred_attempts.get(idx, 0) + 1
            self.deferred_attempts[idx] = attempts

            if idx not in self.deferred_indices and idx not in self.completed_indices:
                self.deferred_indices.append(idx)

            logger.warning(
                f"[DeferredQueue] السطر {cust.row_number} ({cust.lookup_number}) تعرض للحظر ({reason}). "
                f"تم نقله لنهاية القائمة لإعادة المحاولة لاحقاً (المحاولة {attempts})."
            )

            EVENT_BUS.publish("task_deferred", {
                "row": cust.row_number,
                "number": cust.lookup_number,
                "reason": reason,
                "attempt": attempts,
                "deferred_count": len([i for i in self.deferred_indices if i not in self.completed_indices]),
            })

    def record_task_outcome(
        self,
        task_id: str,
        live_amount_raw: Any,
        status: str,  # 'match', 'mismatch', 'error'
        error_msg: str = "",
        worker_id: str = "Worker 1",
    ) -> None:
        """Processes the outcome reported by a worker extension, writes to Excel, and updates stats."""
        lease = None
        with self.lock:
            lease = self.active_leases.pop(task_id, None)

        if not lease:
            return

        cust: Customer = lease["customer"]
        idx: int = lease["index"]

        live_halalas = parse_money_to_halalas(live_amount_raw) or 0
        live_sar = live_halalas / 100.0

        if status == "error":
            with self.lock:
                self.errors_count += 1
                self.completed_indices.add(idx)

            label = "خطأ فحص"
            eff_expected = cust.expected_amount
            diff_sar = 0.0

            result = CheckResult(
                row=cust.row_number,
                record_type=cust.record_type,
                lookup_number=cust.lookup_number,
                customer_name=cust.customer_name,
                expected_amount=cust.expected_amount / 100.0,
                live_amount=0.0,
                status="error",
                diff_sar=0.0,
                worker_id=worker_id,
                timestamp=time.strftime("%H:%M:%S"),
            )
        else:
            # Match Verification with 20 Halalas tolerance
            matched, eff_expected, diff_h = is_amount_match(
                live_halalas=live_halalas,
                expected_primary=cust.expected_amount,
                expected_secondary=cust.expected_amount_2,
            )

            with self.lock:
                self.completed_indices.add(idx)

            if matched:
                with self.lock:
                    self.matches_count += 1
                label = "مسدد بالكامل" if live_sar == 0.0 and eff_expected > 0 else "تطابق تام"
                diff_sar = 0.0
                result = CheckResult(
                    row=cust.row_number,
                    record_type=cust.record_type,
                    lookup_number=cust.lookup_number,
                    customer_name=cust.customer_name,
                    expected_amount=eff_expected / 100.0,
                    live_amount=live_sar,
                    status="match",
                    diff_sar=0.0,
                    worker_id=worker_id,
                    timestamp=time.strftime("%H:%M:%S"),
                )
            else:
                label = "مسدد بالكامل" if live_sar == 0.0 else "فرق رصيد"
                diff_sar = diff_h / 100.0
                with self.lock:
                    self.mismatches.append(ProgressMismatch(
                        sequence_index=idx,
                        expected_amount=eff_expected,
                        website_amount=live_halalas,
                    ))

                result = CheckResult(
                    row=cust.row_number,
                    record_type=cust.record_type,
                    lookup_number=cust.lookup_number,
                    customer_name=cust.customer_name,
                    expected_amount=eff_expected / 100.0,
                    live_amount=live_sar,
                    status="mismatch",
                    diff_sar=diff_sar,
                    worker_id=worker_id,
                    timestamp=time.strftime("%H:%M:%S"),
                )

        # Build 19-column executive deliverable record
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
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

        with self.lock:
            self.all_completed_records[idx] = row_record

        # Push to async writer queue in RAM (0.000001s, non-blocking)
        if self.result_queue is not None:
            self.result_queue.put(row_record)

        actor = self.supervisor.get_worker(worker_id)

        if actor:
            actor.record_outcome(result.status)

        with self.lock:
            self.recent_results.insert(0, result.to_dict())
            if len(self.recent_results) > 150:
                self.recent_results.pop()

        # Save checkpoint periodically (every record)
        if self.checkpoint_manager:
            self.checkpoint_manager.save(
                completed_indices=self.completed_indices,
                mismatches=self.mismatches,
                last_index=idx,
                workbook_name=self.workbook_name,
                extra={
                    "errors_count": self.errors_count,
                    "matches_count": self.matches_count,
                },
            )

        # Update queue progress
        if self.current_job_id:
            self.queue_service.update_job_progress(
                job_id=self.current_job_id,
                completed=len(self.completed_indices),
                remaining=max(0, len(self.customers) - len(self.completed_indices)),
                matches=self.matches_count,
                mismatches=len(self.mismatches),
                errors=self.errors_count,
            )

        EVENT_BUS.publish("task_completed", result.to_dict())

    def pause_session(self) -> None:
        with self.lock:
            self.is_paused = True
        EVENT_BUS.publish("session_paused", {})

    def resume_session(self) -> None:
        with self.lock:
            self.is_paused = False
        EVENT_BUS.publish("session_resumed", {})

    def cancel_session(self) -> None:
        with self.lock:
            self.is_running = False
            self.is_paused = False
            self.active_leases.clear()
        self.supervisor.stop_all()
        if self.writer_thread and self.result_queue:
            self.result_queue.put("STOP_SENTINEL")
        EVENT_BUS.publish("session_cancelled", {})

    def _run_worker_api_loop(self, worker_id: str) -> None:
        """Continuously leases and executes verification tasks directly via Zain REST API."""
        logger.info(f"Worker {worker_id} direct REST API engine started.")
        actor = self.supervisor.get_worker(worker_id)
        if not actor:
            return

        while self.is_running:
            if self.is_paused:
                time.sleep(0.5)
                continue

            if actor.is_in_cooldown():
                time.sleep(1.0)
                continue

            task = self.lease_next_task_for_worker(worker_id)
            if not task:
                time.sleep(0.4)
                should_finalize = False
                with self.lock:
                    if not self.is_running or self.is_finalizing:
                        break
                    remaining_deferred = [i for i in self.deferred_indices if i not in self.completed_indices]
                    if len(self.customers) > 0 and len(self.completed_indices) >= len(self.customers) and not self.active_leases and not remaining_deferred:
                        self.is_finalizing = True
                        should_finalize = True

                if should_finalize:
                    self._finalize_job()
                    break
                continue

            contract = task.get("contract") or task.get("search_number")
            service_num = task.get("service_number") or ""
            task_id = task.get("task_id")

            # Route service vs contract queries intelligently
            if str(service_num).startswith("2") and task.get("record_type") == "wallet":
                status, live_amount, msg = actor.execute_task_api(service_num)
            else:
                status, live_amount, msg = actor.execute_task_api(contract)
                # If contract search returned error/not_found and service_num starts with 2, fallback
                if (status != "ok" or live_amount is None) and str(service_num).startswith("2"):
                    s_status, s_amount, s_msg = actor.execute_task_api(service_num)
                    if s_status == "ok":
                        status, live_amount, msg = s_status, s_amount, s_msg
                    elif status not in ("ok", "match") and s_status in ("error", "not_found", "blocked"):
                        status, live_amount, msg = s_status, s_amount, s_msg

            try:
                if status == "ok":
                    self.record_task_outcome(
                        task_id=task_id,
                        live_amount_raw=live_amount,
                        status="match",
                        worker_id=worker_id,
                    )
                elif status == "blocked":
                    idx = task.get("index")
                    attempts = self.deferred_attempts.get(idx, 0) + 1
                    if attempts <= 2:
                        logger.warning(
                            f"Worker {worker_id} blocked/redirected on {service_num or contract}: {msg}. "
                            f"Moving to end of list (Attempt {attempts}/2)."
                        )
                        # Proxy workers (Worker 3) have isolated clean IPs and must NEVER be placed in cooldown
                        if not actor.use_proxy:
                            actor.set_cooldown(12.0, f"حظر مؤقت: {msg}")
                        self.defer_task_to_end(task_id, reason=msg)
                        continue
                    else:
                        logger.error(f"Record {service_num or contract} permanently blocked after {attempts} deferred retries.")
                        self.record_task_outcome(
                            task_id=task_id,
                            live_amount_raw=0.0,
                            status="error",
                            error_msg=f"حظر دائم بعد المحاولات: {msg}",
                            worker_id=worker_id,
                        )
                elif status == "session_expired":
                    idx = task.get("index")
                    attempts = self.deferred_attempts.get(idx, 0) + 1
                    if attempts <= 2:
                        logger.info(
                            f"Worker {worker_id} session refreshed on {service_num or contract}: {msg}. "
                            f"Moving to end of list (No worker cooldown)."
                        )
                        # Session challenge: NO worker cooldown needed! Just defer to end of list
                        self.defer_task_to_end(task_id, reason=msg)
                        continue
                    else:
                        self.record_task_outcome(
                            task_id=task_id,
                            live_amount_raw=0.0,
                            status="error",
                            error_msg=f"انتهاء صلاحية الجلسة بعد المحاولات: {msg}",
                            worker_id=worker_id,
                        )
                elif status in ("error", "not_found"):
                    self.record_task_outcome(
                        task_id=task_id,
                        live_amount_raw=0.0,
                        status="error",
                        error_msg=msg or "لم يتم العثور على بيانات الفاتورة",
                        worker_id=worker_id,
                    )
                else:  # network_error
                    retries = 0
                    with self.lock:
                        if task_id in self.active_leases:
                            self.active_leases[task_id]["retries"] = self.active_leases[task_id].get("retries", 0) + 1
                            retries = self.active_leases[task_id]["retries"]

                    if retries <= 2:
                        logger.warning(f"Worker {worker_id} network issue on contract {contract}: {msg} (retry {retries}/2)")
                        if not actor.use_proxy:
                            actor.set_cooldown(2.0, f"Network error: {msg}")
                        time.sleep(0.5)
                        continue
                    else:
                        logger.error(f"Worker {worker_id} permanent network error on {contract} after {retries} retries: {msg}")
                        self.record_task_outcome(
                            task_id=task_id,
                            live_amount_raw=0.0,
                            status="error",
                            error_msg=f"خطأ اتصال: {msg}",
                            worker_id=worker_id,
                        )
            except Exception as outcome_err:
                logger.error(f"Worker {worker_id} error processing outcome for contract {contract}: {outcome_err}")

            # Safe pacing interval (0.35s) to stay well within rate limits
            time.sleep(0.35)

        logger.info(f"Worker {worker_id} direct REST API engine finished.")

    def _finalize_job(self) -> None:
        with self.lock:
            self.is_running = False
            self.is_paused = False
        self.supervisor.stop_all()

        # Stop and flush async writer thread
        if self.writer_thread and self.result_queue:
            self.result_queue.put("STOP_SENTINEL")
            try:
                self.writer_thread.join(timeout=10.0)
            except Exception as e:
                logger.warning(f"Writer thread join error: {e}")

        # Build Executive Multi-Tab Deliverable (3 tabs: Dashboard, Actionable Net Diff, Master Archive)
        try:
            from zain_checker.executive_reporter import export_executive_workbook
            if self.all_completed_records:
                export_executive_workbook(list(self.all_completed_records.values()), self.result_path)
                logger.info(f"Executive 3-tab workbook generated at {self.result_path}")
                try:
                    import shutil
                    default_path = self.project_root / "نتائج فحص زين.xlsx"
                    if default_path.resolve() != self.result_path.resolve():
                        shutil.copy2(self.result_path, default_path)
                except Exception as copy_err:
                    logger.debug(f"Default result copy note: {copy_err}")
        except Exception as e:
            logger.error(f"Failed to generate executive deliverable: {e}")

        finished_job_id = self.current_job_id
        if finished_job_id:
            self.queue_service.mark_job_completed(finished_job_id)

        EVENT_BUS.publish("session_completed", {
            "workbook": self.workbook_name,
            "total": len(self.customers),
            "matches": self.matches_count,
            "mismatches": len(self.mismatches),
            "errors": self.errors_count,
            "result_file": str(self.result_path),
        })

        # Auto-advance to next pending queue job if present
        next_job = self.queue_service.get_next_pending_job(exclude_job_id=finished_job_id)
        if next_job:
            logger.info(f"Advancing automatically to queued job: {next_job.filename} [{next_job.sheet_name}] (ID: {next_job.id})")
            self.queue_service.mark_job_active(next_job.id)

            def _start_next_job():
                time.sleep(2.0)
                wb_path = self.project_root / next_job.filename
                self.start_session_from_config(
                    workbook_path=wb_path,
                    sheet_index=next_job.sheet_index,
                    mapping=next_job.column_mapping,
                    mode=next_job.mode,
                    amount_target=next_job.amount_target,
                    target_url=getattr(next_job, "target_url", "https://business.zain.sa/dashboard/quick-pay"),
                    job_id=next_job.id,
                    result_file=getattr(next_job, "result_file", "نتائج فحص زين.xlsx"),
                )

            threading.Thread(target=_start_next_job, daemon=True, name=f"AutoAdvance-{next_job.id}").start()
        else:
            logger.info("All queued audit jobs completed successfully. Queue is empty.")

    def start_queue(self) -> bool:
        """Starts processing the first pending job in the sequential queue."""
        with self.lock:
            if self.is_running:
                logger.warning("Cannot start queue: session is already running.")
                return False

        next_job = self.queue_service.get_next_pending_job()
        if not next_job:
            logger.warning("No pending jobs found in queue.")
            return False

        self.queue_service.mark_job_active(next_job.id)
        wb_path = self.project_root / next_job.filename
        return self.start_session_from_config(
            workbook_path=wb_path,
            sheet_index=next_job.sheet_index,
            mapping=next_job.column_mapping,
            mode=next_job.mode,
            amount_target=next_job.amount_target,
            target_url=getattr(next_job, "target_url", "https://business.zain.sa/dashboard/quick-pay"),
            job_id=next_job.id,
            result_file=getattr(next_job, "result_file", "نتائج فحص زين.xlsx"),
        )

    def get_live_status(self) -> dict[str, Any]:
        """Provides real-time telemetry for the Web UI without locking any inputs."""
        with self.lock:
            total = len(self.customers)
            done = len(self.completed_indices)
            remaining = max(0, total - done)
            matches = self.matches_count
            mismatches = len(self.mismatches)
            errors = self.errors_count

            # Calculate net mismatch halalas
            net_mismatch_halalas = sum(abs(m.website_amount - m.expected_amount) for m in self.mismatches)

            table_snapshot = list(self.recent_results)
            active_leases_count = len(self.active_leases)

        workers_info = self.supervisor.get_status_summary()

        return {
            "running": self.is_running,
            "paused": self.is_paused,
            "workbook": self.workbook_name,
            "kpis": {
                "total": total,
                "completed": done,
                "remaining": remaining,
                "matches": matches,
                "mismatches": mismatches,
                "errors": errors,
                "mismatch_total": net_mismatch_halalas / 100.0,
                "active_leases": active_leases_count,
            },
            "workers": workers_info,
            "table_rows": table_snapshot,
        }

    def restart_queue_job(self, job_id: str, auto_start: bool = True) -> bool:
        """Resets the checkpoint and queue record for a job, optionally auto-starting it."""
        job = self.queue_service.get_job(job_id)
        if not job:
            logger.warning(f"Cannot restart unknown job: {job_id}")
            return False

        with self.lock:
            if self.is_running and self.current_job_id == job_id:
                logger.warning(f"Cannot restart actively running job: {job_id}")
                return False

        # Clear checkpoint and temp stream files
        wb_stem = Path(job.filename).stem
        chk_path = self.project_root / f".checkpoint_{wb_stem}_{job.sheet_index}.json"
        CheckpointManager(chk_path).reset()

        wal_path = self.project_root / f".wal_{wb_stem}.jsonl"
        stream_path = self.project_root / f".stream_{wb_stem}.xlsx"
        if wal_path.exists():
            wal_path.unlink(missing_ok=True)
        if stream_path.exists():
            stream_path.unlink(missing_ok=True)

        # Reset in queue service
        self.queue_service.restart_job(job_id)

        if auto_start:
            if not self.is_running:
                logger.info(f"Auto-starting restarted job: {job.filename}")
                self.start_queue()
        return True

