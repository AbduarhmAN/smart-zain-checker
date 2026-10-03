"""Exact Executive Multi-Tab Workbook Reporter for Smart Zain Checker.
Replicates the user's template (2.xlsx) with 100% exact design, dimensions, fonts, colors, and formulas:
1. 'الفروقات الصافية للمحصلين' (Actionable net differences excluding settled/paid accounts)
2. 'جميع الفروقات (شامل المسدد)' (All differences including settled/paid accounts)
3. 'الأخطاء والملاحظات للمحصل' (Errors, zero-balance rows, and collector notes)
"""
from __future__ import annotations

import io
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import openpyxl
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

# Enforce UTF-8 globally on Windows
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

# Exact Typography & Palette matching 2.xlsx
FONT_FAMILY = "Calibri"

FONT_HEADER = Font(name=FONT_FAMILY, size=11.0, bold=True, color="00FFFFFF")
FONT_DATA_REGULAR = Font(name=FONT_FAMILY, size=11.0, bold=False, color="00111827")
FONT_HYPERLINK = Font(name=FONT_FAMILY, size=11.0, bold=True, color="001565C0", underline="single")
FONT_SUMMARY = Font(name=FONT_FAMILY, size=11.0, bold=True, color="00111827")

# Fills matching 2.xlsx
FILL_HEADER_GREEN = PatternFill(start_color="001B5E20", end_color="001B5E20", fill_type="solid")  # Tab 1 (الفروقات الصافية للمحصلين)
FILL_HEADER_PURPLE = PatternFill(start_color="004A148C", end_color="004A148C", fill_type="solid") # Tab 2 (فروقات زيادة المديونية بالسالب)
FILL_HEADER_BLUE = PatternFill(start_color="000D47A1", end_color="000D47A1", fill_type="solid")   # Tab 3 (جميع الفروقات شامل المسدد)
FILL_HEADER_RED = PatternFill(start_color="00B71C1C", end_color="00B71C1C", fill_type="solid")    # Tab 4 (الأخطاء والملاحظات للمحصل)
FILL_HEADER_TEAL = PatternFill(start_color="0000695C", end_color="0000695C", fill_type="solid")   # Maintenance Tab (نتائج صيانة وتدقيق الأخطاء)
FILL_HEADER_NAVY = PatternFill(start_color="000F2942", end_color="000F2942", fill_type="solid")   # Master Archive Tab (جميع السجلات المفحوصة)
FILL_DATA = PatternFill(start_color="00F8FAFC", end_color="00F8FAFC", fill_type="solid")          # Clean slate row fill
FILL_SUMMARY = PatternFill(start_color="00FFF9C4", end_color="00FFF9C4", fill_type="solid")       # Warm Gold Total row


# Borders matching 2.xlsx
BORDER_THIN = Border(
    left=Side(style="thin", color="00CBD5E1"),
    right=Side(style="thin", color="00CBD5E1"),
    top=Side(style="thin", color="00CBD5E1"),
    bottom=Side(style="thin", color="00CBD5E1"),
)

# Alignments matching 2.xlsx
ALIGN_CENTER = Alignment(horizontal="center", vertical="center")
ALIGN_RIGHT = Alignment(horizontal="right", vertical="center")

NUMBER_FORMAT = "#,##0.00"

# Exact Column Widths
WIDTHS_DIFF_TABS = {
    "A": 24.0,  # طريقة البحث
    "B": 32.0,  # الحساب / الخدمة
    "C": 22.0,  # رابط التأكد
    "D": 32.0,  # اسم العميل
    "E": 16.0,  # رقم الصف في الشيت
    "F": 22.0,  # المبلغ المسجل بالشيت
    "G": 24.0,  # المبلغ الحالي في موقع زين
    "H": 18.0,  # الفرق (ريال)
    "I": 24.0,  # الحالة الرئيسية بالملف
    "J": 26.0,  # الحالة الفرعية بالملف
    "K": 35.0,  # أخر متابعة للمحصل
}

WIDTHS_NOTES_TAB = {
    "A": 18.0,  # رقم الصف في الشيت
    "B": 32.0,  # اسم العميل
    "C": 38.0,  # رقم الحساب ورقم الخدمة
    "D": 18.0,  # المبلغ في الشيت
    "E": 44.0,  # المشكلة باختصار
    "F": 30.0,  # رابط صفحة زين
    "G": 24.0,  # الحالة الرئيسية بالملف
    "H": 26.0,  # الحالة الفرعية بالملف
}

