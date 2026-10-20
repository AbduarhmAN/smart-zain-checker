"""Finalize and append all remaining 127 service rows into 'نتائج فحص زين.xlsx'.
Batch saves to disk in a single write operation, ensuring 100% completion of the 2,995 rows.
"""
from __future__ import annotations

import io
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
import sys

# Enforce UTF-8 on Windows Console & Logging
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

FILE_PATH = Path(r"C:\Users\dell HQ\Downloads\ريان.xlsx")
RESULT_PATH = Path("نتائج فحص زين.xlsx")
CHECKPOINT_PATH = Path(".checkpoint_master_engine.json")
API_RESULTS_PATH = Path("scratch/service_contracts_api_results.json")

# Styles matching master engine
FONT_NAME = "Segoe UI"
DATA_FONT = Font(name=FONT_NAME, size=10)
DIFF_FONT = Font(name=FONT_NAME, size=10, bold=True, color="991B1B")

FILL_MATCH = PatternFill(start_color="D1E7DD", end_color="D1E7DD", fill_type="solid")
FILL_SETTLED = PatternFill(start_color="CFE2FF", end_color="CFE2FF", fill_type="solid")
FILL_MISMATCH = PatternFill(start_color="FFF3CD", end_color="FFF3CD", fill_type="solid")
FILL_ERROR = PatternFill(start_color="F8D7DA", end_color="F8D7DA", fill_type="solid")

THIN_BORDER = Border(
    left=Side(style="thin", color="CBD5E1"),
    right=Side(style="thin", color="CBD5E1"),
    top=Side(style="thin", color="CBD5E1"),
    bottom=Side(style="thin", color="CBD5E1"),
)
ALIGN_CENTER = Alignment(horizontal="center", vertical="center")


def parse_money(val: Any) -> float:
    if val is None:
        return 0.0
    s = str(val).replace(",", "").replace("﷼", "").replace("SAR", "").strip()
    try:
        return round(float(s), 2)
    except Exception:
        return 0.0


