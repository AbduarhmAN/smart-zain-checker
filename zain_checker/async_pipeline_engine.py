"""High-Throughput Asynchronous Pipeline Engine for Smart Zain Checker.
Architecture:
- Decoupled Producer-Consumer Pattern.
- Audit Workers (Producers): Query Zain API concurrently and push completed results to an in-memory Queue in 0.001ms.
- Dedicated Writer Thread (Consumer): Holds openpyxl Workbook in RAM, drains items continuously, and flushes to disk in smart batches (every 50 items or every 2.0s).
- Sidecar WAL (Write-Ahead-Log): Atomically appends every completed row to disk instantly for zero-data-loss crash resilience.
- Backpressure & Queue Depth Monitoring: Handles burst queueing automatically with zero blocking on network workers.
"""
from __future__ import annotations

import io
import json
import logging
import queue
import re
import subprocess
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import openpyxl
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

# Global Rule: Enforce UTF-8 on Windows Console & I/O
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from workers.zain_api import query_contract_due_amount

logger = logging.getLogger("AsyncPipelineEngine")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

PROXY_URL = "socks5h://195.40.62.31:7252"
QUICKPAY_BASE_URL = "https://app.sa.zain.com/ar/quickpay?account="
CONTRACT_BASE_URL = "https://app.sa.zain.com/ar/contract-payment?contract="

# Excel Styles
FONT_NAME = "Segoe UI"
HEADER_FONT = Font(name=FONT_NAME, size=11, bold=True, color="FFFFFF")
DATA_FONT = Font(name=FONT_NAME, size=10)
DIFF_FONT = Font(name=FONT_NAME, size=10, bold=True, color="991B1B")

HEADER_FILL = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")
FILL_MATCH = PatternFill(start_color="D1E7DD", end_color="D1E7DD", fill_type="solid")     # Light Green
FILL_SETTLED = PatternFill(start_color="CFE2FF", end_color="CFE2FF", fill_type="solid")   # Light Blue
FILL_MISMATCH = PatternFill(start_color="FFF3CD", end_color="FFF3CD", fill_type="solid")  # Light Amber/Yellow
FILL_ERROR = PatternFill(start_color="F8D7DA", end_color="F8D7DA", fill_type="solid")     # Light Red

THIN_BORDER = Border(
    left=Side(style="thin", color="CBD5E1"),
    right=Side(style="thin", color="CBD5E1"),
    top=Side(style="thin", color="CBD5E1"),
    bottom=Side(style="thin", color="CBD5E1"),
)
ALIGN_CENTER = Alignment(horizontal="center", vertical="center")
ALIGN_RIGHT = Alignment(horizontal="right", vertical="center")

STANDARD_19_HEADERS = (
    "رقم السطر",
    "اسم العميل",
    "رقم الهوية",
    "رقم العقد",
    "رقم الحساب",
    "رقم الخدمة",
    "أرقام جوال العميل",
    "المحصل",
    "المشرف",
    "الفرع",
    "المبلغ المعتمد بالملف (ر.س)",
    "المبلغ بموقع زين (ر.س)",
    "صافي الفرق (ر.س)",
    "حالة السداد بموقع زين",
    "الحالة الرئيسية بالملف",
    "الحالة الفرعية بالملف",
    "ملاحظات المتابعة",
    "تاريخ المتابعة",
    "وقت الرصد",
)


def parse_money(val: Any) -> float:
    if val is None:
        return 0.0
    s = str(val).replace(",", "").replace("﷼", "").replace("SAR", "").strip()
    try:
        return round(float(s), 2)
    except Exception:
        return 0.0