WIDTHS_MASTER_TAB = {
    "A": 22.0,  # طريقة البحث
    "B": 32.0,  # الحساب / الخدمة
    "C": 22.0,  # رابط التأكد
    "D": 32.0,  # اسم العميل
    "E": 16.0,  # رقم الصف في الشيت
    "F": 22.0,  # المبلغ المسجل بالشيت
    "G": 24.0,  # المبلغ الحالي في موقع زين
    "H": 18.0,  # الفرق (ريال)
    "I": 26.0,  # نتيجة الفحص
    "J": 24.0,  # الحالة الرئيسية بالملف
    "K": 26.0,  # الحالة الفرعية بالملف
    "L": 35.0,  # أخر متابعة للمحصل
    "M": 22.0,  # تاريخ ووقت الفحص
}

HEADERS_DIFF_TAB = [
    "طريقة البحث",
    "الحساب / الخدمة",
    "رابط التأكد",
    "اسم العميل",
    "رقم الصف في الشيت",
    "المبلغ المسجل بالشيت",
    "المبلغ الحالي في موقع زين",
    "الفرق (ريال)",
    "الحالة الرئيسية بالملف",
    "الحالة الفرعية بالملف",
    "أخر متابعة للمحصل",
]

HEADERS_NOTES_TAB = [
    "رقم الصف في الشيت",
    "اسم العميل",
    "رقم الحساب ورقم الخدمة",
    "المبلغ في الشيت",
    "المشكلة باختصار",
    "رابط صفحة زين",
    "الحالة الرئيسية بالملف",
    "الحالة الفرعية بالملف",
]

HEADERS_MASTER_TAB = [
    "طريقة البحث",
    "الحساب / الخدمة",
    "رابط التأكد",
    "اسم العميل",
    "رقم الصف في الشيت",
    "المبلغ المسجل بالشيت",
    "المبلغ الحالي في موقع زين",
    "الفرق (ريال)",
    "نتيجة الفحص",
    "الحالة الرئيسية بالملف",
    "الحالة الفرعية بالملف",
    "أخر متابعة للمحصل",
    "تاريخ ووقت الفحص",
]


def is_settled_status(main_st: Any, sub_st: Any, notes: Any, case_st: Any = "") -> bool:
    """Detects whether a customer record is already marked as paid/settled in the source file."""
    text = f"{case_st or ''} {main_st or ''} {sub_st or ''} {notes or ''}".strip().lower()

    # Promises to pay are active debts to be collected, NOT settled!
    if "وعد" in text:
        return False

    keywords = [
        "تم السداد",
        "تم سداد",
        "مسدد",
        "سداد كامل",
        "سداد بالكامل",
        "سداد بتسوية",
        "سداد كلي",
        "مخالصة",
        "مخالصه",
        "خالص",
    ]
    return any(k in text for k in keywords)


def build_hyperlink_and_method(account: str, service: str, contract: str) -> Tuple[str, str, str]:
    """Generates direct Zain payment portal URL and search method description."""
    acc_clean = re.sub(r"\D", "", str(account or ""))
    srv_clean = re.sub(r"\D", "", str(service or ""))
    cnt_clean = re.sub(r"\D", "", str(contract or ""))

    if srv_clean.startswith("2"):
        url = f"https://app.sa.zain.com/ar/quickpay?account={srv_clean}"
        method = "برقم الخدمة"
    elif cnt_clean.startswith("10"):
        url = f"https://app.sa.zain.com/ar/contract-payment?contract={cnt_clean}"
        method = "برقم العقد"
    elif acc_clean:
        url = f"https://app.sa.zain.com/ar/contract-payment?contract={acc_clean}"
        method = "برقم الحساب"
    elif cnt_clean:
        url = f"https://app.sa.zain.com/ar/contract-payment?contract={cnt_clean}"
        method = "برقم العقد"
    elif srv_clean:
        url = f"https://app.sa.zain.com/ar/quickpay?account={srv_clean}"
        method = "برقم الخدمة"
    else:
        url = "https://business.zain.sa/dashboard/quick-pay"
        method = "برقم الحساب"

    # Merge identifiers
    parts = []
    if acc_clean:
        parts.append(f"حساب: {acc_clean}")
    if srv_clean:
        parts.append(f"خدمة: {srv_clean}")
    if not parts and cnt_clean:
        parts.append(f"عقد: {cnt_clean}")

    identifier = " | ".join(parts) if parts else (acc_clean or srv_clean or cnt_clean or "-")
    return method, identifier, url


