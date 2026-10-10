"""Sheet Updater Engine for Smart Zain Checker.
Updates source Excel workbooks in-place and distributes results into their exact correct tabs:
1. 'الفروقات الايجابيه' (Positive Net Differences)
2. 'فروقات سالبه' (Negative Debt Differences)
3. 'فروقات شامله' (All Differences)
4. 'السجلات المطابقة (تطابق تام)' (Fully Matched & Settled Records)
5. 'الأخطاء والملاحظات للمحصل' (Only Unresolved Technical Errors)
6. Raw Master Portfolios (e.g. 2.xlsx) with live Zain columns and audit formulas.
"""
from __future__ import annotations

import io
import logging
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import openpyxl
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from domain.workbook import normalize_header_text, parse_account_and_service
from zain_checker.executive_reporter import (
    build_hyperlink_and_method,
    is_settled_status,
    HEADERS_DIFF_TAB,
    WIDTHS_DIFF_TABS,
    NUMBER_FORMAT,
    FILL_HEADER_GREEN,
    FILL_HEADER_PURPLE,
    FILL_HEADER_BLUE,
    FILL_HEADER_RED,
    FILL_HEADER_TEAL,
    FILL_HEADER_NAVY,
    FILL_DATA,
    FILL_SUMMARY,
    BORDER_THIN,
    FONT_HEADER,
    FONT_DATA_REGULAR,
    FONT_HYPERLINK,
    FONT_SUMMARY,
)

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

logger = logging.getLogger("SheetUpdater")

FONT_NAME = "Calibri"
FONT_BOLD = Font(name=FONT_NAME, size=11, bold=True, color="00111827")
FILL_SUCCESS = PatternFill(start_color="00E8F5E9", end_color="00E8F5E9", fill_type="solid")
FILL_WARNING = PatternFill(start_color="00FFF3E0", end_color="00FFF3E0", fill_type="solid")
FILL_ERROR = PatternFill(start_color="00FFEBEE", end_color="00FFEBEE", fill_type="solid")

ALIGN_CENTER = Alignment(horizontal="center", vertical="center")
ALIGN_RIGHT = Alignment(horizontal="right", vertical="center")


def _find_sheet_by_keywords(wb: openpyxl.Workbook, keywords: tuple[str, ...]) -> Optional[Any]:
    """Finds a worksheet matching any of the given keywords."""
    for s in wb.worksheets:
        title = normalize_header_text(s.title)
        if any(normalize_header_text(k) in title for k in keywords):
            return s
    return None


