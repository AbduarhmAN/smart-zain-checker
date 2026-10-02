# -*- coding: utf-8 -*-
"""
Multi-Sheet Sequential Queue Manager for Zain Retail Audit Workstation.
Manages a persistent FIFO queue of audit jobs across multiple Excel files/sheets,
providing chained start-time ETAs, crash-safe checkpoint isolation, and automatic
transitioning with Telegram dispatch upon each sheet completion.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from zain_checker.config import PROJECT_DIRECTORY


@dataclass
class QueueJob:
    id: str
    file_path: str
    filename: str
    sheet_index: int = 0
    sheet_name: str = "Sheet1"
    mode: str = "smart_hybrid"          # "smart_hybrid" | "account_only" | "service_only"
    amount_target: str = "remaining"     # "remaining" | "contract" | "smart_dual"
    column_mapping: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"              # "pending" | "running" | "completed" | "interrupted" | "failed"
    total_records: int = 0
    completed_records: int = 0
    remaining_records: int = 0
    matches: int = 0
    mismatches: int = 0
    errors: int = 0
    eta_str: str = "قيد الحساب..."
    start_after_str: str = "في انتظار دوره"
    result_file: str = ""
    checkpoint_file: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    error_message: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["percent"] = round((self.completed_records / self.total_records * 100), 1) if self.total_records > 0 else 0
        return d


class SheetQueueManager:
    """Thread-safe persistent manager for sequential multi-sheet execution."""

    def __init__(self, storage_dir: Path | None = None) -> None:
        self.dir = storage_dir or PROJECT_DIRECTORY
        self.queue_file = self.dir / ".sheet_queue.json"
        self.lock = threading.RLock()
        self.jobs: list[QueueJob] = []
        self._load()

    def _save(self) -> None:
        try:
            data = [j.to_dict() for j in self.jobs]
            self.queue_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"[QueueManager] Error saving queue state: {e}", flush=True)

    def _load(self) -> None:
        if not self.queue_file.exists():
            return
        try:
            content = self.queue_file.read_text(encoding="utf-8").strip()
            if not content:
                return
            items = json.loads(content)
            self.jobs = []
            for item in items:
                # Discard percent if present as it's computed dynamically
                item.pop("percent", None)
                job = QueueJob(**item)
                # If was marked running when app died, mark as interrupted so it can resume cleanly
                if job.status == "running":
                    job.status = "interrupted"
                self.jobs.append(job)
        except Exception as e:
            print(f"[QueueManager] Error loading queue state: {e}", flush=True)

    def add_job(
        self,
        file_path: str | Path,
        sheet_index: int = 0,
        sheet_name: str = "Sheet1",
        mode: str = "smart_hybrid",
        amount_target: str = "remaining",
        column_mapping: dict[str, Any] | None = None,
        total_records: int = 0,
    ) -> QueueJob:
        with self.lock:
            fp = Path(file_path).resolve()
            filename = fp.name
            stem = fp.stem
            job_id = f"job_{int(time.time())}_{uuid.uuid4().hex[:6]}"
            res_file = f"نتائج_فحص_{stem}.xlsx"
            chk_file = f".checkpoint_{stem}_{sheet_index}.json"

            job = QueueJob(
                id=job_id,
                file_path=str(fp),
                filename=filename,
                sheet_index=sheet_index,
                sheet_name=sheet_name,
                mode=mode,
                amount_target=amount_target,
                column_mapping=column_mapping or {},
                status="pending",
                total_records=total_records,
                remaining_records=total_records,
                result_file=res_file,
                checkpoint_file=chk_file,
            )
            self.jobs.append(job)
            self.recalculate_schedule()
            self._save()
            return job

    def remove_job(self, job_id: str) -> bool:
        with self.lock:
            for idx, j in enumerate(self.jobs):
                if j.id == job_id:
                    # Can only remove if not actively running
                    if j.status == "running":
                        return False
                    self.jobs.pop(idx)
                    self.recalculate_schedule()
                    self._save()
                    return True
            return False

    def clear_completed(self) -> None:
        with self.lock:
            self.jobs = [j for j in self.jobs if j.status not in ("completed", "failed")]
            self.recalculate_schedule()
            self._save()

    def clear_all(self) -> None:
        with self.lock:
            self.jobs = []
            self.recalculate_schedule()
            self._save()

    def get_jobs(self) -> list[dict[str, Any]]:
        with self.lock:
            self.recalculate_schedule()
            return [j.to_dict() for j in self.jobs]

    def get_active_job(self) -> QueueJob | None:
        with self.lock:
            for j in self.jobs:
                if j.status == "running":
                    return j
            return None

    def get_job(self, job_id: str) -> QueueJob | None:
        with self.lock:
            for j in self.jobs:
                if j.id == job_id:
                    return j
            return None

    def get_next_pending_job(self) -> QueueJob | None:
        with self.lock:
            for j in self.jobs:
                if j.status in ("pending", "interrupted"):
                    return j
            return None

    def mark_job_running(self, job_id: str) -> QueueJob | None:
        with self.lock:
            for j in self.jobs:
                if j.id == job_id:
                    j.status = "running"
                    if not j.started_at:
                        j.started_at = time.time()
                    self.recalculate_schedule()
                    self._save()
                    return j
            return None

    def update_active_progress(
        self,
        job_id: str,
        completed: int,
        remaining: int,
        matches: int,
        mismatches: int,
        errors: int,
        speed_cpm: float = 95.0,
    ) -> None:
        with self.lock:
            for j in self.jobs:
                if j.id == job_id:
                    j.completed_records = completed
                    j.remaining_records = remaining
                    j.matches = matches
                    j.mismatches = mismatches
                    j.errors = errors
                    break
            self.recalculate_schedule(speed_cpm=speed_cpm)
            self._save()

    def mark_job_completed(self, job_id: str, error_msg: str = "") -> QueueJob | None:
        with self.lock:
            for j in self.jobs:
                if j.id == job_id:
                    j.completed_at = time.time()
                    if error_msg:
                        j.status = "failed"
                        j.error_message = error_msg
                    else:
                        j.status = "completed"
                        j.remaining_records = 0
                        j.eta_str = "مكتمل بنجاح ✅"
                        j.start_after_str = "مكتمل ✅"
                    self.recalculate_schedule()
                    self._save()
                    return j
            return None

    def recalculate_schedule(self, speed_cpm: float = 95.0) -> None:
        """Dynamically computes the active job ETA and cascading start-times for queued jobs."""
        with self.lock:
            active_job = self.get_active_job()
            cumulative_seconds = 0.0

            eff_cpm = speed_cpm if speed_cpm > 5.0 else 95.0

            for j in self.jobs:
                if j.status == "completed":
                    j.eta_str = "مكتمل بنجاح ✅"
                    j.start_after_str = "مكتمل ✅"
                elif j.status == "failed":
                    j.eta_str = "فشل ❌"
                    j.start_after_str = "ملغي"
                elif j.status == "running":
                    j.start_after_str = "قيد الفحص الآن 🟢"
                    rem = j.remaining_records if j.remaining_records > 0 else max(0, j.total_records - j.completed_records)
                    mins_left = rem / eff_cpm if eff_cpm > 0 else 0
                    cumulative_seconds += (mins_left * 60.0)

                    if mins_left >= 60:
                        h = int(mins_left // 60)
                        m = int(mins_left % 60)
                        j.eta_str = f"~{h} س و {m} د"
                    elif mins_left >= 1:
                        j.eta_str = f"~{int(mins_left)} دقيقة"
                    elif rem > 0:
                        j.eta_str = "أقل من دقيقة"
                    else:
                        j.eta_str = "لحظات للاكتمال..."
                elif j.status in ("pending", "interrupted"):
                    # Calculate estimated duration for this pending job
                    est_records = j.remaining_records if j.remaining_records > 0 else (j.total_records or 2000)
                    job_duration_mins = est_records / eff_cpm if eff_cpm > 0 else 20.0

                    if cumulative_seconds <= 1.0:
                        j.start_after_str = "يبدأ فوراً (التالي بالطابور) 🚀"
                    else:
                        wait_mins = cumulative_seconds / 60.0
                        if wait_mins >= 60:
                            wh = int(wait_mins // 60)
                            wm = int(wait_mins % 60)
                            j.start_after_str = f"يبدأ بعد ~{wh} س و {wm} د ⏳"
                        else:
                            j.start_after_str = f"يبدأ بعد ~{int(wait_mins)} دقيقة ⏳"

                    # Add this job's run time to cumulative queue clock for subsequent jobs
                    cumulative_seconds += (job_duration_mins * 60.0)
                    if job_duration_mins >= 60:
                        jh = int(job_duration_mins // 60)
                        jm = int(job_duration_mins % 60)
                        j.eta_str = f"مدته التقديرية: ~{jh} س و {jm} د"
                    else:
                        j.eta_str = f"مدته التقديرية: ~{int(job_duration_mins)} دقيقة"


# Global singleton instance
QUEUE_MANAGER = SheetQueueManager()
