"""Atomic checkpoints with SQLite state persistence and JSON backup failover."""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from domain.models import ProgressMismatch

logger = logging.getLogger("CheckpointManager")


class CheckpointManager:
    def __init__(self, checkpoint_path: Path) -> None:
        self.checkpoint_path = checkpoint_path
        self.backup_path = checkpoint_path.with_suffix(checkpoint_path.suffix + ".bak")
        self.db_path = checkpoint_path.with_suffix(".db")
        self._lock = threading.Lock()
        self._persisted_indices: Set[int] = set()

    def _init_sqlite(self) -> Optional[sqlite3.Connection]:
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db_path), timeout=10.0, check_same_thread=False)
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);")
            conn.execute("CREATE TABLE IF NOT EXISTS completed_indices (idx INTEGER PRIMARY KEY);")
            conn.execute("CREATE TABLE IF NOT EXISTS mismatches (sequence_index INTEGER PRIMARY KEY, expected_amount INTEGER, website_amount INTEGER);")
            conn.commit()
            return conn
        except Exception as exc:
            logger.warning(f"Could not initialize SQLite checkpoint at {self.db_path}: {exc}")
            return None

    def load(self) -> Dict[str, Any]:
        """Loads checkpoint data from SQLite or falls back to JSON / backup."""
        # 1. Try SQLite if DB file exists on disk
        if self.db_path.is_file():
            try:
                conn = sqlite3.connect(str(self.db_path), timeout=5.0)
                cur = conn.cursor()
                cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='completed_indices';")
                if cur.fetchone():
                    completed_rows = cur.execute("SELECT idx FROM completed_indices ORDER BY idx").fetchall()
                    completed_indices = [r[0] for r in completed_rows]

                    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='mismatches';")
                    mismatches = []
                    if cur.fetchone():
                        mm_rows = cur.execute("SELECT sequence_index, expected_amount, website_amount FROM mismatches").fetchall()
                        mismatches = [{"sequence_index": r[0], "expected_amount": r[1], "website_amount": r[2]} for r in mm_rows]

                    meta = {}
                    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='meta';")
                    if cur.fetchone():
                        for k, v in cur.execute("SELECT key, value FROM meta").fetchall():
                            try:
                                meta[k] = json.loads(v)
                            except (json.JSONDecodeError, TypeError):
                                meta[k] = v

                    conn.close()
                    if completed_indices or mismatches or meta:
                        self._persisted_indices = set(completed_indices)
                        res = {
                            "version": int(meta.get("version", 2)),
                            "workbook_name": meta.get("workbook_name", ""),
                            "last_index": int(meta.get("last_index", 0)),
                            "completed_indices": completed_indices,
                            "mismatches": mismatches,
                        }
                        for k, v in meta.items():
                            if k not in res:
                                res[k] = v
                        return res
                conn.close()
            except Exception as exc:
                logger.warning(f"Failed to read checkpoint from SQLite DB {self.db_path}: {exc}")

        # 2. Fallback to JSON files
        for path in (self.checkpoint_path, self.backup_path):
            if not path.exists():
                continue
            try:
                raw = path.read_text(encoding="utf-8").replace("\x00", "").strip()
                if not raw:
                    continue
                data = json.loads(raw)
                if not isinstance(data, dict) or not isinstance(data.get('completed_indices', []), list) or not isinstance(data.get('mismatches', []), list):
                    raise ValueError('Invalid checkpoint structure')
                self._persisted_indices = set(data.get('completed_indices', []))
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
        """Atomically saves current progress to SQLite and rotated backup JSON."""
        with self._lock:
            # 1. Fast SQLite persistence
            conn = self._init_sqlite()
            if conn:
                try:
                    new_indices = completed_indices - self._persisted_indices
                    if new_indices:
                        conn.executemany("INSERT OR IGNORE INTO completed_indices (idx) VALUES (?)", [(i,) for i in new_indices])
                        self._persisted_indices.update(new_indices)

                    # Update mismatches
                    conn.execute("DELETE FROM mismatches")
                    if mismatches:
                        conn.executemany(
                            "INSERT OR REPLACE INTO mismatches (sequence_index, expected_amount, website_amount) VALUES (?, ?, ?)",
                            [(m.sequence_index, m.expected_amount, m.website_amount) for m in mismatches]
                        )

                    # Update metadata
                    meta_dict = {
                        "version": "2",
                        "workbook_name": str(workbook_name),
                        "last_index": str(last_index),
                        **(extra or {})
                    }
                    conn.executemany(
                        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                        [(k, json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v))
                         for k, v in meta_dict.items()]
                    )
                    conn.commit()
                except Exception as exc:
                    logger.warning(f"Failed to persist checkpoint to SQLite: {exc}")
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass

            # 2. JSON persistence
            payload = {
                "version": 2,
                "workbook_name": workbook_name,
                "last_index": last_index,
                "completed_indices": sorted(list(completed_indices)),
                "mismatches": [m.to_dict() for m in mismatches],
                **(extra or {}),
            }

            if len(completed_indices) > 500:
                raw = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
            else:
                raw = json.dumps(payload, ensure_ascii=False, indent=2)

            tmp_path = self.checkpoint_path.with_suffix(f".tmp_{threading.get_ident()}")
            try:
                tmp_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path.write_text(raw, encoding="utf-8")

                if self.checkpoint_path.exists():
                    try:
                        self.checkpoint_path.replace(self.backup_path)
                    except Exception:
                        pass

                tmp_path.replace(self.checkpoint_path)
                return True
            except Exception as exc:
                logger.error(f"Failed to save checkpoint: {exc}")
                return False

    def reset(self) -> None:
        """Archive primary, backup, and SQLite checkpoints without deleting either file."""
        with self._lock:
            try:
                import uuid
                suffix = uuid.uuid4().hex
                for path in (
                    self.checkpoint_path,
                    self.backup_path,
                    self.db_path,
                    self.db_path.with_name(self.db_path.name + "-wal"),
                    self.db_path.with_name(self.db_path.name + "-shm"),
                ):
                    if path.exists():
                        path.replace(path.with_name(path.name + '.archive_' + suffix))
                self._persisted_indices.clear()
                logger.info(f"Checkpoint reset successfully: {self.checkpoint_path.name}")
            except Exception as exc:
                logger.warning(f"Error resetting checkpoint {self.checkpoint_path.name}: {exc}")
