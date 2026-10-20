import hashlib
import io
import json
import sys
import os

# Ensure UTF-8 stdout on Windows safely without closing underlying buffer (User Global Rule)
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
elif hasattr(sys.stdout, "buffer") and getattr(sys.stdout, "encoding", "").lower() != "utf-8":
    _PREV_STDOUT = sys.stdout
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# Enforce Multi-Factor Hardware Lock & Anti-Debug Check
from zain_checker.security import enforce_hardware_lock
enforce_hardware_lock()
import hmac
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlparse

from zain_checker.config import (
    BRIDGE_HOST,
    BRIDGE_PORT,
    BRIDGE_TOKEN,
    CHECKPOINT_PATH,
    CHROME_EXTENSION_DIRECTORY,
    BETWEEN_CUSTOMERS_DELAY_SECONDS,
    BETWEEN_CUSTOMERS_PROXY_DELAY_SECONDS,
    MISMATCH_RECHECK_DELAY_SECONDS,
    PROJECT_DIRECTORY,
    RESULT_WORKBOOK_PATH,
    ZAIN_CONTRACT_PAYMENT_URL,
    ZAIN_CHECKER_PROFILE_DIRECTORY,
    ZAIN_CHECKER_PROXY_PROFILE_DIRECTORY,
    ZAIN_CHECKER_PROFILE_READY_PATH,
    ZAIN_QUICKPAY_URL,
    ZAIN_PROXY_SERVER,
    WALLET_COOLDOWN_SECONDS,
)
from zain_checker.chrome_manager import terminate_worker_chrome_process
from zain_checker.console import format_console_message, log
from zain_checker.money import money_to_halalas
from zain_checker.workbook import (
    Customer,
    CheckError,
    Mismatch,
    append_error,
    append_mismatch,
    find_source_workbook,
    load_unresolved_redirect_keys,
    load_records,
    read_workbook_layout,
    resolve_redirect_error,
)


@dataclass(frozen=True)
class ProgressMismatch:
    sequence_index: int | None
    expected_amount: int
    website_amount: int


@dataclass(frozen=True)
class CustomSourceChoice:
    sheet_index: int
    sheet_name: str
    lookup_column: int
    lookup_header: str
    amount_column: int
    amount_header: str
    customer_column: int | None
    customer_header: str
    collector_column: int | None
    record_type: str
    service_column: int | None = None
    no_customer: bool = False
    no_collector: bool = False


INCOGNITO_HANDOFF_TIMEOUT_SECONDS = 20
INITIAL_CHROME_BOOTSTRAP_TIMEOUT_SECONDS = 90
MAX_INCOGNITO_BOOTSTRAP_RECOVERIES = 2


