"""Checkpoint Persistence Engine for Smart Zain Checker.
Guarantees zero data loss with atomic file writes and automatic backup failover.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from domain.models import ProgressMismatch

import threading

logger = logging.getLogger("CheckpointManager")


class CheckpointManager:
    def __init__(self, checkpoint_path: Path) -> None:
        self.checkpoint_path = checkpoint_path
        self.backup_path = checkpoint_path.with_suffix(checkpoint_path.suffix + ".bak")
        self._lock = threading.Lock()

    def load(self) -> Dict[str, Any]:
        """Loads checkpoint data, falling back to backup if necessary."""
        for path in (self.checkpoint_path, self.backup_path):
            if not path.exists():
                continue
            try:
                raw = path.read_text(encoding="utf-8").replace("\x00", "").strip()
                if not raw:
                    continue
                data = json.loads(raw)
                return data
            except Exception as exc:
                logger.warning(f"Failed to read checkpoint from {path}: {exc}")
                continue

        return {"completed_indices": [], "mismatches": [], "last_index": 0}

    def save(
        self,
        completed_indices: Set[int],
        mismatches: List[ProgressMismatch],
        last_index: int,
        workbook_name: str = "",
        extra: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Atomically saves current progress and rotates to backup."""
        with self._lock:
            payload = {
                "version": 2,
                "workbook_name": workbook_name,
                "last_index": last_index,
                "completed_indices": sorted(list(completed_indices)),
                "mismatches": [m.to_dict() for m in mismatches],
                **(extra or {}),
            }

            raw = json.dumps(payload, ensure_ascii=False, indent=2)
            tmp_path = self.checkpoint_path.with_suffix(f".tmp_{threading.get_ident()}")

            try:
                tmp_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path.write_text(raw, encoding="utf-8")

                # Rotate existing to backup
                if self.checkpoint_path.exists():
                    try:
                        self.checkpoint_path.replace(self.backup_path)
                    except Exception:
                        pass

                tmp_path.replace(self.checkpoint_path)
                return True
            except Exception as exc:
                logger.error(f"Failed to save checkpoint: {exc}")
                if tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)
                return False

    def reset(self) -> None:
        """Clears both primary and backup checkpoint files."""
        with self._lock:
            try:
                if self.checkpoint_path.exists():
                    self.checkpoint_path.unlink(missing_ok=True)
                if self.backup_path.exists():
                    self.backup_path.unlink(missing_ok=True)
                logger.info(f"Checkpoint reset successfully: {self.checkpoint_path.name}")
            except Exception as exc:
                logger.warning(f"Error resetting checkpoint {self.checkpoint_path.name}: {exc}")

