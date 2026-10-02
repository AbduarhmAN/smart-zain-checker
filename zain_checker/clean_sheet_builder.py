"""Sequential First-Seen Clean Sheet Builder (تنظيف الملف المتسلسل قبل البحث في زين).

Implements the exact row-by-row pre-processing algorithm specified by the user:
1. Reads the workbook from the first row to the last row sequentially (one by one).
2. Takes the first row's debt and stores its Account Number in memory.
3. For each subsequent row:
   - Checks its Account Number (`رقم الحساب`) and Service Number (`رقم الخدمة`).
   - If the Service Number starts with '2' (`2xxxxxxx`):
     It is KEPT in the Clean Sheet (unless the exact same '2xxxxxxx' service number
     was already kept earlier).
   - If the Service Number does NOT start with '2':
     Checks if we already kept a non-'2' row for this Account Number in memory:
     - If YES -> IGNORES this row (`نتجاهله`) so the account debt is never duplicated.
     - If NO  -> KEEPS this row (`أول رقم مديونية لهذا الحساب`) and records the Account
       Number in memory.
4. Produces a Clean Sheet (`الشيت النظيف`) where Account Numbers NEVER repeat except when:
   - One service starts with '2' and one does not (`[2..., غير 2]`)
   - Two services start with '2' (`[2..., 2...]`)
   - Three (or more) services start with '2' (`[2..., 2..., 2...]`)
   - Two (or more) services start with '2' and at most one does not (`[2..., 2..., غير 2]`)
   - Never two services that both do NOT start with '2' for the same Account Number!
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
import io
from pathlib import Path
import re
import sys
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill

from .pipeline import classify_and_parse_amount, normalize_arabic_text, sanitize_identifier_cell

# Enforce UTF-8 on Windows stdout/stderr safely without closing existing buffer (User Global Rule)
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
elif hasattr(sys.stdout, "buffer") and getattr(sys.stdout, "encoding", "").lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")


@dataclass
class CleanSheetRowDecision:
    """Represents the sequential decision made for a single Excel row."""

    sheet_name: str
    row_number: int
    account_number: str
    service_number: str
    starts_with_2: bool
    amount_text: str
    amount_halalas: int | None
    customer_name: str
    collector_name: str
    status_text: str
    kept: bool
    decision_code: str
    decision_reason_ar: str
    first_seen_row: int | None = None
    raw_row_values: tuple[Any, ...] = field(default_factory=tuple)


@dataclass
class CleanSheetResult:
    """Result of running the sequential Clean Sheet builder on a workbook."""

    source_workbook: Path
    clean_workbook_path: Path
    total_rows_scanned: int
    kept_rows_count: int
    ignored_rows_count: int
    unique_accounts_in_clean_sheet: int
    single_row_accounts_count: int
    multi_row_allowed_accounts_count: int
    pattern_breakdown: dict[str, int]
    account_patterns: dict[str, str]
    kept_decisions: list[CleanSheetRowDecision]
    ignored_decisions: list[CleanSheetRowDecision]

    def to_summary_dict(self) -> dict[str, Any]:
        return {
            "source_workbook": str(self.source_workbook),
            "clean_workbook_path": str(self.clean_workbook_path),
            "total_rows_scanned": self.total_rows_scanned,
            "kept_rows_count": self.kept_rows_count,
            "ignored_rows_count": self.ignored_rows_count,
            "unique_accounts_in_clean_sheet": self.unique_accounts_in_clean_sheet,
            "single_row_accounts_count": self.single_row_accounts_count,
            "multi_row_allowed_accounts_count": self.multi_row_allowed_accounts_count,
            "pattern_breakdown": self.pattern_breakdown,
        }


def _detect_columns_from_header(headers: tuple[Any, ...], sheet_index: int = 0) -> dict[str, int | None]:
    """Detects 1-based column indices for Account, Service, Amount, Customer, Collector, Status."""
    acc_col = None
    srv_col = None
    amt_col = None
    cust_col = None
    coll_col = None
    status_col = None

    AMOUNT_EXCLUSIONS = ("نوع", "رقم", "تاريخ", "كود", "عمر", "تصنيف", "ملاحظات", "حاوية", "جهة", "فرع", "مشرف", "مستخدم")
    contract_col = None
    remaining_col = None
    debt_col = None

    for idx, val in enumerate(headers, start=1):
        norm = normalize_arabic_text(val)
        if not norm:
            continue
        if acc_col is None and any(k in norm for k in ("رقم الحساب", "رقم العقد", "الحساب", "account")) and not any(ex in norm for ex in ("نوع", "تاريخ", "مبلغ")):
            acc_col = idx
        elif srv_col is None and any(
            k in norm for k in ("رقم الخدمه", "رقم الخدمة", "رقم المحفظه", "رقم المحفظة", "المحفظه", "الخدمه", "service")
        ) and not any(ex in norm for ex in ("تاريخ", "مبلغ")):
            srv_col = idx
        elif not any(ex in norm for ex in AMOUNT_EXCLUSIONS):
            if contract_col is None and any(k in norm for k in ("مبلغ العقد", "قيمة العقد", "إجمالي العقد", "contract")):
                contract_col = idx
            elif remaining_col is None and any(k in norm for k in ("متبقي سداد موثق", "باقي السداد", "متبقي السداد", "remaining")):
                remaining_col = idx
            elif debt_col is None and any(k in norm for k in ("مبلغ المديونية", "مبلغ الميدونية", "مبلغ المطالبة", "اجمالي المديونية")):
                debt_col = idx
            elif amt_col is None and any(k in norm for k in ("المديونية", "المديونيه", "المبلغ", "amount")):
                amt_col = idx
        if cust_col is None and any(k in norm for k in ("اسم العميل", "العميل", "الاسم", "customer")) and not any(ex in norm for ex in ("ارقام", "تصنيف", "رقم")):
            cust_col = idx
        elif coll_col is None and any(k in norm for k in ("المحصل", "اسم المحصل", "الموظف", "collector")):
            coll_col = idx
        elif status_col is None and any(k in norm for k in ("الحاله الرئيسيه", "الحالة الرئيسية", "الحاله", "status")) and not any(ex in norm for ex in ("فرعية", "فرعيه")):
            status_col = idx

    amt_col = contract_col or remaining_col or debt_col or amt_col

    # Fallback to standard Zain workbook column positions if header names differ
    if acc_col is None:
        acc_col = 7 if len(headers) >= 7 else 1
    if srv_col is None:
        srv_col = 6 if len(headers) >= 6 else acc_col
    if amt_col is None:
        amt_col = 8 if len(headers) >= 8 else 2
    if cust_col is None and len(headers) >= 4:
        cust_col = 4
    if coll_col is None and len(headers) >= 2:
        coll_col = 2
    if status_col is None and len(headers) >= 25:
        status_col = 25

    return {
        "account": acc_col,
        "service": srv_col,
        "amount": amt_col,
        "contract_amount": contract_col,
        "remaining_amount": remaining_col,
        "customer": cust_col,
        "collector": coll_col,
        "status": status_col,
    }


def classify_clean_account_pattern(services_for_account: list[str]) -> str:
    """Classifies how an Account Number appears in the Clean Sheet (`الشيت النظيف`)."""
    count_2 = sum(1 for s in services_for_account if s.startswith("2"))
    count_non2 = sum(1 for s in services_for_account if not s.startswith("2"))

    if len(services_for_account) == 1:
        if count_2 == 1:
            return "منفرد (رقم خدمة واحد يبدأ بـ 2)"
        return "منفرد (رقم خدمة واحد لا يبدأ بـ 2)"

    if count_2 == 1 and count_non2 == 1:
        return "مكرر مسموح: رقم خدمة يبدأ بـ 2 + رقم لا يبدأ بـ 2"
    if count_2 == 2 and count_non2 == 0:
        return "مكرر مسموح: رقمان يبدآن بـ 2"
    if count_2 == 3 and count_non2 == 0:
        return "مكرر مسموح: 3 أرقام تبدأ بـ 2"
    if count_2 >= 2 and count_non2 == 1:
        return f"مكرر مسموح: {count_2} أرقام تبدأ بـ 2 + رقم واحد لا يبدأ بـ 2"
    if count_2 > 3 and count_non2 == 0:
        return f"مكرر مسموح: {count_2} أرقام تبدأ بـ 2"
    return f"نمط ({count_2} يبدأ بـ 2، {count_non2} غير 2)"


def build_sequential_clean_sheet(
    workbook_path: Path,
    target_collector: str = "",
    filter_status: bool = False,
    status_column: int | None = None,
    output_clean_path: Path | None = None,
    sheet_indices: list[int] | None = None,
    custom_column_map: dict[str, int | None] | None = None,
) -> CleanSheetResult:
    """Executes the sequential top-to-bottom row scan and builds the Clean Sheet (`الشيت النظيف`).

    Memory Rules applied row-by-row from first to last:
    - Maintain `seen_non2_account_row[account_number]` -> first row where a non-'2' service was kept.
    - Maintain `seen_2_service_row[service_number]` -> first row where a '2xxxxxxx' service was kept.
    - When visiting row i:
      1. If `service_number` starts with '2':
         - Keep it (unless the exact same `service_number` was already kept).
      2. If `service_number` does NOT start with '2':
         - Check if `account_number` is already in `seen_non2_account_row`:
           - If YES -> Ignore row i (`تجاهل لأن رقم الحساب مكرر والخدمة لا تبدأ بـ 2`).
           - If NO  -> Keep row i (`أول مديونية لهذا الحساب`) and add `account_number` to `seen_non2_account_row`.
    """
    workbook_path = Path(workbook_path).resolve()
    if output_clean_path is None:
        from .config import PROJECT_DIRECTORY

        output_clean_path = PROJECT_DIRECTORY / f"{workbook_path.stem}_الشيت_النظيف.xlsx"

    wb_in = load_workbook(workbook_path, read_only=True, data_only=True)
    try:
        norm_target_coll = normalize_arabic_text(target_collector) if (target_collector and target_collector != "all") else ""

        # Sequential Memory State
        seen_non2_account_row: dict[str, tuple[str, int, str]] = {}  # acc_norm -> (sheet_name, row_num, srv_norm)
        seen_service_row: dict[str, tuple[str, int, str]] = {}       # srv_norm -> (sheet_name, row_num, acc_norm)

        kept_decisions: list[CleanSheetRowDecision] = []
        ignored_decisions: list[CleanSheetRowDecision] = []
        sheet_headers_map: dict[str, tuple[Any, ...]] = {}
        total_scanned = 0

        target_sheets = (
            [wb_in.worksheets[i] for i in sheet_indices if 0 <= i < len(wb_in.worksheets)]
            if sheet_indices is not None
            else list(wb_in.worksheets)
        )

        for sheet_idx, sheet in enumerate(target_sheets):
            # Skip previously generated audit/clean sheets if re-running on an output file
            if sheet.title in ("الشيت_النظيف", "السطور_المتجاهلة", "ملخص_التنظيف"):
                continue

            rows_iter = sheet.iter_rows(values_only=True)
            try:
                header_row = next(rows_iter)
            except StopIteration:
                continue

            sheet_headers_map[sheet.title] = tuple(header_row)
            col_map = custom_column_map or _detect_columns_from_header(header_row, sheet_idx)
            acc_col = col_map.get("account") or 7
            srv_col = col_map.get("service") or 6
            amt_col = col_map.get("amount") or 8
            cust_col = col_map.get("customer")
            coll_col = col_map.get("collector")
            stat_col = status_column if status_column is not None else col_map.get("status")

            for row_number, values in enumerate(rows_iter, start=2):
                if not any(v is not None and str(v).strip() != "" for v in values):
                    continue

                # Check optional collector filter ("إذا كان الملف للعميل / المحصل")
                raw_coll = values[coll_col - 1] if (coll_col and len(values) >= coll_col) else ""
                coll_text = str(raw_coll or "").strip()
                if norm_target_coll and normalize_arabic_text(coll_text) != norm_target_coll:
                    continue

                # Check optional status filter
                raw_stat = values[stat_col - 1] if (stat_col and len(values) >= stat_col) else ""
                stat_text = str(raw_stat or "").strip()
                if filter_status and stat_col:
                    hyphens_only = bool(stat_text) and bool(re.fullmatch(r"-+", stat_text.replace(" ", "")))
                    if stat_text and not hyphens_only and "عدم توصل" not in stat_text and "نشط" not in stat_text:
                        continue

                total_scanned += 1

                raw_acc = values[acc_col - 1] if (acc_col and len(values) >= acc_col) else None
                raw_srv = values[srv_col - 1] if (srv_col and len(values) >= srv_col) else None
                raw_amt = values[amt_col - 1] if (amt_col and len(values) >= amt_col) else None
                raw_cust = values[cust_col - 1] if (cust_col and len(values) >= cust_col) else "عميل غير محدد"

                acc_norm, _, _ = sanitize_identifier_cell(raw_acc)
                srv_norm, _, _ = sanitize_identifier_cell(raw_srv if raw_srv is not None else raw_acc)

                acc_key = acc_norm or str(raw_acc or "").strip()
                srv_key = srv_norm or str(raw_srv or "").strip()
                starts_with_2 = bool(srv_key.startswith("2"))

                amt_dec, _, _, _ = classify_and_parse_amount(
                    raw_amt, account_norm=acc_norm, service_norm=srv_norm
                )
                amt_halalas = int((amt_dec * 100).to_integral_value()) if amt_dec is not None else None
                amt_text = f"{float(amt_dec):.2f}" if amt_dec is not None else str(raw_amt or "0")

                # =========================================================================
                # SEQUENTIAL STEP-BY-STEP MEMORY DECISION (قانون التنظيف المتسلسل)
                # =========================================================================
                if starts_with_2:
                    # Service starts with '2': Keep even if Account Number was seen before,
                    # unless this exact same 2xxxxxxx Service Number was already kept!
                    if srv_key and srv_key in seen_service_row:
                        prev_sheet, prev_row, _ = seen_service_row[srv_key]
                        decision = CleanSheetRowDecision(
                            sheet_name=sheet.title,
                            row_number=row_number,
                            account_number=acc_key,
                            service_number=srv_key,
                            starts_with_2=True,
                            amount_text=amt_text,
                            amount_halalas=amt_halalas,
                            customer_name=str(raw_cust or "").strip(),
                            collector_name=coll_text,
                            status_text=stat_text,
                            kept=False,
                            decision_code="IGNORED_DUPLICATE_SERVICE_2",
                            decision_reason_ar=(
                                f"تم تجاهله: رقم الخدمة الذي يبدأ بـ 2 ({srv_key}) مكرر بذاته وتم أخذه مسبقاً في ({prev_sheet} سطر {prev_row})"
                            ),
                            first_seen_row=prev_row,
                            raw_row_values=tuple(values),
                        )
                        ignored_decisions.append(decision)
                    else:
                        if srv_key:
                            seen_service_row[srv_key] = (sheet.title, row_number, acc_key)
                        reason = (
                            "مقبول في الشيت النظيف: رقم الخدمة يبدأ بـ 2 (محفظة مستقلة تُقبل حتى لو تكرر رقم الحساب)"
                        )
                        decision = CleanSheetRowDecision(
                            sheet_name=sheet.title,
                            row_number=row_number,
                            account_number=acc_key,
                            service_number=srv_key,
                            starts_with_2=True,
                            amount_text=amt_text,
                            amount_halalas=amt_halalas,
                            customer_name=str(raw_cust or "").strip(),
                            collector_name=coll_text,
                            status_text=stat_text,
                            kept=True,
                            decision_code="KEPT_SERVICE_STARTS_WITH_2",
                            decision_reason_ar=reason,
                            first_seen_row=row_number,
                            raw_row_values=tuple(values),
                        )
                        kept_decisions.append(decision)
                else:
                    # Service does NOT start with '2':
                    # Check if Account Number already exists in our kept non-'2' memory!
                    if acc_key and acc_key in seen_non2_account_row:
                        prev_sheet, prev_row, prev_srv = seen_non2_account_row[acc_key]
                        decision = CleanSheetRowDecision(
                            sheet_name=sheet.title,
                            row_number=row_number,
                            account_number=acc_key,
                            service_number=srv_key,
                            starts_with_2=False,
                            amount_text=amt_text,
                            amount_halalas=amt_halalas,
                            customer_name=str(raw_cust or "").strip(),
                            collector_name=coll_text,
                            status_text=stat_text,
                            kept=False,
                            decision_code="IGNORED_DUPLICATE_ACCOUNT_NON_2",
                            decision_reason_ar=(
                                f"تم تجاهله: رقم الحساب ({acc_key}) مكرر في الذاكرة (أُخذت مديونيته في {prev_sheet} سطر {prev_row} "
                                f"برقم خدمة {prev_srv}) ورقم الخدمة الحالي ({srv_key}) لا يبدأ بـ 2"
                            ),
                            first_seen_row=prev_row,
                            raw_row_values=tuple(values),
                        )
                        ignored_decisions.append(decision)
                    else:
                        if acc_key:
                            seen_non2_account_row[acc_key] = (sheet.title, row_number, srv_key)
                        if srv_key:
                            seen_service_row[srv_key] = (sheet.title, row_number, acc_key)
                        decision = CleanSheetRowDecision(
                            sheet_name=sheet.title,
                            row_number=row_number,
                            account_number=acc_key,
                            service_number=srv_key,
                            starts_with_2=False,
                            amount_text=amt_text,
                            amount_halalas=amt_halalas,
                            customer_name=str(raw_cust or "").strip(),
                            collector_name=coll_text,
                            status_text=stat_text,
                            kept=True,
                            decision_code="KEPT_FIRST_ACCOUNT_DEBT",
                            decision_reason_ar="مقبول في الشيت النظيف: أول مديونية لهذا الحساب (رقم خدمة لا يبدأ بـ 2)",
                            first_seen_row=row_number,
                            raw_row_values=tuple(values),
                        )
                        kept_decisions.append(decision)
    finally:
        wb_in.close()

    # Classify the resulting Clean Sheet account patterns to verify the exact invariant
    acc_services_in_clean: dict[str, list[str]] = defaultdict(list)
    for d in kept_decisions:
        key = d.account_number or f"NO_ACC_{d.service_number}"
        acc_services_in_clean[key].append(d.service_number)

    account_patterns: dict[str, str] = {}
    pattern_breakdown: dict[str, int] = defaultdict(int)
    single_row_accounts = 0
    multi_row_allowed_accounts = 0

    for acc_key, srvs in acc_services_in_clean.items():
        pat = classify_clean_account_pattern(srvs)
        account_patterns[acc_key] = pat
        pattern_breakdown[pat] += 1
        if len(srvs) == 1:
            single_row_accounts += 1
        else:
            multi_row_allowed_accounts += 1

    result = CleanSheetResult(
        source_workbook=workbook_path,
        clean_workbook_path=output_clean_path,
        total_rows_scanned=total_scanned,
        kept_rows_count=len(kept_decisions),
        ignored_rows_count=len(ignored_decisions),
        unique_accounts_in_clean_sheet=len(acc_services_in_clean),
        single_row_accounts_count=single_row_accounts,
        multi_row_allowed_accounts_count=multi_row_allowed_accounts,
        pattern_breakdown=dict(pattern_breakdown),
        account_patterns=account_patterns,
        kept_decisions=kept_decisions,
        ignored_decisions=ignored_decisions,
    )

    _write_clean_workbook(result, sheet_headers_map)
    return result


def _write_clean_workbook(
    result: CleanSheetResult,
    sheet_headers_map: dict[str, tuple[Any, ...]],
) -> None:
    """Writes the physical Clean Workbook (`<name>_الشيت_النظيف.xlsx`) with 3 clear tabs."""
    wb = Workbook()
    ws_clean = wb.active
    ws_clean.title = "الشيت_النظيف"
    ws_clean.views.sheetView[0].rightToLeft = True

    header_fill = PatternFill(start_color="4C1D95", end_color="4C1D95", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True, size=11)
    fill_2 = PatternFill(start_color="DBEAFE", end_color="DBEAFE", fill_type="solid")
    fill_multi = PatternFill(start_color="FEF3C7", end_color="FEF3C7", fill_type="solid")
    fill_ignored = PatternFill(start_color="FEE2E2", end_color="FEE2E2", fill_type="solid")

    clean_headers = [
        "تسلسل_الشيت_النظيف",
        "الورقة_الأصلية",
        "رقم_السطر_الأصلي",
        "اسم_العميل",
        "المحصل",
        "رقم_الحساب",
        "رقم_الخدمة",
        "هل_الخدمة_تبدأ_بـ_2؟",
        "المديونية_المعتمدة",
        "طريقة_البحث_في_زين",
        "حالة_رقم_الحساب_في_الشيت_النظيف",
        "سبب_القبول_في_الذاكرة",
    ]
    ws_clean.append(clean_headers)
    for col_idx in range(1, len(clean_headers) + 1):
        cell = ws_clean.cell(1, col_idx)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for seq_idx, d in enumerate(result.kept_decisions, start=1):
        acc_key = d.account_number or f"NO_ACC_{d.service_number}"
        pat = result.account_patterns.get(acc_key, "منفرد")
        search_method = (
            f"بحث برقم الخدمة ({d.service_number}) — يبدأ بـ 2"
            if d.starts_with_2
            else f"بحث برقم الحساب ({d.account_number}) — أول مديونية للحساب"
        )
        ws_clean.append(
            [
                seq_idx,
                d.sheet_name,
                d.row_number,
                d.customer_name,
                d.collector_name,
                d.account_number,
                d.service_number,
                "نعم (2xxxxxxx)" if d.starts_with_2 else "لا (غير 2)",
                float(d.amount_halalas / 100.0) if d.amount_halalas is not None else d.amount_text,
                search_method,
                pat,
                d.decision_reason_ar,
            ]
        )
        row_fill = fill_2 if d.starts_with_2 else (fill_multi if "مكرر مسموح" in pat else None)
        if row_fill:
            for col_idx in range(1, len(clean_headers) + 1):
                ws_clean.cell(seq_idx + 1, col_idx).fill = row_fill

    # Sheet 2: Ignored Duplicate Rows (`السطور_المتجاهلة`)
    ws_ignored = wb.create_sheet("السطور_المتجاهلة")
    ws_ignored.views.sheetView[0].rightToLeft = True
    ignored_headers = [
        "تسلسل",
        "الورقة_الأصلية",
        "رقم_السطر_المتجاهل",
        "اسم_العميل",
        "رقم_الحساب_المكرر",
        "رقم_الخدمة_المتجاهل",
        "المديونية_المتجاهلة",
        "السطر_الأول_الذي_أُخذت_منه_المديونية",
        "سبب_التجاهل_في_الذاكرة",
    ]
    ws_ignored.append(ignored_headers)
    for col_idx in range(1, len(ignored_headers) + 1):
        cell = ws_ignored.cell(1, col_idx)
        cell.fill = PatternFill(start_color="991B1B", end_color="991B1B", fill_type="solid")
        cell.font = header_font

    for idx, d in enumerate(result.ignored_decisions, start=1):
        ws_ignored.append(
            [
                idx,
                d.sheet_name,
                d.row_number,
                d.customer_name,
                d.account_number,
                d.service_number,
                float(d.amount_halalas / 100.0) if d.amount_halalas is not None else d.amount_text,
                d.first_seen_row or "-",
                d.decision_reason_ar,
            ]
        )
        for col_idx in range(1, len(ignored_headers) + 1):
            ws_ignored.cell(idx + 1, col_idx).fill = fill_ignored

    # Sheet 3: Summary (`ملخص_التنظيف`)
    ws_sum = wb.create_sheet("ملخص_التنظيف")
    ws_sum.views.sheetView[0].rightToLeft = True
    ws_sum.append(["البند", "القيمة / الإحصائية"])
    ws_sum.cell(1, 1).fill = header_fill
    ws_sum.cell(1, 1).font = header_font
    ws_sum.cell(1, 2).fill = header_fill
    ws_sum.cell(1, 2).font = header_font

    summary_rows = [
        ("الملف الأصلي", str(result.source_workbook.name)),
        ("تاريخ التنظيف", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ("إجمالي السطور المفحوصة من الأول إلى الأخير", result.total_rows_scanned),
        ("إجمالي السطور الصافية في (الشيت النظيف)", result.kept_rows_count),
        ("إجمالي السطور المتجاهلة (لتكرار رقم الحساب لغير رقم 2)", result.ignored_rows_count),
        ("عدد أرقام الحسابات الفريدة في الشيت النظيف", result.unique_accounts_in_clean_sheet),
        ("حسابات ظهرت مرة واحدة فقط في الشيت النظيف", result.single_row_accounts_count),
        ("حسابات تكررت بشكل مسموح (بسبب وجود أرقام خدمة تبدأ بـ 2)", result.multi_row_allowed_accounts_count),
    ]
    for k, v in summary_rows:
        ws_sum.append([k, v])

    ws_sum.append(["", ""])
    ws_sum.append(["تفصيل حالات ظهور أرقام الحسابات في الشيت النظيف", "عدد الحسابات"])
    for pat, cnt in result.pattern_breakdown.items():
        ws_sum.append([pat, cnt])

    for ws in (ws_clean, ws_ignored, ws_sum):
        for col in ws.columns:
            max_len = max((len(str(cell.value or "")) for cell in col), default=12)
            ws.column_dimensions[col[0].column_letter].width = min(max(max_len + 4, 14), 55)

    result.clean_workbook_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(result.clean_workbook_path)
    wb.close()
