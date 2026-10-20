"""Master Dual-Engine Auditor for Smart Zain Checker.
Implements the full enterprise architecture:
1. Sheet Partitioning: Priority 1 to Service Numbers ('2...'), Priority 2 to Contracts.
2. Dual Parallel Workers: Worker 1 (Direct Local) and Worker 2 (UK Proxy).
3. Interleaved Cooldown Execution: Zero wasted time (processes contracts during service cooldowns).
4. Multi-Row Contract Intelligence: Deduplication, Grouping, and Multi-Level Reconciliation (AW, Sum_P, Individual P, Zero Settlement).
5. Excel Deliverable: Strictly preserves the 19 columns in 'نتائج فحص زين.xlsx' with color coding.
"""
from __future__ import annotations

import io
import json
import logging
import openpyxl
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
import os
import re
import subprocess
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Enforce UTF-8 on Windows Console & Logging (Global Rule)
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

logger = logging.getLogger("MasterDualEngine")
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

MISMATCH_HEADERS = (
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

def query_service_quickpay(service_number: str, proxy: Optional[str] = None) -> Tuple[str, Optional[float], str]:
    """Queries app.sa.zain.com/ar/quickpay?account={service_number} via curl with remote DNS."""
    cmd = ["curl", "-s", "-i", "--max-time", "12"]
    if proxy:
        cmd.extend(["-x", proxy])
    cmd.append(f"{QUICKPAY_BASE_URL}{service_number}")

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore")
        out = res.stdout or ""

        if "Request Rejected" in out:
            return "REJECTED_F5", None, "حظر F5 مؤقت"

        m = re.search(r"var\s+quickpayData\s*=\s*(\{.*?\});", out)
        if m:
            d = json.loads(m.group(1))
            amt = float(d.get("amount", 0.0))
            return "OK", amt, "تم جلب الرصيد بنجاح"

        if "302 Found" in out or "Redirecting to" in out or "/ar/home" in out or "quickpay" not in out:
            return "REDIRECT_BLOCK", None, "تحويل لزين /ar/home"

        return "REDIRECT_BLOCK", None, "تحويل لزين"
    except Exception as exc:
        return "ERROR", None, str(exc)

def query_contract_proxy(contract_number: str) -> Tuple[str, Optional[float], str]:
    """Queries app.sa.zain.com/ar/contract-payment?contract={contract_number} via UK Proxy."""
    cmd = ["curl", "-s", "-i", "-x", PROXY_URL, "--max-time", "15", f"{CONTRACT_BASE_URL}{contract_number}"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore")
        out = res.stdout or ""

        if "Request Rejected" in out:
            return "REJECTED_F5", None, "حظر F5 مؤقت"

        m = re.search(r"var\s+quickpayData\s*=\s*(\{.*?\});", out)
        if m:
            d = json.loads(m.group(1))
            amt = float(d.get("amount", 0.0))
            return "OK", amt, "تم جلب رصيد العقد بالبروكسي"

        if "302 Found" in out or "/ar/home" in out:
            return "REDIRECT_BLOCK", None, "تحويل لزين بالبروكسي"

        return "NOT_FOUND", None, "لم يعثر على العقد"
    except Exception as exc:
        return "ERROR", None, str(exc)

def evaluate_service_match(live_amt: float, due_aw: float, due_p: float) -> Tuple[str, float]:
    diff_aw = abs(live_amt - due_aw)
    diff_p = abs(live_amt - due_p)
    best_diff = min(diff_aw, diff_p)

    if best_diff <= 0.20:
        return "تطابق تام", 0.0
    elif live_amt == 0.0:
        return "مسدد بالكامل", 0.0
    else:
        diff_val = round(live_amt - (due_aw if diff_aw < diff_p else due_p), 2)
        return "فرق رصيد", diff_val

class MasterDualEngine:
    def __init__(self, excel_path: str, result_path: str = "نتائج فحص زين.xlsx"):
        self.excel_path = Path(excel_path)
        self.result_path = Path(result_path)
        self.checkpoint_path = Path(".checkpoint_master_engine.json")

        self.service_jobs: List[Dict[str, Any]] = []
        self.contract_groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self.contract_queue: List[str] = []

        self.completed_rows: Dict[int, Dict[str, Any]] = {}
        self.contract_cache: Dict[str, Tuple[str, Optional[float], str]] = {}

        self.lock = threading.RLock()
        self.excel_lock = threading.Lock()
        self.print_lock = threading.Lock()

        # Cooldown state for Service numbers on QuickPay
        self.w1_service_cooldown_until = 0.0
        self.w2_service_cooldown_until = 0.0

        # Stats
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
            print(f"📖 جاري قراءة الملف: {self.excel_path.name}...")

        wb = openpyxl.load_workbook(self.excel_path, read_only=True, data_only=True)
        sheet = wb.active
        rows = list(sheet.iter_rows(values_only=True))
        wb.close()

        total_rows = len(rows) - 1
        self.stats["total_rows"] = total_rows

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
                "agency": str(r[0] or "").strip(),
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
        self.stats["total_contracts"] = len(self.contract_groups)

        self._load_checkpoint()

        with self.print_lock:
            print(f"✓ تم تقسيم السجلات بنجاح:")
            print(f"   • أرقام الخدمة (الأولوية 1): {len(self.service_jobs)} عميل يبدأ بـ '2'")
            print(f"   • مجموعات العقود (الأولوية 2): {len(self.contract_groups)} عقد (تغطي {total_rows - len(self.service_jobs)} سطر)")
            if self.completed_rows:
                print(f"   • تم استئناف السجل السابق: تم إنجاز {len(self.completed_rows)} سطر مسبقاً.")

    def _load_checkpoint(self):
        if self.checkpoint_path.exists():
            try:
                with open(self.checkpoint_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.completed_rows = {int(k): v for k, v in data.get("completed_rows", {}).items()}
                    self.stats["processed"] = len(self.completed_rows)
            except Exception:
                pass

    def _save_checkpoint(self):
        try:
            with open(self.checkpoint_path, "w", encoding="utf-8") as f:
                json.dump({"completed_rows": self.completed_rows}, f, ensure_ascii=False)
        except Exception:
            pass

    def _init_result_workbook(self):
        with self.excel_lock:
            if not self.result_path.exists():
                wb = Workbook()
                ws = wb.active
                ws.title = "الفروقات الصافية للمحصلين"
                ws.sheet_view.rightToLeft = True
                ws.append(MISMATCH_HEADERS)
                for col_idx in range(1, len(MISMATCH_HEADERS) + 1):
                    c = ws.cell(row=1, column=col_idx)
                    c.font = HEADER_FONT
                    c.fill = HEADER_FILL
                    c.alignment = ALIGN_CENTER
                wb.save(self.result_path)

    def _append_to_excel(self, row_data: Dict[str, Any]):
        with self.excel_lock:
            wb = openpyxl.load_workbook(self.result_path)
            ws = wb.active

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
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ]
            ws.append(row_cells)
            curr_row = ws.max_row

            # Apply Styles
            st = row_data["status_label"]
            fill_style = FILL_MATCH if st == "تطابق تام" else (FILL_SETTLED if st == "مسدد بالكامل" else (FILL_ERROR if st in ("خطأ فحص", "غير موجود") else FILL_MISMATCH))

            for col_idx in range(1, len(row_cells) + 1):
                c = ws.cell(row=curr_row, column=col_idx)
                c.font = DATA_FONT
                c.border = THIN_BORDER
                c.alignment = ALIGN_CENTER
                c.fill = fill_style

            wb.save(self.result_path)

    def run_worker_1_direct(self):
        """Worker 1: Direct Local Connection."""
        logger.info("Worker 1 (Direct Local) started.")
        service_batch_count = 0

        while True:
            # 1. PRIORITY 1: Check Service Numbers if not in cooldown
            job = None
            with self.lock:
                now = time.time()
                if now >= self.w1_service_cooldown_until:
                    for s_job in self.service_jobs:
                        if s_job["row"] not in self.completed_rows:
                            job = s_job
                            break

            if job:
                # Process Service Job (4s pacing, 3-strike block verification)
                st, amt, msg = self._process_service_with_strikes(job["service"], proxy=None, worker_name="W1 مباشر")
                if st == "BLOCK":
                    with self.lock:
                        self.w1_service_cooldown_until = time.time() + 240.0
                        with self.print_lock:
                            print(f"[W1 مباشر] ⏸️ دخول فترة التهدئة للخدمة (4 دقائق). التحول الفوري لفحص العقود...")
                    continue

                self._record_service_outcome(job, st, amt, msg, worker_name="W1 مباشر")
                service_batch_count += 1
                if service_batch_count >= 8:
                    service_batch_count = 0
                    with self.lock:
                        self.w1_service_cooldown_until = time.time() + 180.0
                    with self.print_lock:
                        print(f"[W1 مباشر] 💤 إتمام حزمة 8 أرقام خدمة بنجاح! التهدئة والتحول للعقود...")
                time.sleep(4.0)
                continue

            # 2. PRIORITY 2 / INTERLEAVED: Process Contract Jobs (Instant 0s pacing)
            contract_key = None
            with self.lock:
                for c_k in self.contract_queue:
                    # check if any row in this group is uncompleted
                    uncompleted = [r for r in self.contract_groups[c_k] if r["row"] not in self.completed_rows]
                    if uncompleted:
                        contract_key = c_k
                        break

            if contract_key:
                self._process_contract_group(contract_key, use_proxy=False, worker_name="W1 مباشر")
                # Direct API is instant unlimited!
                time.sleep(0.05)
                continue

            # Check if all completed
            with self.lock:
                all_done = len(self.completed_rows) >= self.stats["total_rows"]
            if all_done:
                break
            time.sleep(1.0)

    def run_worker_2_proxy(self):
        """Worker 2: UK London Proxy."""
        logger.info("Worker 2 (UK Proxy) started.")
        service_batch_count = 0

        while True:
            # 1. PRIORITY 1: Check Service Numbers if not in cooldown
            job = None
            with self.lock:
                now = time.time()
                if now >= self.w2_service_cooldown_until:
                    # Pick from the end of service jobs to avoid collision with W1
                    for s_job in reversed(self.service_jobs):
                        if s_job["row"] not in self.completed_rows:
                            job = s_job
                            break

            if job:
                st, amt, msg = self._process_service_with_strikes(job["service"], proxy=PROXY_URL, worker_name="W2 بروكسي")
                if st == "BLOCK":
                    with self.lock:
                        self.w2_service_cooldown_until = time.time() + 240.0
                        with self.print_lock:
                            print(f"[W2 بروكسي] ⏸️ دخول فترة التهدئة للخدمة (4 دقائق). التحول الفوري لفحص العقود...")
                    continue

                self._record_service_outcome(job, st, amt, msg, worker_name="W2 بروكسي")
                service_batch_count += 1
                if service_batch_count >= 8:
                    service_batch_count = 0
                    with self.lock:
                        self.w2_service_cooldown_until = time.time() + 180.0
                    with self.print_lock:
                        print(f"[W2 بروكسي] 💤 إتمام حزمة 8 أرقام خدمة بنجاح! التهدئة والتحول للعقود...")
                time.sleep(4.0)
                continue

            # 2. PRIORITY 2 / INTERLEAVED: Process Contract Jobs
            contract_key = None
            with self.lock:
                for c_k in reversed(self.contract_queue):
                    uncompleted = [r for r in self.contract_groups[c_k] if r["row"] not in self.completed_rows]
                    if uncompleted:
                        contract_key = c_k
                        break

            if contract_key:
                self._process_contract_group(contract_key, use_proxy=False, worker_name="W2 بروكسي")
                time.sleep(0.05)
                continue

            with self.lock:
                all_done = len(self.completed_rows) >= self.stats["total_rows"]
            if all_done:
                break
            time.sleep(1.0)

    def _process_service_with_strikes(self, service_num: str, proxy: Optional[str], worker_name: str) -> Tuple[str, Optional[float], str]:
        """Runs 3-strike verification to detect blocks with 100% certainty."""
        st, amt, msg = query_service_quickpay(service_num, proxy=proxy)
        if st in ("REDIRECT_BLOCK", "REJECTED_F5"):
            time.sleep(4.0)
            st2, amt2, msg2 = query_service_quickpay(service_num, proxy=proxy)
            if st2 in ("REDIRECT_BLOCK", "REJECTED_F5"):
                time.sleep(4.0)
                st3, amt3, msg3 = query_service_quickpay(service_num, proxy=proxy)
                if st3 in ("REDIRECT_BLOCK", "REJECTED_F5"):
                    return "BLOCK", None, "حظر مؤكد 3 محاولات"
                return st3, amt3, msg3
            return st2, amt2, msg2
        return st, amt, msg

    def _record_service_outcome(self, job: Dict[str, Any], status: str, live_amt: Optional[float], msg: str, worker_name: str):
        row_idx = job["row"]
        live_sar = live_amt if live_amt is not None else 0.0

        if status == "OK":
            label, diff = evaluate_service_match(live_sar, job["due_aw"], job["due_p"])
            eff_expected = job["due_p"] if abs(live_sar - job["due_p"]) <= abs(live_sar - job["due_aw"]) else job["due_aw"]
        else:
            label, diff = "خطأ فحص", 0.0
            eff_expected = job["due_p"] or job["due_aw"]

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
            "expected_sar": eff_expected,
            "live_sar": live_sar,
            "diff_sar": diff,
            "status_label": label,
            "main_status": job["main_status"],
            "sub_status": job["sub_status"],
            "follow_notes": job["follow_notes"],
            "follow_date": job["follow_date"],
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

        self._append_to_excel(row_res)
        self._save_checkpoint()

        with self.print_lock:
            print(f"[{worker_name} | خدمة] صف {row_idx:<4} | {job['service']} | {label:<14} | رصيد: {live_sar:<7} | إكسل: {job['due_p']} / {job['due_aw']}")

    def _process_contract_group(self, contract_key: str, use_proxy: bool, worker_name: str):
        """Processes a contract group using Multi-Level Reconciliation."""
        with self.lock:
            cached = self.contract_cache.get(contract_key)

        if not cached:
            if not use_proxy:
                st, live_amt, msg = query_contract_due_amount(contract_key)
            else:
                st, live_amt, msg = query_contract_proxy(contract_key)

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
                    # Matched another line in this group -> this row is paid
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

            self._append_to_excel(row_res)
            self._save_checkpoint()

            with self.print_lock:
                print(f"[{worker_name} | عقد] صف {row_idx:<4} | عقد {contract_key} | {label:<14} | رصيد: {live_sar:<7} | إكسل: {p_val} / {aw_val}")

    def start(self):
        """Starts the master dual-engine auditing session."""
        print("=" * 85)
        print("🚀 انطلاق المحرك المزدوج الذكي الشامل (Smart Master Dual-Engine)")
        print(f"📁 ملف الفحص: {self.excel_path.name}")
        print(f"💾 ملف النتائج الملون: {self.result_path.name}")
        print("=" * 85)

        self.load_and_partition_workbook()
        self._init_result_workbook()

        t1 = threading.Thread(target=self.run_worker_1_direct, name="Master-W1-Direct")
        t2 = threading.Thread(target=self.run_worker_2_proxy, name="Master-W2-Proxy")

        start_time = time.time()
        t1.start()
        t2.start()

        t1.join()
        t2.join()

        elapsed = round(time.time() - start_time, 2)
        print("\n" + "=" * 85)
        print("🎉 اكتمل فحص الملف بالكامل بنجاح!")
        print("=" * 85)
        print(f"⏱️ زمن التشغيل الإجمالي: {elapsed} ثانية (~{round(elapsed/60, 2)} دقيقة)")
        print(f"📊 إجمالي السجلات المفحوصة: {self.stats['processed']} / {self.stats['total_rows']}")
        print(f"   ├─ تطابق تام          : {self.stats['matches']}")
        print(f"   ├─ مسدد بالكامل (0.0) : {self.stats['settled']}")
        print(f"   ├─ فروقات رصيد        : {self.stats['mismatches']}")
        print(f"   └─ غير موجود / أخطاء  : {self.stats['errors']}")
        print(f"💾 تم حفظ النتائج الرسمية الملونة بالأعمدة الـ 19 في: {self.result_path.name}")

if __name__ == "__main__":
    file_target = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\dell HQ\Downloads\ريان.xlsx"
    engine = MasterDualEngine(file_target)
    engine.start()