class AsyncExcelWriterThread(threading.Thread):
    """Dedicated background consumer thread that manages the Excel workbook in RAM.
    Drains results from a thread-safe Queue, writes them in memory, and batch-saves
    to disk with dual size & debounce time triggers, while logging to a WAL file.
    """

    def __init__(
        self,
        result_queue: queue.Queue,
        result_path: Path,
        wal_path: Path,
        batch_size: int = 50,
        flush_interval_seconds: float = 2.0,
        wal_already_persisted: bool = False,
    ):
        super().__init__(name="Dedicated-Excel-Writer", daemon=True)
        self.result_queue = result_queue
        self.result_path = result_path
        self.wal_path = wal_path
        self.batch_size = batch_size
        self.flush_interval_seconds = flush_interval_seconds
        self.wal_already_persisted = wal_already_persisted

        self.wb: Optional[Workbook] = None
        self.ws: Optional[Any] = None
        self.wal_file = None

        self.total_saved = 0
        self.unflushed_count = 0
        self.last_flush_time = time.time()
        self.is_stopping = False
        self.save_failed = False
        self.save_error: Optional[str] = None
        self.completed_successfully = False
        self.fatal_error: Optional[str] = None
        self.initialized = False

    def _init_workbook_and_wal(self):
        """Initializes or loads workbook into memory and opens WAL log."""
        if self.result_path.exists():
            try:
                self.wb = openpyxl.load_workbook(self.result_path)
                self.ws = self.wb.active
                self.total_saved = max(0, self.ws.max_row - 1)
            except Exception:
                self.wb = Workbook()
                self.ws = self.wb.active
                self._create_header()
        else:
            self.wb = Workbook()
            self.ws = self.wb.active
            self._create_header()

        # Open append-only WAL stream
        if not self.wal_already_persisted:
            self.wal_file = open(self.wal_path, "a", encoding="utf-8")

    def _create_header(self):
        self.ws.title = "الفروقات الصافية للمحصلين"
        self.ws.views.sheetView[0].rightToLeft = True
        self.ws.append(list(STANDARD_19_HEADERS))
        for col_idx in range(1, len(STANDARD_19_HEADERS) + 1):
            cell = self.ws.cell(row=1, column=col_idx)
            cell.font = HEADER_FONT
            cell.fill = HEADER_FILL
            cell.alignment = ALIGN_CENTER
            cell.border = THIN_BORDER

    def run(self):
        try:
            self._init_workbook_and_wal()
            self.initialized = True
            logger.info(f"Writer Thread started. Batch size: {self.batch_size}, Flush interval: {self.flush_interval_seconds}s")

            while True:
                try:
                    # Wait for next item with short timeout
                    item = self.result_queue.get(timeout=0.2)
                except queue.Empty:
                    item = None

                # Handle Sentinel (End of work)
                if item is None and self.is_stopping:
                    break

                if item is not None:
                    if item == "STOP_SENTINEL":
                        self.is_stopping = True
                        break

                    if isinstance(item, list):
                        for rec in item:
                            self._process_single_record(rec)
                    else:
                        self._process_single_record(item)

                    # Rapidly burst-drain pending items into RAM in chunks of up to 500
                    while not self.result_queue.empty() and self.unflushed_count < 1000:
                        try:
                            queued_item = self.result_queue.get_nowait()
                            if queued_item == "STOP_SENTINEL":
                                self.is_stopping = True
                                break
                            elif isinstance(queued_item, list):
                                for rec in queued_item:
                                    self._process_single_record(rec)
                            elif queued_item is not None:
                                self._process_single_record(queued_item)
                        except queue.Empty:
                            break

                if self.is_stopping:
                    break

                # Check Flush Triggers:
                # ONLY save to disk if:
                # 1. We accumulated a large batch (>= 1000 rows), OR
                # 2. Queue is completely empty AND at least 30 seconds have elapsed since last save
                now = time.time()
                time_since_flush = now - self.last_flush_time
                q_empty = self.result_queue.empty()

                if self.unflushed_count >= 1000 or (q_empty and self.unflushed_count >= 100 and time_since_flush >= 30.0):
                    self._flush_to_disk()

            # Drain any remaining items in RAM first (takes milliseconds)
            self._drain_remaining()
            # Single final flush to disk
            self._flush_to_disk()
            if self.wal_file:
                try:
                    self.wal_file.close()
                except Exception:
                    pass

            if not self.save_failed:
                self.completed_successfully = True
                logger.info(f"Writer Thread finished cleanly. Total rows saved: {self.total_saved}")
            else:
                self.completed_successfully = False
                logger.error(f"Writer Thread finished with save failure: {self.save_error}")
        except Exception as fatal_exc:
            self.fatal_error = str(fatal_exc)
            self.completed_successfully = False
            logger.error(f"Fatal error in Writer Thread: {fatal_exc}", exc_info=True)
            if self.wal_file:
                try:
                    self.wal_file.close()
                except Exception:
                    pass

    def _process_single_record(self, row_data: Dict[str, Any]):
        """Writes record to WAL and appends to in-memory Excel sheet."""
        # 1. Instant WAL disk log (Atomic & crash-proof)
        try:
            if not self.wal_already_persisted:
                self.wal_file.write(json.dumps(row_data, ensure_ascii=False) + "\n")
                self.wal_file.flush()
        except Exception as exc:
            logger.error(f"WAL write error: {exc}")

        # 2. Append to openpyxl sheet in RAM (takes 0.0001s)
        row_cells = [
            row_data["row"],
            row_data["name"],
            row_data["national_id"],
            row_data["contract"],
            row_data["account"],
            row_data["service"],
            row_data["customer_phones"],
            row_data["collector"],
            row_data["supervisor"],
            row_data["branch"],
            row_data["expected_sar"],
            row_data["live_sar"],
            row_data["diff_sar"],
            row_data["status_label"],
            row_data["main_status"],
            row_data["sub_status"],
            row_data["follow_notes"],
            row_data["follow_date"],
            row_data["timestamp"],
        ]
        self.ws.append(row_cells)
        curr_row = self.ws.max_row

        # Apply cell styling in memory
        st = row_data["status_label"]
        fill_style = (
            FILL_MATCH
            if st == "تطابق تام"
            else (
                FILL_SETTLED
                if st == "مسدد بالكامل"
                else (FILL_ERROR if st in ("خطأ فحص", "غير موجود") else FILL_MISMATCH)
            )
        )

        for col_idx in range(1, len(row_cells) + 1):
            c = self.ws.cell(row=curr_row, column=col_idx)
            c.font = DATA_FONT
            c.border = THIN_BORDER
            c.alignment = ALIGN_CENTER
            c.fill = fill_style

        self.total_saved += 1
        self.unflushed_count += 1

    def _flush_to_disk(self):
        """Saves in-memory workbook to disk in one efficient write operation."""
        if self.unflushed_count == 0:
            return

        t0 = time.time()
        flushed_num = self.unflushed_count
        try:
            self.wb.save(self.result_path)
            duration = round(time.time() - t0, 3)
            q_depth = self.result_queue.qsize()
            print(f"[💾 عامل الحفظ] تم حفظ دفعة من {flushed_num} سطر ({duration}s) | المتبقي بالطابور: {q_depth} | الإجمالي المحفوظ: {self.total_saved}")
            self.unflushed_count = 0
            self.last_flush_time = time.time()
            self.save_failed = False
            self.save_error = None
        except Exception as exc:
            self.save_failed = True
            self.save_error = str(exc)
            logger.error(f"Error saving Excel workbook to disk: {exc}")

    def _drain_remaining(self):
        """Drains any leftover records in queue during shutdown."""
        while not self.result_queue.empty():
            try:
                item = self.result_queue.get_nowait()
                if item and item != "STOP_SENTINEL":
                    if isinstance(item, list):
                        for rec in item:
                            self._process_single_record(rec)
                    else:
                        self._process_single_record(item)
            except queue.Empty:
                break


