# -*- coding: utf-8 -*-
"""
generate_mawarid_deliverable.py
المهمة: توليد الملف النهائي بصياغة شركة الموارد (Mawared Format)
باسم: "باقي فصح نتيجه.xlsx" و "باقي فحص نتيجه.xlsx"
وفق القواعد المعتمدة لشركة الموارد:
- Tab 1: الفروقات الايجابيه (سدادات العملاء بدون المسددين مسبقاً، وعمود اسم المحصل بدل العميل)
- Tab 2: فروقات سالبه (زيادة مديونية في زين)
- Tab 3: فروقات شامله (جميع الفروقات بما فيها السداد التام)
- Tab 4: الأخطاء والملاحظات للمحصل (الأخطاء والمديونيات الصفرية مع اسم المحصل ورقم الحساب النقي)
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

# ضبط ترميز ويندوز
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import openpyxl
from zain_checker.executive_reporter import export_executive_workbook

SOURCE_WORKBOOK = Path("رقم عقد03-10-2026_من_الصف_26022.xlsx")
AUDITED_JSONL = Path("نتائج_فحص_زين_المباشرة_19ألف.jsonl")
OUT_MAWARID_1 = Path("باقي فصح نتيجه.xlsx")
OUT_MAWARID_2 = Path("باقي فحص نتيجه.xlsx")


def main():
    print("=" * 80, flush=True)
    print("🏢 جاري توليد التقرير النهائي بصياغة شركة الموارد (Mawared Format)...", flush=True)
    print(f"📁 ملف المصدر الأساسي : {SOURCE_WORKBOOK}", flush=True)
    print(f"🔒 سجل نتائج الفحص   : {AUDITED_JSONL}", flush=True)
    print(f"📊 اسم الملف المستهدف : {OUT_MAWARID_1}", flush=True)
    print("=" * 80, flush=True)

    # 1. تحميل نتائج الفحص من JSONL
    print("⏳ قراءة نتائج الفحص المكتملة من السجل...", flush=True)
    audited_map: Dict[str, Dict[str, Any]] = {}
    with open(AUDITED_JSONL, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                item = json.loads(line)
                contract = str(item.get("contract") or "").strip()
                if contract:
                    audited_map[contract] = item

    print(f"✅ تم تحميل نتائج {len(audited_map):,} عقد مفحوص.", flush=True)

    # 2. قراءة بيانات الأعمدة الإضافية للمحصل من الشيت الأصلي
    print("⏳ قراءة بيانات المحصلين والحالات والمتابعات من الملف الأصلي...", flush=True)
    t0 = time.time()
    wb_src = openpyxl.load_workbook(SOURCE_WORKBOOK, read_only=True, data_only=True)
    ws_src = wb_src.active

    records: List[Dict[str, Any]] = []

    for idx, row in enumerate(ws_src.iter_rows(min_row=2, values_only=True), start=2):
        if not row or len(row) < 46:
            continue
        contract_raw = str(row[40] or row[11] or "").strip()
        if not contract_raw or contract_raw.lower() in ("none", "", "0"):
            continue

        audit_res = audited_map.get(contract_raw, {})

        name = str(row[6] or "").strip()
        account = str(row[11] or "").strip()
        service = str(row[42] or "").strip()

        # بيانات المحصل والحالات لشركة الموارد
        collector = str(row[20] or "").strip() or "محصل غير محدد"
        main_status = str(row[24] or "").strip()
        sub_status = str(row[25] or "").strip()
        follow_notes = str(row[27] or "").strip()
        case_status = str(row[4] or "").strip()

        try:
            debt = float(str(row[12] or "0").replace(",", "").strip())
        except Exception:
            debt = 0.0

        try:
            contract_amt = float(str(row[45] or "0").replace(",", "").strip())
        except Exception:
            contract_amt = 0.0

        expected = contract_amt if contract_amt > 0 else debt

        live_val = audit_res.get("live_amount")
        status = audit_res.get("status", "pending")
        status_label = audit_res.get("status_label", "قيد الفحص")
        error_msg = audit_res.get("message", "")

        diff_sar = 0.0
        if live_val is not None:
            diff_sar = round(expected - live_val, 2)

        # تحويل حالة الفحص إلى الصيغة المعتمدة للمحصل (تجاهل الفروقات الأقل من 0.20 ريال واعتبارها تطابقاً)
        rec_status = "match"
        if abs(diff_sar) < 0.20:
            rec_status = "match"
            status_label = "تطابق تام"
        elif "تطابق" in status_label:
            rec_status = "match"
        elif "فرق" in status_label or abs(diff_sar) >= 0.20:
            rec_status = "mismatch"
        elif "غير موجود" in status_label or status == "not_found":
            rec_status = "not_found"
        elif "خطأ" in status_label or status in ("error", "network_error"):
            rec_status = "error"

        records.append({
            "row": idx,
            "name": name,
            "account": account,
            "service": service,
            "contract": contract_raw,
            "collector": collector,
            "collector_name": collector,
            "main_status": main_status,
            "sub_status": sub_status,
            "follow_notes": follow_notes,
            "case_status": case_status,
            "expected_sar": expected,
            "live_sar": live_val,
            "diff_sar": diff_sar,
            "status": rec_status,
            "status_label": status_label,
            "error": error_msg,
            "timestamp": audit_res.get("timestamp", ""),
            "mawarid_format": True,
        })

    wb_src.close()
    print(f"✅ تم دمج وتجهيز {len(records):,} سجل بالكامل في {time.time()-t0:.2f} ثانية.", flush=True)

    # 3. بناء وتصدير مصنف شركة الموارد بصيغة ExactTemplateReporter
    print("📑 جاري إنشاء وتنسيق شيتات شركة الموارد...", flush=True)
    t_build = time.time()
    export_executive_workbook(records, OUT_MAWARID_1, is_repair=False, mawarid_format=True)
    
    # حفظ نسخة إضافية بالاسم المصحح إملائياً
    shutil.copyfile(OUT_MAWARID_1, OUT_MAWARID_2)
    print(f"🎉 تم إنشاء الملف بنجاح في {time.time()-t_build:.2f} ثانية!", flush=True)

    # 4. إحصائيات سريعة للتحقق
    wb_chk = openpyxl.load_workbook(OUT_MAWARID_1, read_only=True)
    print("\n--- 📋 محتويات الشيتات في ملف شركة الموارد ---", flush=True)
    for sname in wb_chk.sheetnames:
        s = wb_chk[sname]
        print(f"  🔹 شيت: [{sname}] - عدد الأسطر: {s.max_row:,}")
    wb_chk.close()
    print("=" * 80, flush=True)


if __name__ == "__main__":
    main()
