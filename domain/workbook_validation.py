"""Offline validation of explicitly mapped workbook columns and populated rows."""
from pathlib import Path
from domain.workbook import parse_excel_column, parse_account_and_service, is_empty_input_row
from domain.money import parse_money_to_halalas

FIELDS = {
    "lookup_col": "رقم الحساب / العقد", "service_col": "رقم الخدمة",
    "amount_col": "المبلغ المسجل بالشيت", "amount_col_2": "مبلغ ثانٍ اختياري",
    "customer_col": "اسم العميل · اختياري", "collector_col": "المحصل · اختياري",
}
MODES = {"smart_hybrid": "حساب أو خدمة", "account_only": "حسابات", "service_only": "خدمات"}


def validate_sheet(sheet, mapping, mode="smart_hybrid", amount_target="remaining"):
    """Check every populated row; blank/invalid money must never silently become zero."""
    if isinstance(sheet, (str, Path)):
        from domain.workbook import load_fast_workbook
        wb = load_fast_workbook(sheet)
        try:
            ws = wb.worksheets[0]
            return validate_sheet(ws, mapping, mode=mode, amount_target=amount_target)
        finally:
            wb.close()

    problems = []
    has_headers = mapping.get("has_headers", True)
    if not isinstance(has_headers, bool):
        return {"ok": False, "problems": ["حدد هل يحتوي الملف على صف عناوين."], "rows": 0, "invalid_rows": 0}
    first_row = 2 if has_headers else 1
    width = sheet.max_column or 0
    indices = {key: parse_excel_column(mapping.get(key)) for key in FIELDS}
    for key, index in indices.items():
        if mapping.get(key) and (not index or index > width):
            problems.append(f"عمود {FIELDS[key]} خارج حدود الورقة.")
    if mode not in MODES:
        problems.append("اختر طريقة فحص صحيحة.")
    required = ("lookup_col",) if mode == "account_only" else (("service_col",) if mode == "service_only" else ("lookup_col", "service_col"))
    if not any(indices.get(key) for key in required):
        problems.append("حدد عمود الرقم المناسب لطريقة الفحص.")
    if not indices["amount_col"]:
        problems.append("حدد عمود المبلغ المسجل بالشيت.")
    if amount_target not in {"contract", "remaining", "smart_dual"}:
        problems.append("اختر المبلغ المطلوب مراجعته.")
    if amount_target in {"contract", "smart_dual"} and not indices["amount_col_2"]:
        problems.append("حدد المبلغ الثاني أو اختر المبلغ المسجل بالشيت.")
    money_keys = ("amount_col_2",) if amount_target == "contract" else (("amount_col", "amount_col_2") if amount_target == "smart_dual" else ("amount_col",))
    numeric_columns = {indices[k] for k in ("amount_col", "amount_col_2") if indices[k]}
    if any(indices[k] in numeric_columns for k in required if indices[k]):
        problems.append("عمود الرقم يجب أن يختلف عن عمود المبلغ.")
    if problems:
        populated = sum(not is_empty_input_row(values, indices.values())
                        for values in sheet.iter_rows(min_row=first_row, values_only=True))
        return {"ok": False, "problems": problems, "rows": populated, "invalid_rows": 0}
    rows = 0
    invalid = []
    row_errors = []
    for row_number, values in enumerate(sheet.iter_rows(min_row=first_row, values_only=True), first_row):
        if is_empty_input_row(values, indices.values()):
            continue
        rows += 1
        def cell(key):
            index = indices[key]
            return values[index-1] if index and index <= len(values) else None
        account, service = parse_account_and_service(cell("lookup_col"))
        service_account, separate_service = parse_account_and_service(cell("service_col"))
        account = account if account.startswith("1") else ""
        service_account = service_account if service_account.startswith("1") else ""
        service = service if service.startswith("2") else ""
        separate_service = separate_service if separate_service.startswith("2") else ""
        valid_number = (account if mode == "account_only" else
                        separate_service if mode == "service_only" else
                        (account or service or service_account or separate_service))
        valid_money = all(cell(key) is not None and str(cell(key)).strip() and
                          parse_money_to_halalas(cell(key)) is not None for key in money_keys)
        if not valid_number or not valid_money:
            invalid.append(row_number)
            if len(invalid) <= 8:
                reasons = []
                if not valid_number:
                    raw_number = cell("service_col") if mode == "service_only" else cell("lookup_col")
                    if mode == "smart_hybrid" and (raw_number is None or not str(raw_number).strip()):
                        raw_number = cell("service_col")
                    if mode == "account_only" and not (account or service_account) and (service or separate_service):
                        reasons.append("الرقم يبدأ بـ٢ ولا يوجد رقم عقد (١)؛ اختر «العقود والخدمات» لفحصه")
                    elif mode == "service_only" and not (service or separate_service) and (account or service_account):
                        reasons.append("الرقم يبدأ بـ١ ولا يوجد رقم خدمة (٢)؛ اختر «العقود والخدمات» لفحصه")
                    elif raw_number is None or not str(raw_number).strip():
                        reasons.append("رقم البحث فارغ")
                    else:
                        reasons.append("رقم البحث غير قابل للقراءة أو لا يبدأ بـ١ أو ٢")
                for key in money_keys:
                    value = cell(key)
                    if value is None or not str(value).strip():
                        reasons.append(FIELDS[key] + " فارغ")
                    elif parse_money_to_halalas(value) is None:
                        reasons.append(FIELDS[key] + " ليس مبلغًا صالحًا")
                row_errors.append({"row": row_number, "reasons": reasons})
    if not rows:
        problems.append("لا توجد صفوف بيانات للفحص.")
    if invalid:
        problems.append(f"{len(invalid):,} صف يحتاج تصحيحًا. " + " · ".join(
            f"الصف {item['row']}: " + "، ".join(item["reasons"]) for item in row_errors))
    return {"ok": not problems, "problems": problems, "rows": rows, "invalid_rows": len(invalid), "row_errors": row_errors}