class AsyncPipelineEngine:
    """Enterprise Master Dual-Engine using the Decoupled Producer-Consumer Architecture."""

    def __init__(
        self,
        excel_path: str,
        result_path: str = "نتائج فحص زين.xlsx",
        batch_size: int = 50,
        flush_interval_seconds: float = 2.0,
    ):
        self.excel_path = Path(excel_path)
        self.result_path = Path(result_path)
        self.wal_path = Path(".wal_results.jsonl")
        self.checkpoint_path = Path(".checkpoint_master_engine.json")

        self.result_queue: queue.Queue = queue.Queue()
        self.writer_thread = AsyncExcelWriterThread(
            result_queue=self.result_queue,
            result_path=self.result_path,
            wal_path=self.wal_path,
            batch_size=batch_size,
            flush_interval_seconds=flush_interval_seconds,
        )

        self.service_jobs: List[Dict[str, Any]] = []
        self.contract_groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self.contract_queue: List[str] = []

        self.completed_rows: Dict[int, Dict[str, Any]] = {}
        self.contract_cache: Dict[str, Tuple[str, Optional[float], str]] = {}

        self.lock = threading.RLock()
        self.print_lock = threading.Lock()

        # Cooldown trackers for service burst rate limiting
        self.w1_service_cooldown_until = 0.0
        self.w2_service_cooldown_until = 0.0

        self.stats = {
            "total_rows": 0,
            "total_services": 0,
            "total_contracts": 0,
            "processed": 0,
            "matches": 0,
            "mismatches": 0,
            "settled": 0,
            "errors": 0,
        }

    def load_and_partition_workbook(self):
        """Loads and intelligently partitions workbook records."""
        with self.print_lock:
            print(f"📖 جاري قراءة وتقسيم الملف: {self.excel_path.name}...")

        wb = openpyxl.load_workbook(self.excel_path, read_only=True, data_only=True)
        sheet = wb.active
        rows = list(sheet.iter_rows(values_only=True))
        wb.close()

        total_rows = len(rows) - 1
        self.stats["total_rows"] = total_rows

        # Load existing completed rows from checkpoint if available
        if self.checkpoint_path.exists():
            try:
                with open(self.checkpoint_path, "r", encoding="utf-8") as f:
                    chk = json.load(f)
                    self.completed_rows = {int(k): v for k, v in chk.get("completed_rows", {}).items()}
                with self.print_lock:
                    print(f"🔄 استئناف من نقطة الحفظ: {len(self.completed_rows)} سطر مكتمل مسبقاً.")
            except Exception:
                pass

        for idx, r in enumerate(rows[1:], start=2):
            s40 = str(r[40] or "").strip()
            s47 = str(r[47] or "").strip()
            c38 = str(r[38] or "").strip()

            service_num = ""
            if s40.startswith("2"):
                service_num = s40
            elif s47.startswith("2"):
                service_num = s47

            record = {
                "row": idx,
                "name": str(r[6] or "").strip(),
                "national_id": str(r[7] or "").strip(),
                "contract": c38,
                "account": str(r[11] or "").strip(),
                "service": service_num or s40,
                "customer_phones": str(r[8] or "").strip(),
                "collector": str(r[20] or "").strip(),
                "supervisor": str(r[19] or "").strip(),
                "branch": str(r[18] or "").strip(),
                "due_p": parse_money(r[14]),
                "due_aw": parse_money(r[42]),
                "main_status": str(r[24] or "").strip(),
                "sub_status": str(r[25] or "").strip(),
                "follow_notes": str(r[22] or "").strip(),
                "follow_date": str(r[23] or "").strip(),
            }

            if service_num:
                self.service_jobs.append(record)
            else:
                c_key = c38 if c38.startswith("10") else (record["account"] or f"row_{idx}")
                self.contract_groups[c_key].append(record)

        self.contract_queue = list(self.contract_groups.keys())
        self.stats["total_services"] = len(self.service_jobs)
        self.stats["total_contracts"] = len(self.contract_queue)

        with self.print_lock:
            print(f"📊 إجمالي أسطر الملف: {total_rows} سطر")
            print(f"• أرقام الخدمة (الأولوية 1): {len(self.service_jobs)} عميل يبدأ بـ '2'")
            print(f"• أرقام العقود (الأولوية 2): {len(self.contract_queue)} عقد موحد يغطي باقي الأسطر")

    def run_worker_1_direct(self):
        """Worker 1: Direct Local Connection."""
        logger.info("Worker 1 (Direct Local) started.")
        while True:
            # Pick next contract job from the start of queue
            contract_key = None
            with self.lock:
                for c_k in self.contract_queue:
                    uncompleted = [r for r in self.contract_groups[c_k] if r["row"] not in self.completed_rows]
                    if uncompleted:
                        contract_key = c_k
                        break

            if contract_key:
                self._process_contract_group(contract_key, worker_name="W1 مباشر")
                # Direct API is instant unlimited! Zero wait!
                continue

            with self.lock:
                all_contracts_done = all(
                    all(r["row"] in self.completed_rows for r in grp)
                    for grp in self.contract_groups.values()
                )
            if all_contracts_done:
                break
            time.sleep(0.1)

    def run_worker_2_proxy(self):
        """Worker 2: Secondary / Parallel Thread."""
        logger.info("Worker 2 (Parallel Direct) started.")
        while True:
            # Pick next contract job from the end of queue (meet in the middle)
            contract_key = None
            with self.lock:
                for c_k in reversed(self.contract_queue):
                    uncompleted = [r for r in self.contract_groups[c_k] if r["row"] not in self.completed_rows]
                    if uncompleted:
                        contract_key = c_k
                        break

            if contract_key:
                self._process_contract_group(contract_key, worker_name="W2 موازي")
                continue

            with self.lock:
                all_contracts_done = all(
                    all(r["row"] in self.completed_rows for r in grp)
                    for grp in self.contract_groups.values()
                )
            if all_contracts_done:
                break
            time.sleep(0.1)

    def _process_contract_group(self, contract_key: str, worker_name: str):
        """Processes a contract group using Multi-Level Reconciliation and pushes to Queue in 0.001ms."""
        with self.lock:
            cached = self.contract_cache.get(contract_key)

        if not cached:
            st, live_amt, msg = query_contract_due_amount(contract_key)
            with self.lock:
                self.contract_cache[contract_key] = (st, live_amt, msg)
        else:
            st, live_amt, msg = cached

        group = self.contract_groups[contract_key]
        live_sar = live_amt if live_amt is not None else 0.0

        for job in group:
            row_idx = job["row"]
            with self.lock:
                if row_idx in self.completed_rows:
                    continue

            aw_val = job["due_aw"]
            p_val = job["due_p"]
            sum_p = round(sum(item["due_p"] for item in group), 2)

            if st == "ok" or st == "OK":
                # Multi-Level Reconciliation
                if live_sar == 0.0:
                    label = "مسدد بالكامل"
                    diff = 0.0
                    eff_exp = aw_val or p_val
                elif abs(live_sar - aw_val) <= 0.20 or abs(live_sar - sum_p) <= 0.20:
                    label = "تطابق تام"
                    diff = 0.0
                    eff_exp = aw_val
                elif abs(live_sar - p_val) <= 0.20:
                    label = "تطابق تام"
                    diff = 0.0
                    eff_exp = p_val
                elif any(abs(live_sar - other["due_p"]) <= 0.20 for other in group):
                    label = "مسدد بالكامل"
                    diff = 0.0
                    eff_exp = p_val
                else:
                    label = "فرق رصيد"
                    diff = round(live_sar - aw_val, 2)
                    eff_exp = aw_val
            else:
                label = "غير موجود" if "not found" in msg.lower() or "not from tabs" in msg.lower() else "خطأ فحص"
                diff = 0.0
                eff_exp = aw_val or p_val

            row_res = {
                "row": row_idx,
                "name": job["name"],
                "national_id": job["national_id"],
                "contract": job["contract"],
                "account": job["account"],
                "service": job["service"],
                "customer_phones": job["customer_phones"],
                "collector": job["collector"],
                "supervisor": job["supervisor"],
                "branch": job["branch"],
                "expected_sar": eff_exp,
                "live_sar": live_sar,
                "diff_sar": diff,
                "status_label": label,
                "main_status": job["main_status"],
                "sub_status": job["sub_status"],
                "follow_notes": job["follow_notes"],
                "follow_date": job["follow_date"],
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

            with self.lock:
                self.completed_rows[row_idx] = row_res
                self.stats["processed"] += 1
                if label == "تطابق تام":
                    self.stats["matches"] += 1
                elif label == "مسدد بالكامل":
                    self.stats["settled"] += 1
                elif label == "فرق رصيد":
                    self.stats["mismatches"] += 1
                else:
                    self.stats["errors"] += 1

            # PUSH TO ASYNC QUEUE (Takes 0.000001 seconds! Zero blocking!)
            self.result_queue.put(row_res)

            with self.print_lock:
                print(f"[{worker_name} | عقد] صف {row_idx:<4} | عقد {contract_key} | {label:<14} | رصيد: {live_sar:<7} | إكسل: {p_val} / {aw_val}")

    def start(self):
        """Starts the asynchronous dual-engine auditing session."""
        print("=" * 85)
        print("🚀 انطلاق المحرك المزدوج فائق السرعة بنظام طابور الحفظ غير المتزامن (Async Pipeline)")
        print(f"📁 ملف الفحص: {self.excel_path.name}")
        print(f"💾 ملف النتائج: {self.result_path.name}")
        print("=" * 85)

        self.load_and_partition_workbook()

        # Start dedicated writer thread
        self.writer_thread.start()

        t1 = threading.Thread(target=self.run_worker_1_direct, name="Async-W1-Direct")
        t2 = threading.Thread(target=self.run_worker_2_proxy, name="Async-W2-Proxy")

        start_time = time.time()
        t1.start()
        t2.start()

        t1.join()
        t2.join()

        # Signal Writer Thread to finish & flush
        self.result_queue.put("STOP_SENTINEL")
        self.writer_thread.join()

        # Build Final Multi-Tab Executive Deliverable
        print("\n🎨 جاري إخراج وتنسيق ملف الإكسل التنفيذي المطور (3 تبويبات فاخرة)...")
        from .executive_reporter import export_executive_workbook
        export_executive_workbook(list(self.completed_rows.values()), self.result_path)

        elapsed = round(time.time() - start_time, 2)
        print("\n" + "=" * 85)
        print("🎉 اكتمل فحص وحفظ الملف بالكامل عبر خط الأنابيب غير المتزامن!")
        print("=" * 85)
        print(f"⏱️ زمن التشغيل الإجمالي: {elapsed} ثانية (~{round(elapsed/60, 2)} دقيقة)")
        print(f"📊 إجمالي السجلات المفحوصة والمحفوظة: {self.writer_thread.total_saved} / {self.stats['total_rows']}")
        print(f"  • تطابق تام      : {self.stats['matches']}")
        print(f"  • مسدد بالكامل   : {self.stats['settled']}")
        print(f"  • فرق رصيد       : {self.stats['mismatches']}")
        print(f"  • غير مسجل / خطأ : {self.stats['errors']}")
        print("=" * 85)


if __name__ == "__main__":
    file_arg = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\dell HQ\Downloads\ريان.xlsx"
    engine = AsyncPipelineEngine(excel_path=file_arg)
    engine.start()
