"""Domain Workbook Engine for Smart Zain Checker.
Dedicated to Excel file parsing, header schema analysis, and row extraction.
Pure domain logic: completely decoupled from UI and browser processes.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from domain.models import Customer
from domain.money import parse_money_to_halalas

HEADER_ROW_NUMBER = 1

AMOUNT_EXCLUSIONS = (
    "نوع", "رقم", "عمر", "تاريخ", "كود", "ايام", "أيام", "هاتف", "جوال",
    "مفضل", "حاوية", "جهة", "فرع", "مشرف", "مستخدم", "سسس", "يي", "تصنيف",
    "ملاحظات", "متابعة"
)


def normalize_header_text(val: Any) -> str:
    """Normalizes Arabic/English header text for fuzzy synonym matching."""
    if val is None:
        return ""
    text = str(val).strip().lower()
    text = re.sub(r"[أإآ]", "ا", text)
    text = re.sub(r"ة", "ه", text)
    text = re.sub(r"ى", "ي", text)
    text = re.sub(r"[_\-–—/\\.]+", " ", text)
    return " ".join(text.split())


def parse_excel_column(col_str: str) -> Optional[int]:
    """Converts column letter ('A', 'AW') or 1-based index number to integer index."""
    if not col_str:
        return None
    s = str(col_str).strip()
    if s.isdigit():
        val = int(s)
        return val if val > 0 else None
    
    # Column letters (e.g. 'L', 'AW')
    res = 0
    for char in s.upper():
        if "A" <= char <= "Z":
            res = res * 26 + (ord(char) - ord("A") + 1)
        else:
            return None
    return res if res > 0 else None


def get_sheet_header_columns(sheet) -> list[dict[str, Any]]:
    """Returns a list of all header columns in Row 1 with their index and letters."""
    headers = []
    first_row = next(
        sheet.iter_rows(min_row=HEADER_ROW_NUMBER, max_row=HEADER_ROW_NUMBER, values_only=True),
        (),
    )
    for idx, val in enumerate(first_row, start=1):
        if val is not None and str(val).strip() != "":
            headers.append({
                "col_index": idx,
                "col_letter": get_column_letter(idx),
                "header": str(val).strip(),
            })
    return headers


def detect_smart_sheet_columns(sheet) -> dict[str, Any]:
    """Analyzes Row 1 of an Excel sheet and discovers all critical columns:
    - رقم الحساب (lookup_col)
    - رقم الخدمة (service_col)
    - مبلغ العقد (contract_col) -> AW in 2.xlsx
    - متبقي سداد موثق (remaining_col) -> P in 2.xlsx
    - اسم العميل (customer_col)
    - المحصل (collector_col)
    """
    header_row = next(
        sheet.iter_rows(min_row=HEADER_ROW_NUMBER, max_row=HEADER_ROW_NUMBER, values_only=True),
        (),
    )
    sample_row = next(
        sheet.iter_rows(min_row=HEADER_ROW_NUMBER + 1, max_row=HEADER_ROW_NUMBER + 1, values_only=True),
        (),
    )

    detected: dict[str, Optional[int]] = {
        "customer_col": None,
        "lookup_col": None,
        "contract_col": None,
        "remaining_col": None,
        "debt_col": None,
        "amount_col": None,
        "amount_col_2": None,
        "service_col": None,
        "collector_col": None,
        "case_status_col": None,
        "main_status_col": None,
        "sub_status_col": None,
        "notes_col": None,
    }

    contract_col: Optional[int] = None
    remaining_col: Optional[int] = None
    debt_col: Optional[int] = None

    def _matches_any(text: str, synonyms: tuple[str, ...]) -> bool:
        return any(normalize_header_text(s) in text for s in synonyms)

    def _has_negative_keywords(text: str, neg_words: tuple[str, ...]) -> bool:
        return any(w in text for w in neg_words)

    for idx, raw_val in enumerate(header_row, start=1):
        norm = normalize_header_text(raw_val)
        if not norm:
            continue

        # 1. اسم العميل
        if (
            not detected["customer_col"]
            and not _has_negative_keywords(norm, ("ارقام", "تصنيف", "متابعة", "تاريخ", "رقم"))
            and _matches_any(norm, ("اسم العميل", "العميل", "الاسم", "اسم المشترك", "المشترك", "customer", "name"))
        ):
            detected["customer_col"] = idx

        # 2. رقم الحساب / العقد
        elif (
            not detected["lookup_col"]
            and not _has_negative_keywords(norm, ("تاريخ", "سدادات", "متبقي", "مبلغ", "قيمة", "نوع"))
            and _matches_any(norm, ("رقم الحساب", "رقم العقد", "الحساب", "العقد", "رقم الفاتورة", "account", "contract"))
        ):
            detected["lookup_col"] = idx

        # 3. مبلغ العقد (المطابق لموقع زين عند البحث بالحساب)
        elif (
            not contract_col
            and not _has_negative_keywords(norm, AMOUNT_EXCLUSIONS)
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
            and _matches_any(norm, (
                "متبقي سداد موثق", "باقي السداد الموثق", "باقي السداد", "متبقي السداد",
                "المبلغ المتبقي", "الرصيد المتبقي", "المديونية المتبقية", "صافي المديونية", "remaining"
            ))
        ):
            remaining_col = idx

        # 5. مبلغ المديونية الإجمالية
        elif (
            not debt_col
            and not _has_negative_keywords(norm, AMOUNT_EXCLUSIONS)
            and _matches_any(norm, (
                "مبلغ المديونية", "مبلغ الميدونية", "مبلغ المطالبة", "المبلغ الإجمالي", "الإجمالي", "total amount"
            ))
        ):
            debt_col = idx

        # 6. رقم الخدمة
        elif (
            not detected["service_col"]
            and not _has_negative_keywords(norm, ("تاريخ", "نوع", "مبلغ"))
            and _matches_any(norm, (
                "رقم الخدمة", "رقم الخدمه", "الخدمة", "الخدمه", "رقم المحفظة", "رقم المحفظه",
                "المحفظة", "رقم الجوال", "الجوال", "رقم الهاتف", "الهاتف", "service", "phone"
            ))
        ):
            detected["service_col"] = idx

        # 7. المحصل
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
        elif not detected["case_status_col"] and norm in ("الحالة", "الحاله", "حالة", "حاله", "status", "case status"):
            detected["case_status_col"] = idx
        elif not detected["notes_col"] and _matches_any(norm, ("المتابعة", "المتابعه", "أخر متابعة", "اخر متابعة", "ملاحظات", "notes")):
            detected["notes_col"] = idx

    detected["contract_col"] = contract_col
    detected["remaining_col"] = remaining_col
    detected["debt_col"] = debt_col

    # Primary & Secondary amounts
    detected["amount_col_2"] = contract_col or debt_col
    detected["amount_col"] = remaining_col or debt_col or contract_col

    if not detected["amount_col"] and detected["amount_col_2"]:
        detected["amount_col"] = detected["amount_col_2"]
    elif not detected["amount_col_2"] and detected["amount_col"]:
        detected["amount_col_2"] = detected["amount_col"]

    letters = {
        k: (get_column_letter(v) if v else None)
        for k, v in detected.items()
    }
    return {"indices": detected, "letters": letters}


def inspect_sheet_schema(sheet) -> dict[str, Any]:
    """Provides complete schema analysis, detected columns, and mode compatibility."""
    header_cols = get_sheet_header_columns(sheet)
    smart_info = detect_smart_sheet_columns(sheet)
    indices = smart_info["indices"]
    letters = smart_info["letters"]

    has_acc = bool(indices.get("lookup_col"))
    has_srv = bool(indices.get("service_col"))
    has_amt1 = bool(indices.get("amount_col"))
    has_amt2 = bool(indices.get("amount_col_2"))
    contract_c = indices.get("contract_col") or indices.get("amount_col_2")
    remaining_c = indices.get("remaining_col") or indices.get("amount_col")

    supported = {
        "smart_hybrid": bool(has_acc and has_srv and (has_amt1 or has_amt2)),
        "account_only": bool(has_acc and (has_amt1 or has_amt2)),
        "service_only": bool(has_srv and (has_amt1 or has_amt2)),
    }

    if supported["smart_hybrid"]:
        rec_mode = "smart_hybrid"
    elif supported["account_only"]:
        rec_mode = "account_only"
    elif supported["service_only"]:
        rec_mode = "service_only"
    else:
        rec_mode = "custom"

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

    is_acceptable = bool((has_acc or has_srv) and (has_amt1 or has_amt2))
    ver_status = "✓ الشيت صالح ومقبول للتدقيق" if is_acceptable else "⚠️ الشيت غير مكتمل (ينقص عمود الحساب أو المبلغ)"
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
        "amount_options": amt_options,
        "has_customer_col": bool(indices.get("customer_col")),
        "has_collector_col": bool(indices.get("collector_col")),
        "contract_amount_col": contract_c,
        "contract_amount_letter": get_column_letter(contract_c) if contract_c else None,
        "remaining_amount_col": remaining_c,
        "remaining_amount_letter": get_column_letter(remaining_c) if remaining_c else None,
    }


def extract_customer_records(
    sheet,
    lookup_col: int,
    amount_col: int,
    amount_col_2: Optional[int] = None,
    service_col: Optional[int] = None,
    customer_col: Optional[int] = None,
    collector_col: Optional[int] = None,
    case_status_col: Optional[int] = None,
    main_status_col: Optional[int] = None,
    sub_status_col: Optional[int] = None,
    notes_col: Optional[int] = None,
    target_collector: str = "",
    record_type: str = "mixed",
) -> list[Customer]:
    """Iterates through rows starting from Row 2 and constructs validated Customer objects."""
    cols_to_read = []
    for c in (lookup_col, amount_col, amount_col_2, service_col, customer_col, collector_col, case_status_col, main_status_col, sub_status_col, notes_col):
        if c:
            try:
                cols_to_read.append(int(c))
            except (ValueError, TypeError):
                pass
    max_c = max(cols_to_read) if cols_to_read else 30
    max_c = max(max_c, 30)


    rows_iter = sheet.iter_rows(
        min_row=HEADER_ROW_NUMBER + 1,
        min_col=1,
        max_col=max_c,
        values_only=True,
    )

    norm_target_collector = normalize_header_text(target_collector) if target_collector else ""
    customers: list[Customer] = []

    for row_num, values in enumerate(rows_iter, start=HEADER_ROW_NUMBER + 1):
        if not any(v is not None and str(v).strip() != "" for v in values):
            continue

        # Optional collector filter
        raw_coll = str(values[collector_col - 1] or "").strip() if (collector_col and len(values) >= collector_col) else ""
        if norm_target_collector and normalize_header_text(raw_coll) != norm_target_collector:
            continue

        raw_lookup = str(values[lookup_col - 1] or "").strip() if len(values) >= lookup_col else ""
        if not raw_lookup:
            continue

        # Sanitize lookup number
        clean_lookup = re.sub(r"\D", "", raw_lookup)
        if not clean_lookup:
            continue

        raw_serv = str(values[service_col - 1] or "").strip() if (service_col and len(values) >= service_col) else ""
        clean_serv = re.sub(r"\D", "", raw_serv) if raw_serv else ""

        # Parse Amounts into integer Halalas
        raw_amt1 = values[amount_col - 1] if len(values) >= amount_col else None
        amt1_halalas = parse_money_to_halalas(raw_amt1) or 0

        amt2_halalas = None
        if amount_col_2 and len(values) >= amount_col_2:
            raw_amt2 = values[amount_col_2 - 1]
            amt2_halalas = parse_money_to_halalas(raw_amt2)

        cust_name = str(values[customer_col - 1] or "").strip() if (customer_col and len(values) >= customer_col) else "عميل غير محدد"
        c_stat = str(values[case_status_col - 1] or "").strip() if (case_status_col and len(values) >= case_status_col) else ""
        m_stat = str(values[main_status_col - 1] or "").strip() if (main_status_col and len(values) >= main_status_col) else ""
        s_stat = str(values[sub_status_col - 1] or "").strip() if (sub_status_col and len(values) >= sub_status_col) else ""
        notes = str(values[notes_col - 1] or "").strip() if (notes_col and len(values) >= notes_col) else ""

        # Rich Metadata Extraction from Sheet
        nat_id = str(values[7] or "") if len(values) >= 8 else ""
        ph_nums = str(values[8] or "") if len(values) >= 9 else ""
        br_name = str(values[19] or "") if len(values) >= 20 else ""
        sup_name = str(values[20] or "") if len(values) >= 21 else ""
        f_date = str(values[24] or "") if len(values) >= 25 else ""

        # Determine effective search number
        # If record_type is mixed and clean_serv starts with '2', service search can be used, otherwise account search
        effective_rec_type = record_type
        if record_type == "mixed":
            effective_rec_type = "wallet" if clean_serv.startswith("2") and clean_serv else "account"

        customers.append(Customer(
            row_number=row_num,
            record_type=effective_rec_type,
            lookup_number=clean_lookup,
            contract=clean_lookup,
            expected_amount=amt1_halalas,
            expected_amount_2=amt2_halalas,
            original_account_number=clean_lookup,
            service_number=clean_serv,
            customer_name=cust_name or "عميل غير محدد",
            collector_name=raw_coll,
            case_status=c_stat,
            main_status=m_stat,
            sub_status=s_stat,
            notes=notes,
            national_id=nat_id,
            phones=ph_nums,
            supervisor_name=sup_name,
            branch_name=br_name,
            followup_date=f_date,
        ))

    return customers

