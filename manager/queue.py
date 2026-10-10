"""Sequential Queue Service for Smart Zain Checker.
Manages multi-sheet audit jobs, persistence, and execution ordering.
"""
from __future__ import annotations

import json
import os
import copy
import logging
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, List, Optional

from domain.models import QueueJob
from manager.event_bus import EVENT_BUS
from manager.pricing import PricingSettings, normalize_quote

logger = logging.getLogger("QueueService")


class QueueService:
    def __init__(self, storage_path: Path) -> None:
        self.storage_path = storage_path
        self._lock = threading.RLock()
        self._jobs: List[QueueJob] = []
        self.pricing_settings = PricingSettings(storage_path.parent / '.zain-pricing.json')
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        if not self.storage_path.exists():
            return
        try:
            raw = self.storage_path.read_text(encoding="utf-8").strip()
            if not raw:
                return
            data = json.loads(raw)
            with self._lock:
                self._jobs = []
                for item in data:
                    job = QueueJob(**item)
                    # Self-Healing: Reset orphaned 'active' jobs to 'pending' if uncompleted
                    if job.status == "active":
                        logger.info(f"Self-healing orphaned active job {job.filename} -> reset to 'pending'")
                        job.status = "pending"
                    if job.status == 'pending':
                        from manager.checkpoint import CheckpointManager
                        checkpoint = self.storage_path.parent / (job.checkpoint_file or f'.checkpoint_{Path(job.filename).stem}_{job.sheet_index}.json')
                        if checkpoint.exists() or checkpoint.with_suffix(checkpoint.suffix+'.bak').exists():
                            saved = CheckpointManager(checkpoint).load()
                            completed = {i for i in saved.get('completed_indices', []) if isinstance(i, int) and 0 <= i < job.total_records}
                            job.completed = len(completed)
                            job.remaining = max(0, job.total_records-job.completed)
                            job.matches = int(saved.get('matches_count', job.matches))
                            job.mismatches = len(saved.get('mismatches', []))
                            job.errors = int(saved.get('errors_count', job.errors))
                    self._jobs.append(job)
        except Exception as exc:
            logger.warning(f"Could not load queue from disk: {exc}")

    def _save_to_disk(self) -> None:
        # Serialize and replace under the same lock: a second writer must not
        # publish an older snapshot after a newer job has been committed.
        with self._lock:
            try:
                self.storage_path.parent.mkdir(parents=True, exist_ok=True)
                data = [j.to_dict() for j in self._jobs]
                encoded = json.dumps(data, ensure_ascii=False, indent=2)
                temporary = self.storage_path.with_name(self.storage_path.name + '.' + uuid.uuid4().hex + '.tmp')
                with temporary.open('x', encoding='utf-8') as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.storage_path)
            except Exception as exc:
                # Preserve the original queue and unsuccessful temporary file.
                logger.error('Could not commit queue to disk (%s).', type(exc).__name__)
                raise

    def _existing_payment_job(self, candidate: QueueJob) -> Optional[QueueJob]:
        """An invoice token identifies one job; ordinary file rechecks stay distinct."""
        token = candidate.column_mapping.get('payment_token')
        if token is None or token == '':
            return None
        if not isinstance(token, str):
            raise ValueError('payment_token must be a non-empty string')
        for existing in self._jobs:
            if existing.column_mapping.get('payment_token') != token:
                continue
            identity = ('filename', 'sheet_index', 'sheet_name', 'mode', 'amount_target', 'target_url')
            if (any(getattr(existing, field) != getattr(candidate, field) for field in identity)
                    or existing.column_mapping != candidate.column_mapping
                    or existing.total_records != candidate.total_records):
                raise ValueError('Payment token is already assigned to a different queue job')
            return existing
        return None

    def add_job(
        self,
        file_path: Path,
        sheet_index: int,
        sheet_name: str,
        mode: str = "smart_hybrid",
        amount_target: str = "contract",
        target_url: str = "https://business.zain.sa/dashboard/quick-pay",
        column_mapping: Optional[dict[str, Any]] = None,
        total_records: int = 0,
        force_clean: bool = False,
        pricing: Optional[dict[str, Any]] = None,
    ) -> QueueJob:
        price_snapshot = self.pricing_settings.quote(pricing)
        job_id = f"job_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        safe_stem = re.sub(r'[\\/*?:"<>|]', '_', file_path.stem)
        safe_sheet = re.sub(r'[\\/*?:"<>|]', '_', sheet_name)
        res_filename = f"نتائج فحص زين - {safe_stem} - {safe_sheet} - {job_id}.xlsx"

        chk_filename = f".checkpoint_{safe_stem}_{sheet_index}_{job_id}.json"

        job = QueueJob(
            id=job_id,
            filename=file_path.name,
            sheet_index=sheet_index,
            sheet_name=sheet_name,
            mode=mode,
            amount_target=amount_target,
            target_url=target_url,
            column_mapping=copy.deepcopy(column_mapping or {}),
            total_records=total_records,
            status="pending",
            created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            result_file=res_filename,
            checkpoint_file=chk_filename,
            completed=0,
            remaining=total_records,
            matches=0,
            mismatches=0,
            errors=0,
            pricing=price_snapshot,
        )

        with self._lock:
            existing = self._existing_payment_job(job)
            if existing:
                return existing
            self._jobs.append(job)
            try:
                self._save_to_disk()
            except Exception:
                self._jobs.remove(job)
                raise
        EVENT_BUS.publish("queue_job_added", job.to_dict())
        return job

    def get_jobs(self) -> List[dict[str, Any]]:
        with self._lock:
            if not self._jobs:
                self._load_from_disk()
            return [j.to_dict() for j in self._jobs]

    def get_next_pending_job(self, exclude_job_id: Optional[str] = None) -> Optional[QueueJob]:
        with self._lock:
            for j in self._jobs:
                if exclude_job_id and j.id == exclude_job_id:
                    continue
                if j.status in {"completed", "archived"}:
                    continue
                if j.status == "pending":
                    return j
                # If uncompleted (e.g. stalled active/interrupted), heal to pending in strict FIFO order
                if j.total_records == 0 or j.completed < j.total_records:
                    logger.warning(
                        f"Heuristic recovery: Auto-advancing uncompleted FIFO job {j.filename} "
                        f"(status was '{j.status}', completed={j.completed}/{j.total_records}) to 'pending'."
                    )
                    j.status = "pending"
                    self._save_to_disk()
                    return j

            return None

    def claim_next_pending_job(self, exclude_job_id: Optional[str] = None, job_id: Optional[str] = None) -> Optional[QueueJob]:
        """Reserve the next job atomically against queue edits and removal."""
        with self._lock:
            job = self.get_job(job_id) if job_id else self.get_next_pending_job(exclude_job_id)
            if job and job_id and job.status != 'pending':
                return None
            if job:
                self.mark_job_active(job.id)
            return job

    def mark_job_active(self, job_id: str) -> None:
        with self._lock:
            for j in self._jobs:
                if j.id == job_id:
                    j.status = "active"
                    break
        self._save_to_disk()
        EVENT_BUS.publish("queue_job_started", {"job_id": job_id})

    def update_job_progress(
        self,
        job_id: str,
        completed: int,
        remaining: int,
        matches: int,
        mismatches: int,
        errors: int,
    ) -> None:
        with self._lock:
            for j in self._jobs:
                if j.id == job_id:
                    j.completed = completed
                    j.remaining = remaining
                    j.matches = matches
                    j.mismatches = mismatches
                    j.errors = errors
                    break
        self._save_to_disk()

    def update_job_result_file(self, job_id: str, result_file: str) -> None:
        with self._lock:
            for j in self._jobs:
                if j.id == job_id:
                    j.result_file = result_file
                    break
        self._save_to_disk()

    def mark_job_completed(self, job_id: str, result_file: Optional[str] = None) -> None:
        job_data = None
        with self._lock:
            for j in self._jobs:
                if j.id == job_id:
                    j.status = "completed"
                    j.remaining = 0
                    j.completed = j.total_records
                    if result_file:
                        j.result_file = result_file
                    job_data = j.to_dict()
                    break
        self._save_to_disk()
        if job_data:
            EVENT_BUS.publish("queue_job_completed", job_data)

    def mark_job_failed(self, job_id: str, reason: str = "") -> None:
        with self._lock:
            for j in self._jobs:
                if j.id == job_id:
                    j.status = "failed"
                    break
        self._save_to_disk()
        EVENT_BUS.publish("queue_job_failed", {"job_id": job_id, "reason": reason})

    def remove_job(self, job_id: str) -> bool:
        removed = False
        with self._lock:
            if any(j.id == job_id and j.status == "active" for j in self._jobs):
                return False
            orig_len = len(self._jobs)
            self._jobs = [j for j in self._jobs if j.id != job_id]
            removed = len(self._jobs) < orig_len

        if removed:
            self._save_to_disk()
            EVENT_BUS.publish("queue_job_removed", {"job_id": job_id})
        return removed

    def update_pending_mapping(self, job_id: str, *, column_mapping: dict, mode: str,
                               amount_target: str, total_records: int) -> bool:
        """Only untouched, waiting jobs may have their input configuration changed."""
        with self._lock:
            job = next((j for j in self._jobs if j.id == job_id), None)
            if not job or job.status != "pending" or job.completed:
                return False
            updated_mapping = copy.deepcopy(dict(column_mapping))
            # These fields bind a paid invoice and result ownership to one job.
            # A column editor may omit internal fields, but cannot replace them.
            job_mapping = getattr(job, 'column_mapping', {}) or {}
            for key in ('payment_token', 'telegram_chat_id'):
                if key in job_mapping:
                    if key in updated_mapping and updated_mapping[key] != job_mapping[key]:
                        raise ValueError('Paid queue job identity cannot be changed')
                    updated_mapping[key] = job_mapping[key]
            previous = (job_mapping, getattr(job, 'mode', None), getattr(job, 'amount_target', None), getattr(job, 'total_records', 0), getattr(job, 'remaining', 0))
            job.column_mapping = updated_mapping
            job.mode = mode
            job.amount_target = amount_target
            job.total_records = total_records
            job.remaining = total_records
            try:
                self._save_to_disk()
            except Exception:
                (job.column_mapping, job.mode, job.amount_target,
                 job.total_records, job.remaining) = previous
                raise
        EVENT_BUS.publish("queue_job_updated", job.to_dict())
        return True

    def get_job(self, job_id: str) -> Optional[QueueJob]:
        with self._lock:
            for j in self._jobs:
                if j.id == job_id:
                    return j
            return None

    def update_job_pricing(self, job_id: str, value: dict) -> bool:
        quote = normalize_quote(value)
        with self._lock:
            job = next((j for j in self._jobs if j.id == job_id), None)
            if not job or job.status != 'pending' or job.completed:
                return False
            previous = job.pricing
            job.pricing = quote
            try:
                # Persist before reporting success; protect the price snapshot.
                self._save_to_disk()
            except Exception:
                job.pricing = previous
                raise
        return True

    def get_job_by_file_and_sheet(self, filename: str, sheet_index: int) -> Optional[QueueJob]:
        with self._lock:
            for j in reversed(self._jobs):
                if j.filename == filename and j.sheet_index == sheet_index:
                    return j
            return None

    def fork_job(self, job_id: str) -> Optional[QueueJob]:
        """Create a fresh run and retain previous progress/files as history."""
        with self._lock:
            source = self.get_job(job_id)
            if not source or source.status == 'active':
                return None
            price = {'mode':source.pricing['mode'], 'unit_price':f"{source.pricing['unit_minor']/100:.2f}"} if source.pricing else None
            fork_mapping = copy.deepcopy(source.column_mapping)
            # An explicit new round is a separate job, not a replay of payment.
            fork_mapping.pop('payment_token', None)
            job = self.add_job(self.storage_path.parent/source.filename, source.sheet_index,
                source.sheet_name, source.mode, source.amount_target, source.target_url,
                fork_mapping, source.total_records, pricing=price)
            source.status = 'archived'
            self._save_to_disk()
            return job

    def restart_job(self, job_id: str) -> Optional[QueueJob]:
        """Resets a job to pending status with 0 progress for a fresh restart."""
        target_job = None
        with self._lock:
            for j in self._jobs:
                if j.id == job_id:
                    j.status = "pending"
                    j.completed = 0
                    j.remaining = j.total_records
                    j.matches = 0
                    j.mismatches = 0
                    j.errors = 0
                    target_job = j
                    break
        if target_job:
            self._save_to_disk()
            EVENT_BUS.publish("queue_job_updated", target_job.to_dict())
            logger.info(f"Queue job {job_id} ({target_job.filename}) reset to pending for restart.")
        return target_job

    def restart_all_jobs(self) -> List[QueueJob]:
        """Resets all jobs to pending status with 0 progress for a fresh queue restart."""
        with self._lock:
            for j in self._jobs:
                j.status = "pending"
                j.completed = 0
                j.remaining = j.total_records
                j.matches = 0
                j.mismatches = 0
                j.errors = 0
        self._save_to_disk()
        EVENT_BUS.publish("queue_restarted_all", {})
        logger.info(f"All {len(self._jobs)} queue jobs reset to pending.")
        return list(self._jobs)

    def clear_completed(self) -> None:
        with self._lock:
            self._jobs = [j for j in self._jobs if j.status not in ("completed", "failed")]
        self._save_to_disk()