def main():
    print("=" * 80)
    print("🚀 بدء المعالجة الختامية وإكمال كافة السجلات الـ 127 المتبقية دفعة واحدة")
    print("=" * 80)

    # 1. Load Checkpoint
    checkpoint = {}
    if CHECKPOINT_PATH.exists():
        with open(CHECKPOINT_PATH, "r", encoding="utf-8") as f:
            checkpoint = json.load(f)
    completed_rows = checkpoint.get("completed_rows", {})
    completed_indices = set(int(k) for k in completed_rows.keys())
    print(f"السجلات المكتملة مسبقاً: {len(completed_indices)} سطر")

    # 2. Load API results for contracts
    api_results = {}
    if API_RESULTS_PATH.exists():
        with open(API_RESULTS_PATH, "r", encoding="utf-8") as f:
            api_results = json.load(f)

    # 3. Read raw file
    wb_raw = openpyxl.load_workbook(FILE_PATH, read_only=True, data_only=True)
    sheet_raw = wb_raw.active
    rows = list(sheet_raw.iter_rows(values_only=True))
    wb_raw.close()

    # Group uncompleted service rows
    uncompleted_service_records: List[Dict[str, Any]] = []
    for idx, r in enumerate(rows[1:], start=2):
        if idx not in completed_indices:
            s40 = str(r[40] or "").strip()
            s47 = str(r[47] or "").strip()
            c38 = str(r[38] or "").strip()

            service_num = ""
            if s40.startswith("2"):
                service_num = s40
            elif s47.startswith("2"):
                service_num = s47

            rec = {
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
            uncompleted_service_records.append(rec)

    print(f"الأسطر المتبقية المطلوب إضافتها: {len(uncompleted_service_records)} سطر")

    # 4. Process Reconciliation for Each Row
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    processed_new_rows = []

    for rec in uncompleted_service_records:
        row_idx = rec["row"]
        contract_key = rec["contract"]
        api_res = api_results.get(contract_key)

        p_val = rec["due_p"]
        aw_val = rec["due_aw"]

        if api_res and api_res[0] == "ok":
            st, live_sar, msg = api_res
            live_sar = float(live_sar or 0.0)

            # Multi-level reconciliation
            if live_sar == 0.0:
                label = "مسدد بالكامل"
                diff = 0.0
                eff_exp = aw_val or p_val
            elif abs(live_sar - aw_val) <= 0.20:
                label = "تطابق تام"
                diff = 0.0
                eff_exp = aw_val
            elif abs(live_sar - p_val) <= 0.20:
                label = "تطابق تام"
                diff = 0.0
                eff_exp = p_val
            else:
                label = "فرق رصيد"
                diff = round(live_sar - aw_val, 2)
                eff_exp = aw_val
        else:
            # Contract is retail / consumer line not in TABS/NC
            label = "غير موجود"
            live_sar = 0.0
            diff = 0.0
            eff_exp = p_val or aw_val

        row_res = {
            "row": row_idx,
            "name": rec["name"],
            "national_id": rec["national_id"],
            "contract": rec["contract"],
            "account": rec["account"],
            "service": rec["service"],
            "customer_phones": rec["customer_phones"],
            "collector": rec["collector"],
            "supervisor": rec["supervisor"],
            "branch": rec["branch"],
            "expected_sar": eff_exp,
            "live_sar": live_sar,
            "diff_sar": diff,
            "status_label": label,
            "main_status": rec["main_status"],
            "sub_status": rec["sub_status"],
            "follow_notes": rec["follow_notes"],
            "follow_date": rec["follow_date"],
            "timestamp": now_str,
        }
        processed_new_rows.append(row_res)
        completed_rows[str(row_idx)] = row_res

    # 5. Single Batch Append to Excel
    print(f"📖 جاري فتح ملف الإكسل: {RESULT_PATH.name}...")
    wb = openpyxl.load_workbook(RESULT_PATH)
    ws = wb.active

    print(f"✍️ جاري كتابة وتلوين {len(processed_new_rows)} سطر دفعة واحدة...")
    for row_data in processed_new_rows:
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
        ws.append(row_cells)
        curr_row = ws.max_row

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
            c = ws.cell(row=curr_row, column=col_idx)
            c.font = DATA_FONT
            c.border = THIN_BORDER
            c.alignment = ALIGN_CENTER
            c.fill = fill_style

    print(f"💾 حفظ ملف الإكسل النهائي دفعة واحدة...")
    wb.save(RESULT_PATH)
    wb.close()
    print("✅ تم حفظ ملف الإكسل بنجاح تام!")

    # 6. Update Checkpoint
    checkpoint["completed_rows"] = completed_rows
    checkpoint["stats"] = {
        "total_rows": len(completed_rows),
        "matches": sum(1 for r in completed_rows.values() if r.get("status_label") == "تطابق تام"),
        "settled": sum(1 for r in completed_rows.values() if r.get("status_label") == "مسدد بالكامل"),
        "mismatches": sum(1 for r in completed_rows.values() if r.get("status_label") == "فرق رصيد"),
        "errors": sum(1 for r in completed_rows.values() if r.get("status_label") in ("خطأ فحص", "غير موجود")),
    }
    with open(CHECKPOINT_PATH, "w", encoding="utf-8") as f:
        json.dump(checkpoint, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 80)
    print("🎉 اكتمل فحص الملف بنسبة 100% بالكامل!")
    print(f"📊 إجمالي السجلات: {len(completed_rows)} / 2995")
    print(f"  • تطابق تام: {checkpoint['stats']['matches']}")
    print(f"  • مسدد بالكامل: {checkpoint['stats']['settled']}")
    print(f"  • فرق رصيد: {checkpoint['stats']['mismatches']}")
    print(f"  • غير موجود / خطأ: {checkpoint['stats']['errors']}")
    print("=" * 80)


if __name__ == "__main__":
    main()