def _write_diff_row(ws: Any, row_idx: int, rec: Dict[str, Any]) -> None:
    """Writes an 11-column difference or match row into ws at row_idx."""
    mawarid = bool(rec.get("mawarid_format"))
    method, identifier, url = build_hyperlink_and_method(
        account=rec.get("account") or "",
        service=rec.get("service") or "",
        contract=rec.get("contract") or "",
        mawarid_format=mawarid,
    )
    exp_sar = float(rec.get("expected_sar") or 0.0)
    live_sar_val = rec.get("live_sar")
    live_sar = float(live_sar_val) if live_sar_val is not None else 0.0

    ws.row_dimensions[row_idx].height = 26.0

    # 1: طريقة البحث
    c1 = ws.cell(row=row_idx, column=1, value=method)
    c1.font = FONT_DATA_REGULAR
    c1.fill = FILL_DATA
    c1.alignment = ALIGN_CENTER
    c1.border = BORDER_THIN

    # 2: الحساب / الخدمة أو رقم الحساب
    c2 = ws.cell(row=row_idx, column=2, value=identifier)
    c2.font = FONT_DATA_REGULAR
    c2.fill = FILL_DATA
    c2.alignment = ALIGN_CENTER
    c2.border = BORDER_THIN

    # 3: رابط التأكد
    c3 = ws.cell(row=row_idx, column=3, value="اضغط لفتح صفحة زين")
    c3.hyperlink = url
    c3.font = FONT_HYPERLINK
    c3.fill = FILL_DATA
    c3.alignment = ALIGN_CENTER
    c3.border = BORDER_THIN

    # 4: اسم العميل أو اسم المحصل
    col4_val = (rec.get("collector") or rec.get("collector_name") or "محصل غير محدد") if mawarid else (rec.get("name") or "عميل غير محدد")
    c4 = ws.cell(row=row_idx, column=4, value=col4_val)
    c4.font = FONT_DATA_REGULAR
    c4.fill = FILL_DATA
    c4.alignment = ALIGN_RIGHT
    c4.border = BORDER_THIN

    # 5: رقم الصف في الشيت
    c5 = ws.cell(row=row_idx, column=5, value=str(rec.get("row") or row_idx))
    c5.font = FONT_DATA_REGULAR
    c5.fill = FILL_DATA
    c5.alignment = ALIGN_CENTER
    c5.border = BORDER_THIN

    # 6: المبلغ المسجل بالشيت
    c6 = ws.cell(row=row_idx, column=6, value=exp_sar)
    c6.number_format = NUMBER_FORMAT
    c6.font = FONT_DATA_REGULAR
    c6.fill = FILL_DATA
    c6.alignment = ALIGN_CENTER
    c6.border = BORDER_THIN

    # 7: المبلغ الحالي في موقع زين
    c7 = ws.cell(row=row_idx, column=7, value=live_sar)
    c7.number_format = NUMBER_FORMAT
    c7.font = FONT_DATA_REGULAR
    c7.fill = FILL_DATA
    c7.alignment = ALIGN_CENTER
    c7.border = BORDER_THIN

    # 8: الفرق Formula
    c8 = ws.cell(row=row_idx, column=8, value=f"=F{row_idx}-G{row_idx}")
    c8.number_format = NUMBER_FORMAT
    c8.font = FONT_DATA_REGULAR
    c8.fill = FILL_DATA
    c8.alignment = ALIGN_CENTER
    c8.border = BORDER_THIN

    # 9: الحالة الرئيسية بالملف
    c9 = ws.cell(row=row_idx, column=9, value=rec.get("main_status") or "-")
    c9.font = FONT_DATA_REGULAR
    c9.fill = FILL_DATA
    c9.alignment = ALIGN_CENTER
    c9.border = BORDER_THIN

    # 10: الحالة الفرعية بالملف
    c10 = ws.cell(row=row_idx, column=10, value=rec.get("sub_status") or "-")
    c10.font = FONT_DATA_REGULAR
    c10.fill = FILL_DATA
    c10.alignment = ALIGN_CENTER
    c10.border = BORDER_THIN

    # 11: أخر متابعة للمحصل
    c11 = ws.cell(row=row_idx, column=11, value=rec.get("follow_notes") or "-")
    c11.font = FONT_DATA_REGULAR
    c11.fill = FILL_DATA
    c11.alignment = ALIGN_RIGHT
    c11.border = BORDER_THIN

    # 12: الزمن
    time_val = rec.get("timestamp") or rec.get("time") or rec.get("occurred_at") or "-"
    c12 = ws.cell(row=row_idx, column=12, value=str(time_val))
    c12.font = FONT_DATA_REGULAR
    c12.fill = FILL_DATA
    c12.alignment = ALIGN_CENTER
    c12.border = BORDER_THIN


def _append_to_diff_sheet(
    ws: Any,
    records_to_add: List[Dict[str, Any]],
    total_label: str,
    count_suffix: str,
) -> int:
    """Appends records before the summary row and recalculates summary formulas."""
    if not records_to_add:
        return 0

    existing_keys: Set[str] = set()
    summary_row_idx: Optional[int] = None

    for r in range(2, ws.max_row + 1):
        c1_val = str(ws.cell(r, 1).value or "").strip()
        c6_val = str(ws.cell(r, 6).value or "").strip()
        if c1_val.startswith("إجمالي") or c1_val.startswith("الإجمالي") or c6_val.startswith("=SUM"):
            summary_row_idx = r
            break
        row_id = str(ws.cell(r, 5).value or "").strip()
        if row_id:
            existing_keys.add(row_id)
        acc_text = str(ws.cell(r, 2).value or "").strip()
        for num in re.findall(r"\d+", acc_text):
            existing_keys.add(num)

    new_records = []
    for rec in records_to_add:
        r_id = str(rec.get("row") or "")
        clean_acc = re.sub(r"\D", "", str(rec.get("account") or ""))
        clean_srv = re.sub(r"\D", "", str(rec.get("service") or ""))
        if (r_id and r_id in existing_keys) or (clean_acc and clean_acc in existing_keys) or (clean_srv and clean_srv in existing_keys):
            continue
        new_records.append(rec)
        if r_id:
            existing_keys.add(r_id)
        if clean_acc:
            existing_keys.add(clean_acc)
        if clean_srv:
            existing_keys.add(clean_srv)

    if not new_records:
        return 0

    if summary_row_idx is None:
        summary_row_idx = ws.max_row + 1

    # Insert rows before summary row
    for rec in new_records:
        ws.insert_rows(summary_row_idx)
        _write_diff_row(ws, summary_row_idx, rec)
        summary_row_idx += 1

    # Now update summary row at bottom
    last_data_row = summary_row_idx - 1
    total_data_count = max(0, last_data_row - 1)

    ws.row_dimensions[summary_row_idx].height = 26.0
    ws.cell(summary_row_idx, 1, value=total_label)
    ws.cell(summary_row_idx, 2, value=f"{total_data_count} {count_suffix}")

    for c_idx in (1, 2, 3, 4, 5, 9, 10, 11, 12):
        c = ws.cell(summary_row_idx, c_idx)
        c.font = FONT_SUMMARY
        c.fill = FILL_SUMMARY
        c.alignment = ALIGN_CENTER
        c.border = BORDER_THIN

    c6 = ws.cell(summary_row_idx, 6, value=f"=SUM(F2:F{last_data_row})" if total_data_count > 0 else 0.0)
    c6.number_format = NUMBER_FORMAT
    c6.font = FONT_SUMMARY
    c6.fill = FILL_SUMMARY
    c6.alignment = ALIGN_CENTER
    c6.border = BORDER_THIN

    c7 = ws.cell(summary_row_idx, 7, value=f"=SUM(G2:G{last_data_row})" if total_data_count > 0 else 0.0)
    c7.number_format = NUMBER_FORMAT
    c7.font = FONT_SUMMARY
    c7.fill = FILL_SUMMARY
    c7.alignment = ALIGN_CENTER
    c7.border = BORDER_THIN

    c8 = ws.cell(summary_row_idx, 8, value=f"=SUM(H2:H{last_data_row})" if total_data_count > 0 else 0.0)
    c8.number_format = NUMBER_FORMAT
    c8.font = FONT_SUMMARY
    c8.fill = FILL_SUMMARY
    c8.alignment = ALIGN_CENTER
    c8.border = BORDER_THIN

    return len(new_records)


def _create_or_get_matched_sheet(wb: openpyxl.Workbook, matched_records: List[Dict[str, Any]]) -> Any:
    """Finds or creates 'السجلات المطابقة (تطابق تام)' and appends all matched records."""
    ws = _find_sheet_by_keywords(wb, ("مطابقة", "مطابقه", "تطابق"))
    if ws is not None:
        _append_to_diff_sheet(
            ws=ws,
            records_to_add=matched_records,
            total_label="إجمالي السجلات المطابقة",
            count_suffix="سجل",
        )
        return ws

    ws = wb.create_sheet(title="السجلات المطابقة (تطابق تام)")
    ws.views.sheetView[0].rightToLeft = True
    ws.freeze_panes = "A2"

    for col_letter, width in WIDTHS_DIFF_TABS.items():
        ws.column_dimensions[col_letter].width = width

        # Row 1: Header
        ws.row_dimensions[1].height = 30.0
        has_mawarid = any(bool(r.get("mawarid_format")) for r in matched_records if isinstance(r, dict))
        headers = list(HEADERS_DIFF_TAB)
        if has_mawarid:
            headers[1] = "رقم الحساب"
            headers[3] = "اسم المحصل"
        for col_idx, header_text in enumerate(headers, start=1):
            cell = ws.cell(row=1, column=col_idx, value=header_text)
            cell.font = FONT_HEADER
            cell.fill = FILL_HEADER_TEAL
            cell.alignment = ALIGN_CENTER
            cell.border = BORDER_THIN

    current_row = 2
    for rec in matched_records:
        _write_diff_row(ws, current_row, rec)
        current_row += 1

    last_row = current_row
    last_data_row = max(2, last_row - 1)
    num_records = len(matched_records)

    ws.row_dimensions[last_row].height = 26.0
    ws.cell(last_row, 1, value="إجمالي السجلات المطابقة")
    ws.cell(last_row, 2, value=f"{num_records} سجل")

    for c_idx in (1, 2, 3, 4, 5, 9, 10, 11):
        c = ws.cell(last_row, c_idx)
        c.font = FONT_SUMMARY
        c.fill = FILL_SUMMARY
        c.alignment = ALIGN_CENTER
        c.border = BORDER_THIN

    c6 = ws.cell(last_row, 6, value=f"=SUM(F2:F{last_data_row})" if num_records > 0 else 0.0)
    c6.number_format = NUMBER_FORMAT
    c6.font = FONT_SUMMARY
    c6.fill = FILL_SUMMARY
    c6.alignment = ALIGN_CENTER
    c6.border = BORDER_THIN

    c7 = ws.cell(last_row, 7, value=f"=SUM(G2:G{last_data_row})" if num_records > 0 else 0.0)
    c7.number_format = NUMBER_FORMAT
    c7.font = FONT_SUMMARY
    c7.fill = FILL_SUMMARY
    c7.alignment = ALIGN_CENTER
    c7.border = BORDER_THIN

    c8 = ws.cell(last_row, 8, value=f"=SUM(H2:H{last_data_row})" if num_records > 0 else 0.0)
    c8.number_format = NUMBER_FORMAT
    c8.font = FONT_SUMMARY
    c8.fill = FILL_SUMMARY
    c8.alignment = ALIGN_CENTER
    c8.border = BORDER_THIN

    return ws


def _clean_up_errors_sheet(ws_err: Any, resolved_row_ids: Set[str], resolved_numbers: Set[str]) -> None:
    """Removes resolved error rows from 'الأخطاء والملاحظات للمحصل' so only pending errors remain."""
    for r in range(ws_err.max_row, 1, -1):
        r_id = str(ws_err.cell(r, 1).value or "").strip()
        acc_text = str(ws_err.cell(r, 3).value or "").strip()
        is_resolved = (r_id and r_id in resolved_row_ids)
        if not is_resolved and acc_text:
            for num in re.findall(r"\d+", acc_text):
                if num in resolved_numbers:
                    is_resolved = True
                    break
        if is_resolved:
            ws_err.delete_rows(r, 1)

    # If all errors were cleared, write a clean success badge
    if ws_err.max_row <= 1:
        ws_err.row_dimensions[2].height = 28.0
        ws_err.cell(2, 1, value="-")
        ws_err.cell(2, 2, value="كافة السجلات")
        ws_err.cell(2, 3, value="-")
        ws_err.cell(2, 4, value=0.0)
        c5 = ws_err.cell(2, 5, value="✓ تم فحص وصيانة كافة السجلات بنجاح وترحيلها إلى التبويبات المناسبة (الفروقات / السجلات المطابقة)")
        c5.font = FONT_SUMMARY
        c5.fill = FILL_SUCCESS
        ws_err.cell(2, 6, value="-")
        ws_err.cell(2, 7, value="تمت المعالجة")
        ws_err.cell(2, 8, value="مكتمل")
        for c in range(1, 9):
            ws_err.cell(2, c).border = BORDER_THIN
            ws_err.cell(2, c).alignment = ALIGN_CENTER


def update_errors_or_results_sheet(
    workbook_path: Path,
    records: List[Dict[str, Any]],
    sheet_index: int = 0,
) -> Tuple[bool, Path, str]:
    """Updates the original results workbook in-place:
    - Appends positive differences to 'الفروقات الايجابيه' and 'فروقات شامله'.
    - Appends negative differences to 'فروقات سالبه' and 'فروقات شامله'.
    - Creates/appends to 'السجلات المطابقة (تطابق تام)' for all matching records.
    - Removes resolved rows from 'الأخطاء والملاحظات للمحصل'.
    - Recalculates all summary rows and formulas.
    """
    if not workbook_path.exists():
        return False, workbook_path, f"الملف غير موجود: {workbook_path}"

    try:
        wb = load_workbook(workbook_path)
    except Exception as e:
        logger.error(f"Failed to load workbook {workbook_path}: {e}")
        return False, workbook_path, str(e)

    # Classify records
    diff_pos_records: List[Dict[str, Any]] = []
    diff_neg_records: List[Dict[str, Any]] = []
    diff_all_records: List[Dict[str, Any]] = []
    match_records: List[Dict[str, Any]] = []
    resolved_row_ids: Set[str] = set()
    resolved_numbers: Set[str] = set()

    for rec in records:
        status = rec.get("status")
        live_sar_val = rec.get("live_sar")
        if status in ("not_found", "needs_review", "error") or live_sar_val is None:
            continue

        exp_val = float(rec.get("expected_sar") or 0.0)
        live_val = float(live_sar_val)
        diff_val = round(exp_val - live_val, 2)
        has_difference = (abs(diff_val) > 0.20) or (live_val == 0.0 and exp_val > 0.0) or (exp_val == 0.0 and live_val > 0.0)

        row_str = str(rec.get("row") or "")
        if row_str:
            resolved_row_ids.add(row_str)
        for k in ("lookup_number", "number", "account", "service", "contract"):
            raw = str(rec.get(k) or "").strip()
            clean = re.sub(r"\D", "", raw)
            if clean:
                resolved_numbers.add(clean)

        if has_difference:
            diff_all_records.append(rec)
            if diff_val < -0.20:
                diff_neg_records.append(rec)
            else:
                main_st = rec.get("main_status") or ""
                sub_st = rec.get("sub_status") or ""
                notes = rec.get("follow_notes") or ""
                case_st = rec.get("case_status") or ""
                if not is_settled_status(main_st, sub_st, notes, case_st):
                    diff_pos_records.append(rec)
        else:
            match_records.append(rec)

    # 1. Update Positive Differences
    ws_pos = _find_sheet_by_keywords(wb, ("ايجابيه", "ايجابية", "الصافية"))
    if ws_pos is not None and diff_pos_records:
        _append_to_diff_sheet(ws_pos, diff_pos_records, "إجمالي الفروقات الإيجابية", "عملاء")

    # 2. Update Negative Differences
    ws_neg = _find_sheet_by_keywords(wb, ("سالبه", "سالبة", "زيادة المديونية"))
    if ws_neg is not None and diff_neg_records:
        _append_to_diff_sheet(ws_neg, diff_neg_records, "إجمالي الفروقات السالبة", "عملاء")

    # 3. Update All Differences
    ws_all = _find_sheet_by_keywords(wb, ("شامله", "شاملة", "جميع الفروقات"))
    if ws_all is not None and diff_all_records:
        _append_to_diff_sheet(ws_all, diff_all_records, "الإجمالي الكلي للفروقات الشاملة", "سجل")

    # 4. Create / Append to Matched Sheet
    if match_records:
        _create_or_get_matched_sheet(wb, match_records)

    # 5. Clean up Errors Sheet
    ws_err = _find_sheet_by_keywords(wb, ("أخطاء", "اخطاء", "ملاحظات"))
    if ws_err is not None and (resolved_row_ids or resolved_numbers):
        _clean_up_errors_sheet(ws_err, resolved_row_ids, resolved_numbers)

    # Save to disk: In-place with fallback copy
    updated_path = workbook_path.with_name(f"{workbook_path.stem}_محدث.xlsx")
    final_return_path = None
    saved_any = False
    save_errors = []

    try:
        wb.save(updated_path)
        logger.info(f"Saved distributed results to {updated_path}")
        final_return_path = updated_path
        saved_any = True
    except Exception as e:
        logger.warning(f"Failed to save {updated_path}: {e}")
        save_errors.append(f"نسخة محدث: {e}")

    # Fast Clone: If updated copy saved successfully, use high-speed OS copy (0.01s) instead of redundant 30s wb.save()
    if saved_any and updated_path.exists():
        try:
            import shutil
            shutil.copy2(updated_path, workbook_path)
            logger.info(f"Updated original results file in-place at {workbook_path} via fast clone")
            final_return_path = workbook_path
        except PermissionError:
            logger.warning(f"Original file {workbook_path} is locked by another process (e.g. Excel). Saved to {updated_path}")
            save_errors.append("الملف الأصلي مغلق: PermissionError")
        except Exception as e:
            logger.warning(f"Could not overwrite {workbook_path}: {e}")
            save_errors.append(f"الملف الأصلي: {e}")
    elif not saved_any:
        try:
            wb.save(workbook_path)
            logger.info(f"Updated original results file in-place at {workbook_path}")
            final_return_path = workbook_path
            saved_any = True
        except Exception as e:
            save_errors.append(f"الملف الأصلي: {e}")

    wb.close()
    if not saved_any:
        return False, None, f"فشل حفظ التحديث في كلا المسارين ({workbook_path} و {updated_path}): {'; '.join(save_errors)}"

    return True, final_return_path, (
        f"تم بنجاح تحديث ملف النتائج الأصلي وتوزيع السجلات في تبويباتها الصحيحة: "
        f"({len(match_records)} مطابقة، {len(diff_pos_records)} فروقات إيجابية، "
        f"{len(diff_neg_records)} فروقات سالبة)."
    )


