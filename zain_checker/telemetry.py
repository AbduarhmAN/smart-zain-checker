from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from zain_checker.config import PROJECT_DIRECTORY as PROJECT_DIR
FEED_FILE = PROJECT_DIR / ".live_feed.jsonl"
ACTIVE_TASK_FILE = PROJECT_DIR / ".active_task.json"


def emit_active_task(task_data: dict[str, Any]) -> None:
    """Non-blocking, zero-overhead write of the active account Chrome is inspecting right now."""
    try:
        with open(ACTIVE_TASK_FILE, "w", encoding="utf-8") as f:
            json.dump(task_data, f, ensure_ascii=False)
    except Exception:
        pass


def emit_check_result(
    row: int,
    record_type: str,
    lookup_number: str,
    customer_name: str,
    expected_amount: float,
    live_amount: float,
    status: str,
    worker_id: str = "Worker 1 (Router)",
    **kwargs: Any,
) -> None:
    """Appends a single JSON line for the web table (<0.0001s). Completely safe, never throws."""
    try:
        record = {
            "row": row,
            "record_type": record_type,
            "lookup_number": lookup_number,
            "customer_name": customer_name,
            "expected_amount": expected_amount,
            "live_amount": live_amount,
            "status": status,
            "worker_id": worker_id,
            "time": time.strftime("%H:%M:%S"),
        }
        with open(FEED_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass

