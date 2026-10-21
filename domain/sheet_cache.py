"""Unified In-Memory Data Loading and Bounded Cache for Smart Zain Checker.

Architecture Design:
1. Unified Data Loading:
   Raw sheet cells are loaded once into RawSheetData. Preview, schema inspection,
   and task input generation all derive in-memory from this shared representation.
2. Separation of Schema Interpretation from Raw Data:
   Changing custom column mapping or has_headers re-interprets in-memory (< 0.1ms)
   without re-reading the workbook from disk.
3. Separation of Comparison Policy from Query Policy:
   Amount policies ('remaining', 'contract', 'smart_dual') and query policies
   ('account' vs 'service') recalculate in-memory without disk I/O or network requests.
4. Memory-Bounded LRU Cache with Accurate Measurement:
   Enforces byte-size memory limit, LRU eviction, and stat-based (mtime/size) change detection.
5. Truly Immutable Task Snapshot:
   Uses MappingProxyType to deeply freeze task options so running tasks cannot be mutated.
"""
from __future__ import annotations

import io
import logging
import os
import re
import sys
import threading
import time
import tracemalloc
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, List, Optional, Set, Tuple

# Windows UTF-8 encoding rule
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
elif hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import openpyxl
from openpyxl.utils import get_column_letter, column_index_from_string

from domain.models import Customer
from domain.money import TOLERANCE_HALALAS, is_amount_match, parse_money_to_halalas
from domain.workbook import (
    detect_smart_sheet_columns,
    get_sheet_header_columns,
    is_empty_input_row,
    normalize_header_text,
    parse_account_and_service,
    parse_excel_column,
)

logger = logging.getLogger("SharedSheetCache")


def calculate_deep_size(obj: Any, seen: Optional[Set[int]] = None) -> int:
    """Recursively calculates deep memory footprint of primitive Python objects in bytes."""
    if seen is None:
        seen = set()
    obj_id = id(obj)
    if obj_id in seen:
        return 0
    seen.add(obj_id)
    size = sys.getsizeof(obj)
    if isinstance(obj, (tuple, list, set, frozenset)):
        size += sum(calculate_deep_size(item, seen) for item in obj)
    elif isinstance(obj, dict):
        size += sum(calculate_deep_size(k, seen) + calculate_deep_size(v, seen) for k, v in obj.items())
    return size


@dataclass(slots=True)
class RawSheetData:
    file_path: Path
    sheet_index: int
    sheet_name: str
    total_sheets: int
    sheet_names: Tuple[str, ...]
    mtime: float
    file_size: int
    raw_rows: Tuple[Tuple[Any, ...], ...]
    memory_bytes: int
    last_accessed: float = field(default_factory=time.time)
    precomputed_validations: Dict[Tuple[str, str], Dict[str, Any]] = field(default_factory=dict)
    total_populated_rows: int = 0
    detected_indices: Dict[str, Optional[int]] = field(default_factory=dict)
    detected_letters: Dict[str, Optional[str]] = field(default_factory=dict)
    _max_column: int = 0
    default_schema: Optional[Dict[str, Any]] = None

    def touch(self) -> None:
        self.last_accessed = time.time()

    @property
    def max_column(self) -> int:
        if self._max_column > 0:
            return self._max_column
        if not self.raw_rows:
            return 0
        return max((len(r) for r in self.raw_rows), default=0)

    @property
    def max_row(self) -> int:
        return len(self.raw_rows)

    def iter_rows(
        self,
        min_row: int = 1,
        max_row: Optional[int] = None,
        min_col: int = 1,
        max_col: Optional[int] = None,
        values_only: bool = True,
    ):
        start = max(0, min_row - 1)
        end = max_row if max_row is not None else len(self.raw_rows)
        for row in self.raw_rows[start:end]:
            col_start = max(0, min_col - 1)
            col_end = max_col if max_col is not None else len(row)
            yield row[col_start:col_end]


@dataclass(frozen=True)
class StructuredLiveBalance:
    number: str
    query_type: str  # "account" or "service"
    balance_sar: float
    session_token: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class FrozenTaskConfig:
    task_id: str
    file_path: Path
    sheet_index: int
    mode: str  # "smart_hybrid", "account_only", "service_only"
    amount_target: str  # "remaining", "contract", "smart_dual"
    column_mapping: MappingProxyType
    has_headers: bool = True
    frozen_at: float = field(default_factory=time.time)


def parse_row_numbers(
    raw_acc_val: Any,
    raw_srv_val: Any,
    raw_con_val: Any = None,
    is_shared_column: bool = False,
) -> Tuple[str, str, str]:
    """Extracts (account, service, contract) numbers from row values.
    Properly distinguishes between dedicated columns and compound cells.
    If is_shared_column is False, values in dedicated columns are NOT cross-assigned
    unless explicit labels (e.g. 'حساب:' or 'خدمة:') or compound patterns are present.
    """
    def parse_col_val(val: Any, is_service_col: bool) -> Tuple[str, str]:
        if val is None:
            return "", ""
        s = str(val).strip()
        if not s or s == "-":
            return "", ""
        p_acc, p_srv = parse_account_and_service(s)
        has_acc_lbl = bool(re.search(r'(?:حساب|عقد|account|contract)[\s:]*([0-9]{8,12})', s, re.IGNORECASE))
        has_srv_lbl = bool(re.search(r'(?:خدمة|خدمه|جوال|هاتف|محفظة|محفظه|service|phone|wallet)[\s:]*([0-9]{8,12})', s, re.IGNORECASE))
        digit_blocks = re.findall(r'\b[0-9]{8,12}\b', s) or re.findall(r'[0-9]{6,14}', s)

        # Compound cells or shared column: respect parse_account_and_service
        if (p_acc and p_srv) or has_acc_lbl or has_srv_lbl or len(digit_blocks) >= 2 or is_shared_column:
            return p_acc, p_srv

        # Dedicated column with single number and NO cross labels:
        if digit_blocks:
            num = digit_blocks[0]
            if is_service_col:
                return "", num
            else:
                return num, ""

        return p_acc, p_srv

    acc1, srv1 = parse_col_val(raw_acc_val, is_service_col=False)
    acc2, srv2 = parse_col_val(raw_srv_val, is_service_col=True) if raw_srv_val is not None else ("", "")
    acc3, srv3 = parse_col_val(raw_con_val, is_service_col=False) if raw_con_val is not None else ("", "")

    clean_acc = acc1 or acc2 or acc3
    clean_srv = srv2 or srv1 or srv3
    clean_con = acc3 or clean_acc

    if not clean_acc and not clean_srv:
        raw_a = re.sub(r"\D", "", str(raw_acc_val or ""))
        raw_s = re.sub(r"\D", "", str(raw_srv_val or ""))
        if is_shared_column:
            if raw_a.startswith("2"):
                clean_srv = raw_s or raw_a
            else:
                clean_acc = raw_a
                clean_srv = raw_s
        else:
            clean_acc = raw_a
            clean_srv = raw_s
        clean_con = re.sub(r"\D", "", str(raw_con_val or "")) or clean_acc

    return clean_acc, clean_srv, clean_con


FIELDS = {
    "lookup_col": "رقم الحساب / العقد",
    "service_col": "رقم الخدمة",
    "amount_col": "المبلغ المسجل بالشيت",
    "amount_col_2": "مبلغ ثانٍ اختياري",
    "customer_col": "اسم العميل · اختياري",
    "collector_col": "المحصل · اختياري",
}
MODES = {"smart_hybrid": "حساب أو خدمة", "account_only": "حسابات", "service_only": "خدمات"}
ALL_KNOWN_MODES = ("smart_hybrid", "account_only", "service_only")
ALL_KNOWN_AMOUNT_TARGETS = ("remaining", "contract", "smart_dual")
ALL_9_COMBOS = [(m, at) for m in ALL_KNOWN_MODES for at in ALL_KNOWN_AMOUNT_TARGETS]


def evaluate_column_requirements(
    indices: Dict[str, Optional[int]],
    width: int,
    mode: str,
    amount_target: str,
    mapping: Optional[Dict[str, Any]] = None,
) -> Tuple[List[str], List[str]]:
    """Evaluates column requirements according to production domain rules."""
    problems: List[str] = []
    missing_columns: List[str] = []
    mapping = mapping or {}
    for key, index in indices.items():
        if mapping.get(key) and (not index or index > width):
            problems.append(f"عمود {FIELDS[key]} خارج حدود الورقة.")
    if mode not in MODES:
        problems.append("اختر طريقة فحص صحيحة.")
    required = ("lookup_col",) if mode == "account_only" else (("service_col",) if mode == "service_only" else ("lookup_col", "service_col"))
    if not any(indices.get(key) for key in required):
        problems.append("حدد عمود الرقم المناسب لطريقة الفحص.")
        if mode == "account_only":
            missing_columns.append(FIELDS["lookup_col"])
        elif mode == "service_only":
            missing_columns.append(FIELDS["service_col"])
        else:
            missing_columns.append(f"{FIELDS['lookup_col']} أو {FIELDS['service_col']}")
    if not indices.get("amount_col"):
        problems.append("حدد عمود المبلغ المسجل بالشيت.")
        missing_columns.append(FIELDS["amount_col"])
    if amount_target not in {"contract", "remaining", "smart_dual"}:
        problems.append("اختر المبلغ المطلوب مراجعته.")
    distinct_amt2 = indices.get("amount_col_2")
    if distinct_amt2 and distinct_amt2 == indices.get("amount_col") and not (mapping and mapping.get("amount_col_2")):
        distinct_amt2 = None

    if amount_target in {"contract", "smart_dual"} and not distinct_amt2:
        problems.append("حدد المبلغ الثاني أو اختر المبلغ المسجل بالشيت.")
        missing_columns.append(FIELDS["amount_col_2"])
    numeric_columns = {col for col in (indices.get("amount_col"), distinct_amt2) if col}
    if any(indices.get(k) in numeric_columns for k in required if indices.get(k)):
        problems.append("عمود الرقم يجب أن يختلف عن عمود المبلغ.")
    return problems, missing_columns


class SharedWorkbookCache:
    """Thread-safe, memory-bounded, LRU cache for unified raw sheet data."""

    def __init__(self, max_memory_bytes: int = 50 * 1024 * 1024) -> None:
        self.max_memory_bytes = max_memory_bytes
        self._raw_cache: Dict[Tuple[str, int], RawSheetData] = {}
        self._validation_cache: Dict[Tuple[str, int, float, int, Tuple, Tuple], Dict[str, Any]] = {}
        self._validation_access: Dict[Tuple[str, int, float, int, Tuple, Tuple], float] = {}
        self._summary_cache: Dict[Tuple[str, int, float, int], Dict[str, Any]] = {}
        self._sheet_names_cache: Dict[Tuple[str, float, int], List[str]] = {}
        self._global_lock = threading.RLock()
        self._in_flight_locks: Dict[Tuple[str, int], threading.Lock] = {}
        self._frozen_tasks: Dict[str, FrozenTaskConfig] = {}

        # Telemetry
        self.cache_hits: int = 0
        self.cache_misses: int = 0
        self.evictions: int = 0
        self.excel_open_count: int = 0
        self.validation_cache_hits: int = 0
        self.validation_cache_misses: int = 0
        self.precomputed_hits: int = 0
        self.validation_row_passes: int = 0

    def clear(self) -> None:
        with self._global_lock:
            self._raw_cache.clear()
            self._validation_cache.clear()
            self._validation_access.clear()
            self._summary_cache.clear()
            self._sheet_names_cache.clear()
            self._frozen_tasks.clear()
            self.cache_hits = 0
            self.cache_misses = 0
            self.evictions = 0
            self.excel_open_count = 0
            self.validation_cache_hits = 0
            self.validation_cache_misses = 0
            self.precomputed_hits = 0
            self.validation_row_passes = 0

    def get_memory_usage(self) -> int:
        raw_bytes = sum(item.memory_bytes for item in self._raw_cache.values())
        val_bytes = calculate_deep_size(self._validation_cache)
        sum_bytes = calculate_deep_size(self._summary_cache)
        return raw_bytes + val_bytes + sum_bytes

    def _get_flight_lock(self, key: Tuple[str, int]) -> threading.Lock:
        with self._global_lock:
            if key not in self._in_flight_locks:
                self._in_flight_locks[key] = threading.Lock()
            return self._in_flight_locks[key]

    def _enforce_memory_limit_locked(self, incoming_bytes: int = 0) -> None:
        # Step 1: Evict oldest raw sheets while memory budget exceeded
        while (self.get_memory_usage() + incoming_bytes) > self.max_memory_bytes and self._raw_cache:
            oldest_key = min(self._raw_cache.keys(), key=lambda k: self._raw_cache[k].last_accessed)
            evicted = self._raw_cache.pop(oldest_key)
            self.evictions += 1
            # Preserve lightweight summary (~4 KB) so that option switching never re-reads from disk!
            sum_key = (oldest_key[0], oldest_key[1], evicted.mtime, evicted.file_size)
            self._summary_cache[sum_key] = {
                "validations": dict(evicted.precomputed_validations),
                "indices": dict(evicted.detected_indices),
                "letters": dict(evicted.detected_letters),
                "total_rows": evicted.total_populated_rows,
                "max_column": evicted.max_column,
                "default_schema": dict(evicted.default_schema) if evicted.default_schema else None,
                "sheet_name": evicted.sheet_name,
                "last_accessed": time.time(),
            }
            # Purge memoized custom validation entries for this evicted sheet
            to_del = [k for k in self._validation_cache if k[0] == oldest_key[0] and k[1] == oldest_key[1]]
            for k in to_del:
                del self._validation_cache[k]
                self._validation_access.pop(k, None)
            logger.info(f"LRU Evicted raw cache entry {oldest_key} freeing {evicted.memory_bytes} bytes. Preserved summary cache.")

        # Step 2: If still over memory budget, evict oldest custom validation entries
        while self.get_memory_usage() > self.max_memory_bytes and self._validation_cache:
            oldest_val_key = min(self._validation_cache.keys(), key=lambda k: self._validation_access.get(k, 0.0))
            del self._validation_cache[oldest_val_key]
            self._validation_access.pop(oldest_val_key, None)

        # Step 3: If still over memory budget and no raw sheets remain, evict oldest summaries
        while self.get_memory_usage() > self.max_memory_bytes and not self._raw_cache and self._summary_cache:
            oldest_sum_key = min(self._summary_cache.keys(), key=lambda k: self._summary_cache[k].get("last_accessed", 0.0))
            del self._summary_cache[oldest_sum_key]

        # Step 4: Cap entry counts bounds
        while len(self._validation_cache) > 500:
            oldest_val_key = min(self._validation_cache.keys(), key=lambda k: self._validation_access.get(k, 0.0))
            del self._validation_cache[oldest_val_key]
            self._validation_access.pop(oldest_val_key, None)

        while len(self._summary_cache) > 100:
            oldest_sum_key = min(self._summary_cache.keys(), key=lambda k: self._summary_cache[k].get("last_accessed", 0.0))
            del self._summary_cache[oldest_sum_key]

    def get_or_load_raw(self, file_path: Path | str, sheet_index: int = 0) -> RawSheetData:
        resolved_path = Path(file_path).resolve()
        if not resolved_path.exists():
            raise FileNotFoundError(f"ملف الإكسل غير موجود: {resolved_path}")

        key = (str(resolved_path), sheet_index)
        flight_lock = self._get_flight_lock(key)

        with flight_lock:
            st = resolved_path.stat()
            curr_mtime = st.st_mtime
            curr_size = st.st_size

            with self._global_lock:
                existing = self._raw_cache.get(key)
                if existing is not None:
                    # Invalidate if file modified or size changed (with floating-point timestamp tolerance)
                    mtime_matches = (existing.mtime == curr_mtime or abs(existing.mtime - curr_mtime) < 0.001)
                    if mtime_matches and existing.file_size == curr_size:
                        self.cache_hits += 1
                        existing.touch()
                        return existing
                    else:
                        logger.info(f"File {resolved_path} changed on disk. Invalidating cache entry.")
                        del self._raw_cache[key]
                        # Invalidate validation memo for this file
                        to_del = [k for k in self._validation_cache if k[0] == str(resolved_path) and k[1] == sheet_index]
                        for k in to_del:
                            del self._validation_cache[k]

            # Cache Miss: Single-flight disk read
            self.cache_misses += 1
            raw_data = self._read_raw_from_disk(resolved_path, sheet_index, curr_mtime, curr_size)

            with self._global_lock:
                self._enforce_memory_limit_locked(incoming_bytes=raw_data.memory_bytes)
                if raw_data.memory_bytes <= self.max_memory_bytes:
                    self._raw_cache[key] = raw_data
                else:
                    sum_key = (key[0], key[1], curr_mtime, curr_size)
                    self._summary_cache[sum_key] = {
                        "validations": dict(raw_data.precomputed_validations),
                        "indices": dict(raw_data.detected_indices),
                        "letters": dict(raw_data.detected_letters),
                        "total_rows": raw_data.total_populated_rows,
                        "max_column": raw_data.max_column,
                        "default_schema": dict(raw_data.default_schema) if raw_data.default_schema else None,
                        "sheet_name": raw_data.sheet_name,
                        "last_accessed": time.time(),
                    }
                    logger.warning(
                        f"Sheet memory footprint ({raw_data.memory_bytes} bytes) exceeds maximum cache limit "
                        f"({self.max_memory_bytes} bytes). Preserved summary cache ({len(raw_data.precomputed_validations)} combinations) "
                        f"and returning active sheet data."
                    )

            return raw_data

    def get_sheet_names(self, file_path: Path | str) -> List[str]:
        """Returns sheet names without reading rows or loading full raw data into memory.
        Fast inspection (< 2ms) memoized by (path, mtime, size).
        """
        p = Path(file_path).resolve()
        if not p.exists():
            return []
        st = p.stat()
        key = (str(p), st.st_mtime, st.st_size)
        with self._global_lock:
            if key in self._sheet_names_cache:
                return list(self._sheet_names_cache[key])
            for (f_str, idx), raw in self._raw_cache.items():
                if f_str == str(p) and raw.sheet_names:
                    self._sheet_names_cache[key] = list(raw.sheet_names)
                    return list(raw.sheet_names)

        names: List[str] = []
        try:
            import python_calamine
            cal_wb = python_calamine.CalamineWorkbook.from_path(str(p))
            names = list(cal_wb.sheet_names)
        except Exception:
            try:
                from openpyxl import load_workbook
                wb = load_workbook(p, read_only=True, keep_links=False)
                names = list(wb.sheetnames)
                wb.close()
            except Exception as e:
                logger.warning(f"Could not inspect sheet names for {p}: {e}")
                names = []

        if names:
            with self._global_lock:
                self._sheet_names_cache[key] = list(names)
        return list(names)

    def authorize_in_place_update(self, file_path: Path | str, new_mtime: float, new_size: int) -> None:
        """Updates mtime and file_size for cached entries of file_path after an authorized in-place update.
        This preserves the loaded raw sheet in-memory and ensures subsequent get_or_load_raw calls
        hit the cache with 0 disk reads.
        """
        resolved_path = str(Path(file_path).resolve())
        with self._global_lock:
            updated_count = 0
            for (fp, idx), entry in self._raw_cache.items():
                if fp == resolved_path:
                    entry.mtime = new_mtime
                    entry.file_size = new_size
                    entry.touch()
                    updated_count += 1
            if updated_count > 0:
                logger.info(
                    f"Authorized in-place update for {resolved_path}: updated {updated_count} cache entries "
                    f"to mtime={new_mtime}, size={new_size}"
                )

    def _read_raw_from_disk(
        self, file_path: Path, sheet_index: int, mtime: float, file_size: int
    ) -> RawSheetData:
        self.excel_open_count += 1
        raw_rows = None
        sheet_name = ""
        total_sheets = 1
        sheet_names = ()

        # 1. High-performance Rust Calamine engine
        try:
            import python_calamine
            cal_wb = python_calamine.CalamineWorkbook.from_path(str(file_path))
            sheet_names = tuple(cal_wb.sheet_names)
            total_sheets = len(sheet_names)
            eff_idx = sheet_index if 0 <= sheet_index < total_sheets else 0
            sheet_name = sheet_names[eff_idx]
            cal_sheet = cal_wb.get_sheet_by_index(eff_idx)
            raw_rows = tuple(tuple(None if cell == "" else cell for cell in r) for r in cal_sheet.to_python(skip_empty_area=False))
        except Exception as cal_err:
            logger.warning(f"Calamine loader failed or unavailable for {file_path}: {cal_err}. Falling back to openpyxl.")

        # 2. openpyxl fallback if Calamine not available or failed
        if raw_rows is None:
            wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
            try:
                total_sheets = len(wb.worksheets)
                sheet_names = tuple(ws.title for ws in wb.worksheets)
                ws = wb.worksheets[sheet_index] if 0 <= sheet_index < total_sheets else wb.worksheets[0]
                sheet_name = ws.title

                # Read all rows into lightweight nested tuples
                rows_list = []
                for row in ws.iter_rows(values_only=True):
                    rows_list.append(tuple(row))
                raw_rows = tuple(rows_list)
            finally:
                wb.close()

        deep_size = calculate_deep_size(raw_rows)
        measured_bytes = deep_size + 2048
        max_col = max((len(r) for r in raw_rows), default=0) if raw_rows else 0

        raw_sheet = RawSheetData(
            file_path=file_path,
            sheet_index=sheet_index,
            sheet_name=sheet_name,
            total_sheets=total_sheets,
            sheet_names=sheet_names,
            mtime=mtime,
            file_size=file_size,
            raw_rows=raw_rows,
            memory_bytes=measured_bytes,
            _max_column=max_col,
        )
        with self._global_lock:
            if sheet_names:
                self._sheet_names_cache[(str(file_path), mtime, file_size)] = list(sheet_names)
        self._precompute_sheet_validations(raw_sheet)
        return raw_sheet

    def _precompute_sheet_validations(self, raw_sheet: RawSheetData) -> None:
        """Executes a single shared analysis pass over all data rows to precompute
        readiness, missing columns, warnings, and accepted record counts for all 9 known options.
        """
        detect_res = detect_smart_sheet_columns(raw_sheet, has_headers=True)
        detected = dict(detect_res["indices"])
        raw_sheet.detected_indices = detected
        raw_sheet.detected_letters = {k: get_column_letter(v) if v else None for k, v in detected.items()}

        width = raw_sheet.max_column or 0
        indices = {key: detected.get(key) for key in FIELDS}

        combo_col_problems: Dict[Tuple[str, str], List[str]] = {}
        combo_missing: Dict[Tuple[str, str], List[str]] = {}
        for m in ALL_KNOWN_MODES:
            for at in ALL_KNOWN_AMOUNT_TARGETS:
                p, mis = evaluate_column_requirements(indices, width, m, at)
                combo_col_problems[(m, at)] = p
                combo_missing[(m, at)] = mis

        raw_rows = raw_sheet.raw_rows
        data_rows = raw_rows[1:] if len(raw_rows) > 1 else ()

        mapped_indices = [v for v in indices.values() if v]
        total_populated = 0
        invalid_rows: Dict[Tuple[str, str], List[int]] = {combo: [] for combo in ALL_9_COMBOS}
        row_errors: Dict[Tuple[str, str], List[Dict[str, Any]]] = {combo: [] for combo in ALL_9_COMBOS}

        lookup_idx = indices.get("lookup_col")
        service_idx = indices.get("service_col")
        amt1_idx = indices.get("amount_col")
        amt2_idx = indices.get("amount_col_2")

        for row_number, values in enumerate(data_rows, start=2):
            if is_empty_input_row(values, mapped_indices):
                continue
            total_populated += 1

            val_lookup = values[lookup_idx - 1] if lookup_idx and lookup_idx <= len(values) else None
            val_service = values[service_idx - 1] if service_idx and service_idx <= len(values) else None
            val_amt1 = values[amt1_idx - 1] if amt1_idx and amt1_idx <= len(values) else None
            val_amt2 = values[amt2_idx - 1] if amt2_idx and amt2_idx <= len(values) else None

            account, service = parse_account_and_service(val_lookup)
            service_account, separate_service = parse_account_and_service(val_service)
            account = account if account.startswith("1") else ""
            service_account = service_account if service_account.startswith("1") else ""
            service = service if service.startswith("2") else ""
            separate_service = separate_service if separate_service.startswith("2") else ""

            valid_num_acc = bool(account)
            valid_num_srv = bool(separate_service)
            valid_num_hyb = bool(account or service or service_account or separate_service)

            num_valid_by_mode = {
                "account_only": valid_num_acc,
                "service_only": valid_num_srv,
                "smart_hybrid": valid_num_hyb,
            }

            money1_valid = bool(val_amt1 is not None and str(val_amt1).strip() and parse_money_to_halalas(val_amt1) is not None)
            money2_valid = bool(val_amt2 is not None and str(val_amt2).strip() and parse_money_to_halalas(val_amt2) is not None)

            money_valid_by_target = {
                "remaining": money1_valid,
                "contract": money2_valid,
                "smart_dual": (money1_valid and money2_valid),
            }

            for combo in ALL_9_COMBOS:
                if combo_col_problems[combo]:
                    continue
                m, at = combo
                v_num = num_valid_by_mode[m]
                v_money = money_valid_by_target[at]
                if not v_num or not v_money:
                    invalid_rows[combo].append(row_number)
                    if len(invalid_rows[combo]) <= 8:
                        reasons = []
                        if not v_num:
                            raw_number = val_service if m == "service_only" else val_lookup
                            if m == "smart_hybrid" and (raw_number is None or not str(raw_number).strip()):
                                raw_number = val_service
                            if m == "account_only" and not (account or service_account) and (service or separate_service):
                                reasons.append("الرقم يبدأ بـ٢ ولا يوجد رقم عقد (١)؛ اختر «العقود والخدمات» لفحصه")
                            elif m == "service_only" and not (service or separate_service) and (account or service_account):
                                reasons.append("الرقم يبدأ بـ١ ولا يوجد رقم خدمة (٢)؛ اختر «العقود والخدمات» لفحصه")
                            elif raw_number is None or not str(raw_number).strip():
                                reasons.append("رقم البحث فارغ")
                            else:
                                reasons.append("رقم البحث غير قابل للقراءة أو لا يبدأ بـ١ أو ٢")

                        money_keys = ("amount_col_2",) if at == "contract" else (("amount_col", "amount_col_2") if at == "smart_dual" else ("amount_col",))
                        for key in money_keys:
                            v_m = val_amt2 if key == "amount_col_2" else val_amt1
                            if v_m is None or not str(v_m).strip():
                                reasons.append(FIELDS[key] + " فارغ")
                            elif parse_money_to_halalas(v_m) is None:
                                reasons.append(FIELDS[key] + " ليس مبلغًا صالحًا")
                        row_errors[combo].append({"row": row_number, "reasons": reasons})

        for combo in ALL_9_COMBOS:
            probs = list(combo_col_problems[combo])
            missing = list(combo_missing[combo])
            inv_list = invalid_rows[combo]
            errs = row_errors[combo]

            if not probs:
                if not total_populated:
                    probs.append("لا توجد صفوف بيانات للفحص.")
                elif inv_list:
                    probs.append(f"{len(inv_list):,} صف يحتاج تصحيحًا. " + " · ".join(
                        f"الصف {item['row']}: " + "، ".join(item["reasons"]) for item in errs
                    ))

            is_ready = bool(not probs and total_populated > 0 and len(inv_list) == 0)
            accepted = max(0, total_populated - len(inv_list)) if not combo_col_problems[combo] else 0

            raw_sheet.precomputed_validations[combo] = {
                "ok": is_ready,
                "is_ready": is_ready,
                "missing_columns": missing,
                "problems": probs,
                "warnings": probs,
                "rows": total_populated,
                "invalid_rows": len(inv_list),
                "accepted_records": accepted,
                "row_errors": errs,
            }

        raw_sheet.total_populated_rows = total_populated
        self.validation_row_passes += 1
        raw_sheet.memory_bytes += calculate_deep_size(raw_sheet.precomputed_validations)
        raw_sheet.default_schema = self.interpret_schema(raw_sheet, has_headers=True, custom_mapping=None, count_rows=False)

    def _validate_custom_mapping(
        self,
        raw_sheet: RawSheetData,
        indices: Dict[str, Optional[int]],
        mode: str,
        amount_target: str,
        has_headers: bool = True,
        mapping: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Validates custom mapped columns entirely in-memory from cached RawSheetData with 0 disk reads."""
        width = raw_sheet.max_column or 0
        problems, missing_columns = evaluate_column_requirements(indices, width, mode, amount_target, mapping)

        first_row = 2 if has_headers else 1
        raw_rows = raw_sheet.raw_rows
        data_rows = raw_rows[first_row - 1:] if len(raw_rows) >= first_row else ()
        mapped_indices = [v for v in indices.values() if v]

        if problems:
            populated = sum(not is_empty_input_row(vals, mapped_indices) for vals in data_rows)
            return {
                "ok": False,
                "is_ready": False,
                "missing_columns": missing_columns,
                "problems": problems,
                "warnings": problems,
                "rows": populated,
                "invalid_rows": 0,
                "accepted_records": 0,
                "row_errors": [],
            }

        lookup_idx = indices.get("lookup_col")
        service_idx = indices.get("service_col")
        amt1_idx = indices.get("amount_col")
        amt2_idx = indices.get("amount_col_2")
        money_keys = ("amount_col_2",) if amount_target == "contract" else (("amount_col", "amount_col_2") if amount_target == "smart_dual" else ("amount_col",))

        total_rows = 0
        invalid = []
        row_errors = []

        for row_number, values in enumerate(data_rows, start=first_row):
            if is_empty_input_row(values, mapped_indices):
                continue
            total_rows += 1

            val_lookup = values[lookup_idx - 1] if lookup_idx and lookup_idx <= len(values) else None
            val_service = values[service_idx - 1] if service_idx and service_idx <= len(values) else None

            account, service = parse_account_and_service(val_lookup)
            service_account, separate_service = parse_account_and_service(val_service)
            account = account if account.startswith("1") else ""
            service_account = service_account if service_account.startswith("1") else ""
            service = service if service.startswith("2") else ""
            separate_service = separate_service if separate_service.startswith("2") else ""

            valid_number = (
                account if mode == "account_only"
                else separate_service if mode == "service_only"
                else (account or service or service_account or separate_service)
            )

            def get_cell(k: str):
                idx = indices.get(k)
                return values[idx - 1] if idx and idx <= len(values) else None

            valid_money = all(
                get_cell(k) is not None
                and str(get_cell(k)).strip()
                and parse_money_to_halalas(get_cell(k)) is not None
                for k in money_keys
            )

            if not valid_number or not valid_money:
                invalid.append(row_number)
                if len(invalid) <= 8:
                    reasons = []
                    if not valid_number:
                        raw_number = val_service if mode == "service_only" else val_lookup
                        if mode == "smart_hybrid" and (raw_number is None or not str(raw_number).strip()):
                            raw_number = val_service
                        if mode == "account_only" and not (account or service_account) and (service or separate_service):
                            reasons.append("الرقم يبدأ بـ٢ ولا يوجد رقم عقد (١)؛ اختر «العقود والخدمات» لفحصه")
                        elif mode == "service_only" and not (service or separate_service) and (account or service_account):
                            reasons.append("الرقم يبدأ بـ١ ولا يوجد رقم خدمة (٢)؛ اختر «العقود والخدمات» لفحصه")
                        elif raw_number is None or not str(raw_number).strip():
                            reasons.append("رقم البحث فارغ")
                        else:
                            reasons.append("رقم البحث غير قابل للقراءة أو لا يبدأ بـ١ أو ٢")
                    for k in money_keys:
                        val_m = get_cell(k)
                        if val_m is None or not str(val_m).strip():
                            reasons.append(FIELDS[k] + " فارغ")
                        elif parse_money_to_halalas(val_m) is None:
                            reasons.append(FIELDS[k] + " ليس مبلغًا صالحًا")
                    row_errors.append({"row": row_number, "reasons": reasons})

        probs = []
        if not total_rows:
            probs.append("لا توجد صفوف بيانات للفحص.")
        elif invalid:
            probs.append(
                f"{len(invalid):,} صف يحتاج تصحيحًا. " + " · ".join(
                    f"الصف {item['row']}: " + "، ".join(item["reasons"]) for item in row_errors
                )
            )

        is_ok = bool(not probs and total_rows > 0 and len(invalid) == 0)
        accepted = max(0, total_rows - len(invalid))

        self.validation_row_passes += 1
        return {
            "ok": is_ok,
            "is_ready": is_ok,
            "missing_columns": missing_columns,
            "problems": probs,
            "warnings": probs,
            "rows": total_rows,
            "invalid_rows": len(invalid),
            "accepted_records": accepted,
            "row_errors": row_errors,
        }

    def get_validation_result(
        self,
        raw_sheet: RawSheetData,
        mapping: Optional[Dict[str, Any]] = None,
        mode: str = "smart_hybrid",
        amount_target: str = "remaining",
        has_headers: bool = True,
    ) -> Dict[str, Any]:
        """Returns precomputed validation result for known options in O(1) (< 0.1ms),
        or computes and memoizes custom column variations in-memory with zero disk reads.
        """
        mode = mode or "smart_hybrid"
        amount_target = amount_target or "remaining"
        combo = (mode, amount_target)

        # Check if mapping matches detected defaults exactly
        is_default = (has_headers is True)
        if is_default and mapping:
            for k in FIELDS:
                mapped_val = mapping.get(k)
                if mapped_val is not None:
                    p_idx = parse_excel_column(mapped_val)
                    if p_idx != raw_sheet.detected_indices.get(k):
                        is_default = False
                        break

        # Fast path 1: Instant O(1) hit from precomputed validations
        if is_default and combo in raw_sheet.precomputed_validations:
            self.precomputed_hits += 1
            self.validation_cache_hits += 1
            res = dict(raw_sheet.precomputed_validations[combo])
            res["problems"] = list(res["problems"])
            res["warnings"] = list(res["warnings"])
            res["missing_columns"] = list(res["missing_columns"])
            res["row_errors"] = list(res["row_errors"])
            return res

        # Fast path 2: Check summary cache for evicted or oversized sheet
        sum_key = (str(raw_sheet.file_path), raw_sheet.sheet_index, raw_sheet.mtime, raw_sheet.file_size)
        if is_default and sum_key in self._summary_cache:
            sum_entry = self._summary_cache[sum_key]
            if combo in sum_entry["validations"]:
                self.precomputed_hits += 1
                self.validation_cache_hits += 1
                res = dict(sum_entry["validations"][combo])
                res["problems"] = list(res["problems"])
                res["warnings"] = list(res["warnings"])
                res["missing_columns"] = list(res["missing_columns"])
                res["row_errors"] = list(res["row_errors"])
                return res

        # Custom mapping path: resolve effective indices without re-detecting
        effective_indices = dict(raw_sheet.detected_indices)
        if mapping:
            for k in FIELDS:
                if mapping.get(k) is not None:
                    effective_indices[k] = parse_excel_column(mapping[k])

        # Custom mapping memoization key
        col_key = tuple(sorted((k, v) for k, v in effective_indices.items() if v))
        opt_key = (mode, amount_target, has_headers)
        memo_key = (str(raw_sheet.file_path), raw_sheet.sheet_index, raw_sheet.mtime, raw_sheet.file_size, col_key, opt_key)

        with self._global_lock:
            cached = self._validation_cache.get(memo_key)
            if cached is not None:
                self.validation_cache_hits += 1
                self._validation_access[memo_key] = time.time()
                res = dict(cached)
                res["problems"] = list(res["problems"])
                res["warnings"] = list(res["warnings"])
                res["missing_columns"] = list(res["missing_columns"])
                res["row_errors"] = list(res.get("row_errors", []))
                res["from_validation_cache"] = True
                return res

        # Cache miss for custom mapping
        self.validation_cache_misses += 1
        result = self._validate_custom_mapping(
            raw_sheet, effective_indices, mode, amount_target, has_headers=has_headers, mapping=mapping
        )

        with self._global_lock:
            self._validation_cache[memo_key] = result
            self._validation_access[memo_key] = time.time()
            self._enforce_memory_limit_locked()

        res = dict(result)
        res["problems"] = list(res["problems"])
        res["warnings"] = list(res["warnings"])
        res["missing_columns"] = list(res["missing_columns"])
        res["row_errors"] = list(res["row_errors"])
        return res

    def get_validation_by_path(
        self,
        file_path: Path | str,
        sheet_index: int = 0,
        mapping: Optional[Dict[str, Any]] = None,
        mode: str = "smart_hybrid",
        amount_target: str = "remaining",
        has_headers: bool = True,
    ) -> Dict[str, Any]:
        """Validates a sheet by file path, checking summary cache and validation cache
        BEFORE loading rows or opening the workbook from disk.
        Enables true O(1) response (< 0.1ms) with 0 disk opens even if raw rows were evicted!
        """
        resolved_path = Path(file_path).resolve()
        if not resolved_path.exists():
            raise FileNotFoundError(f"ملف الإكسل غير موجود: {resolved_path}")

        st = resolved_path.stat()
        curr_mtime = st.st_mtime
        curr_size = st.st_size

        key = (str(resolved_path), sheet_index)
        sum_key = (str(resolved_path), sheet_index, curr_mtime, curr_size)
        mode = mode or "smart_hybrid"
        amount_target = amount_target or "remaining"
        combo = (mode, amount_target)
        combo_str = f"{mode}:{amount_target}"

        # 1. Fast Path: Check if raw sheet is already in _raw_cache and fresh
        existing = None
        with self._global_lock:
            cached = self._raw_cache.get(key)
            if cached is not None:
                mtime_matches = (cached.mtime == curr_mtime or abs(cached.mtime - curr_mtime) < 0.001)
                if mtime_matches and cached.file_size == curr_size:
                    cached.touch()
                    existing = cached

        if existing is not None:
            res = self.get_validation_result(
                existing, mapping=mapping, mode=mode, amount_target=amount_target, has_headers=has_headers
            )
            res["sheet_name"] = existing.sheet_name
            res["letters"] = dict(existing.detected_letters)
            return res

        # 2. Summary Cache Path: Check if precomputed summary exists (evicted or oversized sheet)
        with self._global_lock:
            summary = self._summary_cache.get(sum_key)
            if summary is not None:
                summary["last_accessed"] = time.time()
                detected_letters = summary.get("letters", {})

                # Check if mapping matches detected defaults exactly
                is_default = (has_headers is True)
                if is_default and mapping:
                    for k in FIELDS:
                        val = mapping.get(k)
                        if val is not None and str(val).strip():
                            if str(val).strip().upper() != str(detected_letters.get(k, "")).strip().upper():
                                is_default = False
                                break

                if is_default:
                    val_res = summary["validations"].get(combo) or summary["validations"].get(combo_str)
                    if val_res is not None:
                        self.precomputed_hits += 1
                        self.validation_cache_hits += 1
                        res = dict(val_res)
                        res["problems"] = list(res["problems"])
                        res["warnings"] = list(res["warnings"])
                        res["missing_columns"] = list(res["missing_columns"])
                        res["row_errors"] = list(res.get("row_errors", []))
                        res["from_summary_cache"] = True
                        res["sheet_name"] = summary.get("sheet_name", f"Sheet {sheet_index+1}")
                        res["letters"] = dict(detected_letters)
                        return res

                # Check if custom mapping is memoized in _validation_cache
                effective_indices = dict(summary.get("indices", {}))
                if mapping:
                    for k in FIELDS:
                        if mapping.get(k) is not None and str(mapping[k]).strip():
                            effective_indices[k] = parse_excel_column(mapping[k])

                col_key = tuple(sorted((k, v) for k, v in effective_indices.items() if v))
                opt_key = (mode, amount_target, has_headers)
                memo_key = (str(resolved_path), sheet_index, curr_mtime, curr_size, col_key, opt_key)
                cached = self._validation_cache.get(memo_key)
                if cached is not None:
                    self.validation_cache_hits += 1
                    self._validation_access[memo_key] = time.time()
                    res = dict(cached)
                    res["problems"] = list(res["problems"])
                    res["warnings"] = list(res["warnings"])
                    res["missing_columns"] = list(res["missing_columns"])
                    res["row_errors"] = list(res.get("row_errors", []))
                    res["from_validation_cache"] = True
                    res["sheet_name"] = summary.get("sheet_name", f"Sheet {sheet_index+1}")
                    res["letters"] = dict(detected_letters)
                    return res

                # Check if custom mapping has out-of-bounds column using cached max_column
                max_col = summary.get("max_column", 0)
                if max_col > 0:
                    probs, missing = evaluate_column_requirements(
                        indices=effective_indices,
                        width=max_col,
                        mode=mode,
                        amount_target=amount_target,
                        mapping=mapping,
                    )
                    if probs:
                        return {
                            "ok": False,
                            "is_ready": False,
                            "missing_columns": missing,
                            "problems": probs,
                            "warnings": probs,
                            "rows": 0,
                            "invalid_rows": 0,
                            "accepted_records": 0,
                            "row_errors": [],
                            "sheet_name": summary.get("sheet_name", f"Sheet {sheet_index+1}"),
                            "letters": dict(detected_letters),
                            "from_summary_cache": True,
                        }

        # 3. Cache Miss: Only load raw sheet from disk if neither raw cache nor summary cache can satisfy
        raw_sheet = self.get_or_load_raw(resolved_path, sheet_index=sheet_index)
        res = self.get_validation_result(
            raw_sheet, mapping=mapping, mode=mode, amount_target=amount_target, has_headers=has_headers
        )
        res["sheet_name"] = raw_sheet.sheet_name
        res["letters"] = dict(raw_sheet.detected_letters)
        return res

    # -------------------------------------------------------------------------
    # In-Memory Schema Interpretation (Zero Disk I/O)
    # -------------------------------------------------------------------------
    def interpret_schema(
        self,
        raw_sheet: RawSheetData,
        has_headers: bool = True,
        custom_mapping: Optional[Dict[str, Any]] = None,
        count_rows: bool = True,
    ) -> Dict[str, Any]:
        """Interprets schema and column layout 100% in-memory from cached RawSheetData.
        Changing has_headers or custom_mapping re-interprets without reading the disk.
        """
        # Fast path: Return cached default schema instantly in O(1)
        is_default = (has_headers is True and not (custom_mapping and any(custom_mapping.get(k) for k in FIELDS)))
        if is_default and getattr(raw_sheet, "default_schema", None) is not None:
            res = dict(raw_sheet.default_schema)
            res["precomputed_validations"] = {
                f"{m}:{at}": dict(v)
                for (m, at), v in getattr(raw_sheet, "precomputed_validations", {}).items()
            }
            if count_rows:
                res["estimated_rows"] = raw_sheet.total_populated_rows
            return res

        raw_rows = raw_sheet.raw_rows
        first_data_row_idx = 1 if has_headers else 0
        total_rows = len(raw_rows)

        # 1. Header columns
        header_cols: List[Dict[str, Any]] = []
        header_row_vals: Tuple[Any, ...] = ()
        if has_headers and total_rows > 0:
            header_row_vals = raw_rows[0]
            for col_idx, val in enumerate(header_row_vals, start=1):
                col_letter = get_column_letter(col_idx)
                header_cols.append({
                    "column": col_letter,
                    "col_index": col_idx,
                    "header": str(val).strip() if val is not None else "",
                })
        elif not has_headers and total_rows > 0:
            max_cols = max(len(r) for r in raw_rows) if raw_rows else 0
            for col_idx in range(1, max_cols + 1):
                col_letter = get_column_letter(col_idx)
                header_cols.append({
                    "column": col_letter,
                    "col_index": col_idx,
                    "header": f"عمود {col_idx} [{col_letter}]",
                })

        # 2. Column Auto-detection via canonical domain engine
        detect_res = detect_smart_sheet_columns(raw_sheet, has_headers=has_headers)
        detected: Dict[str, Optional[int]] = dict(detect_res["indices"])

        # 3. Apply custom_mapping overrides if provided
        column_keys = {
            "lookup_col", "service_col", "amount_col", "amount_col_2", "customer_col",
            "collector_col", "contract_col", "remaining_col", "debt_col", "case_status_col",
            "main_status_col", "sub_status_col", "notes_col", "source_row_col",
            "national_id_col", "phones_col", "phone_col", "supervisor_col", "branch_col",
            "followup_date_col", "follow_date_col", "account", "service", "amount"
        }
        if custom_mapping:
            for k, col_val in custom_mapping.items():
                if k in column_keys and col_val:
                    parsed_idx = parse_excel_column(col_val)
                    if parsed_idx:
                        detected[k] = parsed_idx
            if custom_mapping.get("amount") and not custom_mapping.get("amount_col"):
                p_c = parse_excel_column(custom_mapping["amount"])
                if p_c: detected["amount_col"] = p_c
            if custom_mapping.get("amount_col_2"):
                p_c = parse_excel_column(custom_mapping["amount_col_2"])
                if p_c: detected["amount_col_2"] = p_c
            if custom_mapping.get("account"):
                p_c = parse_excel_column(custom_mapping["account"])
                if p_c: detected["lookup_col"] = p_c
            if custom_mapping.get("service"):
                p_c = parse_excel_column(custom_mapping["service"])
                if p_c: detected["service_col"] = p_c

            # UNIFIED PRECEDENCE: Explicit custom mapping strictly overrides auto-detected columns!
            explicit_amt1 = (
                parse_excel_column(custom_mapping.get("amount_col"))
                or parse_excel_column(custom_mapping.get("amount"))
                or parse_excel_column(custom_mapping.get("remaining_col"))
                or parse_excel_column(custom_mapping.get("debt_col"))
            )
            if explicit_amt1:
                detected["amount_col"] = explicit_amt1
                detected["remaining_col"] = explicit_amt1
                detected["debt_col"] = explicit_amt1

            explicit_amt2 = (
                parse_excel_column(custom_mapping.get("amount_col_2"))
                or parse_excel_column(custom_mapping.get("contract_col"))
            )
            if explicit_amt2:
                detected["amount_col_2"] = explicit_amt2
                detected["contract_col"] = explicit_amt2

        letters = {k: get_column_letter(v) if v else None for k, v in detected.items()}

        # 4. Mode Support & Document Type (Strict Production Contract)
        has_acc = bool(detected.get("lookup_col"))
        has_srv = bool(detected.get("service_col"))
        has_amt1 = bool(detected.get("amount_col"))
        has_amt2 = bool(detected.get("amount_col_2"))
        has_any_amt = bool(has_amt1 or has_amt2)

        norm_title = normalize_header_text(raw_sheet.sheet_name)
        is_errors_tab = any(k in norm_title for k in ("اخطاء", "ملاحظات", "errors", "notes"))
        is_results_tab = any(k in norm_title for k in ("فروقات", "نتائج", "results", "diff"))
        has_error_header = any(
            normalize_header_text(c.get("header") or "") in ("المشكله باختصار", "المشكلة باختصار", "سبب الخطا", "سبب الخطأ")
            for c in header_cols
        )
        has_live_header = any(
            "زين" in normalize_header_text(c.get("header") or "") or "فرق" in normalize_header_text(c.get("header") or "")
            for c in header_cols
        )

        if is_errors_tab or has_error_header:
            doc_type = "🛠️ شيت أخطاء وملاحظات فحص سابقة (Errors Sheet - جاهز لإعادة التدقيق ومعالجة الأخطاء)"
            sheet_kind = "errors_sheet"
        elif is_results_tab or has_live_header:
            doc_type = "📊 شيت نتائج وفروقات فحص سابقة (Results Sheet - جاهز لإعادة التدقيق والتحديث)"
            sheet_kind = "results_sheet"
        elif detected.get("lookup_col") and detected.get("lookup_col") == detected.get("service_col") and has_amt1:
            doc_type = "ملف أرقام ومبالغ"
            sheet_kind = "raw_portfolio"
        elif has_acc and has_srv and bool(detected.get("contract_col")):
            doc_type = "محفظة تحصيل مديونيات زين (شاملة أرقام الحسابات وعقود الخدمات ومبالغ العقود)"
            sheet_kind = "raw_portfolio"
        elif has_acc and has_srv:
            doc_type = "محفظة مديونيات هجينة (أرقام حسابات وأرقام خدمات)"
            sheet_kind = "raw_portfolio"
        elif has_acc:
            doc_type = "شيت حسابات زين (Account Numbers Sheet)"
            sheet_kind = "raw_portfolio"
        elif has_srv:
            doc_type = "شيت محافظ/خدمات زين (Wallets/Services Sheet)"
            sheet_kind = "raw_portfolio"
        else:
            doc_type = "شيت بيانات عام / بحاجة لضبط الأعمدة"
            sheet_kind = "unknown"

        is_acceptable = bool((has_acc or has_srv) and has_any_amt)
        ver_status = "✓ الشيت صالح ومقبول للتدقيق (Valid & Acceptable)" if is_acceptable else "⚠️ الشيت غير مكتمل (ينقص عمود الحساب/الخدمة أو عمود المبلغ)"

        # Strict Production Contract:
        # smart_hybrid requires BOTH account AND service, plus at least one amount column!
        supported = {
            "smart_hybrid": bool(has_acc and has_srv and has_any_amt),
            "account_only": bool(has_acc and has_any_amt),
            "service_only": bool(has_srv and has_any_amt),
        }

        if supported["smart_hybrid"]:
            rec_mode = "smart_hybrid"
        elif supported["account_only"]:
            rec_mode = "account_only"
        elif supported["service_only"]:
            rec_mode = "service_only"
        else:
            rec_mode = "custom"

        missing = {
            "smart_hybrid": [],
            "account_only": [],
            "service_only": [],
        }
        if not has_acc:
            missing["smart_hybrid"].append("رقم الحساب / العقد")
            missing["account_only"].append("رقم الحساب / العقد")
        if not has_srv:
            missing["smart_hybrid"].append("رقم الخدمة")
            missing["service_only"].append("رقم الخدمة")
        if not has_any_amt:
            missing["smart_hybrid"].append("المبلغ (باقي السداد أو مبلغ العقد)")
            missing["account_only"].append("المبلغ (باقي السداد أو مبلغ العقد)")
            missing["service_only"].append("المبلغ (باقي السداد أو مبلغ العقد)")

        # 5. Amount options
        contract_c = detected.get("contract_col") or detected.get("amount_col_2")
        remaining_c = detected.get("remaining_col") or detected.get("debt_col") or detected.get("amount_col")
        amt_options = []
        if contract_c:
            contract_l = get_column_letter(contract_c)
            amt_options.append({
                "id": "contract",
                "col_index": contract_c,
                "letter": contract_l,
                "label": f"مبلغ العقد [{contract_l}] (المطابق لموقع زين عند البحث برقم الحساب)",
            })
        if remaining_c and remaining_c != contract_c:
            rem_l = get_column_letter(remaining_c)
            amt_options.append({
                "id": "remaining",
                "col_index": remaining_c,
                "letter": rem_l,
                "label": f"باقي السداد الموثق [{rem_l}]",
            })
        if contract_c and remaining_c and remaining_c != contract_c:
            contract_l = get_column_letter(contract_c)
            rem_l = get_column_letter(remaining_c)
            amt_options.append({
                "id": "smart_dual",
                "col_index": contract_c,
                "letter": f"{contract_l}+{rem_l}",
                "label": f"كلاهما (فحص ذكي مزدوج: {contract_l} أو {rem_l} أيهما أقرب)",
            })

        # 6. Preview rows (first 5 data rows)
        preview_rows = []
        data_rows = raw_rows[first_data_row_idx:]
        for row_idx, r_vals in enumerate(data_rows[:5], start=first_data_row_idx + 1):
            preview_rows.append({
                "row_number": row_idx,
                "values": [str(v) if v is not None else "" for v in r_vals],
            })

        # 7. Estimated meaningful rows
        mapped_indices = [v for v in detected.values() if v]
        est_rows = None
        if count_rows:
            if getattr(raw_sheet, "total_populated_rows", 0) > 0 and not custom_mapping:
                est_rows = raw_sheet.total_populated_rows
            else:
                count = 0
                for r_vals in data_rows:
                    if not is_empty_input_row(r_vals, mapped_indices):
                        count += 1
                est_rows = count

        return {
            "columns": header_cols,
            "has_headers": has_headers,
            "preview_rows": preview_rows,
            "indices": detected,
            "letters": letters,
            "document_type": doc_type,
            "is_acceptable": is_acceptable,
            "verification_status": ver_status,
            "estimated_rows": est_rows,
            "supported_modes": supported,
            "recommended_mode": rec_mode,
            "missing_for_modes": missing,
            "amount_options": amt_options,
            "precomputed_validations": {
                f"{m}:{at}": dict(v)
                for (m, at), v in getattr(raw_sheet, "precomputed_validations", {}).items()
            },
            "sheet_name": raw_sheet.sheet_name,
            "total_sheets": raw_sheet.total_sheets,
            "sheet_names": list(raw_sheet.sheet_names),
            "has_customer_col": bool(detected.get("customer_col")),
            "has_collector_col": bool(detected.get("collector_col")),
            "contract_amount_col": contract_c,
            "contract_amount_letter": get_column_letter(contract_c) if contract_c else None,
            "remaining_amount_col": remaining_c,
            "remaining_amount_letter": get_column_letter(remaining_c) if remaining_c else None,
            "source_row_col": detected.get("source_row_col"),
            "source_row_letter": letters.get("source_row_col"),
            "file_path": str(raw_sheet.file_path),
            "sheet_index": raw_sheet.sheet_index,
            "mtime": raw_sheet.mtime,
            "file_size": raw_sheet.file_size,
        }

    # -------------------------------------------------------------------------
    # In-Memory Customer Extraction (Task Inputs from Cache)
    # -------------------------------------------------------------------------
    def extract_customers_from_cache(
        self,
        raw_sheet: RawSheetData,
        mapping: Dict[str, Any],
        mode: str = "smart_hybrid",
        amount_target: str = "contract",
        has_headers: bool = True,
        target_collector: str = "",
        min_debt: Optional[float] = None,
        max_debt: Optional[float] = None,
        filter_status: bool = False,
        status_col: Optional[Union[str, int]] = None,
        start_mode: str = "continue",
        start_value: Optional[Any] = None,
    ) -> List[Customer]:
        """Constructs Customer input records directly from in-memory RawSheetData,
        enforcing explicit column precedence, collector, min/max debt filters, status filtering, and start pointer.
        """
        schema = self.interpret_schema(raw_sheet, has_headers=has_headers, custom_mapping=mapping, count_rows=False)
        auto_letters = schema.get("letters") or {}
        merged_mapping = dict(auto_letters)
        merged_mapping.update(mapping or {})

        raw_rows = raw_sheet.raw_rows
        first_data_idx = 1 if has_headers else 0
        data_rows = raw_rows[first_data_idx:]

        # Explicit custom mapping precedence over auto-discovered indices
        explicit_amt1 = (
            parse_excel_column(mapping.get("amount_col"))
            or parse_excel_column(mapping.get("amount"))
            or parse_excel_column(mapping.get("remaining_col"))
            or parse_excel_column(mapping.get("debt_col"))
        ) if mapping else None

        explicit_amt2 = (
            parse_excel_column(mapping.get("amount_col_2"))
            or parse_excel_column(mapping.get("contract_col"))
        ) if mapping else None

        lookup_c = parse_excel_column(merged_mapping.get("lookup_col")) or parse_excel_column(merged_mapping.get("service_col"))
        amount_c = explicit_amt1 or parse_excel_column(merged_mapping.get("amount_col")) or parse_excel_column(merged_mapping.get("remaining_col"))
        amount2_c = explicit_amt2 or parse_excel_column(merged_mapping.get("amount_col_2")) or parse_excel_column(merged_mapping.get("contract_col"))
        service_c = parse_excel_column(merged_mapping.get("service_col"))
        customer_c = parse_excel_column(merged_mapping.get("customer_col"))
        collector_c = parse_excel_column(merged_mapping.get("collector_col"))
        case_c = parse_excel_column(merged_mapping.get("case_status_col"))
        main_c = parse_excel_column(merged_mapping.get("main_status_col"))
        sub_c = parse_excel_column(merged_mapping.get("sub_status_col"))
        notes_c = parse_excel_column(merged_mapping.get("notes_col"))
        source_row_c = parse_excel_column(merged_mapping.get("source_row_col"))
        nat_c = parse_excel_column(merged_mapping.get("national_id_col")) or parse_excel_column(merged_mapping.get("national_id"))
        phone_c = parse_excel_column(merged_mapping.get("phones_col")) or parse_excel_column(merged_mapping.get("phone_col"))
        sup_c = parse_excel_column(merged_mapping.get("supervisor_col"))
        branch_c = parse_excel_column(merged_mapping.get("branch_col"))
        date_c = parse_excel_column(merged_mapping.get("followup_date_col")) or parse_excel_column(merged_mapping.get("follow_date_col"))

        if mode == "account_only":
            service_c = None
        elif mode == "service_only" and service_c:
            lookup_c = service_c

        collector_map = {
            "all": "",
            "maha": "مها امير حسين محمد-M02253",
            "azza": "عزة مغربى محمد عبدالرحمن-M02254",
            "mawada": "مودة يحي تاج الدين فضيل-M02051",
        }
        raw_coll_target = target_collector or (mapping or {}).get("collector", "")
        eff_coll_target = collector_map.get(str(raw_coll_target).lower(), raw_coll_target)
        norm_target_collector = normalize_header_text(eff_coll_target) if eff_coll_target else ""

        # Debt filter thresholds in Halalas
        eff_min_debt = min_debt if min_debt is not None else (mapping or {}).get("min_debt")
        eff_max_debt = max_debt if max_debt is not None else (mapping or {}).get("max_debt")
        min_halalas = int(round(float(eff_min_debt) * 100)) if eff_min_debt is not None else None
        max_halalas = int(round(float(eff_max_debt) * 100)) if eff_max_debt is not None else None

        # Status filter
        eff_filter_status = filter_status or bool((mapping or {}).get("filter_status", False))
        raw_status_col = status_col or (mapping or {}).get("status_col") or (mapping or {}).get("main_status_col") or (mapping or {}).get("case_status_col")
        eff_status_c = parse_excel_column(raw_status_col) if raw_status_col else None

        customers: List[Customer] = []
        seen_non2_account_row: Set[str] = set()
        seen_service_row: Set[str] = set()

        is_shared = bool(not service_c or lookup_c == service_c)
        for r_idx, r_vals in enumerate(data_rows, start=first_data_idx + 1):
            def cell_val(c_idx: Optional[int]) -> Any:
                if c_idx and 1 <= c_idx <= len(r_vals):
                    return r_vals[c_idx - 1]
                return None

            raw_acc_val = cell_val(lookup_c)
            raw_srv_val = cell_val(service_c)
            raw_con_val = cell_val(parse_excel_column(mapping.get("contract_col"))) if mapping and mapping.get("contract_col") else None

            clean_acc, clean_srv, clean_con = parse_row_numbers(raw_acc_val, raw_srv_val, raw_con_val, is_shared_column=is_shared)

            if not clean_acc and not clean_srv:
                continue

            raw_amt1 = cell_val(amount_c)
            raw_amt2 = cell_val(amount2_c)
            h1 = parse_money_to_halalas(raw_amt1)
            h2 = parse_money_to_halalas(raw_amt2)

            # Strict Halalas calculations based on amount_target (no fallback to other amount or 0)
            if amount_target == "remaining":
                eff_halalas = h1
                sec_halalas = None
            elif amount_target == "contract":
                eff_halalas = h2
                sec_halalas = None
            else:  # "smart_dual"
                eff_halalas = h1 if h1 is not None else h2
                sec_halalas = h2 if h2 is not None else h1

            cust_name = str(cell_val(customer_c) or "").strip()
            coll_name = str(cell_val(collector_c) or "").strip()
            if norm_target_collector and norm_target_collector != "all":
                norm_coll = normalize_header_text(coll_name)
                if norm_target_collector not in norm_coll and norm_coll not in norm_target_collector:
                    continue

            # Status filter: keep only empty, hyphens, "عدم توصل", or "نشط"
            if eff_filter_status and eff_status_c:
                stat_val = str(cell_val(eff_status_c) or "").strip()
                if stat_val:
                    hyphens_only = bool(re.fullmatch(r"-+", stat_val.replace(" ", "")))
                    if not hyphens_only and "عدم توصل" not in stat_val and "نشط" not in stat_val:
                        continue

            # Debt limits filter
            if min_halalas is not None and eff_halalas is not None and eff_halalas < min_halalas:
                continue
            if max_halalas is not None and eff_halalas is not None and eff_halalas > max_halalas:
                continue

            # Route type & deduplication logic matching production
            if mode == "service_only":
                is_service_route = True
            elif mode == "account_only":
                is_service_route = False
            else:
                is_service_route = bool(clean_srv and clean_srv.startswith("2"))

            primary_search = clean_srv if is_service_route else (clean_acc or clean_srv)
            rec_type = "wallet" if is_service_route else ("account" if mode == "account_only" else "mixed")

            if is_service_route:
                if clean_srv in seen_service_row:
                    continue
                seen_service_row.add(clean_srv)
            else:
                if clean_acc and clean_acc in seen_non2_account_row:
                    continue
                if clean_acc:
                    seen_non2_account_row.add(clean_acc)
                if clean_srv:
                    seen_service_row.add(clean_srv)

            final_row_num = r_idx
            if source_row_c:
                try:
                    sr_val = cell_val(source_row_c)
                    if sr_val is not None and str(sr_val).strip().isdigit():
                        final_row_num = int(str(sr_val).strip())
                except Exception:
                    final_row_num = r_idx

            notes_str = str(cell_val(notes_c) or "").strip()
            customers.append(Customer(
                row_number=final_row_num,
                record_type=rec_type,
                lookup_number=primary_search,
                contract=clean_con or clean_acc or primary_search,
                expected_amount=eff_halalas,
                expected_amount_2=sec_halalas,
                original_account_number=clean_acc or primary_search,
                service_number=clean_srv,
                customer_name=cust_name or "عميل غير محدد",
                collector_name=coll_name,
                case_status=str(cell_val(case_c) or "").strip(),
                main_status=str(cell_val(main_c) or "").strip(),
                sub_status=str(cell_val(sub_c) or "").strip(),
                notes=notes_str,
                national_id=str(cell_val(nat_c) or "").strip() if nat_c else "",
                phones=str(cell_val(phone_c) or "").strip() if phone_c else "",
                supervisor_name=str(cell_val(sup_c) or "").strip() if sup_c else "",
                branch_name=str(cell_val(branch_c) or "").strip() if branch_c else "",
                followup_date=str(cell_val(date_c) or "").strip() if date_c else "",
                error_or_review_details=notes_str if notes_str else None,
            ))

        # Start pointer application
        eff_start_mode = start_mode or (mapping or {}).get("start_mode", "continue")
        eff_start_val = start_value if start_value is not None else (mapping or {}).get("start_value")

        if eff_start_mode in ("row", "4") and eff_start_val:
            try:
                target_r = int(str(eff_start_val).strip())
                start_i = next((i for i, c in enumerate(customers) if c.row_number >= target_r), None)
                if start_i is None:
                    raise ValueError(f"مؤشر البداية غير موجود في الورقة: رقم الصف {target_r}")
                customers = customers[start_i:]
            except ValueError as ve:
                if "مؤشر البداية" in str(ve):
                    raise
                raise ValueError(f"مؤشر البداية غير صحيح: {eff_start_val}")
        elif eff_start_mode in ("search", "3") and eff_start_val:
            s_val = str(eff_start_val).strip()
            start_i = next((i for i, c in enumerate(customers) if c.lookup_number == s_val or c.contract == s_val or c.original_account_number == s_val or c.service_number == s_val), None)
            if start_i is None:
                raise ValueError(f"مؤشر البداية غير موجود في الورقة: رقم الحساب/الخدمة {s_val}")
            customers = customers[start_i:]
        elif eff_start_mode in ("customer", "5") and eff_start_val:
            c_val = str(eff_start_val).strip()
            start_i = next((i for i, c in enumerate(customers) if c_val in c.customer_name), None)
            if start_i is None:
                raise ValueError(f"مؤشر البداية غير موجود في الورقة: اسم العميل {c_val}")
            customers = customers[start_i:]
        elif eff_start_mode in ("end", "6"):
            customers = list(reversed(customers))

        return customers

    # -------------------------------------------------------------------------
    # Recalculate Comparison in Memory (Zero Disk & Zero Network)
    # -------------------------------------------------------------------------
    def recalculate_comparison(
        self,
        raw_sheet: RawSheetData,
        amount_target: str,
        live_balances: Any,
        mapping: Optional[Dict[str, Any]] = None,
        query_type: Optional[str] = None,
        has_headers: bool = True,
    ) -> List[Dict[str, Any]]:
        """Recalculates comparison status entirely in-memory using pre-parsed Halalas.
        Strictly enforces comparison policy ('remaining' vs 'contract') and query isolation.
        Disambiguates balances by (query_type, number) or StructuredLiveBalance to prevent numeric collisions.
        """
        typed_balances: Dict[Tuple[str, str], float] = {}
        untyped_balances: Dict[str, float] = {}

        if isinstance(live_balances, dict):
            for k, v in live_balances.items():
                val = v.balance_sar if isinstance(v, StructuredLiveBalance) else float(v)
                if isinstance(k, tuple) and len(k) == 2:
                    q_t = str(k[0]).lower().strip()
                    num = re.sub(r"\D", "", str(k[1]))
                    typed_balances[(q_t, num)] = val
                elif isinstance(k, StructuredLiveBalance):
                    typed_balances[(k.query_type.lower().strip(), re.sub(r"\D", "", str(k.number)))] = float(k.balance_sar)
                elif isinstance(v, StructuredLiveBalance):
                    typed_balances[(v.query_type.lower().strip(), re.sub(r"\D", "", str(v.number)))] = float(v.balance_sar)
                else:
                    untyped_balances[re.sub(r"\D", "", str(k))] = val
        elif isinstance(live_balances, (list, tuple)):
            for item in live_balances:
                if isinstance(item, StructuredLiveBalance):
                    typed_balances[(item.query_type.lower().strip(), re.sub(r"\D", "", str(item.number)))] = float(item.balance_sar)
                elif isinstance(item, dict):
                    q_t = str(item.get("query_type", "")).lower().strip()
                    num = re.sub(r"\D", "", str(item.get("number", "")))
                    val = float(item.get("balance_sar", 0.0))
                    if q_t and num:
                        typed_balances[(q_t, num)] = val
                    elif num:
                        untyped_balances[num] = val

        schema = self.interpret_schema(raw_sheet, has_headers=has_headers, custom_mapping=mapping, count_rows=False)
        indices = schema["indices"]
        raw_rows = raw_sheet.raw_rows
        first_data_idx = 1 if has_headers else 0
        data_rows = raw_rows[first_data_idx:]

        # Explicit custom mapping precedence over auto-discovered indices
        explicit_rem = None
        explicit_con = None
        if mapping:
            explicit_rem = (
                parse_excel_column(mapping.get("amount_col"))
                or parse_excel_column(mapping.get("amount"))
                or parse_excel_column(mapping.get("remaining_col"))
                or parse_excel_column(mapping.get("debt_col"))
            )
            explicit_con = (
                parse_excel_column(mapping.get("amount_col_2"))
                or parse_excel_column(mapping.get("contract_col"))
            )

        acc_col = indices.get("lookup_col")
        srv_col = indices.get("service_col")
        cust_col = indices.get("customer_col")
        rem_col = explicit_rem or indices.get("remaining_col") or indices.get("debt_col") or indices.get("amount_col")
        con_col = explicit_con or indices.get("contract_col") or indices.get("amount_col_2")

        is_shared = bool(not srv_col or acc_col == srv_col)
        evaluated: List[Dict[str, Any]] = []

        for row_idx, r_vals in enumerate(data_rows, start=first_data_idx + 1):
            def cell_val(c_idx: Optional[int]) -> Any:
                if c_idx and 1 <= c_idx <= len(r_vals):
                    return r_vals[c_idx - 1]
                return None

            raw_acc_val = cell_val(acc_col)
            raw_srv_val = cell_val(srv_col)
            raw_cust_val = cell_val(cust_col)
            clean_acc, clean_srv, clean_con = parse_row_numbers(raw_acc_val, raw_srv_val, None, is_shared_column=is_shared)
            cust_name = str(raw_cust_val or "").strip() or "عميل غير محدد"

            # Determine lookup_number and record_type matching row identity
            is_service_row = bool(clean_srv and clean_srv.startswith("2"))
            if query_type == "service":
                lookup_num = clean_srv or clean_acc or "-"
                rec_type = "wallet" if str(lookup_num).startswith("2") else "service"
            elif query_type == "account":
                lookup_num = clean_acc or clean_srv or "-"
                rec_type = "account"
            else:
                if is_service_row:
                    lookup_num = clean_srv or "-"
                    rec_type = "wallet"
                else:
                    lookup_num = clean_acc or clean_srv or "-"
                    rec_type = "account"

            # Query Identity Isolation & Disambiguation
            live_sar: Optional[float] = None
            if query_type == "service":
                if clean_srv:
                    if ("service", clean_srv) in typed_balances:
                        live_sar = typed_balances[("service", clean_srv)]
                    elif clean_srv in untyped_balances:
                        live_sar = untyped_balances[clean_srv]
            elif query_type == "account":
                if clean_acc:
                    if ("account", clean_acc) in typed_balances:
                        live_sar = typed_balances[("account", clean_acc)]
                    elif clean_acc in untyped_balances:
                        live_sar = untyped_balances[clean_acc]
            else:
                if is_service_row and clean_srv:
                    if ("service", clean_srv) in typed_balances:
                        live_sar = typed_balances[("service", clean_srv)]
                    elif clean_srv in untyped_balances:
                        live_sar = untyped_balances[clean_srv]
                if live_sar is None and clean_acc:
                    if ("account", clean_acc) in typed_balances:
                        live_sar = typed_balances[("account", clean_acc)]
                    elif clean_acc in untyped_balances:
                        live_sar = untyped_balances[clean_acc]
                if live_sar is None and clean_srv:
                    if ("service", clean_srv) in typed_balances:
                        live_sar = typed_balances[("service", clean_srv)]
                    elif clean_srv in untyped_balances:
                        live_sar = untyped_balances[clean_srv]

            rem_h = parse_money_to_halalas(cell_val(rem_col))
            con_h = parse_money_to_halalas(cell_val(con_col))

            # Apply strict amount policy (no fallback across policies)
            if amount_target == "remaining":
                p_exp = rem_h
                s_exp = None
            elif amount_target == "contract":
                p_exp = con_h
                s_exp = None
            else:  # "smart_dual"
                p_exp = rem_h if rem_h is not None else con_h
                s_exp = con_h if con_h is not None else rem_h

            if live_sar is None:
                evaluated.append({
                    "row": row_idx,
                    "record_type": rec_type,
                    "lookup_number": lookup_num,
                    "customer_name": cust_name,
                    "account": clean_acc,
                    "service": clean_srv,
                    "expected_sar": p_exp / 100.0 if p_exp is not None else None,
                    "live_sar": None,
                    "diff_sar": None,
                    "status": "needs_review",
                    "status_label": "تحتاج مراجعة",
                })
                continue

            live_h = parse_money_to_halalas(live_sar) or 0

            if p_exp is None and s_exp is None:
                evaluated.append({
                    "row": row_idx,
                    "record_type": rec_type,
                    "lookup_number": lookup_num,
                    "customer_name": cust_name,
                    "account": clean_acc,
                    "service": clean_srv,
                    "expected_sar": None,
                    "live_sar": live_sar,
                    "diff_sar": None,
                    "status": "needs_review",
                    "status_label": "مبلغ غير محدد",
                })
                continue

            matched, eff_h, diff_h = is_amount_match(
                live_halalas=live_h,
                expected_primary=p_exp if p_exp is not None else (s_exp or 0),
                expected_secondary=s_exp,
                tolerance=20,
            )
            diff_sar = 0.0 if matched else round(diff_h / 100.0, 2)
            evaluated.append({
                "row": row_idx,
                "record_type": rec_type,
                "lookup_number": lookup_num,
                "customer_name": cust_name,
                "account": clean_acc,
                "service": clean_srv,
                "expected_sar": eff_h / 100.0,
                "live_sar": live_sar,
                "diff_sar": diff_sar,
                "status": "match" if matched else "mismatch",
                "status_label": "تطابق تام" if matched else "فرق رصيد",
            })

        return evaluated

    # -------------------------------------------------------------------------
    # Immutable Task Snapshot Management
    # -------------------------------------------------------------------------
    def snapshot_task_config(
        self,
        task_id: str,
        file_path: Path,
        sheet_index: int,
        mode: str,
        amount_target: str,
        column_mapping: Dict[str, Any],
        has_headers: bool = True,
    ) -> FrozenTaskConfig:
        """Deeply freezes task configuration using MappingProxyType."""
        frozen_mapping = MappingProxyType(dict(column_mapping))
        cfg = FrozenTaskConfig(
            task_id=task_id,
            file_path=file_path.resolve(),
            sheet_index=sheet_index,
            mode=mode,
            amount_target=amount_target,
            column_mapping=frozen_mapping,
            has_headers=has_headers,
        )
        with self._global_lock:
            self._frozen_tasks[task_id] = cfg
        return cfg

    def get_frozen_task_config(self, task_id: str) -> Optional[FrozenTaskConfig]:
        with self._global_lock:
            return self._frozen_tasks.get(task_id)

    def release_task_snapshot(self, task_id: str) -> None:
        with self._global_lock:
            self._frozen_tasks.pop(task_id, None)


# Global singleton instance
SHARED_WORKBOOK_CACHE = SharedWorkbookCache()