def update_raw_portfolio_sheet(
    workbook_path: Path,
    records: List[Dict[str, Any]],
    sheet_index: int = 0,
    has_headers: bool = True,
) -> Tuple[bool, Path, str]:
    """Updates a raw master portfolio workbook (e.g. 2.xlsx) by appending/updating
    columns for Live Zain Amount, Debt Difference, Audit Status, Timestamp, and Verification URL.
    """
    if not workbook_path.exists():
        return False, workbook_path, f"الملف غير موجود: {workbook_path}"

    try:
        wb = load_workbook(workbook_path)
    except Exception as e:
        logger.error(f"Failed to load raw portfolio workbook {workbook_path}: {e}")
        return False, workbook_path, str(e)

    target_ws = wb.worksheets[sheet_index] if 0 <= sheet_index < len(wb.worksheets) else wb.active

    # Build lookup dictionaries from records
    record_by_row: Dict[str, Dict[str, Any]] = {}
    record_by_num: Dict[str, Dict[str, Any]] = {}

    for rec in records:
        row_val = str(rec.get("row") or "").strip()
        if row_val:
            record_by_row[row_val] = rec
        for k in ("lookup_number", "number", "account", "service", "contract"):
            raw = str(rec.get(k) or "").strip()
            clean = re.sub(r"\D", "", raw)
            if clean:
                record_by_num[clean] = rec

    max_c = target_ws.max_column
    headers = [str(target_ws.cell(row=1, column=c).value or "").strip() for c in range(1, max_c + 1)] if has_headers else []

    col_live_idx = None
    col_diff_idx = None
    col_status_idx = None
    col_time_idx = None
    col_url_idx = None

    for idx, h in enumerate(headers, start=1):
        nh = normalize_header_text(h)
        if any(w in nh for w in ("المبلغ بموقع زين", "المبلغ في زين", "رصيد زين")):
            col_live_idx = idx
        elif any(w in nh for w in ("فرق المديونية", "فرق الرصيد", "الفرق ر س")):
            col_diff_idx = idx
        elif any(w in nh for w in ("نتيجة فحص زين", "حالة فحص زين", "حالة السداد بزين")):
            col_status_idx = idx
        elif any(w in nh for w in ("تاريخ ووقت الفحص", "وقت الرصد")):
            col_time_idx = idx
        elif any(w in nh for w in ("رابط التأكد", "رابط زين")):
            col_url_idx = idx

    next_col = max_c + 1
    new_cols = []
    if not col_live_idx:
        col_live_idx = next_col
        new_cols.append((col_live_idx, "المبلغ بموقع زين (ر.س)", 24.0))
        next_col += 1
    if not col_diff_idx:
        col_diff_idx = next_col
        new_cols.append((col_diff_idx, "فرق المديونية (ر.س)", 18.0))
        next_col += 1
    if not col_status_idx:
        col_status_idx = next_col
        new_cols.append((col_status_idx, "نتيجة فحص زين", 22.0))
        next_col += 1
    if not col_time_idx:
        col_time_idx = next_col
        new_cols.append((col_time_idx, "تاريخ ووقت الفحص", 22.0))
        next_col += 1
    if not col_url_idx:
        col_url_idx = next_col
        new_cols.append((col_url_idx, "رابط التأكد المباشر", 26.0))
        next_col += 1

    for c_idx, title, width in new_cols:
        if has_headers:
            c = target_ws.cell(row=1, column=c_idx, value=title)
            c.font = FONT_HEADER
            c.fill = FILL_HEADER_NAVY
            c.alignment = ALIGN_CENTER
            c.border = BORDER_THIN
        col_letter = get_column_letter(c_idx)
        target_ws.column_dimensions[col_letter].width = width

    max_r = target_ws.max_row
    max_c = target_ws.max_column

    updated_count = 0
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")

    start_row = 2 if has_headers else 1
    for r in range(start_row, max_r + 1):
        rec = record_by_row.get(str(r))
        if not rec:
            for c in range(1, min(15, max_c + 1)):
                val = str(target_ws.cell(row=r, column=c).value or "").strip()
                clean = re.sub(r"\D", "", val)
                if clean and clean in record_by_num:
                    rec = record_by_num[clean]
                    break

        if not rec:
            continue

        status = rec.get("status")
        live_sar = rec.get("live_sar")
        diff_sar = rec.get("diff_sar")
        status_label = rec.get("status_label") or "تم الفحص"
        rec_time = rec.get("timestamp") or now_str
        is_match = (status == "match")

        # 1. Live Amount
        if col_live_idx:
            c = target_ws.cell(row=r, column=col_live_idx)
            if live_sar is not None:
                c.value = float(live_sar)
                c.number_format = NUMBER_FORMAT
            else:
                c.value = "-"
            c.alignment = ALIGN_CENTER
            c.font = FONT_DATA_REGULAR
            c.border = BORDER_THIN

        # 2. Diff
        if col_diff_idx:
            c = target_ws.cell(row=r, column=col_diff_idx)
            if diff_sar is not None:
                c.value = float(diff_sar)
                c.number_format = NUMBER_FORMAT
            else:
                c.value = "-"
            c.alignment = ALIGN_CENTER
            c.font = FONT_DATA_REGULAR
            c.border = BORDER_THIN

        # 3. Status Label
        if col_status_idx:
            c = target_ws.cell(row=r, column=col_status_idx)
            c.value = status_label
            c.alignment = ALIGN_CENTER
            c.font = FONT_BOLD if is_match else FONT_DATA_REGULAR
            c.fill = FILL_SUCCESS if is_match else (FILL_WARNING if status == "mismatch" else FILL_ERROR)
            c.border = BORDER_THIN

        # 4. Timestamp
        if col_time_idx:
            c = target_ws.cell(row=r, column=col_time_idx)
            c.value = rec_time
            c.alignment = ALIGN_CENTER
            c.font = FONT_DATA_REGULAR
            c.border = BORDER_THIN

        # 5. Direct URL
        if col_url_idx:
            c = target_ws.cell(row=r, column=col_url_idx)
            c.value = "اضغط لفتح صفحة زين"
            contract_num = rec.get("contract") or rec.get("account") or ""
            service_num = rec.get("service") or ""
            if service_num and str(service_num).startswith("2"):
                c.hyperlink = f"https://app.sa.zain.com/ar/quickpay?account={service_num}"
            elif contract_num:
                c.hyperlink = f"https://app.sa.zain.com/ar/contract-payment?contract={contract_num}"
            else:
                c.hyperlink = "https://business.zain.sa/dashboard/quick-pay"
            c.font = FONT_HYPERLINK
            c.alignment = ALIGN_CENTER
            c.border = BORDER_THIN

        updated_count += 1

    updated_path = workbook_path.with_name(f"{workbook_path.stem}_محدث_بالفحص.xlsx")
    final_return_path = None
    saved_any = False
    save_errors = []

    try:
        wb.save(updated_path)
        logger.info(f"Saved updated master portfolio to {updated_path}")
        final_return_path = updated_path
        saved_any = True
    except Exception as e:
        logger.warning(f"Failed to save {updated_path}: {e}")
        save_errors.append(f"نسخة محدث: {e}")

    # Fast Clone: If updated copy saved successfully, use high-speed OS copy (0.01s) instead of redundant 30s wb.save()
    if saved_any and updated_path.exists():
        try:
            import shutil
            shutil.copy2(updated_path, workbook_path)
            logger.info(f"Updated original master file in-place at {workbook_path} via fast clone")
            final_return_path = workbook_path
        except PermissionError:
            logger.warning(f"Original master file {workbook_path} is locked by Excel. Saved to {updated_path}")
            save_errors.append("الملف الأصلي مغلق: PermissionError")
        except Exception as e:
            logger.warning(f"Could not overwrite {workbook_path}: {e}")
            save_errors.append(f"الملف الأصلي: {e}")
    elif not saved_any:
        try:
            wb.save(workbook_path)
            logger.info(f"Updated original master file in-place at {workbook_path}")
            final_return_path = workbook_path
            saved_any = True
        except Exception as e:
            save_errors.append(f"الملف الأصلي: {e}")

    wb.close()
    if not saved_any:
        return False, None, f"فشل حفظ التحديث في كلا المسارين ({workbook_path} و {updated_path}): {'; '.join(save_errors)}"

    return True, final_return_path, f"تم بنجاح تحديث وتوثيق نتائج فحص {updated_count} صف في ملف المحفظة."