class ExactTemplateReporter:
    """Constructs the executive multi-tab workbook replicating 2.xlsx with 100% fidelity:
    1. 'جميع السجلات المفحوصة' / 'نتائج صيانة وتدقيق الأخطاء' (Comprehensive master archive)
    2. 'الفروقات الصافية للمحصلين' (Positive net differences / customer payments)
    3. 'فروقات زيادة المديونية (بالسالب)' (Negative differences / extra debt on Zain)
    4. 'جميع الفروقات (شامل المسدد)' (All differences including settled/paid accounts)
    5. 'الأخطاء والملاحظات للمحصل' (Errors, zero-balance rows, and collector notes)
    """

    def __init__(self, records: List[Dict[str, Any]], output_path: str | Path, is_repair: bool = False) -> None:
        self.records = records
        self.output_path = Path(output_path)
        self.is_repair = is_repair or ("صيانة" in str(output_path))

        self.tab_all_records: List[Dict[str, Any]] = []
        self.tab1_net_diffs: List[Dict[str, Any]] = []
        self.tab2_negative_diffs: List[Dict[str, Any]] = []
        self.tab3_all_diffs: List[Dict[str, Any]] = []
        self.tab4_notes: List[Tuple[Dict[str, Any], str]] = []

        self._classify_records()

    def _classify_records(self) -> None:
        """Classifies records into tabs according to exact collector rules."""
        sorted_records = sorted(self.records, key=lambda x: int(x.get("row") or 0))

        for rec in sorted_records:
            self.tab_all_records.append(rec)
            status = rec.get("status")
            if status in ("not_found", "needs_review", "error") or rec.get("live_sar") is None:
                note = rec.get("error") or ("لم يتم العثور على مبلغ مؤكد" if status == "not_found" else "تحتاج نتيجة الفحص إلى مراجعة")
                self.tab4_notes.append((rec, note))
                continue
            exp_val = float(rec.get("expected_sar") or 0.0)
            live_val = float(rec.get("live_sar") or 0.0)
            diff_val = round(exp_val - live_val, 2)
            st_label = rec.get("status_label") or ""

            main_st = rec.get("main_status") or ""
            sub_st = rec.get("sub_status") or ""
            notes = rec.get("follow_notes") or ""
            case_st = rec.get("case_status") or ""

            # Check for technical query errors or unsearchable accounts
            if st_label in ("خطأ فحص", "غير موجود") or "not found" in str(rec.get("error") or "").lower():
                self.tab4_notes.append((rec, f"رقم الحساب أو الخدمة غير مسجل في نظام زين ({rec.get('contract') or rec.get('account')})"))
                continue

            # Zero-balance in both sheet and website
            if exp_val == 0.0 and live_val == 0.0:
                self.tab4_notes.append((rec, "المبلغ المتبقي في الشيت وفي موقع زين يساوي صفر (0.00 ريال)"))
                continue

            # Actionable difference check
            has_difference = (abs(diff_val) > 0.20) or (live_val == 0.0 and exp_val > 0.0) or (exp_val == 0.0 and live_val > 0.0)

            if has_difference:
                # Tab 3 includes ALL differences (both positive and negative)
                self.tab3_all_diffs.append(rec)

                # Negative difference: Live Zain amount > Expected in sheet (مديونية زائدة في زين)
                if diff_val < -0.20:
                    self.tab2_negative_diffs.append(rec)
                else:
                    # Positive difference: Expected > Live (سداد العميل): Exclude settled/paid
                    if not is_settled_status(main_st, sub_st, notes, case_st):
                        self.tab1_net_diffs.append(rec)

    def build(self) -> None:
        wb = Workbook()

        if self.is_repair:
            # 1. Tab 1: نتائج صيانة وتدقيق الأخطاء
            ws1 = wb.active
            ws1.title = "نتائج صيانة وتدقيق الأخطاء"
            self._populate_master_sheet(
                ws=ws1,
                records=self.tab_all_records,
                total_label="إجمالي السجلات التي تمت صيانتها وتدقيقها",
                count_suffix="سجل",
                header_fill=FILL_HEADER_TEAL,
            )

            # 2. Tab 2: فروقات شامله
            ws2 = wb.create_sheet(title="فروقات شامله")
            self._populate_diff_sheet(
                ws=ws2,
                records=self.tab3_all_diffs,
                total_label="الإجمالي الكلي للفروقات الشاملة",
                count_suffix="سجل",
                header_fill=FILL_HEADER_BLUE,
            )

            # 3. Tab 3: الفروقات الايجابيه
            ws3 = wb.create_sheet(title="الفروقات الايجابيه")
            self._populate_diff_sheet(
                ws=ws3,
                records=self.tab1_net_diffs,
                total_label="إجمالي الفروقات الإيجابية",
                count_suffix="عملاء",
                header_fill=FILL_HEADER_GREEN,
            )

            # 4. Tab 4: فروقات سالبه
            ws4 = wb.create_sheet(title="فروقات سالبه")
            self._populate_diff_sheet(
                ws=ws4,
                records=self.tab2_negative_diffs,
                total_label="إجمالي الفروقات السالبة",
                count_suffix="عملاء",
                header_fill=FILL_HEADER_PURPLE,
            )

            # 5. Tab 5: الأخطاء المتبقية (لم تُحل)
            ws5 = wb.create_sheet(title="الأخطاء المتبقية (لم تُحل)")
            self._populate_notes_sheet(ws=ws5, notes_data=self.tab4_notes)
        else:
            # 1. Tab 1: جميع السجلات المفحوصة (الأرشيف الشامل)
            ws1 = wb.active
            ws1.title = "جميع السجلات المفحوصة"
            self._populate_master_sheet(
                ws=ws1,
                records=self.tab_all_records,
                total_label="الإجمالي الكلي لجميع السجلات المفحوصة",
                count_suffix="عميل",
                header_fill=FILL_HEADER_NAVY,
            )

            # 2. Tab 2: الفروقات الايجابيه
            ws2 = wb.create_sheet(title="الفروقات الايجابيه")
            self._populate_diff_sheet(
                ws=ws2,
                records=self.tab1_net_diffs,
                total_label="إجمالي الفروقات الإيجابية",
                count_suffix="عملاء",
                header_fill=FILL_HEADER_GREEN,
            )

            # 3. Tab 3: فروقات سالبه
            ws3 = wb.create_sheet(title="فروقات سالبه")
            self._populate_diff_sheet(
                ws=ws3,
                records=self.tab2_negative_diffs,
                total_label="إجمالي الفروقات السالبة",
                count_suffix="عملاء",
                header_fill=FILL_HEADER_PURPLE,
            )

            # 4. Tab 4: فروقات شامله
            ws4 = wb.create_sheet(title="فروقات شامله")
            self._populate_diff_sheet(
                ws=ws4,
                records=self.tab3_all_diffs,
                total_label="الإجمالي الكلي للفروقات الشاملة",
                count_suffix="سجل",
                header_fill=FILL_HEADER_BLUE,
            )

            # 5. Tab 5: الأخطاء والملاحظات للمحصل
            ws5 = wb.create_sheet(title="الأخطاء والملاحظات للمحصل")
            self._populate_notes_sheet(ws=ws5, notes_data=self.tab4_notes)

        # Save cleanly with retry in case user has file open in Excel
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        import time
        for attempt in range(4):
            try:
                wb.save(self.output_path)
                break
            except PermissionError:
                if attempt < 3:
                    time.sleep(1.0)
                else:
                    alt_path = self.output_path.with_name(f"{self.output_path.stem}_محدث.xlsx")
                    wb.save(alt_path)
                    break
        wb.close()

    def _populate_master_sheet(
        self,
        ws: Any,
        records: List[Dict[str, Any]],
        total_label: str,
        count_suffix: str,
        header_fill: PatternFill = FILL_HEADER_NAVY,
    ) -> None:
        """Populates comprehensive master sheet with all checked records, status badges, and formulas."""
        ws.views.sheetView[0].rightToLeft = True
        ws.freeze_panes = "A2"

        for col_letter, width in WIDTHS_MASTER_TAB.items():
            ws.column_dimensions[col_letter].width = width

        # Row 1: Header
        ws.row_dimensions[1].height = 30.0
        for col_idx, header_text in enumerate(HEADERS_MASTER_TAB, start=1):
            cell = ws.cell(row=1, column=col_idx, value=header_text)
            cell.font = FONT_HEADER
            cell.fill = header_fill
            cell.alignment = ALIGN_CENTER
            cell.border = BORDER_THIN

        # Data Rows
        current_row = 2
        for rec in records:
            ws.row_dimensions[current_row].height = 26.0

            method, identifier, url = build_hyperlink_and_method(
                account=rec.get("account") or "",
                service=rec.get("service") or "",
                contract=rec.get("contract") or "",
            )

            exp_sar = float(rec.get("expected_sar") or 0.0)
            live_sar_val = rec.get("live_sar")
            live_sar = float(live_sar_val) if live_sar_val is not None else None
            status = rec.get("status") or ""
            st_label = rec.get("status_label") or ("تطابق تام" if status == "match" else status)

            # Col 1: طريقة البحث
            c1 = ws.cell(row=current_row, column=1, value=method)
            c1.font = FONT_DATA_REGULAR
            c1.fill = FILL_DATA
            c1.alignment = ALIGN_CENTER
            c1.border = BORDER_THIN

            # Col 2: الحساب / الخدمة
            c2 = ws.cell(row=current_row, column=2, value=identifier)
            c2.font = FONT_DATA_REGULAR
            c2.fill = FILL_DATA
            c2.alignment = ALIGN_CENTER
            c2.border = BORDER_THIN

            # Col 3: رابط التأكد المباشر
            c3 = ws.cell(row=current_row, column=3, value="اضغط لفتح صفحة زين")
            c3.hyperlink = url
            c3.font = FONT_HYPERLINK
            c3.fill = FILL_DATA
            c3.alignment = ALIGN_CENTER
            c3.border = BORDER_THIN

            # Col 4: اسم العميل
            c4 = ws.cell(row=current_row, column=4, value=rec.get("name") or "عميل غير محدد")
            c4.font = FONT_DATA_REGULAR
            c4.fill = FILL_DATA
            c4.alignment = ALIGN_RIGHT
            c4.border = BORDER_THIN

            # Col 5: رقم الصف في الشيت
            c5 = ws.cell(row=current_row, column=5, value=str(rec.get("row") or current_row))
            c5.font = FONT_DATA_REGULAR
            c5.fill = FILL_DATA
            c5.alignment = ALIGN_CENTER
            c5.border = BORDER_THIN

            # Col 6: المبلغ المسجل بالشيت
            c6 = ws.cell(row=current_row, column=6, value=exp_sar)
            c6.number_format = NUMBER_FORMAT
            c6.font = FONT_DATA_REGULAR
            c6.fill = FILL_DATA
            c6.alignment = ALIGN_CENTER
            c6.border = BORDER_THIN

            # Col 7: المبلغ الحالي في موقع زين
            c7 = ws.cell(row=current_row, column=7)
            if live_sar is not None:
                c7.value = live_sar
                c7.number_format = NUMBER_FORMAT
            else:
                c7.value = "-"
            c7.font = FONT_DATA_REGULAR
            c7.fill = FILL_DATA
            c7.alignment = ALIGN_CENTER
            c7.border = BORDER_THIN

            # Col 8: الفرق (ريال)
            c8 = ws.cell(row=current_row, column=8)
            if live_sar is not None:
                c8.value = f"=F{current_row}-G{current_row}"
                c8.number_format = NUMBER_FORMAT
            else:
                c8 = ws.cell(row=current_row, column=8, value="-")
            c8.font = FONT_DATA_REGULAR
            c8.fill = FILL_DATA
            c8.alignment = ALIGN_CENTER
            c8.border = BORDER_THIN

            # Col 9: نتيجة الفحص
            c9 = ws.cell(row=current_row, column=9, value=st_label)
            c9.alignment = ALIGN_CENTER
            c9.border = BORDER_THIN
            c9.font = FONT_SUMMARY
            if status == "match" or "تطابق" in st_label:
                c9.fill = PatternFill(start_color="00E8F5E9", end_color="00E8F5E9", fill_type="solid")
            elif status == "mismatch" or "فرق" in st_label:
                c9.fill = PatternFill(start_color="00FFF3E0", end_color="00FFF3E0", fill_type="solid")
            else:
                c9.fill = PatternFill(start_color="00FFEBEE", end_color="00FFEBEE", fill_type="solid")

            # Col 10: الحالة الرئيسية بالملف
            c10 = ws.cell(row=current_row, column=10, value=rec.get("main_status") or "-")
            c10.font = FONT_DATA_REGULAR
            c10.fill = FILL_DATA
            c10.alignment = ALIGN_CENTER
            c10.border = BORDER_THIN

            # Col 11: الحالة الفرعية بالملف
            c11 = ws.cell(row=current_row, column=11, value=rec.get("sub_status") or "-")
            c11.font = FONT_DATA_REGULAR
            c11.fill = FILL_DATA
            c11.alignment = ALIGN_CENTER
            c11.border = BORDER_THIN

            # Col 12: أخر متابعة للمحصل
            c12 = ws.cell(row=current_row, column=12, value=rec.get("follow_notes") or "-")
            c12.font = FONT_DATA_REGULAR
            c12.fill = FILL_DATA
            c12.alignment = ALIGN_RIGHT
            c12.border = BORDER_THIN

            # Col 13: تاريخ ووقت الفحص
            c13 = ws.cell(row=current_row, column=13, value=rec.get("timestamp") or "-")
            c13.font = FONT_DATA_REGULAR
            c13.fill = FILL_DATA
            c13.alignment = ALIGN_CENTER
            c13.border = BORDER_THIN

            current_row += 1

        # Summary Row (Last row)
        last_row = current_row
        ws.row_dimensions[last_row].height = 26.0

        num_records = len(records)
        c_tot1 = ws.cell(row=last_row, column=1, value=total_label)
        c_tot2 = ws.cell(row=last_row, column=2, value=f"{num_records} {count_suffix}")

        for col_idx in (1, 2, 3, 4, 5, 9, 10, 11, 12, 13):
            c = ws.cell(row=last_row, column=col_idx)
            c.font = FONT_SUMMARY
            c.fill = FILL_SUMMARY
            c.alignment = ALIGN_CENTER
            c.border = BORDER_THIN

        start_row = 2
        end_data_row = max(2, last_row - 1)

        # Col 6: Sum of expected
        c_sum6 = ws.cell(row=last_row, column=6)
        c_sum6.value = f"=SUM(F{start_row}:F{end_data_row})" if num_records > 0 else 0.0
        c_sum6.number_format = NUMBER_FORMAT
        c_sum6.font = FONT_SUMMARY
        c_sum6.fill = FILL_SUMMARY
        c_sum6.alignment = ALIGN_CENTER
        c_sum6.border = BORDER_THIN

        # Col 7: Sum of live
        c_sum7 = ws.cell(row=last_row, column=7)
        c_sum7.value = f"=SUM(G{start_row}:G{end_data_row})" if num_records > 0 else 0.0
        c_sum7.number_format = NUMBER_FORMAT
        c_sum7.font = FONT_SUMMARY
        c_sum7.fill = FILL_SUMMARY
        c_sum7.alignment = ALIGN_CENTER
        c_sum7.border = BORDER_THIN

        # Col 8: Sum of diff
        c_sum8 = ws.cell(row=last_row, column=8)
        c_sum8.value = f"=SUM(H{start_row}:H{end_data_row})" if num_records > 0 else 0.0
        c_sum8.number_format = NUMBER_FORMAT
        c_sum8.font = FONT_SUMMARY
        c_sum8.fill = FILL_SUMMARY
        c_sum8.alignment = ALIGN_CENTER
        c_sum8.border = BORDER_THIN

    def _populate_diff_sheet(
        self,
        ws: Any,
        records: List[Dict[str, Any]],
        total_label: str,
        count_suffix: str,
        header_fill: PatternFill = FILL_HEADER_BLUE,
    ) -> None:
        """Populates an 8-column differences sheet with exact formatting and formulas."""
        ws.views.sheetView[0].rightToLeft = True
        ws.freeze_panes = "A2"

        # Column widths
        for col_letter, width in WIDTHS_DIFF_TABS.items():
            ws.column_dimensions[col_letter].width = width

        # Row 1: Header
        ws.row_dimensions[1].height = 30.0
        for col_idx, header_text in enumerate(HEADERS_DIFF_TAB, start=1):
            cell = ws.cell(row=1, column=col_idx, value=header_text)
            cell.font = FONT_HEADER
            cell.fill = header_fill
            cell.alignment = ALIGN_CENTER
            cell.border = BORDER_THIN

        # Data Rows
        current_row = 2
        for rec in records:
            ws.row_dimensions[current_row].height = 26.0

            method, identifier, url = build_hyperlink_and_method(
                account=rec.get("account") or "",
                service=rec.get("service") or "",
                contract=rec.get("contract") or "",
            )

            exp_sar = float(rec.get("expected_sar") or 0.0)
            live_sar = float(rec.get("live_sar") or 0.0)

            # Col 1: طريقة البحث في زين
            c1 = ws.cell(row=current_row, column=1, value=method)
            c1.font = FONT_DATA_REGULAR
            c1.fill = FILL_DATA
            c1.alignment = ALIGN_CENTER
            c1.border = BORDER_THIN

            # Col 2: رقم الحساب / الخدمة
            c2 = ws.cell(row=current_row, column=2, value=identifier)
            c2.font = FONT_DATA_REGULAR
            c2.fill = FILL_DATA
            c2.alignment = ALIGN_CENTER
            c2.border = BORDER_THIN

            # Col 3: رابط التأكد المباشر (Hyperlink)
            c3 = ws.cell(row=current_row, column=3, value="اضغط لفتح صفحة زين")
            c3.hyperlink = url
            c3.font = FONT_HYPERLINK
            c3.fill = FILL_DATA
            c3.alignment = ALIGN_CENTER
            c3.border = BORDER_THIN

            # Col 4: اسم العميل
            c4 = ws.cell(row=current_row, column=4, value=rec.get("name") or "عميل غير محدد")
            c4.font = FONT_DATA_REGULAR
            c4.fill = FILL_DATA
            c4.alignment = ALIGN_RIGHT
            c4.border = BORDER_THIN

            # Col 5: رقم الصف في الشيت
            c5 = ws.cell(row=current_row, column=5, value=str(rec.get("row") or current_row))
            c5.font = FONT_DATA_REGULAR
            c5.fill = FILL_DATA
            c5.alignment = ALIGN_CENTER
            c5.border = BORDER_THIN

            # Col 6: المبلغ المسجل بالشيت
            c6 = ws.cell(row=current_row, column=6, value=exp_sar)
            c6.number_format = NUMBER_FORMAT
            c6.font = FONT_DATA_REGULAR
            c6.fill = FILL_DATA
            c6.alignment = ALIGN_CENTER
            c6.border = BORDER_THIN

            # Col 7: المبلغ الحالي في موقع زين
            c7 = ws.cell(row=current_row, column=7, value=live_sar)
            c7.number_format = NUMBER_FORMAT
            c7.font = FONT_DATA_REGULAR
            c7.fill = FILL_DATA
            c7.alignment = ALIGN_CENTER
            c7.border = BORDER_THIN

            # Col 8: الفرق (ريال) -> Formula =F{row}-G{row}
            c8 = ws.cell(row=current_row, column=8, value=f"=F{current_row}-G{current_row}")
            c8.number_format = NUMBER_FORMAT
            c8.font = FONT_DATA_REGULAR
            c8.fill = FILL_DATA
            c8.alignment = ALIGN_CENTER
            c8.border = BORDER_THIN

            # Col 9: الحالة الرئيسية بالملف
            c9 = ws.cell(row=current_row, column=9, value=rec.get("main_status") or "-")
            c9.font = FONT_DATA_REGULAR
            c9.fill = FILL_DATA
            c9.alignment = ALIGN_CENTER
            c9.border = BORDER_THIN

            # Col 10: الحالة الفرعية بالملف
            c10 = ws.cell(row=current_row, column=10, value=rec.get("sub_status") or "-")
            c10.font = FONT_DATA_REGULAR
            c10.fill = FILL_DATA
            c10.alignment = ALIGN_CENTER
            c10.border = BORDER_THIN

            # Col 11: أخر متابعة للمحصل
            c11 = ws.cell(row=current_row, column=11, value=rec.get("follow_notes") or "-")
            c11.font = FONT_DATA_REGULAR
            c11.fill = FILL_DATA
            c11.alignment = ALIGN_RIGHT
            c11.border = BORDER_THIN

            current_row += 1

        # Summary Row (Last row)
        last_row = current_row
        ws.row_dimensions[last_row].height = 26.0

        num_records = len(records)
        c_tot1 = ws.cell(row=last_row, column=1, value=total_label)
        c_tot2 = ws.cell(row=last_row, column=2, value=f"{num_records} {count_suffix}")

        for col_idx in (1, 2, 3, 4, 5, 9, 10, 11):
            c = ws.cell(row=last_row, column=col_idx)
            c.font = FONT_SUMMARY
            c.fill = FILL_SUMMARY
            c.alignment = ALIGN_CENTER
            c.border = BORDER_THIN

        start_row = 2
        end_data_row = max(2, last_row - 1)

        # Col 6: Sum of expected
        c_sum6 = ws.cell(row=last_row, column=6)
        c_sum6.value = f"=SUM(F{start_row}:F{end_data_row})" if num_records > 0 else 0.0
        c_sum6.number_format = NUMBER_FORMAT
        c_sum6.font = FONT_SUMMARY
        c_sum6.fill = FILL_SUMMARY
        c_sum6.alignment = ALIGN_CENTER
        c_sum6.border = BORDER_THIN

        # Col 7: Sum of live
        c_sum7 = ws.cell(row=last_row, column=7)
        c_sum7.value = f"=SUM(G{start_row}:G{end_data_row})" if num_records > 0 else 0.0
        c_sum7.number_format = NUMBER_FORMAT
        c_sum7.font = FONT_SUMMARY
        c_sum7.fill = FILL_SUMMARY
        c_sum7.alignment = ALIGN_CENTER
        c_sum7.border = BORDER_THIN

        # Col 8: Sum of diff
        c_sum8 = ws.cell(row=last_row, column=8)
        c_sum8.value = f"=SUM(H{start_row}:H{end_data_row})" if num_records > 0 else 0.0
        c_sum8.number_format = NUMBER_FORMAT
        c_sum8.font = FONT_SUMMARY
        c_sum8.fill = FILL_SUMMARY
        c_sum8.alignment = ALIGN_CENTER
        c_sum8.border = BORDER_THIN

    def _populate_notes_sheet(self, ws: Any, notes_data: List[Tuple[Dict[str, Any], str]]) -> None:
        """Populates the 6-column errors and collector notes sheet matching 2.xlsx."""
        ws.views.sheetView[0].rightToLeft = True
        ws.freeze_panes = "A2"

        # Column widths
        for col_letter, width in WIDTHS_NOTES_TAB.items():
            ws.column_dimensions[col_letter].width = width

        # Row 1: Header (Red)
        ws.row_dimensions[1].height = 30.0
        for col_idx, header_text in enumerate(HEADERS_NOTES_TAB, start=1):
            cell = ws.cell(row=1, column=col_idx, value=header_text)
            cell.font = FONT_HEADER
            cell.fill = FILL_HEADER_RED
            cell.alignment = ALIGN_CENTER
            cell.border = BORDER_THIN

        # Data Rows
        current_row = 2
        if not notes_data:
            ws.row_dimensions[current_row].height = 26.0
            for col_idx in range(1, len(HEADERS_NOTES_TAB) + 1):
                cell = ws.cell(row=current_row, column=col_idx, value="")
                cell.font = FONT_DATA_REGULAR
                cell.fill = FILL_DATA
                cell.border = BORDER_THIN

        for rec, issue_text in notes_data:
            ws.row_dimensions[current_row].height = 26.0

            method, identifier, url = build_hyperlink_and_method(
                account=rec.get("account") or "",
                service=rec.get("service") or "",
                contract=rec.get("contract") or "",
            )

            exp_sar = float(rec.get("expected_sar") or 0.0)

            # Col 1: رقم الصف في الشيت
            c1 = ws.cell(row=current_row, column=1, value=str(rec.get("row") or current_row))
            c1.font = FONT_DATA_REGULAR
            c1.fill = FILL_DATA
            c1.alignment = ALIGN_CENTER
            c1.border = BORDER_THIN

            # Col 2: اسم العميل
            c2 = ws.cell(row=current_row, column=2, value=rec.get("name") or "عميل غير محدد")
            c2.font = FONT_DATA_REGULAR
            c2.fill = FILL_DATA
            c2.alignment = ALIGN_RIGHT
            c2.border = BORDER_THIN

            # Col 3: رقم الحساب ورقم الخدمة
            c3 = ws.cell(row=current_row, column=3, value=identifier)
            c3.font = FONT_DATA_REGULAR
            c3.fill = FILL_DATA
            c3.alignment = ALIGN_RIGHT
            c3.border = BORDER_THIN

            # Col 4: المبلغ في الشيت
            c4 = ws.cell(row=current_row, column=4, value=exp_sar)
            c4.number_format = NUMBER_FORMAT
            c4.font = FONT_DATA_REGULAR
            c4.fill = FILL_DATA
            c4.alignment = ALIGN_CENTER
            c4.border = BORDER_THIN

            # Col 5: المشكلة باختصار
            c5 = ws.cell(row=current_row, column=5, value=issue_text)
            c5.font = FONT_DATA_REGULAR
            c5.fill = FILL_DATA
            c5.alignment = ALIGN_RIGHT
            c5.border = BORDER_THIN

            # Col 6: رابط صفحة زين (Hyperlink)
            c6 = ws.cell(row=current_row, column=6, value="اضغط لفتح حساب العميل بزين")
            c6.hyperlink = url
            c6.font = FONT_HYPERLINK
            c6.fill = FILL_DATA
            c6.alignment = ALIGN_CENTER
            c6.border = BORDER_THIN

            # Col 7: الحالة الرئيسية بالملف
            c7 = ws.cell(row=current_row, column=7, value=rec.get("main_status") or "-")
            c7.font = FONT_DATA_REGULAR
            c7.fill = FILL_DATA
            c7.alignment = ALIGN_CENTER
            c7.border = BORDER_THIN

            # Col 8: الحالة الفرعية بالملف
            c8 = ws.cell(row=current_row, column=8, value=rec.get("sub_status") or "-")
            c8.font = FONT_DATA_REGULAR
            c8.fill = FILL_DATA
            c8.alignment = ALIGN_CENTER
            c8.border = BORDER_THIN

            current_row += 1


def export_executive_workbook(
    records: List[Dict[str, Any]],
    output_path: str | Path,
    is_repair: bool = False,
) -> None:
    """Public export function called by Orchestrator and CLI to build the executive multi-tab workbook."""
    reporter = ExactTemplateReporter(records=records, output_path=output_path, is_repair=is_repair)
    reporter.build()
