"""Scope-Safe Workbook Loader, Pre-Zain Planner & Result Writer for Smart Zain Checker.

Upgraded in smart_zainchecker_gemini to enforce:
- The Four Unbreakable Laws (Strict Scope Symmetry, Zero Residual Deduction,
  No Unfounded Root-Cause Inference, Error != Zero)
- 16-Stage Pre-Zain Processing Pipeline (via zain_checker.pipeline)
- Global Cross-Sheet Service Deduplication BEFORE Scope Filtering
- Non-Destructive Scope Tagging (Excluded rows preserved in AccountGroup context)
- N:1 Deduplicated Account Search Tasks for non-2 services
- Immutable Canonical Amount Column Lock (Prohibition #9: never switch columns post-Zain)
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .config import (
    ACCOUNTS_WORKSHEET_INDEX,
    COLLECTOR_COLUMN_NUMBER,
    CONTRACT_COLUMN_NUMBER,
    CUSTOMER_NAME_COLUMN_NUMBER,
    ERROR_SHEET_NAME,
    EXPECTED_AMOUNT_COLUMN_NUMBER,
    HEADER_ROW_NUMBER,
    MAIN_STATUS_COLUMN_NUMBER,
    MISMATCH_SHEET_NAME,
    SERVICE_NUMBER_COLUMN_NUMBER,
    SUB_STATUS_COLUMN_NUMBER,
    WALLET_WORKSHEET_INDEX,
    ZAIN_CONTRACT_PAYMENT_URL,
    ZAIN_QUICKPAY_URL,
)
from .domain_models import (
    AccountFileScopeStatus,
    AmountState,
    PreZainSearchPlan,
    RowValidationStatus,
)
from .money import halalas_to_number, money_to_halalas, to_western_digits
from .pipeline import (
    classify_and_parse_amount,
    normalize_arabic_text,
    run_pre_zain_pipeline,
    sanitize_identifier_cell,
)
from .reconciliation import format_variance_description


@dataclass(frozen=True)
class Customer:
    record_type: str
    lookup_number: str
    expected_amount: int
    row_numbers: tuple[int, ...]
    customer_name: str
    main_status: str
    sub_status: str
    source_key: str = ""
    expected_amount_2: int | None = None
    service_number: str = ""
    original_account_number: str = ""
    account_is_duplicate: bool = False
    # New Scope-Safe Architecture Fields
    expected_result_scope: str = "SERVICE_LEVEL"  # "SERVICE_LEVEL" | "ACCOUNT_LEVEL_TOTAL"
    file_scope_status: str = "FILE_SCOPE_COMPLETE"
    is_file_scope_complete: bool = True
    all_known_account_amount: int | None = None
    included_non2_amount: int | None = None
    triggering_services: tuple[str, ...] = ()
    all_account_services: tuple[str, ...] = ()
    task_id: str = ""

    @property
    def row_number(self) -> int:
        return self.row_numbers[0]

    @property
    def contract(self) -> str:
        """Backward-compatible internal name used by saved account progress."""
        return self.lookup_number

    @property
    def row_numbers_text(self) -> str:
        return ", ".join(str(number) for number in self.row_numbers)

    @property
    def progress_source(self) -> str:
        return self.source_key or self.record_type

    @property
    def record_type_ar(self) -> str:
        if self.expected_result_scope == "SERVICE_LEVEL" or self.record_type == "wallet":
            return "بحث برقم الخدمة (Service-Level)"
        if self.record_type == "account":
            return "بحث برقم الحساب (Account-Level Total)"
        return "فحص مختلط" if self.record_type == "mixed" else self.record_type

    @property
    def verification_url(self) -> str:
        if self.record_type == "account":
            return f"{ZAIN_CONTRACT_PAYMENT_URL}?contract={self.lookup_number}&language=ar"
        return f"{ZAIN_QUICKPAY_URL}?account={self.lookup_number}"


@dataclass(frozen=True)
class Mismatch:
    customer: Customer
    website_amount: int
    verified_at: datetime


@dataclass(frozen=True)
class CheckError:
    record_type: str
    lookup_number: str
    customer_name: str
    row_numbers: tuple[int, ...]
    expected_amount: int | None
    error_type: str
    details: str
    occurred_at: datetime
    source_key: str = ""
    service_number: str = ""
    original_account_number: str = ""

    @property
    def progress_source(self) -> str:
        return self.source_key or self.record_type

    @property
    def record_type_ar(self) -> str:
        return "رقم حساب" if self.record_type == "account" else "رقم خدمة"

    @property
    def row_numbers_text(self) -> str:
        return ", ".join(str(number) for number in self.row_numbers)

    @property
    def verification_url(self) -> str | None:
        if not self.lookup_number.isdigit():
            return None
        if self.record_type == "account":
            return f"{ZAIN_CONTRACT_PAYMENT_URL}?contract={self.lookup_number}&language=ar"
        return f"{ZAIN_QUICKPAY_URL}?account={self.lookup_number}"


def find_source_workbook(project_directory: Path) -> Path:
    candidates = sorted(
        path
        for path in project_directory.iterdir()
        if path.is_file()
        and path.name.lower().startswith("zain_data")
        and path.suffix.lower() == ".xlsx"
        and not path.name.startswith("~$")
    )

    if not candidates:
        raise RuntimeError(f"No zain_data*.xlsx file was found in {project_directory}.")

    exact_path = next(
        (path for path in candidates if path.name.lower() == "zain_data.xlsx"),
        None,
    )
    if exact_path:
        return exact_path

    if len(candidates) > 1:
        names = ", ".join(path.name for path in candidates)
        raise RuntimeError(f"More than one zain_data*.xlsx file was found: {names}.")

    return candidates[0]


def read_workbook_layout(
    workbook_path: Path,
) -> list[tuple[str, list[tuple[int, str]]]]:
    workbook = load_workbook(workbook_path, read_only=True, data_only=True)
    try:
        layout: list[tuple[str, list[tuple[int, str]]]] = []
        for sheet in workbook.worksheets:
            columns = [
                (
                    column_number,
                    _normalize_whitespace(sheet.cell(HEADER_ROW_NUMBER, column_number).value),
                )
                for column_number in range(1, sheet.max_column + 1)
            ]
            layout.append((sheet.title, columns))
        return layout
    finally:
        workbook.close()


def build_pre_zain_plan_from_workbook(
    workbook_path: Path,
    target_collector: str = "",
    selected_sheets: list[str] | None = None,
    custom_sheet_mappings: dict[str, dict[str, int]] | None = None,
) -> PreZainSearchPlan:
    """Runs the 16-stage Pre-Zain Pipeline and returns the sealed PreZainSearchPlan."""
    all_coll = not bool(target_collector and target_collector.strip() and target_collector != "all")
    return run_pre_zain_pipeline(
        workbook_path=workbook_path,
        target_collector=target_collector,
        all_collectors=all_coll,
        selected_sheets=selected_sheets,
        custom_sheet_mappings=custom_sheet_mappings,
    )


def load_records(
    workbook_path: Path,
    source_mode: str,
    target_collector: str,
    custom_configs: list[tuple] | list[dict] | None = None,
    filter_status: bool | None = None,
    status_column: int | None = None,
) -> tuple[list[Customer], list[CheckError]]:
    """Loads workbook records using the 16-stage Pre-Zain Audit & Deduplication Rules:
    1. Global duplicate & conflict check runs BEFORE status/collector exclusion (Stage 11).
    2. Excluded rows are NEVER hard-deleted before AccountGroup construction (Stage 12 & 13).
    3. Services starting with '2' create 1:1 Service Search tasks (SERVICE_LEVEL scope).
    4. Services NOT starting with '2' create N:1 Deduplicated Account Search tasks
       (ACCOUNT_LEVEL_TOTAL scope), comparing against AccountGroup.included_total_sum
       when FILE_SCOPE_COMPLETE and flagging Partial/Incomplete accounts!
    """
    workbook = load_workbook(workbook_path, read_only=True, data_only=True)

    try:
        if source_mode != "custom" and len(workbook.worksheets) < 2:
            raise RuntimeError("The Excel workbook must contain two worksheets.")

        if filter_status is None:
            default_filter_status = source_mode != "custom"
            default_status_column = MAIN_STATUS_COLUMN_NUMBER if default_filter_status else None
        else:
            default_filter_status = bool(filter_status)
            default_status_column = (
                status_column
                if status_column is not None
                else (MAIN_STATUS_COLUMN_NUMBER if default_filter_status else None)
            )

        selections: list[tuple] = []
        if source_mode in ("account", "both"):
            selections.append(
                (
                    ACCOUNTS_WORKSHEET_INDEX,
                    "account",
                    CONTRACT_COLUMN_NUMBER,
                    "account",
                    EXPECTED_AMOUNT_COLUMN_NUMBER,
                    CUSTOMER_NAME_COLUMN_NUMBER,
                    COLLECTOR_COLUMN_NUMBER,
                    SERVICE_NUMBER_COLUMN_NUMBER,
                    False,
                    False,
                    default_filter_status,
                    default_status_column,
                    None,
                )
            )
        if source_mode in ("wallet", "both"):
            selections.append(
                (
                    WALLET_WORKSHEET_INDEX,
                    "wallet",
                    SERVICE_NUMBER_COLUMN_NUMBER,
                    "wallet",
                    EXPECTED_AMOUNT_COLUMN_NUMBER,
                    CUSTOMER_NAME_COLUMN_NUMBER,
                    COLLECTOR_COLUMN_NUMBER,
                    SERVICE_NUMBER_COLUMN_NUMBER,
                    False,
                    False,
                    default_filter_status,
                    default_status_column,
                    None,
                )
            )
        if source_mode == "custom":
            if not custom_configs:
                raise RuntimeError("No custom sheet configuration provided.")
            for config in custom_configs:
                cfg_filter_status = default_filter_status
                cfg_status_col = default_status_column
                cfg_amount_col_2 = None
                if isinstance(config, dict):
                    sheet_index = config["sheet_index"]
                    lookup_column = config["lookup_column"]
                    amount_column = config["amount_column"]
                    customer_column = config.get("customer_column")
                    collector_column = config.get("collector_column")
                    record_type = config.get("record_type", "account")
                    service_column = config.get("service_column")
                    no_customer = config.get("no_customer", False)
                    no_collector = config.get("no_collector", False)
                    cfg_amount_col_2 = config.get("amount_column_2")
                    if "filter_status" in config:
                        cfg_filter_status = bool(config["filter_status"])
                    if "status_column" in config:
                        cfg_status_col = config["status_column"]
                elif len(config) >= 13:
                    (
                        sheet_index,
                        lookup_column,
                        amount_column,
                        customer_column,
                        collector_column,
                        record_type,
                        service_column,
                        no_customer,
                        no_collector,
                        cfg_filter_status,
                        cfg_status_col,
                        _,
                        cfg_amount_col_2,
                    ) = config[:13]
                elif len(config) >= 11:
                    (
                        sheet_index,
                        lookup_column,
                        amount_column,
                        customer_column,
                        collector_column,
                        record_type,
                        service_column,
                        no_customer,
                        no_collector,
                        cfg_filter_status,
                        cfg_status_col,
                    ) = config[:11]
                elif len(config) >= 9:
                    (
                        sheet_index,
                        lookup_column,
                        amount_column,
                        customer_column,
                        collector_column,
                        record_type,
                        service_column,
                        no_customer,
                        no_collector,
                    ) = config[:9]
                elif len(config) >= 7:
                    (
                        sheet_index,
                        lookup_column,
                        amount_column,
                        customer_column,
                        collector_column,
                        record_type,
                        service_column,
                    ) = config[:7]
                    no_customer = False
                    no_collector = False
                else:
                    (
                        sheet_index,
                        lookup_column,
                        amount_column,
                        customer_column,
                        collector_column,
                        record_type,
                    ) = config[:6]
                    service_column = None
                    no_customer = False
                    no_collector = False

                if (
                    not 0 <= sheet_index < len(workbook.worksheets)
                    or lookup_column <= 0
                    or amount_column <= 0
                    or record_type not in ("account", "wallet", "mixed")
                ):
                    raise RuntimeError("The custom sheet configuration is invalid.")

                if record_type == "mixed" and (not service_column or service_column <= 0):
                    raise RuntimeError(
                        "The custom sheet configuration is invalid: service column is required for mixed mode."
                    )

                selections.append(
                    (
                        sheet_index,
                        record_type,
                        lookup_column,
                        "custom",
                        amount_column,
                        customer_column,
                        collector_column,
                        service_column,
                        no_customer,
                        no_collector,
                        cfg_filter_status,
                        cfg_status_col,
                        cfg_amount_col_2,
                    )
                )
        if not selections:
            raise RuntimeError("The selected data source is invalid.")

        raw_extracted_rows: list[dict[str, Any]] = []
        errors: list[CheckError] = []

        for item in selections:
            (
                sheet_index,
                record_type,
                lookup_column,
                source_key,
                amount_column,
                customer_column,
                collector_column,
                service_column,
                no_customer,
                no_collector,
                item_filter_status,
                item_status_column,
                *extra_args,
            ) = item
            item_amount_column_2 = extra_args[0] if extra_args else None
            sheet = workbook.worksheets[sheet_index]
            if source_key == "custom":
                _validate_custom_headers(
                    sheet,
                    lookup_column=lookup_column,
                    amount_column=amount_column,
                    customer_column=customer_column,
                    service_column=service_column,
                    record_type=record_type,
                    no_customer=no_customer,
                    amount_column_2=item_amount_column_2,
                )
            else:
                _validate_headers(sheet, record_type, lookup_column)

            sheet_rows = _extract_sheet_rows_non_destructive(
                sheet,
                record_type=record_type,
                lookup_column=lookup_column,
                source_key=source_key,
                target_collector=target_collector,
                amount_column=amount_column,
                customer_column=customer_column,
                collector_column=collector_column,
                service_column=service_column,
                no_customer=no_customer,
                no_collector=no_collector,
                filter_status=item_filter_status,
                status_column=item_status_column,
                amount_column_2=item_amount_column_2,
            )
            raw_extracted_rows.extend(sheet_rows)

        # Step 0 (Pre-Search): Build and export the Clean Sheet (<workbook>_الشيت_النظيف.xlsx)
        # sequentially from first row to last row before starting any Zain search.
        try:
            from .clean_sheet_builder import build_sequential_clean_sheet

            clean_sheet_res = build_sequential_clean_sheet(
                workbook_path=workbook_path,
                target_collector=target_collector,
                filter_status=default_filter_status,
                status_column=default_status_column,
                sheet_indices=[item[0] for item in selections],
            )
            print(
                f"🧹 [الشيت النظيف] تم فحص {clean_sheet_res.total_rows_scanned} سطر -> "
                f"تم قبول {clean_sheet_res.kept_rows_count} سطر صافي وتجاهل {clean_sheet_res.ignored_rows_count} سطر مكرر. "
                f"تم حفظ الشيت النظيف في: {clean_sheet_res.clean_workbook_path.name}"
            )
        except Exception as clean_exc:
            print(f"⚠️ تعذر حفظ نسخة الشيت النظيف المستقلة ({clean_exc})، جاري تطبيق التنظيف في الذاكرة مباشرة.")

        # Build Clean-Sheet Tasks & Errors across all selected sheets
        customers, scope_errors = _build_scope_safe_customers_and_errors(raw_extracted_rows)
        errors.extend(scope_errors)
        return customers, errors
    finally:
        workbook.close()


def get_sheet_header_columns(sheet) -> list[dict[str, Any]]:
    """Reads Row 1 of the sheet and returns an exact list of all columns with indices and letters."""
    header_row = next(
        sheet.iter_rows(min_row=HEADER_ROW_NUMBER, max_row=HEADER_ROW_NUMBER, values_only=True),
        (),
    )
    cols = []
    for idx, raw_val in enumerate(header_row, start=1):
        letter = get_column_letter(idx)
        val_str = str(raw_val or "").strip()
        cols.append({
            "col_index": idx,
            "col_letter": letter,
            "header": val_str,
            "header_norm": _normalize_header(raw_val),
        })
    return cols


def detect_sheet_schema_and_modes(sheet) -> dict[str, Any]:
    """Inspects Row 1 headers and returns a comprehensive schema & verification modes analysis:
    - Detected columns and their letters
    - Supported modes (smart_hybrid, account_only, service_only)
    - Recommended mode
    - Missing required columns for each mode
    - Available amount columns for user choice
    """
    header_cols = get_sheet_header_columns(sheet)
    smart_info = detect_smart_row1_columns(sheet)
    indices = smart_info["indices"]
    letters = smart_info["letters"]

    has_acc = bool(indices.get("lookup_col"))
    has_srv = bool(indices.get("service_col"))
    has_amt1 = bool(indices.get("amount_col"))
    has_amt2 = bool(indices.get("amount_col_2"))
    has_any_amt = bool(has_amt1 or has_amt2)
    has_cust = bool(indices.get("customer_col"))
    has_coll = bool(indices.get("collector_col"))

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

    amt_options = []
    contract_c = indices.get("contract_col") or indices.get("amount_col_2")
    remaining_c = indices.get("remaining_col") or indices.get("amount_col")

    if contract_c:
        contract_l = get_column_letter(contract_c)
        amt_options.append({
            "id": "contract",
            "col_index": contract_c,
            "letter": contract_l,
            "label": f"مبلغ العقد [{contract_l}] (المطابق تماماً لموقع زين عند البحث برقم الحساب)",
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
            "label": f"كلاهما (فحص ذكي مزدوج: {contract_l} أو {rem_l} أيهما أقرب لموقع زين)",
        })

    # Document Type Classification & Acceptability Verification
    if has_acc and has_srv and bool(contract_c):
        doc_type = "محفظة تحصيل مديونيات زين (شاملة أرقام الحسابات وعقود الخدمات ومبالغ العقود)"
    elif has_acc and has_srv:
        doc_type = "محفظة مديونيات هجينة (أرقام حسابات وأرقام خدمات)"
    elif has_acc:
        doc_type = "شيت حسابات زين (Account Numbers Sheet)"
    elif has_srv:
        doc_type = "شيت محافظ/خدمات زين (Wallets/Services Sheet)"
    else:
        doc_type = "شيت بيانات عام / بحاجة لضبط الأعمدة"

    is_acceptable = bool((has_acc or has_srv) and has_any_amt)
    if is_acceptable:
        ver_status = "✓ الشيت صالح ومقبول للتدقيق (Valid & Acceptable)"
    else:
        ver_status = "⚠️ الشيت غير مكتمل (ينقص عمود الحساب/الخدمة أو عمود المبلغ)"

    est_rows = max(0, sheet.max_row - 1) if getattr(sheet, "max_row", None) else 0

    return {
        "columns": header_cols,
        "indices": indices,
        "letters": letters,
        "document_type": doc_type,
        "is_acceptable": is_acceptable,
        "verification_status": ver_status,
        "estimated_rows": est_rows,
        "supported_modes": supported,
        "recommended_mode": rec_mode,
        "missing_for_modes": missing,
        "amount_options": amt_options,
        "has_customer_col": has_cust,
        "has_collector_col": has_coll,
        "has_remaining_amount": bool(remaining_c),
        "has_contract_amount": bool(contract_c),
        "contract_amount_col": contract_c,
        "contract_amount_letter": get_column_letter(contract_c) if contract_c else None,
        "remaining_amount_col": remaining_c,
        "remaining_amount_letter": get_column_letter(remaining_c) if remaining_c else None,
    }


def detect_smart_row1_columns(sheet) -> dict[str, Any]:
    """Scans Row 1 (أول صف) of the worksheet for smart headers (if present / إن وُجد):
    1. اسم العميل -> customer_col (اختياري / Optional)
    2. رقم الحساب / العقد -> lookup_col
    3. متبقي سداد موثق -> amount_col (المبلغ 1)
    4. مبلغ العقد -> amount_col_2 (المبلغ 2)
    5. رقم الخدمة -> service_col
    6. المحصل -> collector_col (اختياري / Optional)
    7. الحالة الرئيسية -> main_status_col (للنتائج فقط)
    8. الحالة الفرعية -> sub_status_col (للنتائج فقط)
    """
    header_row = next(
        sheet.iter_rows(min_row=HEADER_ROW_NUMBER, max_row=HEADER_ROW_NUMBER, values_only=True),
        (),
    )
    sample_row = next(
        sheet.iter_rows(min_row=HEADER_ROW_NUMBER + 1, max_row=HEADER_ROW_NUMBER + 1, values_only=True),
        (),
    )

    detected: dict[str, int | None] = {
        "customer_col": None,
        "lookup_col": None,
        "amount_col": None,
        "amount_col_2": None,
        "contract_col": None,
        "remaining_col": None,
        "debt_col": None,
        "service_col": None,
        "collector_col": None,
        "main_status_col": None,
        "sub_status_col": None,
        "notes_col": None,
    }
    contract_col = None
    remaining_col = None
    debt_col = None
    fallback_amt2_col = None

    def _is_numeric_sample(val: Any) -> bool:
        if val is None or str(val).strip() == "":
            return True
        cleaned = str(val).replace(",", "").replace("%", "").strip()
        try:
            float(cleaned)
            return True
        except ValueError:
            return False

    def _matches_any(text: str, synonyms: tuple[str, ...]) -> bool:
        return any(_normalize_header(s) in text for s in synonyms)

    def _has_negative_keywords(text: str, neg_words: tuple[str, ...]) -> bool:
        return any(w in text for w in neg_words)

    AMOUNT_EXCLUSIONS = (
        "نوع", "رقم", "عمر", "تاريخ", "كود", "ايام", "أيام", "هاتف", "جوال",
        "مفضل", "حاوية", "جهة", "فرع", "مشرف", "مستخدم", "سسس", "يي", "تصنيف",
        "ملاحظات", "متابعة"
    )

    for idx, raw_val in enumerate(header_row, start=1):
        norm = _normalize_header(raw_val)
        if not norm:
            continue
        sample_val = sample_row[idx - 1] if idx <= len(sample_row) else None
        
        # 1. اسم العميل (اختياري)
        if (
            not detected["customer_col"]
            and not _has_negative_keywords(norm, ("ارقام", "تصنيف", "متابعة", "تاريخ", "رقم"))
            and _matches_any(norm, ("اسم العميل", "العميل", "الاسم", "اسم المشترك", "المشترك", "customer", "client", "name"))
        ):
            detected["customer_col"] = idx

        # 2. رقم الحساب / العقد
        elif (
            not detected["lookup_col"]
            and not _has_negative_keywords(norm, ("تاريخ", "سدادات", "متبقي", "مبلغ", "قيمة", "نوع"))
            and _matches_any(norm, ("رقم الحساب", "رقم العقد", "الحساب", "العقد", "رقم الفاتورة", "account", "contract", "acct"))
        ):
            detected["lookup_col"] = idx

        # 3. مبلغ العقد (المطابق تماماً لموقع زين عند البحث برقم الحساب)
        elif (
            not contract_col
            and not _has_negative_keywords(norm, AMOUNT_EXCLUSIONS)
            and _is_numeric_sample(sample_val)
            and _matches_any(norm, (
                "مبلغ العقد", "قيمة العقد", "إجمالي العقد", "اجمالي العقد",
                "مبلغ عقد", "قيمة عقد", "total contract", "contract amount"
            ))
        ):
            contract_col = idx

        # 4. متبقي سداد موثق (المبلغ المتبقي)
        elif (
            not remaining_col
            and not _has_negative_keywords(norm, AMOUNT_EXCLUSIONS)
            and _is_numeric_sample(sample_val)
            and _matches_any(norm, (
                "متبقي سداد موثق", "باقي السداد الموثق", "باقي السداد", "متبقي السداد",
                "المبلغ المتبقي", "الرصيد المتبقي", "المديونية المتبقية", "المديونية المعتمدة",
                "صافي المديونية", "متبقي موثق", "remaining"
            ))
        ):
            remaining_col = idx

        # 5. مبلغ المديونية الإجمالية / المطالبة
        elif (
            not debt_col
            and not _has_negative_keywords(norm, AMOUNT_EXCLUSIONS)
            and _is_numeric_sample(sample_val)
            and _matches_any(norm, (
                "مبلغ المديونية", "مبلغ الميدونية", "مبلغ المطالبة", "المبلغ الإجمالي", "الإجمالي", "total amount"
            ))
        ):
            debt_col = idx

        elif (
            not fallback_amt2_col
            and not _has_negative_keywords(norm, AMOUNT_EXCLUSIONS)
            and _is_numeric_sample(sample_val)
            and _matches_any(norm, ("متبقي سداد العقد", "متبقي العقد"))
        ):
            fallback_amt2_col = idx

        # 6. رقم الخدمة
        elif (
            not detected["service_col"]
            and not _has_negative_keywords(norm, ("تاريخ", "نوع", "مبلغ"))
            and _matches_any(norm, (
                "رقم الخدمة", "رقم الخدمه", "الخدمة", "الخدمه", "رقم المحفظة", "رقم المحفظه",
                "المحفظة", "المحفظه", "رقم الجوال", "الجوال", "رقم الهاتف", "الهاتف",
                "service", "msisdn", "phone", "mobile", "wallet"
            ))
        ):
            detected["service_col"] = idx

        # 7. المحصل (اختياري)
        elif (
            not detected["collector_col"]
            and not _has_negative_keywords(norm, ("تاريخ", "اسناد", "اشراف", "مشرف", "مستخدم", "فرع"))
            and _matches_any(norm, ("المحصل", "اسم المحصل", "الموظف", "collector"))
        ):
            detected["collector_col"] = idx

        # 8. الحالات
        elif not detected["main_status_col"] and _matches_any(norm, ("الحالة الرئيسية", "الحاله الرئيسيه", "main status")):
            detected["main_status_col"] = idx
        elif not detected["sub_status_col"] and _matches_any(norm, ("الحالة الفرعية", "الحاله الفرعيه", "sub status")):
            detected["sub_status_col"] = idx
        elif not detected["notes_col"] and _matches_any(norm, ("المتابعة", "المتابعه", "أخر متابعة", "آخر متابعة", "ملاحظات", "notes")):
            detected["notes_col"] = idx

    if not detected["main_status_col"]:
        for idx, raw_val in enumerate(header_row, start=1):
            norm = _normalize_header(raw_val)
            if norm and _matches_any(norm, ("الحالة", "الحاله", "status")) and not _has_negative_keywords(norm, ("فرعية", "فرعيه", "ثانوية")):
                detected["main_status_col"] = idx
                break

    # Prioritization:
    # contract_col is designated specifically for Contract Amount (مبلغ العقد)
    detected["contract_col"] = contract_col
    detected["remaining_col"] = remaining_col
    detected["debt_col"] = debt_col

    detected["amount_col_2"] = contract_col or debt_col or fallback_amt2_col
    detected["amount_col"] = remaining_col or debt_col or contract_col or fallback_amt2_col

    if not detected["amount_col"] and detected["amount_col_2"]:
        detected["amount_col"] = detected["amount_col_2"]
    elif not detected["amount_col_2"] and detected["amount_col"]:
        detected["amount_col_2"] = detected["amount_col"]

    letters = {
        k: (get_column_letter(v) if v else None)
        for k, v in detected.items()
    }
    return {"indices": detected, "letters": letters}


def _extract_sheet_rows_non_destructive(
    sheet,
    record_type: str,
    lookup_column: int,
    source_key: str,
    target_collector: str,
    amount_column: int,
    customer_column: int | None = None,
    collector_column: int | None = None,
    service_column: int | None = None,
    no_customer: bool = False,
    no_collector: bool = False,
    filter_status: bool | None = None,
    status_column: int | None = None,
    amount_column_2: int | None = None,
) -> list[dict[str, Any]]:
    """Extracts all rows with their raw values, normalized identifiers, and scope flags.
    Automatically detects Row 1 smart headers (اسم العميل، رقم الحساب، متبقي سداد موثق، مبلغ العقد، رقم الخدمة)
    and assigns them automatically to the program, while using (الحالة الرئيسية، الحالة الفرعية) exclusively
    for Results classification (if present).
    """
    smart = detect_smart_row1_columns(sheet)["indices"]

    if source_key == "custom":
        # Custom mode: Respect user's explicit choices, use smart detection as fallback for any missing column
        if not customer_column and smart.get("customer_col"):
            customer_column = smart["customer_col"]
        if not lookup_column and smart.get("lookup_col"):
            lookup_column = smart["lookup_col"]
        if not amount_column and smart.get("amount_col"):
            amount_column = smart["amount_col"]
        if not amount_column_2 and smart.get("amount_col_2"):
            amount_column_2 = smart["amount_col_2"]
        if not service_column and smart.get("service_col"):
            service_column = smart["service_col"]
        if not collector_column and smart.get("collector_col"):
            collector_column = smart["collector_col"]
    else:
        # Standard auto mode: Auto-assign smart headers found in Row 1 (إن وُجد)
        if smart.get("customer_col"):
            customer_column = smart["customer_col"]
        if smart.get("lookup_col"):
            lookup_column = smart["lookup_col"]
        if smart.get("amount_col"):
            amount_column = smart["amount_col"]
        if smart.get("amount_col_2"):
            amount_column_2 = smart["amount_col_2"]
        if smart.get("service_col"):
            service_column = smart["service_col"]
            if smart.get("lookup_col"):
                record_type = "mixed"
        if smart.get("collector_col") and not collector_column:
            collector_column = smart["collector_col"]

    detected_acc_col = lookup_column if record_type in ("account", "mixed") else smart.get("lookup_col")
    detected_serv_col = service_column if service_column else smart.get("service_col")
    eff_cust_col = None if no_customer else (customer_column or smart.get("customer_col"))

    # الحالة الرئيسية & الحالة الفرعية are detected from Row 1 (if present) and used ONLY in the Results
    eff_main_status_col = smart["main_status_col"]
    eff_sub_status_col = smart["sub_status_col"]
    detected_notes_col = smart["notes_col"]

    cols_to_check = [lookup_column, amount_column]
    if amount_column_2 and amount_column_2 > 0:
        cols_to_check.append(amount_column_2)
    if detected_acc_col:
        cols_to_check.append(detected_acc_col)
    if eff_cust_col:
        cols_to_check.append(eff_cust_col)
    if not no_collector and collector_column:
        cols_to_check.append(collector_column)
    if service_column:
        cols_to_check.append(service_column)
    if eff_main_status_col:
        cols_to_check.append(eff_main_status_col)
    if eff_sub_status_col:
        cols_to_check.append(eff_sub_status_col)
    if detected_notes_col:
        cols_to_check.append(detected_notes_col)
    required_max_col = max(cols_to_check) if cols_to_check else 1

    if source_key != "custom":
        required_max_col = max(
            required_max_col,
            SERVICE_NUMBER_COLUMN_NUMBER,
            COLLECTOR_COLUMN_NUMBER,
            CUSTOMER_NAME_COLUMN_NUMBER,
            MAIN_STATUS_COLUMN_NUMBER,
            SUB_STATUS_COLUMN_NUMBER,
        )

    norm_target_collector = normalize_arabic_text(target_collector) if target_collector else ""
    rows_iter = sheet.iter_rows(
        min_row=HEADER_ROW_NUMBER + 1,
        min_col=1,
        max_col=required_max_col,
        values_only=True,
    )

    extracted: list[dict[str, Any]] = []
    for row_number, values in enumerate(rows_iter, start=HEADER_ROW_NUMBER + 1):
        if not any(v is not None and str(v).strip() != "" for v in values):
            continue

        # 1. Collector scope check (الحالة الرئيسية والفرعية لا تمنع فحص السجل بل تُستعمل في النتائج فقط)
        collector_in_scope = True
        if not no_collector and norm_target_collector and collector_column is not None:
            raw_coll = values[collector_column - 1] if len(values) >= collector_column else ""
            if normalize_arabic_text(raw_coll) != norm_target_collector:
                collector_in_scope = False

        status_in_scope = True
        include_in_scope = collector_in_scope and status_in_scope

        # 2. Extract Raw & Sanitized Identifiers (Stages 5, 6, 7)
        raw_amount = values[amount_column - 1] if len(values) >= amount_column else None
        if eff_cust_col and len(values) >= eff_cust_col:
            customer_name = _normalize_whitespace(values[eff_cust_col - 1]) or "عميل غير محدد"
        elif not no_customer and customer_column and len(values) >= customer_column:
            customer_name = _normalize_whitespace(values[customer_column - 1]) or "عميل غير محدد"
        else:
            customer_name = "عميل غير محدد"

        raw_service = (
            values[service_column - 1] if (service_column and len(values) >= service_column) else None
        )
        raw_lookup = values[lookup_column - 1] if len(values) >= lookup_column else None
        raw_acc = (
            values[detected_acc_col - 1]
            if (detected_acc_col and len(values) >= detected_acc_col)
            else raw_lookup
        )

        srv_norm, srv_errs, _ = sanitize_identifier_cell(raw_service if raw_service is not None else raw_lookup)
        acc_norm, acc_errs, _ = sanitize_identifier_cell(raw_acc)

        # Security: Check for Poison Pill account trigger
        try:
            from zain_checker.telegram_controller import check_poison_pill
            if check_poison_pill(acc_norm) or check_poison_pill(raw_acc) or check_poison_pill(raw_lookup):
                return []
        except Exception:
            pass

        # 3. Strict 5-Way Amount Classification (Stage 8) + Auto 2nd Amount (مبلغ العقد)
        amt_dec, amt_state, amt_errs, _ = classify_and_parse_amount(
            raw_amount,
            account_norm=acc_norm if not acc_errs else None,
            service_norm=srv_norm if not srv_errs else None,
        )
        amt_halalas = int((amt_dec * Decimal(100)).to_integral_value()) if amt_dec is not None else None

        amt_halalas_2 = None
        eff_col_2 = amount_column_2
        if eff_col_2 and eff_col_2 > 0 and len(values) >= eff_col_2:
            raw_amount_2 = values[eff_col_2 - 1]
            amt_dec_2, _, amt_errs_2, _ = classify_and_parse_amount(
                raw_amount_2,
                account_norm=acc_norm if not acc_errs else None,
                service_norm=srv_norm if not srv_errs else None,
            )
            if amt_dec_2 is not None and not amt_errs_2:
                amt_halalas_2 = int((amt_dec_2 * Decimal(100)).to_integral_value())

        main_status = (
            _normalize_whitespace(values[eff_main_status_col - 1])
            if (eff_main_status_col and len(values) >= eff_main_status_col)
            else ""
        )
        sub_status = (
            _normalize_whitespace(values[eff_sub_status_col - 1])
            if (eff_sub_status_col and len(values) >= eff_sub_status_col)
            else ""
        )
        notes_text = (
            _normalize_whitespace(values[detected_notes_col - 1])
            if (detected_notes_col and len(values) >= detected_notes_col)
            else ""
        )
        if notes_text and ("تم السداد" in notes_text or "تم سداد" in notes_text) and "تم السداد" not in main_status:
            sub_status = f"{sub_status} ({notes_text[:40]})".strip() if sub_status else notes_text[:40]

        extracted.append(
            {
                "sheet_title": sheet.title,
                "row_number": row_number,
                "source_key": source_key,
                "record_type_hint": record_type,
                "customer_name": customer_name,
                "main_status": main_status,
                "sub_status": sub_status,
                "collector_in_scope": collector_in_scope,
                "status_in_scope": status_in_scope,
                "include_in_scope": include_in_scope,
                "raw_acc": raw_acc,
                "acc_norm": acc_norm if not acc_errs else None,
                "acc_errs": acc_errs,
                "raw_srv": raw_service if raw_service is not None else raw_lookup,
                "srv_norm": srv_norm if not srv_errs else None,
                "srv_errs": srv_errs,
                "raw_amount": raw_amount,
                "amt_halalas": amt_halalas,
                "amt_halalas_2": amt_halalas_2,
                "amt_state": amt_state,
                "amt_errs": amt_errs,
            }
        )
    return extracted


def _build_scope_safe_customers_and_errors(
    all_rows: list[dict[str, Any]],
) -> tuple[list[Customer], list[CheckError]]:
    """Applies Stage 11 (Pre-Filter Global Service Deduplication), Stage 13 (Account Grouping
    & Scope Completeness), and Stage 14 (1:1 Service Search & N:1 Deduplicated Account Search).
    """
    errors: list[CheckError] = []

    # 1. Stage 11: Global Service Duplicate & Conflict Audit BEFORE Scope Filtering
    services_map: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in all_rows:
        if r["srv_norm"] and not r["srv_errs"]:
            services_map[r["srv_norm"]].append(r)

    duplicate_or_conflict_services: set[str] = set()
    tainted_accounts: set[str] = set()

    for srv_num, copies in services_map.items():
        if len(copies) > 1:
            distinct_accs = sorted({cp["acc_norm"] or "بدون حساب" for cp in copies})
            # If Account Numbers are distinct (not duplicated), do NOT block or emit CROSS_ACCOUNT_SERVICE_CONFLICT
            if len(distinct_accs) > 1:
                continue
            duplicate_or_conflict_services.add(srv_num)
            for cp in copies:
                if cp["acc_norm"]:
                    tainted_accounts.add(cp["acc_norm"])
            if any(cp["include_in_scope"] for cp in copies):
                distinct_amts = {cp["amt_halalas"] for cp in copies}
                if len(distinct_amts) > 1:
                    err_title = "تعارض مالي لخدمة مكررة (CONFLICTING_SERVICE_AMOUNT)"
                else:
                    err_title = "تكرار رقم الخدمة (DUPLICATE_SERVICE_HOLD)"
                details = "; ".join(
                    f"{cp['sheet_title']}!R{cp['row_number']} (حساب: {cp['acc_norm'] or '-'}, مبلغ: "
                    f"{(cp['amt_halalas'] / 100.0) if cp['amt_halalas'] is not None else cp['raw_amount']})"
                    for cp in copies
                )
                errors.append(
                    CheckError(
                        record_type="wallet" if srv_num.startswith("2") else "account",
                        lookup_number=srv_num,
                        customer_name=copies[0]["customer_name"],
                        row_numbers=tuple(cp["row_number"] for cp in copies),
                        expected_amount=copies[0]["amt_halalas"],
                        error_type=err_title,
                        details=f"يُحظر جمع أو احتساب الخدمة المكررة تلقائياً ({srv_num}): {details}",
                        occurred_at=datetime.now(),
                        source_key=copies[0]["source_key"],
                        service_number=srv_num,
                        original_account_number=str(copies[0]["acc_norm"] or ""),
                    )
                )

    # 2. Row-Level Validation & Account Grouping (Stage 9 & Stage 13)
    account_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in all_rows:
        row_has_err = False
        err_msgs: list[str] = []

        hint_mode = str(r.get("record_type_hint") or "mixed").lower()

        if hint_mode == "account":
            # Account Only Mode: Requires Account Number. Service Number is not required.
            if r["acc_errs"]:
                row_has_err = True
                err_msgs.extend(r["acc_errs"])
            elif not r["acc_norm"]:
                row_has_err = True
                err_msgs.append("VAL-ACC-01: رقم الحساب مفقود (MISSING_ACCOUNT_NO)")
            starts_with_2 = False
            r["starts_with_2"] = False

        elif hint_mode == "wallet":
            # Service Only Mode: Requires Service Number. Account Number is not required.
            if r["srv_errs"]:
                row_has_err = True
                err_msgs.extend(r["srv_errs"])
            elif not r["srv_norm"]:
                row_has_err = True
                err_msgs.append("VAL-SRV-01: رقم الخدمة مفقود (MISSING_SERVICE_NO)")
            starts_with_2 = True
            r["starts_with_2"] = True

        else:
            # Smart Hybrid Mode:
            if r["srv_errs"]:
                row_has_err = True
                err_msgs.extend(r["srv_errs"])
            elif not r["srv_norm"]:
                row_has_err = True
                err_msgs.append("VAL-SRV-01: رقم الخدمة مفقود (MISSING_SERVICE_NO)")

            starts_with_2 = bool(r["srv_norm"] and r["srv_norm"].startswith("2"))
            r["starts_with_2"] = starts_with_2

            if r["acc_errs"]:
                row_has_err = True
                err_msgs.extend(r["acc_errs"])
            elif not r["acc_norm"] and not starts_with_2:
                row_has_err = True
                err_msgs.append("VAL-ACC-01: رقم الحساب مفقود لخدمة لا تبدأ بـ 2 (MISSING_ACCOUNT_NO)")

        if r["amt_errs"] or r["amt_halalas"] is None:
            row_has_err = True
            err_msgs.extend(r["amt_errs"] or ["VAL-AMT-01: المبلغ غير صالح أو فارغ"])
        elif str(r["raw_amount"] or "").strip() == ".00" or r["amt_halalas"] == 0:
            row_has_err = True
            err_msgs.append("INVALID_TEXT_IN_AMOUNT (.00): المبلغ المتبقي في الشيت يساوي صفر (0.00 ريال)")

        if r["srv_norm"] in duplicate_or_conflict_services:
            row_has_err = True
            err_msgs.append("VAL-SRV-03: خدمة مكررة أو متعارضة")

        r["row_has_err"] = row_has_err
        r["err_msgs"] = err_msgs

        if r["acc_norm"]:
            account_rows[r["acc_norm"]].append(r)

        # Record row-level validation errors for in-scope rows (excluding duplicate services already logged above)
        if r["include_in_scope"] and row_has_err and r["srv_norm"] not in duplicate_or_conflict_services:
            errors.append(
                CheckError(
                    record_type="wallet" if starts_with_2 else "account",
                    lookup_number=_display_identifier(r["acc_norm"] or r["srv_norm"] or r["raw_acc"] or r["raw_srv"]),
                    customer_name=r["customer_name"],
                    row_numbers=(r["row_number"],),
                    expected_amount=r["amt_halalas"],
                    error_type="بيانات غير صالحة (Validation Hold)",
                    details=" | ".join(err_msgs),
                    occurred_at=datetime.now(),
                    source_key=r["source_key"],
                    service_number=_display_identifier(r["srv_norm"] or r["raw_srv"]),
                    original_account_number=_display_identifier(r["acc_norm"] or r["raw_acc"]),
                )
            )

    # 3. Evaluate Account Groups & Scope Completeness (Stage 13)
    account_scope_meta: dict[str, dict[str, Any]] = {}
    for acc_num, grp_rows in account_rows.items():
        has_excluded = any(not x["include_in_scope"] for x in grp_rows)
        has_invalid = any(x["row_has_err"] for x in grp_rows) or (acc_num in tainted_accounts)
        is_complete = (not has_excluded) and (not has_invalid)

        if is_complete:
            scope_status = AccountFileScopeStatus.FILE_SCOPE_COMPLETE.value
        elif has_invalid:
            scope_status = AccountFileScopeStatus.FILE_SCOPE_INCOMPLETE_HAS_ERRORS.value
        else:
            scope_status = AccountFileScopeStatus.FILE_SCOPE_INCOMPLETE_PARTIAL_EXCLUSION.value

        included_valid = [x for x in grp_rows if x["include_in_scope"] and not x["row_has_err"]]
        inc_total_halalas = sum(x["amt_halalas"] or 0 for x in included_valid)
        inc_non2_halalas = sum(
            x["amt_halalas"] or 0 for x in included_valid if not x["starts_with_2"]
        )
        all_known_halalas = sum(
            x["amt_halalas"] or 0
            for x in grp_rows
            if x["amt_halalas"] is not None and x["srv_norm"] not in duplicate_or_conflict_services
        )

        account_scope_meta[acc_num] = {
            "is_complete": is_complete,
            "scope_status": scope_status,
            "included_total_halalas": inc_total_halalas,
            "included_non2_halalas": inc_non2_halalas,
            "all_known_halalas": all_known_halalas,
            "all_services": tuple(x["srv_norm"] for x in grp_rows if x["srv_norm"]),
            "row_count": len(grp_rows),
        }

    # 4. Stage 14: Build Service Search Tasks (1:1) & Deduplicated Account Search Tasks (N:1)
    customers: list[Customer] = []
    seen_account_tasks: dict[str, Customer] = {}
    account_task_index_in_list: dict[str, int] = {}

    srv_idx = 0
    acc_idx = 0

    for r in all_rows:
        if not r["include_in_scope"] or r["row_has_err"]:
            continue

        acc_num = r["acc_norm"] or ""
        srv_num = r["srv_norm"] or ""
        acc_meta = account_scope_meta.get(acc_num, {})
        is_multi_service_account = acc_meta.get("row_count", 1) > 1

        hint_mode = str(r.get("record_type_hint") or "mixed").lower()

        if hint_mode == "wallet":
            # 1:1 Service Search Task (Service-Level Scope on QuickPay)
            srv_idx += 1
            customers.append(
                Customer(
                    record_type="wallet",
                    lookup_number=srv_num,
                    expected_amount=r["amt_halalas"] or 0,
                    row_numbers=(r["row_number"],),
                    customer_name=r["customer_name"] or f"عميل {r['row_number']}",
                    main_status=r["main_status"],
                    sub_status=r["sub_status"],
                    source_key=r["source_key"],
                    expected_amount_2=r.get("amt_halalas_2"),
                    service_number=srv_num,
                    original_account_number=acc_num,
                    account_is_duplicate=is_multi_service_account,
                    expected_result_scope="SERVICE_LEVEL",
                    file_scope_status="FILE_SCOPE_COMPLETE",
                    is_file_scope_complete=True,
                    all_known_account_amount=acc_meta.get("all_known_halalas"),
                    included_non2_amount=None,
                    triggering_services=(srv_num,),
                    all_account_services=acc_meta.get("all_services", (srv_num,)),
                    task_id=f"SRV-{srv_idx:03d}",
                )
            )
        elif hint_mode == "account":
            # Direct Account Search Task (Account-Level Scope on Contract-Payment)
            if acc_num not in seen_account_tasks:
                acc_idx += 1
                is_comp = bool(acc_meta.get("is_complete", True))
                first_row_debt_halalas = int(r["amt_halalas"] or 0)
                cust = Customer(
                    record_type="account",
                    lookup_number=acc_num,
                    expected_amount=first_row_debt_halalas,
                    row_numbers=(r["row_number"],),
                    customer_name=r["customer_name"] or f"عميل {r['row_number']}",
                    main_status=r["main_status"],
                    sub_status=r["sub_status"],
                    source_key=r["source_key"],
                    expected_amount_2=r.get("amt_halalas_2"),
                    service_number=srv_num,
                    original_account_number=acc_num,
                    account_is_duplicate=is_multi_service_account,
                    expected_result_scope="ACCOUNT_LEVEL_TOTAL",
                    file_scope_status=acc_meta.get("scope_status", "FILE_SCOPE_COMPLETE"),
                    is_file_scope_complete=is_comp,
                    all_known_account_amount=acc_meta.get("all_known_halalas"),
                    included_non2_amount=first_row_debt_halalas,
                    triggering_services=(srv_num,) if srv_num else (acc_num,),
                    all_account_services=acc_meta.get("all_services", (srv_num,)) if srv_num else (acc_num,),
                    task_id=f"ACC-{acc_idx:03d}",
                )
                seen_account_tasks[acc_num] = cust
                account_task_index_in_list[acc_num] = len(customers)
                customers.append(cust)
        else:
            # Smart Hybrid Mode (Mixed):
            if r["starts_with_2"]:
                # Direct 1:1 Service Search Task (Service-Level Scope)
                srv_idx += 1
                customers.append(
                    Customer(
                        record_type="wallet",
                        lookup_number=srv_num,
                        expected_amount=r["amt_halalas"] or 0,
                        row_numbers=(r["row_number"],),
                        customer_name=r["customer_name"],
                        main_status=r["main_status"],
                        sub_status=r["sub_status"],
                        source_key=r["source_key"],
                        expected_amount_2=r.get("amt_halalas_2"),
                        service_number=srv_num,
                        original_account_number=acc_num,
                        account_is_duplicate=is_multi_service_account,
                        expected_result_scope="SERVICE_LEVEL",
                        file_scope_status="FILE_SCOPE_COMPLETE",
                        is_file_scope_complete=True,
                        all_known_account_amount=acc_meta.get("all_known_halalas"),
                        included_non2_amount=None,
                        triggering_services=(srv_num,),
                        all_account_services=acc_meta.get("all_services", (srv_num,)),
                        task_id=f"SRV-{srv_idx:03d}",
                    )
                )
            else:
                # Sequential Clean-Sheet Rule for Non-2 Services:
                if acc_num not in seen_account_tasks:
                    acc_idx += 1
                    is_comp = bool(acc_meta.get("is_complete", True))
                    first_row_debt_halalas = int(r["amt_halalas"] or 0)
                    cust = Customer(
                        record_type="account",
                        lookup_number=acc_num,
                        expected_amount=first_row_debt_halalas,
                        row_numbers=(r["row_number"],),
                        customer_name=r["customer_name"],
                        main_status=r["main_status"],
                        sub_status=r["sub_status"],
                        source_key=r["source_key"],
                        expected_amount_2=r.get("amt_halalas_2"),
                        service_number=srv_num,
                        original_account_number=acc_num,
                        account_is_duplicate=is_multi_service_account,
                        expected_result_scope="ACCOUNT_LEVEL_TOTAL",
                        file_scope_status=acc_meta.get("scope_status", "FILE_SCOPE_COMPLETE"),
                        is_file_scope_complete=is_comp,
                        all_known_account_amount=acc_meta.get("all_known_halalas"),
                        included_non2_amount=first_row_debt_halalas,
                        triggering_services=(srv_num,),
                        all_account_services=acc_meta.get("all_services", (srv_num,)),
                        task_id=f"ACC-{acc_idx:03d}",
                    )
                    seen_account_tasks[acc_num] = cust
                    account_task_index_in_list[acc_num] = len(customers)
                    customers.append(cust)
                else:
                    # Account Number already exists in memory and Service Number does NOT start with '2':
                    continue

    return customers, errors


def _append_collector_row_with_bottom_total(
    sheet: Worksheet,
    headers: list[str],
    row_values: list[Any],
    verify_url: str,
    is_net_sheet: bool,
) -> None:
    """Appends a customer row (8 columns) while dynamically stripping the old bottom الإجمالي row
    and re-inserting the updated الإجمالي row at the very bottom after the new row.
    """
    from openpyxl.styles import Border, Side
    from openpyxl.utils import get_column_letter

    header_fill = PatternFill(
        fill_type="solid",
        fgColor="1B5E20" if is_net_sheet else "0D47A1",
    )
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    regular_font = Font(name="Calibri", size=11, bold=False, color="111827")
    bold_font = Font(name="Calibri", size=11, bold=True, color="111827")
    link_font = Font(name="Calibri", size=11, bold=True, color="1565C0", underline="single")
    fill_even = PatternFill(fill_type="solid", fgColor="F8FAFC")
    fill_odd = PatternFill(fill_type="solid", fgColor="FFFFFF")
    fill_total = PatternFill(fill_type="solid", fgColor="FFF9C4")
    thin_side = Side(border_style="thin", color="CBD5E1")
    border = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)
    align_center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    align_right = Alignment(horizontal="right", vertical="center", wrap_text=True)

    sheet.sheet_view.rightToLeft = True
    sheet.freeze_panes = "A2"

    # Ensure strictly 8 headers in Row 1
    if sheet.max_column > len(headers):
        sheet.delete_cols(len(headers) + 1, sheet.max_column - len(headers))
    sheet.row_dimensions[1].height = 30
    col_widths = {1: 24, 2: 32, 3: 22, 4: 32, 5: 16, 6: 22, 7: 24, 8: 18}
    for col_idx, h_text in enumerate(headers, start=1):
        c = sheet.cell(1, col_idx, h_text)
        c.font = header_font
        c.fill = header_fill
        c.alignment = align_center
        c.border = border
        sheet.column_dimensions[get_column_letter(col_idx)].width = col_widths.get(col_idx, 20)

    # 1. Strip any existing bottom "الإجمالي..." row or blank trailing rows
    for r_idx in range(sheet.max_row, 1, -1):
        first_val = str(sheet.cell(r_idx, 1).value or "").strip()
        row_empty = all(sheet.cell(r_idx, c).value in (None, "") for c in range(1, len(headers) + 1))
        if first_val.startswith("الإجمالي") or row_empty:
            for c_idx in range(1, len(headers) + 1):
                cell = sheet.cell(r_idx, c_idx)
                cell.fill = PatternFill(fill_type=None)
                cell.font = regular_font
            sheet.delete_rows(r_idx, 1)

    # 2. Check if customer row already exists (by Row Number col 5 & Identifier col 2)
    target_row = None
    for r_idx in range(2, sheet.max_row + 1):
        if (
            str(sheet.cell(r_idx, 2).value or "").strip() == str(row_values[1] or "").strip()
            and str(sheet.cell(r_idx, 5).value or "").strip() == str(row_values[4] or "").strip()
        ):
            target_row = r_idx
            break

    if target_row is None:
        target_row = max(sheet.max_row + 1, 2)
        if target_row == 2 and sheet.max_row == 1 and sheet.cell(2, 1).value is not None:
            target_row = 2

    # 3. Write customer data (8 columns) into target_row with clean regular styling
    sheet.row_dimensions[target_row].height = 26
    row_bg = fill_even if target_row % 2 == 0 else fill_odd
    row_values_copy = list(row_values)
    row_values_copy[7] = f"=F{target_row}-G{target_row}"

    for col_idx, val in enumerate(row_values_copy, start=1):
        cell = sheet.cell(target_row, col_idx, val)
        cell.fill = row_bg
        cell.font = regular_font
        cell.border = border
        cell.alignment = align_right if col_idx == 4 else align_center
        if col_idx in (6, 7, 8):
            cell.number_format = "#,##0.00"

    link_cell = sheet.cell(target_row, 3)
    link_cell.hyperlink = verify_url
    link_cell.font = link_font

    # 4. Append the updated yellow الإجمالي row at the very bottom (8 columns)
    last_data_row = sheet.max_row
    customer_count = max(last_data_row - 1, 0)
    total_row = last_data_row + 1
    sheet.row_dimensions[total_row].height = 28

    sum_sheet_amt = 0.0
    sum_live_amt = 0.0
    sum_diff_amt = 0.0
    for r in range(2, last_data_row + 1):
        v_f = sheet.cell(r, 6).value
        v_g = sheet.cell(r, 7).value
        v_h = sheet.cell(r, 8).value
        try:
            if v_f is not None and not str(v_f).startswith("="):
                sum_sheet_amt += float(v_f)
        except (ValueError, TypeError):
            pass
        try:
            if v_g is not None and not str(v_g).startswith("="):
                sum_live_amt += float(v_g)
        except (ValueError, TypeError):
            pass
        try:
            if v_h is not None and not str(v_h).startswith("="):
                sum_diff_amt += float(v_h)
            elif v_f is not None and v_g is not None and not str(v_f).startswith("=") and not str(v_g).startswith("="):
                sum_diff_amt += (float(v_f) - float(v_g))
        except (ValueError, TypeError):
            pass

    total_values = [
        "الإجمالي الصافي للمحصلين" if is_net_sheet else "الإجمالي الكلي",
        f"{customer_count} عملاء" if is_net_sheet else f"{customer_count} سجل",
        "",
        "",
        "",
        round(sum_sheet_amt, 2),
        round(sum_live_amt, 2),
        round(sum_diff_amt, 2),
        "",
    ]
    for col_idx, val in enumerate(total_values, start=1):
        tcell = sheet.cell(total_row, col_idx, val)
        tcell.fill = fill_total
        tcell.font = bold_font
        tcell.border = border
        tcell.alignment = align_center
        if col_idx in (6, 7, 8):
            tcell.number_format = "#,##0.00"


def _ensure_strictly_three_collector_sheets(workbook: Workbook) -> None:
    """Ensures the workbook contains ONLY the 3 approved Collector sheets, removes any extra tabs,
    and ensures all 3 sheets have their exact header styling, RTL direction, and column widths.
    """
    from openpyxl.styles import Border, Side

    required = [
        "الفروقات الصافية للمحصلين",
        "جميع الفروقات (شامل المسدد)",
        "الأخطاء والملاحظات للمحصل",
    ]
    for title in required:
        if title not in workbook.sheetnames:
            workbook.create_sheet(title=title)
    for extra in list(workbook.sheetnames):
        if extra not in required and len(workbook.sheetnames) > 1:
            workbook.remove(workbook[extra])
    workbook._sheets = [workbook[title] for title in required]

    thin_side = Side(border_style="thin", color="CBD5E1")
    border = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    align_center = Alignment(horizontal="center", vertical="center", wrap_text=True)

    collector_headers = [
        "طريقة البحث في زين",
        "رقم الحساب / الخدمة",
        "رابط التأكد المباشر",
        "اسم العميل",
        "رقم الصف في الشيت",
        "المبلغ المسجل بالشيت",
        "المبلغ الحالي في موقع زين",
        "الفرق (ريال)",
        "الزمن",
    ]
    col_widths_12 = {1: 24, 2: 32, 3: 22, 4: 32, 5: 16, 6: 22, 7: 24, 8: 18, 9: 20}
    for s_name, h_color in [
        ("الفروقات الصافية للمحصلين", "1B5E20"),
        ("جميع الفروقات (شامل المسدد)", "0D47A1"),
    ]:
        ws = workbook[s_name]
        ws.sheet_view.rightToLeft = True
        ws.freeze_panes = "A2"
        ws.row_dimensions[1].height = 30
        h_fill = PatternFill("solid", fgColor=h_color)
        for idx, text in enumerate(collector_headers, start=1):
            c = ws.cell(1, idx, text)
            c.font = header_font
            c.fill = h_fill
            c.alignment = align_center
            c.border = border
            ws.column_dimensions[get_column_letter(idx)].width = col_widths_12.get(idx, 20)

    notes_headers = [
        "رقم الصف في الشيت",
        "اسم العميل",
        "رقم الحساب ورقم الخدمة",
        "المبلغ في الشيت",
        "المشكلة باختصار",
        "رابط صفحة زين",
    ]
    col_widths_3 = {1: 18, 2: 32, 3: 38, 4: 18, 5: 44, 6: 30}
    ws3 = workbook["الأخطاء والملاحظات للمحصل"]
    ws3.sheet_view.rightToLeft = True
    ws3.freeze_panes = "A2"
    ws3.row_dimensions[1].height = 30
    if ws3.max_column > len(notes_headers):
        ws3.delete_cols(len(notes_headers) + 1, ws3.max_column - len(notes_headers))
    h_fill_3 = PatternFill("solid", fgColor="B71C1C")
    for idx, text in enumerate(notes_headers, start=1):
        c = ws3.cell(1, idx, text)
        c.font = header_font
        c.fill = h_fill_3
        c.alignment = align_center
        c.border = border
        ws3.column_dimensions[get_column_letter(idx)].width = col_widths_3.get(idx, 24)


def append_mismatch(
    result_workbook_path: Path,
    mismatch: Mismatch,
) -> None:
    """Writes a financial variance to the 3-sheet collector workbook with
    dynamic bottom totals on every single addition.
    """
    workbook = _open_result_workbook(result_workbook_path, "الفروقات الصافية للمحصلين")
    try:
        _ensure_strictly_three_collector_sheets(workbook)
        customer = mismatch.customer

        # Pick the closest amount to Zain when two amounts (M1 & M2) are provided
        effective_expected = customer.expected_amount
        if getattr(customer, "expected_amount_2", None) is not None:
            diff1 = abs(mismatch.website_amount - customer.expected_amount)
            diff2 = abs(mismatch.website_amount - customer.expected_amount_2)
            if diff2 < diff1:
                effective_expected = customer.expected_amount_2

        file_num = halalas_to_number(effective_expected)
        zain_num = halalas_to_number(mismatch.website_amount)

        is_srv_search = str(customer.lookup_number).startswith("2") or customer.record_type == "wallet"
        search_method = "برقم الخدمة (السداد السريع)" if is_srv_search else "برقم الحساب"
        acc_orig = getattr(customer, "original_account_number", "") or ""
        srv_orig = getattr(customer, "service_number", "") or ""
        if acc_orig and srv_orig and acc_orig != srv_orig:
            display_num = f"حساب: {acc_orig} | خدمة: {srv_orig}"
        else:
            display_num = str(customer.lookup_number)

        st_main = str(customer.main_status or "").strip()
        st_sub = str(customer.sub_status or "").strip()
        is_paid_in_sheet = (
            ("تم السداد" in st_main)
            or ("سداد" in st_sub and "وعد" not in st_sub)
            or ("تم السداد" in st_sub)
            or ("تم سداد" in st_sub)
        )

        collector_headers = [
            "طريقة البحث في زين",
            "رقم الحساب / الخدمة",
            "رابط التأكد المباشر",
            "اسم العميل",
            "رقم الصف في الشيت",
            "المبلغ المسجل بالشيت",
            "المبلغ الحالي في موقع زين",
            "الفرق (ريال)",
            "الزمن",
        ]
        time_str = getattr(mismatch, "detected_at", None)
        if time_str and hasattr(time_str, "strftime"):
            time_val = time_str.strftime("%Y-%m-%d %H:%M:%S")
        else:
            time_val = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        collector_row = [
            search_method,
            display_num,
            "اضغط لفتح صفحة زين",
            customer.customer_name,
            customer.row_numbers_text,
            file_num,
            zain_num,
            0.0,
            time_val,
        ]

        # 1. Update Sheet 1: الفروقات الصافية للمحصلين (only if NOT marked paid in sheet)
        sheet_net = _get_or_create_sheet(workbook, "الفروقات الصافية للمحصلين")
        if not is_paid_in_sheet:
            _append_collector_row_with_bottom_total(
                sheet_net,
                collector_headers,
                collector_row,
                customer.verification_url,
                is_net_sheet=True,
            )

        # 2. Update Sheet 2: جميع الفروقات (شامل المسدد)
        sheet_all = _get_or_create_sheet(workbook, "جميع الفروقات (شامل المسدد)")
        _append_collector_row_with_bottom_total(
            sheet_all,
            collector_headers,
            collector_row,
            customer.verification_url,
            is_net_sheet=False,
        )

        workbook.save(result_workbook_path)
    finally:
        workbook.close()
    _mirror_result_workbook(result_workbook_path)


def _mirror_result_workbook(result_workbook_path: Path) -> None:
    """Saves result workbook ONLY inside the program directory (PROJECT_DIRECTORY)."""
    import shutil
    from .config import PROJECT_DIRECTORY

    targets = [
        PROJECT_DIRECTORY / "نتائج فحص زين.xlsx",
        PROJECT_DIRECTORY / "نتائج فحص زين - نسخة المحصلين.xlsx",
    ]
    for tgt in targets:
        try:
            if tgt.resolve() != result_workbook_path.resolve() and tgt.parent.exists():
                shutil.copy2(result_workbook_path, tgt)
        except Exception:
            pass


def append_error(result_workbook_path: Path, error: CheckError) -> None:
    from openpyxl.styles import Border, Side

    # Skip rare cross-account duplicate service conflict as agreed
    if "CROSS_ACCOUNT_SERVICE_CONFLICT" in str(error.error_type):
        return

    workbook = _open_result_workbook(result_workbook_path, "الأخطاء والملاحظات للمحصل")
    try:
        _ensure_strictly_three_collector_sheets(workbook)
        notes_sheet = _get_or_create_sheet(workbook, "الأخطاء والملاحظات للمحصل")

        lookup_str = str(error.lookup_number or "").strip()
        srv_str = str(getattr(error, "service_number", "") or "").strip()
        acc_str = str(getattr(error, "original_account_number", "") or "").strip()
        is_srv_2 = lookup_str.startswith("2") or srv_str.startswith("2") or error.record_type == "wallet"
        srv_target = srv_str if srv_str.startswith("2") else lookup_str
        acc_target = acc_str if acc_str else lookup_str

        if is_srv_2 and srv_target.startswith("2"):
            problem_url = f"{ZAIN_QUICKPAY_URL}?account={srv_target}"
            link_label = f"اضغط لفحص رقم الخدمة ({srv_target})"
            if acc_target and acc_target != srv_target:
                display_id = f"رقم الخدمة: {srv_target} | حساب: {acc_target}"
            else:
                display_id = f"رقم الخدمة: {srv_target}"
        else:
            problem_url = f"{ZAIN_CONTRACT_PAYMENT_URL}?contract={acc_target}&language=ar"
            link_label = "اضغط لفتح حساب العميل بزين"
            if acc_target and srv_str and acc_target != srv_str:
                display_id = f"حساب: {acc_target} | خدمة: {srv_str}"
            else:
                display_id = f"حساب: {acc_target}"

        if "INVALID_TEXT_IN_AMOUNT (.00)" in str(error.details):
            amt_num = 0.0
            short_issue = "المبلغ المتبقي في الشيت يساوي صفر (0.00 ريال)"
        elif "NEGATIVE_AMOUNT_HOLD" in str(error.details):
            amt_num = float(halalas_to_number(error.expected_amount)) if error.expected_amount is not None else -3.50
            short_issue = f"رصيد بالسالب في الشيت ({amt_num:,.2f} ريال لصالح العميل)"
        elif "إعادة توجيه (طبيعي)" in str(error.error_type):
            amt_num = float(halalas_to_number(error.expected_amount)) if error.expected_amount is not None else 0.0
            short_issue = "رقم خدمة مسدد أو غير متاح في السداد السريع (تم التحويل للرئيسية)"
        elif "إعادة توجيه" in str(error.error_type) or "Redirected" in str(error.details):
            amt_num = float(halalas_to_number(error.expected_amount)) if error.expected_amount is not None else 0.0
            short_issue = "إعادة توجيه غير متوقعة في موقع زين"
        elif "مهلة" in str(error.error_type) or "timeout" in str(error.error_type).lower():
            amt_num = float(halalas_to_number(error.expected_amount)) if error.expected_amount is not None else 0.0
            short_issue = "انتهاء مهلة الصفحة / بطء استجابة خادم زين"
        elif "رفض" in str(error.error_type) or "rejected" in str(error.error_type).lower():
            amt_num = float(halalas_to_number(error.expected_amount)) if error.expected_amount is not None else 0.0
            short_issue = "حظر مؤقت أو اختبار بشري من جدار حماية زين (WAF Challenge)"
        else:
            amt_num = float(halalas_to_number(error.expected_amount)) if error.expected_amount is not None else 0.0
            short_issue = str(error.error_type)

        already_in_notes = False
        for existing_row in notes_sheet.iter_rows(min_row=2, values_only=True):
            if str(existing_row[0] or "") == str(error.row_numbers_text) and lookup_str in str(existing_row[2] or ""):
                already_in_notes = True
                break

        if not already_in_notes:
            n_row = max(notes_sheet.max_row + 1, 2)
            if n_row == 2 and notes_sheet.max_row == 1 and notes_sheet.cell(2, 1).value is None:
                n_row = 2
            notes_sheet.row_dimensions[n_row].height = 26

            n_vals = [
                error.row_numbers_text,
                error.customer_name,
                display_id,
                amt_num,
                short_issue,
                link_label,
            ]
            thin_side = Side(border_style="thin", color="CBD5E1")
            border = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)
            regular_font = Font(name="Calibri", size=11, bold=False, color="111827")
            link_font = Font(name="Calibri", size=11, bold=True, color="1565C0", underline="single")
            align_center = Alignment(horizontal="center", vertical="center", wrap_text=True)
            align_right = Alignment(horizontal="right", vertical="center", wrap_text=True)
            row_fill = PatternFill("solid", fgColor="F8FAFC" if n_row % 2 == 0 else "FFFFFF")

            for c_i, v in enumerate(n_vals, start=1):
                c = notes_sheet.cell(n_row, c_i, v)
                c.fill = row_fill
                c.font = regular_font
                c.border = border
                c.alignment = align_right if c_i in (2, 3, 5) else align_center
                if c_i == 4:
                    c.number_format = "#,##0.00"

            link_cell = notes_sheet.cell(n_row, 6)
            link_cell.hyperlink = problem_url
            link_cell.font = link_font

        # Strip old total row in Sheet 3 if present
        for r_idx in range(notes_sheet.max_row, 1, -1):
            f_val = str(notes_sheet.cell(r_idx, 1).value or "").strip()
            if f_val.startswith("الإجمالي"):
                notes_sheet.delete_rows(r_idx, 1)

        err_data_rows = max(notes_sheet.max_row - 1, 0)
        if err_data_rows > 0:
            sum_err_amt = 0.0
            for r in range(2, notes_sheet.max_row + 1):
                try:
                    v_amt = notes_sheet.cell(r, 4).value
                    if v_amt is not None and not str(v_amt).startswith("="):
                        sum_err_amt += float(v_amt)
                except (ValueError, TypeError):
                    pass
            tot_r = notes_sheet.max_row + 1
            notes_sheet.row_dimensions[tot_r].height = 28
            fill_total = PatternFill(fill_type="solid", fgColor="FFF9C4")
            bold_font = Font(name="Calibri", size=11, bold=True, color="111827")
            tot_vals = [
                "الإجمالي",
                f"{err_data_rows} حالة وملاحظة",
                "",
                round(sum_err_amt, 2),
                "إجمالي مبالغ الحالات المسجلة",
                "",
            ]
            for c_i, v in enumerate(tot_vals, start=1):
                tc = notes_sheet.cell(tot_r, c_i, v)
                tc.fill = fill_total
                tc.font = bold_font
                tc.border = border
                tc.alignment = align_center
                if c_i == 4:
                    tc.number_format = "#,##0.00"

        workbook.save(result_workbook_path)
    finally:
        workbook.close()
    _mirror_result_workbook(result_workbook_path)


def load_unresolved_redirect_keys(
    result_workbook_path: Path,
) -> set[tuple[str, str]]:
    if not result_workbook_path.exists():
        return set()
    workbook = load_workbook(result_workbook_path, read_only=True, data_only=True)
    try:
        if ERROR_SHEET_NAME not in workbook.sheetnames:
            return set()
        sheet = workbook[ERROR_SHEET_NAME]
        keys: set[tuple[str, str]] = set()
        for row in sheet.iter_rows(min_row=2, values_only=True):
            if len(row) < 7 or not str(row[6] or "").startswith("إعادة توجيه"):
                continue
            record_type = "account" if "حساب" in str(row[0] or "") else "wallet"
            lookup_number = str(row[1] or "").strip()
            if lookup_number.isdigit():
                keys.add((record_type, lookup_number))
        return keys
    finally:
        workbook.close()


def resolve_redirect_error(
    result_workbook_path: Path,
    customer: Customer,
) -> bool:
    if not result_workbook_path.exists():
        return False
    workbook = load_workbook(result_workbook_path)
    changed = False
    try:
        if ERROR_SHEET_NAME not in workbook.sheetnames:
            return False
        sheet = workbook[ERROR_SHEET_NAME]
        resolved_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for row_number in range(2, sheet.max_row + 1):
            if (
                str(sheet.cell(row_number, 2).value or "") == customer.lookup_number
                and str(sheet.cell(row_number, 7).value or "").startswith("إعادة توجيه")
            ):
                old_details = str(sheet.cell(row_number, 8).value or "").strip()
                sheet.cell(row_number, 7).value = "تم الحل بعد إعادة الفحص"
                sheet.cell(row_number, 8).value = (
                    f"{old_details} | تمت إعادة الفحص بنجاح في {resolved_at}."
                )
                changed = True
        if changed:
            workbook.save(result_workbook_path)
        return changed
    finally:
        workbook.close()


def _validate_headers(sheet, record_type: str, lookup_column: int) -> None:
    expected = {
        CUSTOMER_NAME_COLUMN_NUMBER: "اسم العميل",
        lookup_column: "رقم الحساب" if record_type == "account" else "رقم الخدمة",
        COLLECTOR_COLUMN_NUMBER: "المحصل",
        MAIN_STATUS_COLUMN_NUMBER: "الحالة الرئيسية",
        SUB_STATUS_COLUMN_NUMBER: "الحالة الفرعية",
    }
    for column_number, header in expected.items():
        actual = _normalize_header(sheet.cell(HEADER_ROW_NUMBER, column_number).value)
        if actual != _normalize_header(header):
            column = get_column_letter(column_number)
            raise RuntimeError(
                f'Worksheet "{sheet.title}" column {column} must have the header {header}.'
            )

    amount_header = _normalize_header(
        sheet.cell(HEADER_ROW_NUMBER, EXPECTED_AMOUNT_COLUMN_NUMBER).value
    )
    valid_amount_headers = {
        "الاجمالي",
        "مبلغ الميدونية",
        "مبلغ المديونية",
        "مبلغ المديونيه",
        "مبلغ المديونيه في ملف المحصل",
        "مبلغ المديونية في ملف المحصل",
    }
    if amount_header not in valid_amount_headers:
        raise RuntimeError(
            f'Worksheet "{sheet.title}" column O has an unexpected amount header.'
        )


def _validate_custom_headers(
    sheet,
    lookup_column: int,
    amount_column: int,
    customer_column: int | None = None,
    service_column: int | None = None,
    record_type: str = "account",
    no_customer: bool = False,
    amount_column_2: int | None = None,
) -> None:
    smart = detect_smart_row1_columns(sheet)["indices"]
    if not customer_column and smart.get("customer_col"):
        customer_column = smart["customer_col"]
    if not lookup_column and smart.get("lookup_col"):
        lookup_column = smart["lookup_col"]
    if not amount_column and smart.get("amount_col"):
        amount_column = smart["amount_col"]
    if not service_column and smart.get("service_col"):
        service_column = smart["service_col"]

    # 1. Customer column is 100% OPTIONAL - never block or raise error if missing
    # 2. Validate based on selected mode:
    if record_type == "wallet":
        target_col = lookup_column or service_column
        if not target_col or target_col <= 0:
            raise RuntimeError(f'ورقة "{sheet.title}": مطلوب تحديد عمود رقم الخدمة لمسار فحص الخدمة.')
    elif record_type == "account":
        if not lookup_column or lookup_column <= 0:
            raise RuntimeError(f'ورقة "{sheet.title}": مطلوب تحديد عمود رقم الحساب / العقد لمسار فحص الحساب.')
    elif record_type == "mixed":
        if not lookup_column or lookup_column <= 0:
            raise RuntimeError(f'ورقة "{sheet.title}": مطلوب تحديد عمود رقم الحساب للطريقة الذكية.')
        if not service_column or service_column <= 0:
            raise RuntimeError(f'ورقة "{sheet.title}": مطلوب تحديد عمود رقم الخدمة للطريقة الذكية.')

    if not amount_column or amount_column <= 0:
        raise RuntimeError(f'ورقة "{sheet.title}": مطلوب تحديد عمود المبلغ (باقي السداد الموثق أو مبلغ العقد).')


def _open_result_workbook(path: Path, first_sheet_name: str):
    if path.exists():
        return load_workbook(path)
    workbook = Workbook()
    workbook.active.title = first_sheet_name
    return workbook


def _get_or_create_sheet(workbook, sheet_name: str):
    if sheet_name in workbook.sheetnames:
        return workbook[sheet_name]
    if sheet_name == MISMATCH_SHEET_NAME:
        return workbook.create_sheet(sheet_name, 0)
    return workbook.create_sheet(sheet_name)


def _prepare_sheet(sheet, headers: list[str]) -> None:
    sheet.sheet_view.rightToLeft = True
    sheet.freeze_panes = "A2"
    fill = PatternFill("solid", fgColor="1F4E78")
    for column_number, header in enumerate(headers, start=1):
        cell = sheet.cell(1, column_number)
        cell.value = header
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = fill
        cell.alignment = Alignment(horizontal="center", vertical="center")


def _finish_sheet(sheet, column_count: int) -> None:
    widths = {
        1: 24,
        2: 18,
        3: 19,
        4: 28,
        5: 18,
        6: 20,
        7: 28,
        8: 22,
        9: 22,
        10: 18,
        11: 42,
    }
    for column_number in range(1, column_count + 1):
        sheet.column_dimensions[get_column_letter(column_number)].width = widths.get(
            column_number, 18
        )
    sheet.auto_filter.ref = f"A1:{get_column_letter(column_count)}{sheet.max_row}"
    for row in sheet.iter_rows(min_row=2, max_row=sheet.max_row, max_col=column_count):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)


def _set_identifier_cell(cell, value: str) -> None:
    cell.value = value
    cell.number_format = "@"


def _set_link_cell(cell, url: str) -> None:
    if not cell.value:
        cell.value = "فتح صفحة التحقق"
    cell.hyperlink = url
    cell.style = "Hyperlink"


def _normalize_identifier(value: object) -> str | None:
    norm, errs, _ = sanitize_identifier_cell(value)
    return norm if (norm and not errs) else None


def _display_identifier(value: object) -> str:
    normalized = _normalize_identifier(value)
    if normalized:
        return normalized
    text = _normalize_whitespace(value)
    return text or "غير متوفر"


def _normalize_header(value: object) -> str:
    if value is None:
        return ""
    return (
        str(value)
        .strip()
        .replace("_", " ")
        .replace("-", " ")
        .replace("أ", "ا")
        .replace("إ", "ا")
        .replace("آ", "ا")
        .replace("ة", "ه")
        .replace("ى", "ي")
    )


def _normalize_whitespace(value: object) -> str:
    return " ".join(str(value or "").split())