def update_source_workbook_after_job(
    workbook_path: Optional[Path],
    records: List[Dict[str, Any]],
    sheet_index: int = 0,
    is_repair_session: bool = False,
    has_headers: bool = True,
) -> Tuple[bool, Optional[Path], str]:
    """Inspects the sheet and delegates to either results distributor or master portfolio updater."""
    if not workbook_path or not workbook_path.exists():
        return False, None, "لم يتم تحديد مسار ملف صالح للتحديث"

    if not records:
        return False, workbook_path, "لا توجد سجلات مكتملة لتحديث الملف بها"

    from domain.workbook import inspect_sheet_schema

    try:
        wb = load_workbook(workbook_path, read_only=True, data_only=True)
        try:
            ws = wb.worksheets[sheet_index] if 0 <= sheet_index < len(wb.worksheets) else wb.worksheets[0]
            schema = inspect_sheet_schema(ws, has_headers=has_headers)
            doc_type = schema.get("document_type", "")
        finally:
            wb.close()
    except Exception as e:
        logger.warning(f"Error inspecting schema for update: {e}")
        doc_type = ""

    is_errors_or_results = (
        is_repair_session
        or "شيت أخطاء" in doc_type
        or "شيت نتائج" in doc_type
        or any(k in workbook_path.stem for k in ("نتائج", "أخطاء", "صيانة", "XlsxTable"))
    )

    if has_headers and is_errors_or_results:
        return update_errors_or_results_sheet(
            workbook_path=workbook_path,
            records=records,
            sheet_index=sheet_index,
        )
    else:
        return update_raw_portfolio_sheet(
            workbook_path=workbook_path,
            records=records,
            sheet_index=sheet_index,
            has_headers=has_headers,
        )