class CheckerState:
    def __init__(
        self,
        customers: list[Customer],
        start_index: int,
        mismatches: list[ProgressMismatch],
        save_progress: Callable[[int, list[ProgressMismatch], bool], None],
        write_mismatch: Callable[[Customer, int], None],
        write_error: Callable[[CheckError], None],
        resolve_prior_redirect: Callable[[Customer], None],
    ) -> None:
        self.customers = customers
        self.run_id = uuid.uuid4().hex
        self.attempt_id = 0
        self.mismatches = mismatches
        self.start_index = start_index
        self.assigned_index = start_index
        self.completed_indices: set[int] = set(range(start_index))
        self.in_flight: dict[str, dict[str, Any]] = {}
        self.retry_queue: list[int] = []
        self.save_progress = save_progress
        self.write_mismatch = write_mismatch
        self.write_error = write_error
        self.resolve_prior_redirect = resolve_prior_redirect
        self.next_available_at = 0.0
        self.current_task_started_at: float | None = None
        self.pending_load_samples: list[float] = []
        self.estimated_load_seconds: float | None = None
        self.incognito_retry_index: int | None = None
        self.redirect_retry_index: int | None = None
        self.recent_incognito_reset_indices: list[int] = []
        self.worker_handoff_started_at: dict[str, float] = {}
        self.worker_handoff_customers: dict[str, Customer] = {}
        self.worker_handoff_task_ids: dict[str, str] = {}
        self.worker_handoff_timeout_seconds: dict[str, int] = {}
        self.worker_handoff_recovery_attempts: dict[str, int] = {}
        self.incognito_handoff_timeout_seconds = INCOGNITO_HANDOFF_TIMEOUT_SECONDS
        self.mismatch_recheck_stage = 0
        self.network_waiting = False
        self.network_retry_pending = False
        self.network_change_requested = threading.Event()
        self.paused_workers: set[str] = set()
        self.worker_cooldown_until: dict[str, float] = {}
        self.stopped_by_user = False
        self.error: str | None = None
        self.completed = False
        self.finished = threading.Event()
        self.lock = threading.Lock()
        self.last_logged_task_index = -1
        self.wallet_cooldown_until: float = 0.0
        self.deferred_wallet_indices: list[int] = []

        # Cycle State Machine:
        # - "SERVICE_2": Service Number starting with '2' (wallet). Opens 1 fresh Incognito tab,
        #   refreshes ONCE at the start of the cycle, and reuses the same tab for consecutive '2...' numbers.
        # - "ACCOUNT": Account Number. Breaks the 'SERVICE_2' cycle and reuses the same tab for
        #   consecutive Account Numbers. When a new '2...' Service Number appears after an Account Number,
        #   all Incognito tabs are closed first, 1 fresh Incognito tab is opened, and refreshed once.
        if customers and 0 <= start_index < len(customers):
            first_is_srv2 = self._is_service_2(customers[start_index])
            self.active_cycle_mode: str = "SERVICE_2" if first_is_srv2 else "ACCOUNT"
            self.service_cycle_id: int = 1 if first_is_srv2 else 0
            self.service_cycle_refreshed: bool = False
        else:
            self.active_cycle_mode = "NONE"
            self.service_cycle_id = 0
            self.service_cycle_refreshed = False

    @property
    def index(self) -> int:
        if self.completed:
            return len(self.customers)
        for i in range(self.start_index, len(self.customers)):
            if i not in self.completed_indices:
                return i
        return len(self.customers)

    @index.setter
    def index(self, value: int) -> None:
        if value > len(self.completed_indices):
            for i in range(min(value, len(self.customers))):
                self.completed_indices.add(i)

    @property
    def incognito_handoff_started_at(self) -> float | None:
        if not self.worker_handoff_started_at:
            return None
        return min(self.worker_handoff_started_at.values())

    @incognito_handoff_started_at.setter
    def incognito_handoff_started_at(self, val: float | None) -> None:
        if val is None:
            self.worker_handoff_started_at.clear()
            self.worker_handoff_customers.clear()
            self.worker_handoff_task_ids.clear()
        else:
            self.worker_handoff_started_at["worker_1"] = val

    @property
    def incognito_handoff_recovery_attempts(self) -> int:
        return sum(self.worker_handoff_recovery_attempts.values())

    @incognito_handoff_recovery_attempts.setter
    def incognito_handoff_recovery_attempts(self, val: int) -> None:
        if val == 0:
            self.worker_handoff_recovery_attempts.clear()
        else:
            self.worker_handoff_recovery_attempts["worker_1"] = val

    @staticmethod
    def _is_service_2(customer: Customer) -> bool:
        srv = str(getattr(customer, "service_number", "") or "").strip()
        lookup = str(getattr(customer, "lookup_number", "") or "").strip()
        return (
            customer.record_type == "wallet"
            or srv.startswith("2")
            or lookup.startswith("2")
        )

    def _enter_fresh_service_2_cycle(self) -> None:
        self.active_cycle_mode = "SERVICE_2"
        self.service_cycle_id += 1
        self.service_cycle_refreshed = False

    @staticmethod
    def _save_diagnostic_snapshot(customer: Customer, status: str, payload: dict[str, Any]) -> None:
        try:
            diag_path = PROJECT_DIRECTORY / "debug_redirects.jsonl"
            snapshot = payload.get("page_snapshot") or {}
            entry = {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "row_number": customer.row_number,
                "search_number": customer.lookup_number,
                "record_type": customer.record_type,
                "expected_amount": customer.expected_amount / 100.0,
                "status": status,
                "current_url": str(payload.get("current_url") or snapshot.get("full_url") or ""),
                "page_title": str(payload.get("page_title") or snapshot.get("page_title") or ""),
                "snapshot": snapshot,
            }
            with open(diag_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as err:
            log(f"Notice: Could not save diagnostic snapshot: {err}")

    def get_task(self, worker_id: str = "Worker 1 (Router)") -> dict[str, Any]:
        with self.lock:
            if self.stopped_by_user:
                return {"status": "inactive"}
            if self.error:
                return {"status": "error", "message": self.error}
            if self.completed or len(self.completed_indices) >= len(self.customers):
                self.completed = True
                self.finished.set()
                return {"status": "complete", "total": len(self.customers)}

            now = time.monotonic()
            # 1. Reclaim in-flight tasks whose lease timed out (>45s)
            expired_tasks = [
                (tid, tinfo)
                for tid, tinfo in self.in_flight.items()
                if now - tinfo.get("leased_at", now) > 45.0
            ]
            for tid, tinfo in expired_tasks:
                del self.in_flight[tid]
                idx = tinfo["index"]
                assigned_w = tinfo.get("worker_id", "Unknown")
                if idx not in self.completed_indices and idx not in self.retry_queue:
                    log(
                        f"[{assigned_w}] Task for row {tinfo['customer'].row_number} (search number {tinfo['customer'].lookup_number}) "
                        "timed out (>45s). Returning to queue for retry."
                    )
                    self.retry_queue.insert(0, idx)

            canonical_name, worker_tag = normalize_worker_id(worker_id)
            worker_id = canonical_name

            if canonical_name in self.paused_workers or worker_tag in self.paused_workers:
                cooldown_expiry = self.worker_cooldown_until.get(canonical_name, 0.0)
                if now >= cooldown_expiry and cooldown_expiry > 0.0:
                    self.paused_workers.discard(canonical_name)
                    self.paused_workers.discard(worker_tag)
                    self.worker_cooldown_until.pop(canonical_name, None)
                    log(f"[{canonical_name}] Cooldown expired. Worker auto-unpaused and resumed.")
                else:
                    return {
                        "status": "worker_paused",
                        "worker_id": canonical_name,
                        "retry_after_seconds": 3,
                        "message": f"{canonical_name} is cooling down due to IP block. Auto-recovering...",
                    }

            if self.network_waiting:
                cust = self.customers[self.index] if self.index < len(self.customers) else self.customers[-1]
                return {
                    "status": "network_wait",
                    "task_id": self._task_id(cust, self.index),
                    "worker_id": worker_id,
                    "row_number": cust.row_number,
                    "contract": cust.contract,
                    "record_type": cust.record_type,
                    "search_number": cust.lookup_number,
                    "checked": len(self.completed_indices),
                    "total": len(self.customers),
                    "retry_after_seconds": 3,
                    "cycle_mode": self.active_cycle_mode,
                    "service_cycle_id": self.service_cycle_id,
                    "refresh_once": False,
                }

            if self.network_retry_pending:
                self.network_retry_pending = False
                cust = self.customers[self.index]
                is_srv2 = self._is_service_2(cust)
                if is_srv2:
                    self._enter_fresh_service_2_cycle()
                self._begin_incognito_handoff()
                return {
                    "status": "open_incognito",
                    "task_id": self._task_id(cust, self.index),
                    "worker_id": worker_id,
                    "row_number": cust.row_number,
                    "contract": cust.contract,
                    "record_type": cust.record_type,
                    "search_number": cust.lookup_number,
                    "cycle_mode": self.active_cycle_mode,
                    "service_cycle_id": self.service_cycle_id,
                    "refresh_once": bool(is_srv2 and not self.service_cycle_refreshed),
                }

            # 2. CHECK IF THIS SPECIFIC WORKER ALREADY HAS AN ACTIVE LEASED TASK!
            # If so, return that task again so polling during page load/navigation never skips records!
            for tid, tinfo in self.in_flight.items():
                t_w_canonical, t_w_tag = normalize_worker_id(tinfo.get("worker_id", ""))
                if t_w_canonical == canonical_name or t_w_tag == worker_tag:
                    tinfo["leased_at"] = now
                    cust = tinfo["customer"]
                    t_idx = tinfo["index"]
                    is_srv2 = self._is_service_2(cust)
                    return {
                        "status": "check",
                        "task_id": tid,
                        "worker_id": canonical_name,
                        "row_number": cust.row_number,
                        "contract": cust.contract,
                        "record_type": cust.record_type,
                        "search_number": cust.lookup_number,
                        "sequence": t_idx + 1,
                        "total": len(self.customers),
                        "cycle_mode": self.active_cycle_mode,
                        "service_cycle_id": self.service_cycle_id,
                        "refresh_once": bool(is_srv2 and not self.service_cycle_refreshed),
                    }

            # 3. Smart Dual-Queue Dispatcher with Wallet Circuit Breaker:
            task_idx = None
            wallet_in_cooldown = now < self.wallet_cooldown_until

            def _is_wallet_idx(idx: int) -> bool:
                if 0 <= idx < len(self.customers):
                    return self._is_service_2(self.customers[idx])
                return False

            # A. If wallet cooldown has expired, resume previously deferred wallets!
            if not wallet_in_cooldown and self.deferred_wallet_indices:
                task_idx = self.deferred_wallet_indices.pop(0)

            # B. Check retry queue:
            if task_idx is None and self.retry_queue:
                if not wallet_in_cooldown:
                    task_idx = self.retry_queue.pop(0)
                else:
                    # Cooldown active: prioritize non-wallet (account) items from retry queue
                    for i, q_idx in enumerate(self.retry_queue):
                        if not _is_wallet_idx(q_idx):
                            task_idx = self.retry_queue.pop(i)
                            break

            # C. Check sequential assigner (self.assigned_index):
            if task_idx is None:
                if not wallet_in_cooldown:
                    if self.assigned_index < len(self.customers):
                        task_idx = self.assigned_index
                        self.assigned_index += 1
                else:
                    # Wallet cooldown active: scan forward for the first ACCOUNT number!
                    # Any wallet passed along the way is safely preserved in deferred_wallet_indices
                    while self.assigned_index < len(self.customers):
                        cand_idx = self.assigned_index
                        self.assigned_index += 1
                        if _is_wallet_idx(cand_idx):
                            if cand_idx not in self.deferred_wallet_indices:
                                self.deferred_wallet_indices.append(cand_idx)
                        else:
                            task_idx = cand_idx
                            break

            # D. If still None: check in-flight or waiting for cooldown
            if task_idx is None:
                if self.in_flight:
                    # Still waiting for other in-flight worker(s) to finish
                    return {
                        "status": "waiting",
                        "retry_after_seconds": 1.0,
                        "checked": len(self.completed_indices),
                        "total": len(self.customers),
                        "cycle_mode": self.active_cycle_mode,
                        "service_cycle_id": self.service_cycle_id,
                        "refresh_once": False,
                    }
                elif wallet_in_cooldown and (
                    self.deferred_wallet_indices
                    or any(_is_wallet_idx(q) for q in self.retry_queue)
                ):
                    # All accounts completed! Waiting out remaining cooldown before resuming wallets
                    remaining_cooldown = max(1.0, self.wallet_cooldown_until - now)
                    return {
                        "status": "waiting",
                        "retry_after_seconds": min(remaining_cooldown, 5.0),
                        "checked": len(self.completed_indices),
                        "total": len(self.customers),
                        "cycle_mode": self.active_cycle_mode,
                        "service_cycle_id": self.service_cycle_id,
                        "refresh_once": False,
                    }
                else:
                    self.completed = True
                    self.finished.set()
                    return {"status": "complete", "total": len(self.customers)}

            customer = self.customers[task_idx]
            task_id = self._task_id(customer, task_idx)
            is_srv2 = self._is_service_2(customer)
            if is_srv2 and self.active_cycle_mode != "SERVICE_2":
                self._enter_fresh_service_2_cycle()
            elif not is_srv2 and self.active_cycle_mode != "ACCOUNT":
                self.active_cycle_mode = "ACCOUNT"

            self.in_flight[task_id] = {
                "index": task_idx,
                "customer": customer,
                "worker_id": worker_id,
                "leased_at": now,
                "recheck_stage": 0,
                "redirect_retry": False,
                "incognito_retry": False,
            }

            # PRINT TO CMD: Exactly which worker received this job!
            log(
                f"[{worker_id}] Checking row {customer.row_number}, {customer.record_type} "
                f"{customer.lookup_number} ({task_idx + 1} of {len(self.customers)})..."
            )
            try:
                from zain_checker.telemetry import emit_active_task
                emit_active_task({
                    "worker_id": worker_id,
                    "row": customer.row_number,
                    "lookup_number": customer.lookup_number,
                    "record_type": customer.record_type,
                    "customer_name": customer.customer_name,
                    "expected_amount": customer.expected_amount / 100.0,
                    "live_amount": customer.expected_amount / 100.0,
                    "status": "checking"
                })
            except Exception:
                pass

            return {
                "status": "check",
                "task_id": task_id,
                "worker_id": worker_id,
                "row_number": customer.row_number,
                "contract": customer.contract,
                "record_type": customer.record_type,
                "search_number": customer.lookup_number,
                "sequence": task_idx + 1,
                "total": len(self.customers),
                "cycle_mode": self.active_cycle_mode,
                "service_cycle_id": self.service_cycle_id,
                "refresh_once": bool(is_srv2 and not self.service_cycle_refreshed),
            }

    def resume_after_network_change(self) -> Customer:
        with self.lock:
            if not self.network_waiting:
                raise RuntimeError("The checker is not waiting for a network change.")
            customer = self.customers[self.index]
            self.network_waiting = False
            self.network_retry_pending = True
            self.network_change_requested.clear()
            self.incognito_retry_index = self.index
            self.recent_incognito_reset_indices.clear()
            if self._is_service_2(customer):
                self._enter_fresh_service_2_cycle()
            self._advance_attempt()
            self.next_available_at = 0.0
            self.current_task_started_at = None
            self._begin_incognito_handoff()
            return customer

    def stop_during_network_wait(self) -> None:
        with self.lock:
            self.stopped_by_user = True
            self.network_waiting = False
            self.network_retry_pending = False
            self.network_change_requested.clear()
            self.finished.set()

    def resume_worker(self, worker_id: str = "Worker 2 (Proxy)") -> Customer | None:
        with self.lock:
            canonical_name, worker_tag = normalize_worker_id(worker_id)
            self.paused_workers.discard(canonical_name)
            self.paused_workers.discard(worker_tag)
            self.paused_workers.discard(worker_id)
            self.worker_cooldown_until.pop(canonical_name, None)
            self.worker_cooldown_until.pop(worker_tag, None)
            self.network_waiting = False
            self.network_change_requested.clear()
            if self.retry_queue:
                next_idx = self.retry_queue.pop(0)
            else:
                next_idx = self.index
            if 0 <= next_idx < len(self.customers):
                return self.customers[next_idx]
            return self.customers[0] if self.customers else None

    def submit_result(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if self.error:
                return {"status": "error", "message": self.error}
            if self.completed:
                return {"status": "complete", "total": len(self.customers)}

            task_id = str(payload.get("task_id", "")).strip()
            task_info = self.in_flight.get(task_id)
            if not task_info:
                req_row = payload.get("row_number")
                req_num = str(payload.get("search_number") or payload.get("contract") or "").strip()
                for tid, tinfo in self.in_flight.items():
                    if tinfo["customer"].row_number == req_row or tinfo["customer"].lookup_number == req_num:
                        task_id = tid
                        task_info = tinfo
                        break

            if not task_info:
                log(f"Notice: Result received for unknown or unleased task_id={task_id}; ignoring.")
                return {
                    "status": "waiting",
                    "retry_after_seconds": 0.5,
                    "checked": len(self.completed_indices),
                    "total": len(self.customers),
                }

            customer = task_info["customer"]
            task_idx = task_info["index"]
            worker_id = task_info.get("worker_id", payload.get("worker_id", "Worker 1 (Router)"))
            _, worker_tag = normalize_worker_id(worker_id)
            self.worker_handoff_started_at.pop(worker_tag, None)
            self.worker_handoff_customers.pop(worker_tag, None)
            self.worker_handoff_task_ids.pop(worker_tag, None)
            self.worker_handoff_recovery_attempts.pop(worker_tag, None)

            result_status = payload.get("status")
            if result_status in ("redirected", "page_timeout", "rejected"):
                self._save_diagnostic_snapshot(customer, result_status, payload)
                snapshot = payload.get("page_snapshot") or {}
                snap_url = str(payload.get("current_url") or snapshot.get("full_url") or "")
                snap_title = str(payload.get("page_title") or snapshot.get("page_title") or "")
                snap_val = snapshot.get("amount_input_value") or snapshot.get("amount_span_text")
                log(
                    f"[{worker_id}] 🔍 Diagnostic snapshot recorded for row {customer.row_number} ({customer.lookup_number}): "
                    f"Status={result_status} | URL={snap_url} | Title='{snap_title}' | FoundAmount={snap_val} -> debug_redirects.jsonl"
                )

            if result_status == "redirected":
                current_url = str(payload.get("current_url") or "").strip()[:500]
                destination = f" Redirected to: {current_url}" if current_url else ""

                current_url_lower = current_url.lower()
                is_home_redirect = bool(
                    current_url_lower.rstrip("/").endswith(("/home", "/ar/home", "/en/home"))
                    or "/home?" in current_url_lower
                    or "/home#" in current_url_lower
                    or current_url_lower == "https://app.sa.zain.com"
                    or current_url_lower == "https://sa.zain.com"
                )
                account_is_duplicate = bool(getattr(customer, "account_is_duplicate", False))

                if account_is_duplicate and is_home_redirect:
                    log(
                        f"Row {customer.row_number}, search number {customer.lookup_number}: "
                        f"Normal Zain redirection for duplicate account "
                        f"({customer.original_account_number or customer.lookup_number}). "
                        "Recording immediately and opening fresh session for the next record."
                    )
                    return self._record_current_error(
                        customer,
                        "إعادة توجيه (طبيعي)",
                        "تم التحويل لصفحة زين الرئيسية (حساب متعدد الخدمات مسدد أو مغلق)." + destination,
                        reopen_incognito=True,
                        task_id=task_id,
                    )

                if not task_info.get("redirect_retry"):
                    task_info["redirect_retry"] = True
                    task_info["leased_at"] = time.monotonic()
                    log(
                        f"[{worker_id}] Row {customer.row_number}, search number {customer.lookup_number}: "
                        f"Zain redirected away{destination}. "
                        "Restarting worker Chrome instance for a 100% fresh session and retrying..."
                    )
                    self._begin_incognito_handoff(worker_id=worker_id, customer=customer, task_id=task_id)
                    threading.Thread(
                        target=self._restart_worker_instance,
                        args=(customer, task_id, worker_id),
                        daemon=True,
                    ).start()
                    return {
                        "status": "restarting_instance",
                        "task_id": task_id,
                        "worker_id": worker_id,
                        "row_number": customer.row_number,
                        "contract": customer.contract,
                        "record_type": customer.record_type,
                        "search_number": customer.lookup_number,
                        "cycle_mode": self.active_cycle_mode,
                        "service_cycle_id": self.service_cycle_id,
                        "refresh_once": bool(self._is_service_2(customer)),
                    }

                is_srv2 = self._is_service_2(customer)
                if is_srv2:
                    # WALLET CIRCUIT BREAKER:
                    # Zain quickpay endpoint soft-blocks rapid queries with redirects to /ar/home.
                    # Enter cooldown and switch to pending accounts instead of burning the record!
                    self.wallet_cooldown_until = time.monotonic() + WALLET_COOLDOWN_SECONDS
                    cooldown_mins = max(1, WALLET_COOLDOWN_SECONDS // 60)
                    log(
                        f"⚠️ [{worker_id}] [Wallet Circuit Breaker] Zain quickpay soft-block (redirect) confirmed for row {customer.row_number} ({customer.lookup_number}). "
                        f"Entering a {cooldown_mins}-minute cooldown for wallets. "
                        f"Deferring row {customer.row_number} and immediately switching workers to pending Account numbers!"
                    )
                    task_idx = task_info.get("index")
                    if task_idx is not None and task_idx not in self.deferred_wallet_indices:
                        self.deferred_wallet_indices.append(task_idx)
                    del self.in_flight[task_id]
                    return {
                        "status": "waiting",
                        "retry_after_seconds": 0.5,
                        "checked": len(self.completed_indices),
                        "total": len(self.customers),
                    }

                log(
                    f"Row {customer.row_number}, search number {customer.lookup_number}: "
                    f"Zain redirected away{destination} (abnormal redirect for single account). "
                    "Recording status and continuing to next customer."
                )
                return self._record_current_error(
                    customer,
                    "إعادة توجيه (غير طبيعي)",
                    (
                        "Zain redirected away from the requested verification page "
                        "(abnormal redirect for single account)."
                    )
                    + destination,
                    reopen_incognito=False,
                    task_id=task_id,
                )

            if result_status == "page_timeout":
                current_url = str(payload.get("current_url") or "").strip()[:500]
                destination = f" Redirected to: {current_url}" if current_url else ""

                if not task_info.get("redirect_retry"):
                    task_info["redirect_retry"] = True
                    task_info["leased_at"] = time.monotonic()
                    log(
                        f"The requested page for row {customer.row_number}, search "
                        f"number {customer.lookup_number}, did not expose a stable visible "
                        f"amount before the page timeout.{destination} Retrying once in-session..."
                    )
                    return {
                        "status": "retry_redirect",
                        "task_id": task_id,
                        "row_number": customer.row_number,
                        "contract": customer.contract,
                        "record_type": customer.record_type,
                        "search_number": customer.lookup_number,
                        "retry_after_seconds": 5,
                    }

                if not task_info.get("incognito_retry"):
                    task_info["incognito_retry"] = True
                    task_info["leased_at"] = time.monotonic()
                    self._begin_incognito_handoff(worker_id=worker_id, customer=customer, task_id=task_id)
                    log(
                        f"The requested page for row {customer.row_number}, search "
                        f"number {customer.lookup_number}, still did not expose a stable "
                        "visible amount before the page timeout. Retrying in fresh Incognito session."
                    )
                    return {
                        "status": "open_incognito",
                        "task_id": task_id,
                        "row_number": customer.row_number,
                        "contract": customer.contract,
                        "record_type": customer.record_type,
                        "search_number": customer.lookup_number,
                        "cycle_mode": self.active_cycle_mode,
                        "service_cycle_id": self.service_cycle_id,
                        "refresh_once": bool(self._is_service_2(customer)),
                    }

                return self._record_current_error(
                    customer,
                    "انتهاء مهلة الصفحة بعد جلسة جديدة",
                    (
                        "Zain did not expose a stable visible amount before the "
                        "timeout after one same-session retry and one fresh-Incognito retry."
                    )
                    + destination,
                    task_id=task_id,
                )

            if result_status == "rejected":
                support_id = str(payload.get("support_id") or "").strip()[:100]
                support_text = f" Support ID: {support_id}." if support_id else ""
                if task_info.get("incognito_retry"):
                    if task_id in self.in_flight:
                        del self.in_flight[task_id]
                    if task_idx not in self.retry_queue and task_idx not in self.completed_indices:
                        self.retry_queue.insert(0, task_idx)

                    # Pause THIS worker only so other workers continue checking uninterrupted!
                    self.paused_workers.add(worker_id)
                    self.worker_cooldown_until[worker_id] = time.monotonic() + 20.0
                    _, worker_tag = normalize_worker_id(worker_id)
                    threading.Thread(
                        target=terminate_worker_chrome_process,
                        args=(worker_tag,),
                        daemon=True,
                    ).start()

                    log(
                        f"[{worker_id}] ⚠️ IP block temporary cool-off ({support_text}). "
                        f"Instance cooling down for 20s. Will auto-recover with fresh session and proxy."
                    )
                    return {
                        "status": "worker_paused",
                        "worker_id": worker_id,
                        "task_id": task_id,
                        "message": f"IP block detected. {worker_id} cooling down for 20s.",
                    }

                task_info["incognito_retry"] = True
                task_info["leased_at"] = time.monotonic()
                self._begin_incognito_handoff(worker_id=worker_id, customer=customer, task_id=task_id)
                log(
                    f"[{worker_id}] Zain rejected row {customer.row_number}."
                    f"{support_text} Restarting worker Chrome instance for a fresh session and retrying."
                )
                threading.Thread(
                    target=self._restart_worker_instance,
                    args=(customer, task_id, worker_id),
                    daemon=True,
                ).start()
                return {
                    "status": "restarting_instance",
                    "task_id": task_id,
                    "worker_id": worker_id,
                    "row_number": customer.row_number,
                    "contract": customer.contract,
                    "record_type": customer.record_type,
                    "search_number": customer.lookup_number,
                    "cycle_mode": self.active_cycle_mode,
                    "service_cycle_id": self.service_cycle_id,
                    "refresh_once": bool(self._is_service_2(customer)),
                }

            if result_status == "error":
                detail = str(payload.get("message") or "Unknown browser error.")[:500]
                return self._record_current_error(
                    customer,
                    "خطأ في صفحة زين",
                    detail,
                    task_id=task_id,
                )

            if result_status != "ok":
                raise ValueError(f"The browser returned an invalid result status: {result_status}")

            raw_website_amount = payload.get("website_amount")
            try:
                website_amount = money_to_halalas(raw_website_amount)
            except ValueError as error:
                return self._record_current_error(
                    customer,
                    "مبلغ غير صالح من زين",
                    f"Zain returned an invalid amount: {error}",
                    task_id=task_id,
                )

            TOLERANCE_HALALAS = 20
            is_match = (
                abs(website_amount - customer.expected_amount) <= TOLERANCE_HALALAS
                or (
                    getattr(customer, "expected_amount_2", None) is not None
                    and abs(website_amount - customer.expected_amount_2) <= TOLERANCE_HALALAS
                )
            )

            recheck_stage = task_info.get("recheck_stage", 0)
            if not is_match and recheck_stage < 2:
                task_info["recheck_stage"] = recheck_stage + 1
                task_info["leased_at"] = time.monotonic()
                amt_str = f"CanonicalFileAmount={customer.expected_amount / 100:,.2f}"

                if task_info["recheck_stage"] == 1:
                    log(
                        f"Possible mismatch at row {customer.row_number}, search "
                        f"number {customer.lookup_number}: {amt_str}, "
                        f"Zain raw={raw_website_amount!r}. Waiting "
                        f"{MISMATCH_RECHECK_DELAY_SECONDS} seconds and rechecking..."
                    )
                    return {
                        "status": "recheck_mismatch",
                        "task_id": task_id,
                        "retry_after_seconds": MISMATCH_RECHECK_DELAY_SECONDS,
                    }

                log(
                    f"Mismatch still present at row {customer.row_number}, search "
                    f"number {customer.lookup_number} ({amt_str}). Reloading for final check..."
                )
                return {
                    "status": "reload_mismatch",
                    "task_id": task_id,
                    "row_number": customer.row_number,
                    "contract": customer.contract,
                    "record_type": customer.record_type,
                    "search_number": customer.lookup_number,
                    "retry_after_seconds": MISMATCH_RECHECK_DELAY_SECONDS,
                }

            # Check finalized (match or confirmed mismatch)
            try:
                self.resolve_prior_redirect(customer)
            except Exception as error:
                log(f"Warning: could not resolve prior redirect for {customer.lookup_number}: {error}")

            effective_expected = customer.expected_amount
            if getattr(customer, "expected_amount_2", None) is not None:
                diff1 = abs(website_amount - customer.expected_amount)
                diff2 = abs(website_amount - customer.expected_amount_2)
                effective_expected = customer.expected_amount if diff1 <= diff2 else customer.expected_amount_2

            if not is_match:
                try:
                    self.write_mismatch(customer, website_amount)
                except Exception as error:
                    self.error = f"Could not save mismatch for row {customer.row_number}: {error}"
                    self.finished.set()
                    return {"status": "error", "message": self.error}

                self.mismatches.append(
                    ProgressMismatch(
                        task_idx,
                        effective_expected,
                        website_amount,
                    )
                )

            # Mark completed and remove from in_flight
            worker_id = task_info.get("worker_id", payload.get("worker_id", "Worker 1 (Router)"))
            self.completed_indices.add(task_idx)
            if task_id in self.in_flight:
                del self.in_flight[task_id]

            comparison = "match" if is_match else "mismatch"
            amount_details = ""
            if not is_match:
                amt_info = f"M1={customer.expected_amount / 100:,.2f}"
                if getattr(customer, "expected_amount_2", None) is not None:
                    amt_info += f", M2={customer.expected_amount_2 / 100:,.2f}"
                amount_details = f" {amt_info}, Zain raw={raw_website_amount!r}, Zain parsed={website_amount / 100:,.2f}."
            log(
                f"[{worker_id}] Checked row {customer.row_number}, {customer.record_type} {customer.lookup_number}: "
                f"{comparison}.{amount_details} ({len(self.completed_indices)} of {len(self.customers)})."
            )

            try:
                from zain_checker.telemetry import emit_check_result
                emit_check_result(
                    row=customer.row_number,
                    record_type=customer.record_type,
                    lookup_number=customer.lookup_number,
                    customer_name=customer.customer_name,
                    expected_amount=effective_expected / 100.0,
                    live_amount=website_amount / 100.0,
                    status=comparison,
                    worker_id=worker_id,
                )
            except Exception:
                pass

            is_complete = len(self.completed_indices) >= len(self.customers)
            try:
                self.save_progress(self.index, self.mismatches, is_complete, customer)
            except OSError as error:
                self.error = f"Could not save checking progress: {error}"
                self.finished.set()
                return {"status": "error", "message": self.error}

            if is_complete:
                self.completed = True
                self.finished.set()
                return {"status": "complete", "total": len(self.customers)}

            is_proxy_worker = bool(
                worker_id == "Worker 2 (Proxy)"
                or "proxy" in str(worker_id).lower()
            )
            retry_delay = (
                BETWEEN_CUSTOMERS_PROXY_DELAY_SECONDS
                if is_proxy_worker
                else BETWEEN_CUSTOMERS_DELAY_SECONDS
            )

            return {
                "status": "waiting",
                "retry_after_seconds": retry_delay,
                "checked": len(self.completed_indices),
                "total": len(self.customers),
                "cycle_mode": self.active_cycle_mode,
                "service_cycle_id": self.service_cycle_id,
                "refresh_once": False,
            }

    def _advance_to_next_customer_cycle(
        self,
        next_customer: Customer,
        force_reopen_incognito: bool = False,
        retry_after_seconds: float = 0,
    ) -> dict[str, Any]:
        """Stateful Cycle Transition Machine between Service Numbers starting with '2' (SERVICE_2)
        and Account Numbers (ACCOUNT):
        1. SERVICE_2 -> SERVICE_2 (Consecutive Service Numbers starting with 2):
           - DO NOT close the Incognito tab, DO NOT open a new window, and DO NOT refresh.
           - Reuse the exact same open Incognito tab directly.
        2. ACCOUNT -> SERVICE_2 (or force_reopen_incognito on SERVICE_2):
           - The cycle was broken by an Account Number (or forced reset).
           - Close ALL existing Incognito tabs/windows first, open ONLY 1 fresh Incognito tab,
             and refresh the page ONCE (`refresh_once: True`) for the new SERVICE_2 cycle.
        3. SERVICE_2 -> ACCOUNT:
           - Breaks the SERVICE_2 cycle (`active_cycle_mode = "ACCOUNT"`).
           - Uses the same open tab for the Account Number (unless force_reopen_incognito is True).
        4. ACCOUNT -> ACCOUNT (Consecutive Account Numbers):
           - Reuses the same open tab in ACCOUNT mode without reopening or refreshing.
        """
        next_is_srv2 = self._is_service_2(next_customer)
        prev_mode = self.active_cycle_mode

        if next_is_srv2:
            if prev_mode == "SERVICE_2" and not force_reopen_incognito:
                # Consecutive Service Number starting with 2: Reuse the SAME open Incognito tab without refreshing!
                log(
                    f"Consecutive Service Number starting with 2 (row {next_customer.row_number}, "
                    f"wallet {next_customer.lookup_number}): Reusing the SAME open Incognito tab "
                    f"in Cycle #{self.service_cycle_id} (no new tab, no page refresh)."
                )
                self.next_available_at = time.monotonic() + retry_after_seconds
                return {
                    "status": "waiting",
                    "retry_after_seconds": retry_after_seconds,
                    "checked": self.index,
                    "total": len(self.customers),
                    "cycle_mode": "SERVICE_2",
                    "service_cycle_id": self.service_cycle_id,
                    "refresh_once": False,
                    "reuse_same_tab": True,
                }
            else:
                # Entering a NEW Service Number (2...) cycle after an Account Number (or forced reset):
                # 1. Close ALL existing Incognito tabs/windows
                # 2. Open ONLY 1 fresh Incognito tab
                # 3. Refresh page ONCE for this new cycle
                self._enter_fresh_service_2_cycle()
                self._advance_attempt()
                self._begin_incognito_handoff()
                self.next_available_at = 0.0
                log(
                    f"Starting new Service Number (2...) Cycle #{self.service_cycle_id} "
                    f"(transition {prev_mode} -> SERVICE_2) for row {next_customer.row_number}, "
                    f"service {next_customer.lookup_number}: Closing ALL existing Incognito tabs first, "
                    "opening 1 fresh Incognito tab, and refreshing ONCE."
                )
                return {
                    "status": "open_incognito",
                    "task_id": self._task_id(next_customer),
                    "row_number": next_customer.row_number,
                    "contract": next_customer.contract,
                    "record_type": next_customer.record_type,
                    "search_number": next_customer.lookup_number,
                    "cycle_mode": "SERVICE_2",
                    "service_cycle_id": self.service_cycle_id,
                    "refresh_once": True,
                }
        else:
            # Next record is an Account Number
            if prev_mode == "SERVICE_2":
                log(
                    f"Service Number (2...) Cycle #{self.service_cycle_id} broken by Account Number "
                    f"(row {next_customer.row_number}, account {next_customer.lookup_number}): "
                    "Switching to ACCOUNT mode and reusing the tab (next 2... Service Number will start a fresh Incognito session)."
                )
            else:
                log(
                    f"Consecutive Account Number (row {next_customer.row_number}, "
                    f"account {next_customer.lookup_number}): Reusing the same tab in ACCOUNT mode."
                )
            self.active_cycle_mode = "ACCOUNT"

            if force_reopen_incognito:
                self._advance_attempt()
                self._begin_incognito_handoff()
                self.next_available_at = 0.0
                return {
                    "status": "open_incognito",
                    "task_id": self._task_id(next_customer),
                    "row_number": next_customer.row_number,
                    "contract": next_customer.contract,
                    "record_type": next_customer.record_type,
                    "search_number": next_customer.lookup_number,
                    "cycle_mode": "ACCOUNT",
                    "service_cycle_id": self.service_cycle_id,
                    "refresh_once": False,
                }

            self.next_available_at = time.monotonic() + retry_after_seconds
            return {
                "status": "waiting",
                "retry_after_seconds": retry_after_seconds,
                "checked": self.index,
                "total": len(self.customers),
                "cycle_mode": "ACCOUNT",
                "service_cycle_id": self.service_cycle_id,
                "refresh_once": False,
                "reuse_same_tab": True,
            }

    def _record_current_error(
        self,
        customer: Customer,
        error_type: str,
        details: str,
        reopen_incognito: bool = False,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            self.write_error(
                CheckError(
                    record_type=customer.record_type,
                    lookup_number=customer.lookup_number,
                    customer_name=customer.customer_name,
                    row_numbers=customer.row_numbers,
                    expected_amount=customer.expected_amount,
                    error_type=error_type,
                    details=details,
                    occurred_at=datetime.now(),
                    service_number=getattr(customer, "service_number", "") or "",
                    original_account_number=getattr(customer, "original_account_number", "") or "",
                )
            )
        except Exception as error:
            self.error = (
                f"Could not save the error for Excel row {customer.row_number} "
                f"to Excel: {error}"
            )
            self.finished.set()
            return {"status": "error", "message": self.error}

        worker_id = "Worker"
        task_idx = None
        if task_id and task_id in self.in_flight:
            worker_id = self.in_flight[task_id].get("worker_id", "Worker")
            task_idx = self.in_flight[task_id]["index"]
            del self.in_flight[task_id]
        else:
            for tid, tinfo in list(self.in_flight.items()):
                if tinfo["customer"].lookup_number == customer.lookup_number:
                    worker_id = tinfo.get("worker_id", "Worker")
                    task_idx = tinfo["index"]
                    del self.in_flight[tid]
                    break

        if task_idx is not None:
            self.completed_indices.add(task_idx)
        else:
            self.completed_indices.add(self.index)

        log(
            f"[{worker_id}] Recorded an error for row {customer.row_number}, "
            f"{customer.record_type} {customer.lookup_number}: {details} "
            f"({len(self.completed_indices)} of {len(self.customers)})."
        )
        is_complete = len(self.completed_indices) >= len(self.customers)
        try:
            self.save_progress(self.index, self.mismatches, is_complete, customer)
        except OSError as error:
            self.error = f"Could not save checking progress: {error}"
            self.finished.set()
            return {"status": "error", "message": self.error}

        if is_complete:
            self.completed = True
            self.finished.set()
            return {"status": "complete", "total": len(self.customers)}

        if reopen_incognito:
            next_idx = self.index
            next_cust = self.customers[next_idx] if next_idx < len(self.customers) else customer
            return {
                "status": "open_incognito",
                "task_id": self._task_id(next_cust, next_idx),
                "row_number": next_cust.row_number,
                "contract": next_cust.contract,
                "record_type": next_cust.record_type,
                "search_number": next_cust.lookup_number,
                "cycle_mode": self.active_cycle_mode,
                "service_cycle_id": self.service_cycle_id,
                "refresh_once": bool(self._is_service_2(next_cust)),
            }

        is_proxy_worker = bool(
            worker_id == "Worker 2 (Proxy)"
            or "proxy" in str(worker_id).lower()
        )
        retry_delay = (
            BETWEEN_CUSTOMERS_PROXY_DELAY_SECONDS
            if is_proxy_worker
            else BETWEEN_CUSTOMERS_DELAY_SECONDS
        )
        return {
            "status": "waiting",
            "retry_after_seconds": retry_delay,
            "checked": len(self.completed_indices),
            "total": len(self.customers),
        }

    def confirm_cycle_refreshed(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            cycle_id = payload.get("service_cycle_id")
            if cycle_id is None or int(cycle_id) == self.service_cycle_id:
                self.service_cycle_refreshed = True
                log(
                    f"Single page refresh (1x only) confirmed for Service Number (2...) "
                    f"Cycle #{self.service_cycle_id}. Consecutive 2... numbers will reuse this tab without refreshing."
                )
            return {
                "status": "cycle_refresh_acknowledged",
                "service_cycle_id": self.service_cycle_id,
                "service_cycle_refreshed": self.service_cycle_refreshed,
            }

    def confirm_incognito_handoff(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if self.completed:
                return {"status": "complete", "total": len(self.customers)}
            supplied_task_id = str(payload.get("task_id") or "")
            worker_id = payload.get("worker_id")

            tag = None
            if worker_id:
                _, tag = normalize_worker_id(worker_id)
            elif supplied_task_id and supplied_task_id in self.in_flight:
                _, tag = normalize_worker_id(self.in_flight[supplied_task_id].get("worker_id", ""))

            if tag:
                self.worker_handoff_started_at.pop(tag, None)
                self.worker_handoff_customers.pop(tag, None)
                self.worker_handoff_task_ids.pop(tag, None)
                self.worker_handoff_timeout_seconds.pop(tag, None)
                self.worker_handoff_recovery_attempts.pop(tag, None)
            else:
                self.worker_handoff_started_at.clear()
                self.worker_handoff_customers.clear()
                self.worker_handoff_task_ids.clear()
                self.worker_handoff_timeout_seconds.clear()
                self.worker_handoff_recovery_attempts.clear()

            log(
                f"Incognito handoff acknowledged by extension for task {supplied_task_id or 'active'} (worker: {tag or 'all'})."
            )
            return {
                "status": "handoff_acknowledged",
                "task_id": supplied_task_id,
            }

    def current_task_id(self) -> str:
        with self.lock:
            cust = self.customers[self.index] if self.index < len(self.customers) else self.customers[-1]
            return self._task_id(cust, self.index)

    def _task_id(self, customer: Customer, index: int | None = None) -> str:
        idx = self.index if index is None else index
        return (
            f"{self.run_id}:{idx}:{self.attempt_id}:"
            f"{customer.record_type}:{customer.row_number}:{customer.lookup_number}"
        )

    def _advance_attempt(self) -> None:
        self.attempt_id += 1

    def note_initial_incognito_launch(self, worker_id: str | None = None) -> None:
        with self.lock:
            from zain_checker.config import WORKERS_CONFIG
            if worker_id:
                self._begin_incognito_handoff(
                    INITIAL_CHROME_BOOTSTRAP_TIMEOUT_SECONDS,
                    worker_id=worker_id,
                )
            else:
                for w_cfg in WORKERS_CONFIG:
                    w_tag = w_cfg.get("worker_id", "worker_1")
                    self._begin_incognito_handoff(
                        INITIAL_CHROME_BOOTSTRAP_TIMEOUT_SECONDS,
                        worker_id=w_tag,
                    )

    def get_stalled_worker_handoffs(self) -> list[tuple[str, Customer, str]]:
        """Returns list of (worker_tag, customer, task_id) for workers whose handoff timed out."""
        with self.lock:
            if self.completed:
                return []
            now = time.monotonic()
            stalled: list[tuple[str, Customer, str]] = []
            for tag, started_at in list(self.worker_handoff_started_at.items()):
                timeout = self.worker_handoff_timeout_seconds.get(tag, self.incognito_handoff_timeout_seconds)
                if now - started_at >= timeout:
                    attempts = self.worker_handoff_recovery_attempts.get(tag, 0) + 1
                    self.worker_handoff_recovery_attempts[tag] = attempts
                    self.worker_handoff_started_at[tag] = now
                    cust = self.worker_handoff_customers.get(tag)
                    if cust is None:
                        cust = self.customers[self.index] if self.index < len(self.customers) else self.customers[-1]
                    t_id = self.worker_handoff_task_ids.get(tag) or self._task_id(cust)
                    stalled.append((tag, cust, t_id))
            return stalled

    def get_stalled_handoff_customer(self) -> Customer | None:
        stalled = self.get_stalled_worker_handoffs()
        if stalled:
            return stalled[0][1]
        return None

    def _restart_worker_instance(
        self,
        customer: Customer,
        task_id: str,
        worker_id: str,
    ) -> None:
        from zain_checker.chrome_manager import clean_extension_storage, get_worker_profile_dir

        canonical_name, worker_tag = normalize_worker_id(worker_id)
        w_proxy = resolve_worker_proxy(worker_id)

        time.sleep(0.3)
        terminate_worker_chrome_process(worker_tag)
        time.sleep(0.8)
        clean_extension_storage(get_worker_profile_dir(worker_tag))
        launch_initial_incognito_tab(customer, task_id, worker_id=worker_tag, proxy_server=w_proxy)
        log(f"[{canonical_name}] Fresh Chrome instance launched successfully for {customer.lookup_number}.")

    def _begin_incognito_handoff(
        self,
        timeout_seconds: int = INCOGNITO_HANDOFF_TIMEOUT_SECONDS,
        worker_id: str = "worker_1",
        customer: Customer | None = None,
        task_id: str | None = None,
    ) -> None:
        _, tag = normalize_worker_id(worker_id)
        now = time.monotonic()
        self.worker_handoff_started_at[tag] = now
        self.worker_handoff_timeout_seconds[tag] = timeout_seconds
        self.incognito_handoff_timeout_seconds = timeout_seconds
        if customer is not None:
            self.worker_handoff_customers[tag] = customer
        if task_id is not None:
            self.worker_handoff_task_ids[tag] = task_id

    def _register_incognito_reset(self) -> None:
        self.recent_incognito_reset_indices = [
            index
            for index in self.recent_incognito_reset_indices
            if index >= self.index - 4
        ]
        if self.index not in self.recent_incognito_reset_indices:
            self.recent_incognito_reset_indices.append(self.index)

    def _incognito_reset_limit_reached(self) -> bool:
        self.recent_incognito_reset_indices = [
            index
            for index in self.recent_incognito_reset_indices
            if index >= self.index - 4
        ]
        return len(self.recent_incognito_reset_indices) >= 2

    def _pause_for_session_recovery(
        self,
        customer: Customer,
        reason: str,
    ) -> dict[str, Any]:
        self.network_waiting = True
        self.network_retry_pending = False
        self.next_available_at = 0.0
        self.current_task_started_at = None
        self.network_change_requested.set()
        log(
            f"{reason} The checker is paused before row {customer.row_number}, "
            f"search number {customer.lookup_number}, and will send no more "
            "Zain requests until you confirm recovery."
        )
        return {
            "status": "network_wait",
            "task_id": self._task_id(customer),
            "row_number": customer.row_number,
            "contract": customer.contract,
            "record_type": customer.record_type,
            "search_number": customer.lookup_number,
            "checked": self.index,
            "total": len(self.customers),
            "retry_after_seconds": 3,
        }

    def log_estimate(self, page_load_seconds: float) -> None:
        self.pending_load_samples.append(max(0.0, page_load_seconds))
        if len(self.pending_load_samples) == 20:
            self.estimated_load_seconds = (
                sum(self.pending_load_samples) / len(self.pending_load_samples)
            )
            self.pending_load_samples.clear()

        if self.estimated_load_seconds is None:
            average_load_seconds = (
                sum(self.pending_load_samples) / len(self.pending_load_samples)
            )
        else:
            average_load_seconds = self.estimated_load_seconds

        remaining_accounts = len(self.customers) - self.index
        seconds_per_account = (
            average_load_seconds + BETWEEN_CUSTOMERS_DELAY_SECONDS
        )
        estimated_remaining_seconds = seconds_per_account * remaining_accounts
        log(
            f"Estimate: {remaining_accounts} record(s) left; "
            f"{format_duration(estimated_remaining_seconds)} remaining."
        )


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, state: CheckerState) -> None:
        super().__init__((BRIDGE_HOST, BRIDGE_PORT), BridgeRequestHandler)
        self.state = state


class BridgeRequestHandler(BaseHTTPRequestHandler):
    server: BridgeServer

    def do_OPTIONS(self) -> None:
        origin = self.headers.get("Origin", "")
        if origin and not origin.startswith("chrome-extension://"):
            self.send_error(403)
            return
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        if not self._authorized():
            self._write_json(403, {"status": "error", "message": "Forbidden."})
            return

        parsed = urlparse(self.path)
        if parsed.path != "/task":
            self._write_json(404, {"status": "error", "message": "Not found."})
            return

        worker_id = self.headers.get("X-Worker-Id")
        if not worker_id:
            from urllib.parse import parse_qs
            qs = parse_qs(parsed.query)
            worker_id = qs.get("worker", [None])[0]

        if not worker_id:
            worker_id = "Worker 1 (Router)"

        self._write_json(200, self.server.state.get_task(worker_id=worker_id))

    def do_POST(self) -> None:
        if not self._authorized():
            self._write_json(403, {"status": "error", "message": "Forbidden."})
            return

        request_path = urlparse(self.path).path
        if request_path not in ("/result", "/handoff-ready", "/cycle-refreshed"):
            self._write_json(404, {"status": "error", "message": "Not found."})
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > 65_536:
                raise ValueError("Invalid request size.")
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("The request body must be a JSON object.")
            worker_id = self.headers.get("X-Worker-Id")
            if worker_id and "worker_id" not in payload:
                payload["worker_id"] = worker_id
            if request_path == "/result":
                response = self.server.state.submit_result(payload)
            elif request_path == "/handoff-ready":
                response = self.server.state.confirm_incognito_handoff(payload)
            else:
                response = self.server.state.confirm_cycle_refreshed(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            log(f"Bridge POST {request_path} error: {error}")
            self._write_json(400, {"status": "error", "message": str(error)})
            return

        self._write_json(200, response)

    def log_message(self, format_string: str, *args: object) -> None:
        return

    def _authorized(self) -> bool:
        supplied_token = self.headers.get("X-Zain-Bridge-Token", "")
        return hmac.compare_digest(supplied_token, BRIDGE_TOKEN)

    def _send_cors_headers(self) -> None:
        origin = self.headers.get("Origin", "")
        if origin.startswith("chrome-extension://"):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, X-Zain-Bridge-Token, X-Worker-Id",
        )
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _write_json(self, status_code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def format_duration(total_seconds: float) -> str:
    rounded_seconds = max(0, round(total_seconds))
    days, remainder = divmod(rounded_seconds, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, seconds = divmod(remainder, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def find_chrome_executable() -> str:
    path_from_command = shutil.which("chrome") or shutil.which("chrome.exe")
    if path_from_command:
        return path_from_command

    candidates = [
        Path(os.environ.get("PROGRAMFILES", ""))
        / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", ""))
        / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Google/Chrome/Application/chrome.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)

    raise RuntimeError(
        "لم يتم العثور على متصفح جوجل كروم على هذا الجهاز! يرجى تثبيت Google Chrome أولاً للبدء بالفحص (Google Chrome was not found)."
    )


def normalize_worker_id(worker_id: Any) -> tuple[str, str]:
    """Returns (canonical_display_name, tag) e.g. ('Worker 2 (Proxy)', 'worker_2')"""
    if isinstance(worker_id, bool):
        return ("Worker 2 (Proxy)", "worker_2") if worker_id else ("Worker 1 (Router)", "worker_1")
    w_str = str(worker_id or "").lower()
    if "3" in w_str:
        return ("Worker 3 (Proxy)", "worker_3")
    if "2" in w_str:
        return ("Worker 2 (Proxy)", "worker_2")
    if "proxy" in w_str:
        return ("Worker 3 (Proxy)", "worker_3")
    return ("Worker 1 (Router)", "worker_1")


def resolve_worker_proxy(worker_id: Any) -> str | None:
    """Resolves proxy for any worker from WORKERS_CONFIG. Returns None to use direct network."""
    from zain_checker.config import WORKERS_CONFIG, ZAIN_PROXY_SERVER
    raw_id = str(worker_id or "").strip()
    for w in WORKERS_CONFIG:
        if str(w.get("worker_id", "")).strip() == raw_id:
            proxy = w.get("proxy")
            return str(proxy).strip() if proxy else None
    canonical_name, tag = normalize_worker_id(worker_id)
    for w in WORKERS_CONFIG:
        w_id = str(w.get("worker_id", "")).strip()
        w_canon, w_tag = normalize_worker_id(w_id)
        if w_tag == tag or w_canon == canonical_name:
            proxy = w.get("proxy")
            return str(proxy).strip() if proxy else None
    if tag in ("worker_2", "worker_3") and "proxy" in str(worker_id).lower():
        return ZAIN_PROXY_SERVER
    return None


from zain_checker.chrome_manager import get_worker_profile_dir

def checker_chrome_arguments(worker_id: str, proxy_server: str | None = None) -> list[str]:
    profile_dir = get_worker_profile_dir(worker_id)
    args = [
        f"--user-data-dir={profile_dir}",
        f"--load-extension={CHROME_EXTENSION_DIRECTORY}",
        "--blink-settings=imagesEnabled=false",
        "--disk-cache-size=209715200",
        "--mute-audio",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-domain-reliability",
        "--disable-sync",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if proxy_server:
        args.append(f"--proxy-server={proxy_server}")
        args.append("--proxy-bypass-list=127.0.0.1;localhost;::1;<local>")
        # Cybersecurity hardening for proxy workers against WAF / F5 Bot Defense fingerprinting:
        # 1. Enforce WebRTC leak isolation so local domestic IP / STUN candidates are never exposed
        args.append("--force-webrtc-ip-handling-policy=disable_non_proxied_udp")
        args.append("--enforce-webrtc-ip-permission-check")
        args.append("--disable-features=WebRtcHideLocalIpsWithMdns")
        # 2. Prevent local DNS resolution leaks (route DNS lookups through remote proxy)
        args.append("--disable-async-dns")
    return args


def launch_initial_incognito_tab(
    customer: Customer,
    task_id: str,
    worker_id: str = "worker_1",
    proxy_server: str | None = None,
    use_proxy: bool = False,
    **kwargs: Any,
) -> None:
    canonical_name, worker_tag = normalize_worker_id(worker_id)
    if not proxy_server:
        proxy_server = resolve_worker_proxy(worker_id)
    chrome_executable = find_chrome_executable()
    if customer.record_type == "account":
        startup_url = (
            f"{ZAIN_CONTRACT_PAYMENT_URL}?contract={quote(customer.lookup_number)}&language=ar#worker={worker_tag}"
        )
    else:
        startup_url = f"{ZAIN_QUICKPAY_URL}?account={quote(customer.lookup_number)}#worker={worker_tag}"
    subprocess.Popen(
        [
            chrome_executable,
            *checker_chrome_arguments(worker_tag, proxy_server),
            "--incognito",
            "--new-window",
            startup_url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def launch_initial_tabs_for_workers(
    customers: list[Customer],
    state: "CheckerState",
) -> None:
    from zain_checker.config import WORKERS_CONFIG
    from zain_checker.chrome_manager import ensure_worker_profile_ready
    
    ensure_checker_chrome_profile_ready(interactive=False)
    
    with state.lock:
        for i, w_config in enumerate(WORKERS_CONFIG):
            w_id = w_config.get("worker_id", f"worker_{i+1}")
            canonical_name, worker_tag = normalize_worker_id(w_id)
            w_proxy = resolve_worker_proxy(w_id)
            
            ensure_worker_profile_ready(worker_tag)
            
            c_idx = state.index + i
            if c_idx >= len(customers):
                c_idx = state.index
            cust = customers[c_idx]
            task_id = state._task_id(cust, c_idx)
            
            # Pre-lease the task in in_flight so when this worker extension polls /task,
            # it receives the EXACT customer that was opened in its browser window!
            state.in_flight[task_id] = {
                "index": c_idx,
                "customer": cust,
                "worker_id": canonical_name,
                "leased_at": time.monotonic(),
                "recheck_stage": 0,
                "redirect_retry": False,
                "incognito_retry": False,
            }
            state.assigned_index = max(state.assigned_index, c_idx + 1)
            
            launch_initial_incognito_tab(cust, task_id, worker_id=worker_tag, proxy_server=w_proxy)
            log(f"[{canonical_name}] Initialized Chrome on row {cust.row_number} ({cust.lookup_number}).")
            state._begin_incognito_handoff(
                INITIAL_CHROME_BOOTSTRAP_TIMEOUT_SECONDS,
                worker_id=worker_tag,
                customer=cust,
                task_id=task_id,
            )
    
    log(f"Multi-Worker Mode Active: Spawned {len(WORKERS_CONFIG)} Chrome windows dynamically.")



from zain_checker.chrome_manager import (
    ensure_checker_chrome_profile_ready,
    ensure_proxy_profile_ready,
    mark_checker_chrome_profile_ready,
    terminate_checker_chrome_processes,
    terminate_worker_chrome_process,
    clean_extension_storage,
    get_worker_profile_dir,
)


def accounts_signature(customers: list[Customer]) -> str:
    digest = hashlib.sha256()
    legacy_account_only = all(
        customer.progress_source == "account"
        and customer.record_type == "account"
        and len(customer.row_numbers) == 1
        for customer in customers
    )
    for customer in customers:
        if legacy_account_only:
            line = (
                f"{customer.row_number}\t{customer.contract}\t"
                f"{customer.expected_amount}\n"
            )
        else:
            rows = ",".join(str(number) for number in customer.row_numbers)
            line = (
                f"{customer.progress_source}\t{customer.record_type}\t{rows}\t"
                f"{customer.lookup_number}\t"
                f"{customer.expected_amount}\n"
            )
        digest.update(line.encode("utf-8"))
    return digest.hexdigest()


def write_checkpoint(
    progress: dict[str, Any],
    mismatches: list[ProgressMismatch],
    checkpoint_path: Path | None = None,
) -> None:
    target_path = checkpoint_path or CHECKPOINT_PATH
    payload = {
        "version": 6,
        "workbook_name": progress["workbook_name"],
        "sources": progress["sources"],
        "mismatches": [
            {
                "sequence_index": item.sequence_index,
                "expected_amount": item.expected_amount,
                "website_amount": item.website_amount,
            }
            for item in mismatches
        ],
    }
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    temp_path = target_path.with_suffix(".json.tmp")
    bak_path = target_path.with_suffix(".json.bak")

    # 1. Atomic write to temporary file with explicit flush and fsync
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        # Fallback direct write if temp file cannot be created
        target_path.write_text(content, encoding="utf-8")
        return

    # 2. Maintain a backup copy of previous valid checkpoint before replacing
    if target_path.exists():
        try:
            if target_path.stat().st_size > 0:
                shutil.copy2(target_path, bak_path)
        except Exception:
            pass

    # 3. Atomic replacement to prevent zero-byte/null-byte file on abrupt shutdown
    try:
        os.replace(temp_path, target_path)
    except Exception:
        target_path.write_text(content, encoding="utf-8")
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass


def read_checkpoint(checkpoint_path: Path | None = None) -> dict[str, Any]:
    target_path = checkpoint_path or CHECKPOINT_PATH
    bak_path = target_path.with_suffix(".json.bak")

    def _try_parse(path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            raw = path.read_text(encoding="utf-8")
            cleaned = raw.replace("\x00", "").strip()
            if not cleaned:
                return None
            data = json.loads(cleaned)
            if isinstance(data, dict) and data.get("version") in (2, 3, 4, 5, 6):
                return data
        except Exception:
            return None
        return None

    # 1. Attempt reading the primary checkpoint file
    payload = _try_parse(target_path)
    if payload is not None:
        return payload

    # 2. If primary was corrupt or empty, attempt recovering from backup
    if bak_path.exists():
        backup_payload = _try_parse(bak_path)
        if backup_payload is not None:
            try:
                write_checkpoint(backup_payload, parse_saved_mismatches(backup_payload), checkpoint_path=target_path)
                log("تم استعادة سجل التقدم بنجاح من النسخة الاحتياطية.")
            except Exception:
                pass
            return backup_payload

    # 3. If primary file exists but is corrupt/null-filled, archive it safely
    if target_path.exists():
        try:
            corrupt_dest = target_path.with_name(f"{target_path.stem}.corrupt_{int(time.time())}.json")
            os.replace(target_path, corrupt_dest)
            log(f"تم نقل ملف التقدم التالف للأرشيف: {corrupt_dest.name}")
        except Exception:
            pass

    raise RuntimeError("The saved checking progress is empty or corrupted, and no valid backup was found.")


def selected_source_names(source_mode: str) -> list[str]:
    if source_mode == "account":
        return ["account"]
    if source_mode == "wallet":
        return ["wallet"]
    if source_mode == "both":
        return ["account", "wallet"]
    if source_mode == "custom":
        return ["custom"]
    raise RuntimeError("The selected data source is invalid.")


def source_display_name(source: str) -> str:
    if source == "account":
        return "Account Number"
    if source == "wallet":
        return "Wallet Number"
    if source == "custom":
        return "Custom Sheet"
    return source


def new_source_progress(customers: list[Customer], direction: str = "forward") -> dict[str, Any]:
    return {
        "signature": accounts_signature(customers),
        "total": len(customers),
        "completed": [],
        "direction": direction,
    }


def parse_saved_mismatches(checkpoint: dict[str, Any]) -> list[ProgressMismatch]:
    raw_mismatches = checkpoint.get("mismatches", [])
    if not isinstance(raw_mismatches, list):
        raise RuntimeError("The saved mismatch data is invalid.")

    parsed: list[ProgressMismatch] = []
    checkpoint_version = checkpoint.get("version")
    for item in raw_mismatches:
        if not isinstance(item, dict):
            raise RuntimeError("The saved mismatch data is invalid.")
        expected_amount = item.get("expected_amount")
        website_amount = item.get("website_amount")
        if (
            isinstance(expected_amount, bool)
            or not isinstance(expected_amount, int)
            or isinstance(website_amount, bool)
            or not isinstance(website_amount, int)
        ):
            raise RuntimeError("The saved mismatch amount is invalid.")
        sequence_index = (
            item.get("sequence_index")
            if checkpoint_version in (3, 4, 5, 6)
            else None
        )
        if sequence_index is not None and (
            isinstance(sequence_index, bool)
            or not isinstance(sequence_index, int)
            or sequence_index < 0
        ):
            raise RuntimeError("The saved mismatch position is invalid.")
        parsed.append(
            ProgressMismatch(sequence_index, expected_amount, website_amount)
        )
    return parsed


def load_independent_progress(
    workbook_name: str,
    records_by_source: dict[str, list[Customer]],
    checkpoint_path: Path | None = None,
) -> tuple[dict[str, Any], list[ProgressMismatch], dict[str, str]]:
    target_path = checkpoint_path or CHECKPOINT_PATH
    empty_progress = {
        "workbook_name": workbook_name,
        "sources": {
            source: new_source_progress(records)
            for source, records in records_by_source.items()
        },
    }
    if not target_path.exists():
        return empty_progress, [], {}

    try:
        checkpoint = read_checkpoint(target_path)
        mismatches = parse_saved_mismatches(checkpoint)
    except Exception as exc:
        log(f"تنبيه: تعذر قراءة سجل التقدم المحفوظ ({exc}). سيتم بدء فحص جديد بأمان لتجنب التوقف.")
        return empty_progress, [], {"checkpoint": "progress file was unreadable"}
    issues: dict[str, str] = {}

    if checkpoint.get("version") == 6:
        raw_sources = checkpoint.get("sources")
        if not isinstance(raw_sources, dict):
            raise RuntimeError("The saved source progress is invalid.")

        progress = {
            "workbook_name": workbook_name,
            "sources": {
                source: dict(raw_state)
                for source, raw_state in raw_sources.items()
                if isinstance(source, str) and isinstance(raw_state, dict)
            },
        }
        for source, records in records_by_source.items():
            raw_state = raw_sources.get(source)
            if not isinstance(raw_state, dict):
                progress["sources"][source] = new_source_progress(records)
                continue

            completed = raw_state.get("completed", [])
            direction = raw_state.get("direction", "forward")
            if (
                not isinstance(completed, list)
                or not all(isinstance(value, str) and value.isdigit() for value in completed)
                or direction not in ("forward", "reverse")
            ):
                raise RuntimeError(
                    f"The saved {source_display_name(source)} progress is invalid."
                )
            progress["sources"][source] = {
                "signature": raw_state.get("signature"),
                "total": raw_state.get("total"),
                "completed": list(dict.fromkeys(completed)),
                "direction": direction,
            }
            if checkpoint.get("workbook_name") != workbook_name:
                issues[source] = "the source workbook changed"
            elif (
                raw_state.get("signature") != accounts_signature(records)
                or raw_state.get("total") != len(records)
            ):
                issues[source] = "the sheet data or row order changed"
        return progress, mismatches, issues

    legacy_source_mode = checkpoint.get("source_mode", "account")
    legacy_sources = selected_source_names(legacy_source_mode)
    legacy_customers = [
        customer
        for source in legacy_sources
        for customer in records_by_source.get(source, [])
    ]
    direction = checkpoint.get("direction", "forward")
    next_index = checkpoint.get("next_index")
    if direction not in ("forward", "reverse"):
        raise RuntimeError("The saved checking direction is invalid.")
    if (
        isinstance(next_index, bool)
        or not isinstance(next_index, int)
        or not 0 <= next_index <= len(legacy_customers)
    ):
        raise RuntimeError("The saved checking position is invalid.")

    legacy_matches = (
        checkpoint.get("workbook_name") == workbook_name
        and checkpoint.get("accounts_signature") == accounts_signature(legacy_customers)
        and checkpoint.get("total", len(legacy_customers)) == len(legacy_customers)
    )
    if not legacy_matches:
        for source in legacy_sources:
            issues[source] = "the older saved progress does not match the current sheet"
        return empty_progress, mismatches, issues

    ordered_legacy = (
        legacy_customers
        if direction == "forward"
        else list(reversed(legacy_customers))
    )
    completed_legacy = ordered_legacy[:next_index]
    for source in legacy_sources:
        if source not in empty_progress["sources"]:
            continue
        source_state = empty_progress["sources"][source]
        source_state["direction"] = direction
        source_state["completed"] = [
            customer.lookup_number
            for customer in completed_legacy
            if customer.record_type == source
        ]
    log("Migrated the older saved position to independent Account and Wallet progress.")
    return empty_progress, mismatches, issues


def ask_workbook_choice() -> Path:
    candidates = sorted(
        path
        for path in PROJECT_DIRECTORY.iterdir()
        if path.is_file()
        and path.suffix.lower() == ".xlsx"
        and not path.name.startswith("~$")
        and path.name != "نتائج فحص زين.xlsx"
    )
    if not candidates:
        raise RuntimeError(f"No Excel files found in {PROJECT_DIRECTORY}.")
    
    if len(candidates) == 1:
        log(f"Auto-selected the only Excel file: {candidates[0].name}")
        return candidates[0]
        
    log("Choose the Excel file to process:")
    for number, path in enumerate(candidates, start=1):
        log(f"  {number}. {path.name}")
        
    while True:
        answer = input(
            format_console_message(
                f"Select file 1-{len(candidates)}, or enter a full path to an Excel file: "
            )
        ).strip()
        
        # Default to 1 if empty? Let's just require explicit choice to be safe.
        if answer.isdigit() and 1 <= int(answer) <= len(candidates):
            return candidates[int(answer) - 1]
            
        # Check if it's a valid path
        if answer.lower().endswith('.xlsx'):
            custom_path = Path(answer)
            if custom_path.is_file():
                return custom_path
                
        log(f"Please select a number from 1 to {len(candidates)}, or enter a valid .xlsx file path.")


def ask_collector_choice() -> str:
    log("Choose the target collector:")
    log("  1. Maha")
    log("  2. Azza")
    log("  3. Mawada")
    log("  4. Custom (Enter name manually)")
    log("  5. All the sheet")
    while True:
        answer = input(format_console_message("Select 1, 2, 3, 4, or 5: ")).strip()
        if answer == "1":
            return "مها امير حسين محمد-M02253"
        if answer == "2":
            return "عزة مغربى محمد عبدالرحمن-M02254"
        if answer == "3":
            return "مودة يحي تاج الدين فضيل-M02051"
        if answer == "4":
            while True:
                custom_name = input(format_console_message("Enter the exact collector name: ")).strip()
                if custom_name:
                    return custom_name
                log("Name cannot be empty.")
        if answer == "5":
            return ""
        log("Please select 1, 2, 3, 4, or 5.")

def ask_source_choice() -> tuple[str, int | None, int | None]:
    while True:
        log("Choose what to check:")
        log("  1. Account Number sheet (default)")
        log("  2. Wallet Number sheet")
        log("  3. Both sheets")
        log("  4. Custom sheet, lookup column, and URL type")
        log("  (Tip: Add 'm' for min, 'x' for max, or 'f' for both, e.g., '3f')")
        answer = input(
            format_console_message(
                "Select 1-4 (with optional m/x/f), or press Enter for option 1: "
            )
        ).strip().lower()
        
        ask_min = False
        ask_max = False
        
        if answer.endswith('f'):
            ask_min = ask_max = True
            answer = answer[:-1].strip()
        elif answer.endswith('m'):
            ask_min = True
            answer = answer[:-1].strip()
        elif answer.endswith('x'):
            ask_max = True
            answer = answer[:-1].strip()

        if answer in ("", "1"):
            source_mode = "account"
        elif answer == "2":
            source_mode = "wallet"
        elif answer == "3":
            source_mode = "both"
        elif answer == "4":
            source_mode = "custom"
        else:
            log("Please select a number from 1 to 4.")
            continue
            
        min_amount_halalas = None
        max_amount_halalas = None
        
        if ask_min:
            while True:
                amount_str = input(format_console_message("Enter the MINIMUM amount in SAR (e.g., 5000): ")).strip()
                try:
                    min_amount_halalas = money_to_halalas(amount_str)
                    break
                except ValueError:
                    log("Please enter a valid amount.")
                    
        if ask_max:
            while True:
                amount_str = input(format_console_message("Enter the MAXIMUM amount in SAR (e.g., 10000): ")).strip()
                try:
                    max_amount_halalas = money_to_halalas(amount_str)
                    break
                except ValueError:
                    log("Please enter a valid amount.")
                    
        return source_mode, min_amount_halalas, max_amount_halalas


def excel_column_name(column_number: int) -> str:
    name = ""
    value = column_number
    while value > 0:
        value, remainder = divmod(value - 1, 26)
        name = chr(65 + remainder) + name
    return name


def parse_excel_column(value: str) -> int | None:
    normalized = value.strip().upper()
    if normalized.isdigit():
        number = int(normalized)
        return number if number > 0 else None
    if not normalized.isalpha():
        return None
    number = 0
    for character in normalized:
        number = number * 26 + ord(character) - 64
    return number or None


def _prompt_for_columns(
    header_by_column: dict[int, str], target_collector: str
) -> tuple[int, int, int | None, int | None, str, int | None, bool, bool]:
    while True:
        log("Choose the Zain URL type:")
        log("  1. Contract payment URL (?contract=NUMBER)")
        log("  2. Quick-pay URL (?account=NUMBER)")
        log("  3. Mixed Mode (Smart check: 1,5,7,8 -> Account, 2 -> Wallet)")
        answer = input(format_console_message("Select URL type 1, 2, or 3: ")).strip()
        if answer == "1":
            record_type = "account"
            break
        if answer == "2":
            record_type = "wallet"
            break
        if answer == "3":
            record_type = "mixed"
            break
        log("Please select 1, 2, or 3.")

    service_column = None
    if record_type == "mixed":
        while True:
            answer = input(
                format_console_message("Select the Service Number column (رقم الخدمة) by letter or number: ")
            )
            service_column = parse_excel_column(answer)
            if service_column is not None and service_column in header_by_column and header_by_column[service_column]:
                break
            log("Please select one of the displayed non-empty columns.")

    while True:
        prompt_text = (
            "Select the Account / Contract column (رقم الحساب / العقد) by letter or number: "
            if record_type == "mixed"
            else "Select the lookup-number column by letter or number: "
        )
        answer = input(format_console_message(prompt_text))
        lookup_column = parse_excel_column(answer)
        if lookup_column is not None and lookup_column in header_by_column and header_by_column[lookup_column]:
            break
        log("Please select one of the displayed non-empty columns.")

    while True:
        answer = input(
            format_console_message("Select the expected-amount column by letter or number: ")
        )
        amount_column = parse_excel_column(answer)
        if amount_column is not None and amount_column in header_by_column and header_by_column[amount_column]:
            break
        log("Please select one of the displayed non-empty columns.")

    no_customer_ans = input(format_console_message("Check without customer name? (y/n, default n): ")).strip().lower()
    no_customer = no_customer_ans in ("y", "yes")
    customer_column = None
    if not no_customer:
        while True:
            answer = input(
                format_console_message("Select the customer name column by letter or number: ")
            )
            customer_column = parse_excel_column(answer)
            if customer_column is not None and customer_column in header_by_column and header_by_column[customer_column]:
                break
            log("Please select one of the displayed non-empty columns.")

    no_collector_ans = input(format_console_message("Check without collector name (check all records)? (y/n, default n): ")).strip().lower()
    no_collector = no_collector_ans in ("y", "yes")
    collector_column = None
    if not no_collector and target_collector:
        while True:
            answer = input(format_console_message("Select the collector column by letter or number: ")).strip()
            collector_column = parse_excel_column(answer)
            if collector_column is not None and collector_column in header_by_column:
                break
            log("Please select a valid column.")

    return lookup_column, amount_column, customer_column, collector_column, record_type, service_column, no_customer, no_collector


def ask_custom_source(workbook_path: Path, target_collector: str) -> list[CustomSourceChoice]:
    layout = read_workbook_layout(workbook_path)
    if not layout:
        raise RuntimeError("The Excel workbook contains no worksheets.")

    log(f"The workbook contains {len(layout)} worksheets:")
    for number, (sheet_name, _) in enumerate(layout, start=1):
        log(f"  {number}. {sheet_name}")

    while True:
        answer = input(
            format_console_message("Enter sheet numbers to check (comma-separated, e.g., 1,3), or 'all': ")
        ).strip().lower()
        if answer == 'all':
            sheet_indices = list(range(len(layout)))
            break
        
        parts = [p.strip() for p in answer.split(',')]
        if all(p.isdigit() and 1 <= int(p) <= len(layout) for p in parts if p):
            sheet_indices = [int(p) - 1 for p in parts if p]
            if sheet_indices:
                sheet_indices = list(dict.fromkeys(sheet_indices))
                break
        log(f"Please enter valid numbers from 1 to {len(layout)}, or 'all'.")

    same_config = True
    if len(sheet_indices) > 1:
        while True:
            answer = input(
                format_console_message("Do all selected sheets use the same column structure and URL type? (y/n): ")
            ).strip().lower()
            if answer in ("y", "yes"):
                same_config = True
                break
            if answer in ("n", "no"):
                same_config = False
                break
            log("Please enter y or n.")

    choices: list[CustomSourceChoice] = []
    
    if same_config:
        log("Configuring columns for all selected sheets...")
        first_sheet_name, columns = layout[sheet_indices[0]]
        populated_columns = [item for item in columns if item[1]]
        log(f'Available columns in worksheet "{first_sheet_name}" (as reference):')
        for offset in range(0, len(populated_columns), 4):
            group = populated_columns[offset : offset + 4]
            log("  " + " | ".join(f"{excel_column_name(number)} ({number}): {header}" for number, header in group))

        lookup_column, amount_column, customer_column, collector_column, record_type, service_column, no_customer, no_collector = _prompt_for_columns(dict(columns), target_collector)
        
        for idx in sheet_indices:
            sheet_name, columns = layout[idx]
            header_by_column = dict(columns)
            choices.append(CustomSourceChoice(
                sheet_index=idx,
                sheet_name=sheet_name,
                lookup_column=lookup_column,
                lookup_header=header_by_column.get(lookup_column, ""),
                amount_column=amount_column,
                amount_header=header_by_column.get(amount_column, ""),
                customer_column=customer_column,
                customer_header=header_by_column.get(customer_column, "") if customer_column else "",
                collector_column=collector_column,
                record_type=record_type,
                service_column=service_column,
                no_customer=no_customer,
                no_collector=no_collector,
            ))
    else:
        for idx in sheet_indices:
            sheet_name, columns = layout[idx]
            log(f'\nConfiguring worksheet "{sheet_name}":')
            populated_columns = [item for item in columns if item[1]]
            for offset in range(0, len(populated_columns), 4):
                group = populated_columns[offset : offset + 4]
                log("  " + " | ".join(f"{excel_column_name(number)} ({number}): {header}" for number, header in group))
            
            lookup_column, amount_column, customer_column, collector_column, record_type, service_column, no_customer, no_collector = _prompt_for_columns(dict(columns), target_collector)
            header_by_column = dict(columns)
            choices.append(CustomSourceChoice(
                sheet_index=idx,
                sheet_name=sheet_name,
                lookup_column=lookup_column,
                lookup_header=header_by_column.get(lookup_column, ""),
                amount_column=amount_column,
                amount_header=header_by_column.get(amount_column, ""),
                customer_column=customer_column,
                customer_header=header_by_column.get(customer_column, "") if customer_column else "",
                collector_column=collector_column,
                record_type=record_type,
                service_column=service_column,
                no_customer=no_customer,
                no_collector=no_collector,
            ))
            
    if same_config:
        c = choices[0]
        url_label = "3 (mixed)" if c.record_type == "mixed" else ("1 (contract)" if c.record_type == "account" else "2 (quick pay)")
        cust_label = excel_column_name(c.customer_column) if c.customer_column else "None"
        log(f'Custom source selected for {len(choices)} sheet(s). '
            f'Lookup col {excel_column_name(c.lookup_column)}, '
            f'amount col {excel_column_name(c.amount_column)}, '
            f'customer col {cust_label}, URL type {url_label}.')
    else:
        log(f"Custom source configured for {len(choices)} sheets individually.")

    return choices


def ask_start_choice() -> tuple[str, int | None]:
    while True:
        log("Choose where to start:")
        log("  1. Continue from saved progress (default)")
        log("  2. Start new from the beginning")
        log("  3. Continue from a search number (account or service number)")
        log("  4. Continue from an Excel row number")
        log("  5. Continue from a customer number")
        log("  6. Start new from the end of the sheet (bottom to top)")
        answer = input(
            format_console_message(
                "Select 1-6, or press Enter for option 1: "
            )
        ).strip().lower()
        if answer in ("", "1", "yes", "y"):
            return "continue", None
        if answer in ("2", "no", "n"):
            return "new", None
        if answer == "3":
            return "search", ask_positive_integer("Enter the search number: ")
        if answer == "4":
            return "row", ask_positive_integer("Enter the Excel row number: ")
        if answer == "5":
            return "customer", ask_positive_integer("Enter the customer number: ")
        if answer == "6":
            return "end", None

        log("Please select a number from 1 to 6.")


def ask_positive_integer(prompt: str) -> int:
    while True:
        answer = input(format_console_message(prompt)).strip()
        if re.fullmatch(r"[0-9]+", answer) and int(answer) > 0:
            return int(answer)
        log("Please enter a positive whole number.")


def resolve_manual_position(
    mode: str,
    value: int,
    customers: list[Customer],
) -> int:
    if mode == "customer":
        if not 1 <= value <= len(customers):
            raise RuntimeError(
                f"Customer number must be between 1 and {len(customers)}."
            )
        return value - 1

    if mode == "row":
        matches = [
            index
            for index, customer in enumerate(customers)
            if value in customer.row_numbers
        ]
        if not matches:
            raise RuntimeError(
                f"Excel row {value} is not one of the selected collector's customers."
            )
        if len(matches) > 1:
            return resolve_ambiguous_position(matches, customers, f"Excel row {value}")
        return matches[0]

    matches = [
        index
        for index, customer in enumerate(customers)
        if customer.lookup_number == str(value)
    ]
    if not matches:
        raise RuntimeError(
            f"Search number {value} was not found in the selected customers."
        )
    if len(matches) > 1:
        return resolve_ambiguous_position(matches, customers, f"Search number {value}")
    return matches[0]


def resolve_ambiguous_position(
    matches: list[int],
    customers: list[Customer],
    description: str,
) -> int:
    available = {customers[index].record_type: index for index in matches}
    if set(available) != {"account", "wallet"}:
        raise RuntimeError(f"{description} is ambiguous in the selected records.")

    while True:
        log(f"{description} exists in both selected sheets:")
        log("  1. Account Number sheet")
        log("  2. Wallet Number sheet")
        answer = input(
            format_console_message("Select 1-2 to identify the record: ")
        ).strip()
        if answer == "1":
            return available["account"]
        if answer == "2":
            return available["wallet"]
        log("Please select 1 or 2.")


def ordered_selected_records(
    records_by_source: dict[str, list[Customer]],
    progress: dict[str, Any],
    source_mode: str,
) -> list[Customer]:
    selected = selected_source_names(source_mode)
    if (
        source_mode == "both"
        and all(
            progress["sources"][source]["direction"] == "reverse"
            for source in selected
        )
    ):
        selected = ["wallet", "account"]

    ordered: list[Customer] = []
    for source in selected:
        records = records_by_source[source]
        if progress["sources"][source]["direction"] == "reverse":
            records = list(reversed(records))
        ordered.extend(records)
    return ordered


def show_saved_progress(
    progress: dict[str, Any],
    issues: dict[str, str],
    records_by_source: dict[str, list[Customer]],
    source_mode: str,
) -> None:
    log("Saved progress for the selected scope:")
    for source in selected_source_names(source_mode):
        total = len(records_by_source[source])
        if source in issues:
            log(
                f"  {source_display_name(source)}: cannot continue because "
                f"{issues[source]}."
            )
            continue
        valid_keys = {customer.lookup_number for customer in records_by_source[source]}
        completed = len(
            valid_keys.intersection(progress["sources"][source]["completed"])
        )
        log(
            f"  {source_display_name(source)}: {completed} of {total} completed; "
            f"{total - completed} remaining."
        )


def prepare_run(
    workbook_name: str,
    records_by_source: dict[str, list[Customer]],
    source_mode: str,
    start_choice: tuple[str, int | None] | None = None,
    checkpoint_path: Path | None = None,
) -> tuple[list[Customer], list[ProgressMismatch], dict[str, Any], str]:
    progress, mismatches, issues = load_independent_progress(
        workbook_name,
        records_by_source,
        checkpoint_path=checkpoint_path,
    )
    if start_choice is None or (start_choice and start_choice[0] == "continue"):
        show_saved_progress(progress, issues, records_by_source, source_mode)
    if start_choice is not None:
        mode, manual_value = start_choice
    else:
        mode, manual_value = ask_start_choice()
    selected = selected_source_names(source_mode)

    if mode in ("new", "end"):
        direction = "reverse" if mode == "end" else "forward"
        for source in selected:
            progress["sources"][source] = new_source_progress(
                records_by_source[source],
                direction,
            )
            issues.pop(source, None)
        action = (
            "Starting new from the end; only the selected source progress was reset."
            if mode == "end"
            else "Starting new from the beginning; only the selected source progress was reset."
        )
        log(action)
    elif mode in ("search", "row", "customer"):
        for source in selected:
            if source in issues:
                log(
                    f"Resetting incompatible {source_display_name(source)} progress "
                    "before using the manual starting position."
                )
                progress["sources"][source] = new_source_progress(
                    records_by_source[source]
                )
                issues.pop(source, None)
            progress["sources"][source]["direction"] = "forward"
    else:
        incompatible = [source for source in selected if source in issues]
        if incompatible:
            names = ", ".join(source_display_name(source) for source in incompatible)
            raise RuntimeError(
                f"Cannot continue saved progress for {names} because its sheet data "
                "changed. Run again and select Start new for that scope."
            )

    ordered = ordered_selected_records(records_by_source, progress, source_mode)
    selected_directions = {
        progress["sources"][source]["direction"] for source in selected
    }
    direction = (
        next(iter(selected_directions))
        if len(selected_directions) == 1
        else "mixed"
    )

    if mode == "continue":
        completed_by_source = {
            source: set(progress["sources"][source]["completed"])
            for source in selected
        }
        run_customers = [
            customer
            for customer in ordered
            if customer.lookup_number
            not in completed_by_source[customer.progress_source]
        ]
        log(
            f"Continuing with {len(run_customers)} unfinished record(s) in the "
            "selected scope."
        )
        return run_customers, mismatches, progress, direction

    if mode in ("search", "row", "customer"):
        assert manual_value is not None
        target_index = resolve_manual_position(mode, manual_value, ordered)
        target = ordered[target_index]
        if target_index > 0:
            log(
                f"This run will begin at customer {target_index + 1}; earlier "
                "unchecked records remain available for a later Continue run."
            )
        log(
            f"Starting at {target.record_type} record, Excel row "
            f"{target.row_number}, search number {target.lookup_number}."
        )
        return ordered[target_index:], mismatches, progress, direction

    return ordered, mismatches, progress, direction


def run() -> None:
    workbook_path = ask_workbook_choice()
    source_mode, min_amount_halalas, max_amount_halalas = ask_source_choice()
    if source_mode == "custom":
        target_collector = ask_collector_choice()
        custom_choices = ask_custom_source(workbook_path, target_collector)
        configs = [
            (
                choice.sheet_index,
                choice.lookup_column,
                choice.amount_column,
                choice.customer_column,
                choice.collector_column,
                choice.record_type,
                choice.service_column,
                choice.no_customer,
                choice.no_collector,
            )
            for choice in custom_choices
        ]
        all_customers, all_data_errors = load_records(
            workbook_path,
            "custom",
            target_collector,
            configs,
        )
        records_by_source = {"custom": all_customers}
    else:
        target_collector = ask_collector_choice()
        all_customers, all_data_errors = load_records(
            workbook_path, 
            "both",
            target_collector
        )
        records_by_source = {
            "account": [
                customer for customer in all_customers if customer.record_type == "account"
            ],
            "wallet": [
                customer for customer in all_customers if customer.record_type == "wallet"
            ],
        }
        
    if min_amount_halalas is not None or max_amount_halalas is not None:
        for source in records_by_source:
            filtered = []
            for c in records_by_source[source]:
                valid = True
                if min_amount_halalas is not None and c.expected_amount < min_amount_halalas:
                    valid = False
                if max_amount_halalas is not None and c.expected_amount > max_amount_halalas:
                    valid = False
                if valid:
                    filtered.append(c)
            records_by_source[source] = filtered
            
        conds = []
        if min_amount_halalas is not None:
            conds.append(f">= {min_amount_halalas / 100:,.2f}")
        if max_amount_halalas is not None:
            conds.append(f"<= {max_amount_halalas / 100:,.2f}")
        log(f"Filtered records to targets (debt {' and '.join(conds)} SAR).")

    selected_sources = selected_source_names(source_mode)
    selected_data_errors = [
        error for error in all_data_errors if error.progress_source in selected_sources
    ]
    unresolved_redirects = load_unresolved_redirect_keys(RESULT_WORKBOOK_PATH)
    selected_customer_count = sum(
        len(records_by_source[source]) for source in selected_sources
    )

    if selected_customer_count == 0:
        for data_error in selected_data_errors:
            append_error(RESULT_WORKBOOK_PATH, data_error)
        if selected_data_errors:
            log(
                f"Recorded {len(selected_data_errors)} source-data error(s) in the Arabic "
                "errors sheet."
            )
        log("No customer rows were found for the selected collector.")
        return

    customers, mismatches, progress, direction = prepare_run(
        workbook_path.name,
        records_by_source,
        source_mode,
    )
    for data_error in selected_data_errors:
        append_error(RESULT_WORKBOOK_PATH, data_error)
    if selected_data_errors:
        log(
            f"Recorded {len(selected_data_errors)} source-data error(s) in the Arabic "
            "errors sheet; conflicting records will not be sent to Zain."
        )

    def save_progress(
        next_index: int,
        current_mismatches: list[ProgressMismatch],
        completed: bool,
        completed_customer: Customer | None = None,
    ) -> None:
        del completed
        if completed_customer is not None:
            source_state = progress["sources"][completed_customer.progress_source]
            if completed_customer.lookup_number not in source_state["completed"]:
                source_state["completed"].append(completed_customer.lookup_number)
        elif next_index > 0 and next_index <= len(customers):
            checked_customer = customers[next_index - 1]
            source_state = progress["sources"][checked_customer.progress_source]
            if checked_customer.lookup_number not in source_state["completed"]:
                source_state["completed"].append(checked_customer.lookup_number)
        write_checkpoint(progress, current_mismatches)

    def save_mismatch(customer: Customer, website_amount: int) -> None:
        append_mismatch(
            RESULT_WORKBOOK_PATH,
            Mismatch(customer, website_amount, datetime.now()),
        )

    def save_error(error: CheckError) -> None:
        append_error(RESULT_WORKBOOK_PATH, error)

    def resolve_prior_redirect(customer: Customer) -> None:
        key = (customer.record_type, customer.lookup_number)
        if key not in unresolved_redirects:
            return
        if resolve_redirect_error(RESULT_WORKBOOK_PATH, customer):
            unresolved_redirects.discard(key)
            log(
                f"Marked the older redirect error as resolved for "
                f"{customer.record_type} {customer.lookup_number}."
            )

    save_progress(0, mismatches, not customers)

    if not customers:
        log("All selected records were already checked in the saved progress.")
        if mismatches:
            log(
                f"Saved progress contains {len(mismatches)} mismatched amount(s)."
            )
        else:
            log("All checked website amounts match column M.")
        return

    terminate_checker_chrome_processes()
    ensure_checker_chrome_profile_ready()

    state = CheckerState(
        customers,
        0,
        mismatches,
        save_progress,
        save_mismatch,
        save_error,
        resolve_prior_redirect,
    )
    server = BridgeServer(state)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    direction_text = {
        "forward": "top to bottom",
        "reverse": "bottom to top",
        "mixed": "using each sheet's saved direction",
    }[direction]
    source_text = {
        "account": "Account Number sheet",
        "wallet": "Wallet Number sheet",
        "both": "Account Number and Wallet Number sheets",
        "custom": "Custom sheet(s)",
    }[source_mode]
    log(
        f"Loaded {len(customers)} unique customer record(s) from {source_text}; "
        f"checking {direction_text}."
    )
    log(
        "Python bridge is ready. Closing existing Incognito windows, opening "
        "the current record in a fresh Incognito window, and starting the Zain "
        "checker automatically."
    )

    try:
        state.note_initial_incognito_launch()
        launch_initial_tabs_for_workers(customers, state)
        while not state.finished.wait(0.25):
            stalled_workers = state.get_stalled_worker_handoffs()
            for worker_tag, stalled_cust, stalled_tid in stalled_workers:
                canonical_name, _ = normalize_worker_id(worker_tag)
                log(
                    f"[{canonical_name}] The extension did not finish Incognito handoff within "
                    f"{state.incognito_handoff_timeout_seconds} seconds on row {stalled_cust.row_number} "
                    f"({stalled_cust.lookup_number}). Reopening ONLY this worker with Chrome bootstrap..."
                )
                terminate_worker_chrome_process(worker_tag)
                time.sleep(0.8)
                clean_extension_storage(get_worker_profile_dir(worker_tag))
                w_proxy = resolve_worker_proxy(worker_tag)
                launch_initial_incognito_tab(
                    stalled_cust,
                    stalled_tid,
                    worker_id=worker_tag,
                    proxy_server=w_proxy,
                )
                log(f"[{canonical_name}] Independent restart complete.")

            if not state.network_change_requested.is_set():
                continue

            while state.network_change_requested.is_set():
                answer = input(
                    format_console_message(
                        "Zain page/session recovery is required. Change your "
                        "network only if needed, then press Enter to retry the "
                        "same unfinished record in a fresh Incognito session. "
                        "Type Q to save and exit: "
                    )
                ).strip().lower()
                if answer in ("", "yes", "y"):
                    customer = state.resume_after_network_change()
                    log(
                        f"Recovery confirmed. Opening a fresh Incognito "
                        f"window and retrying row {customer.row_number}, "
                        f"search number {customer.lookup_number}."
                    )
                    break
                if answer in ("q", "quit", "stop"):
                    state.stop_during_network_wait()
                    break
                log("Press Enter to retry, or type Q to save and exit.")
    except KeyboardInterrupt:
        log("Stopped by the user.")
        return
    finally:
        server.shutdown()
        server.server_close()

    if state.stopped_by_user:
        log("Stopped while waiting for a network change. Saved progress is unchanged.")
        return

    if state.error:
        raise RuntimeError(state.error)

    if state.mismatches:
        log(
            f"Saved progress contains {len(state.mismatches)} mismatched amount(s). "
            'Every new mismatch was appended to "نتائج فحص زين" immediately.'
        )
    else:
        log("All checked website amounts match column M.")


if __name__ == "__main__":
    try:
        if "-e" in sys.argv or "--export-extension" in sys.argv:
            from zain_checker.web_bridge import export_extension
            out_dir = export_extension()
            print("=" * 64)
            print("  تصدير إضافة متصفح كروم | Chrome Extension Export")
            print("=" * 64)
            print(f"[OK] تم استخراج إضافة كروم بنجاح إلى المجلد:")
            print(f"     {out_dir}")
            print()
            print("يمكنك الآن تثبيتها في كروم:")
            print("  1. افتح متصفح كروم وانتقل إلى: chrome://extensions")
            print("  2. قم بتفعيل (وضع مطوّر البرامج / Developer mode).")
            print("  3. اضغط على (تحميل إضافة تم فك حزمتها / Load unpacked).")
            print(f"  4. حدد المجلد: {out_dir}")
            print("=" * 64)
            sys.exit(0)
        elif "--cli" in sys.argv:
            run()
        else:
            try:
                from zain_checker.telegram_controller import init_telegram_controller
                from zain_checker.web_bridge import read_checkpoint_info
                init_telegram_controller(get_stats_callback=read_checkpoint_info)
            except Exception:
                pass
            from zain_checker.web_bridge import launch_app
            launch_app()
    except Exception as error:
        log(str(error), file=sys.stderr)
        raise SystemExit(1) from error
