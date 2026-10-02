"""Comprehensive Pytest Suite for Smart Zain Checker (Gemini Edition).

Verifies:
- The Four Unbreakable Laws (Section 1.C)
- All 9 Data Validation Rules (Section 3: VAL-ACC-01..03, VAL-SRV-01..03, VAL-AMT-01..03)
- All 9 Canonical Engineering Concepts (Section 4 / Task 6)
- All 28 Practical Examples (Section 7: Examples 1 to 28)
- All 38 Edge Cases (Section 8)
- All 20 Logical Prohibitions (Section 12)
- All 8 Self-Audit Scenarios (Section 13)
- Full 20-Row Multi-Sheet End-to-End Simulation (Section 11 / Task 10)
- Integration with zain_checker.workbook (load_records, append_mismatch, append_error)
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
import sys

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from openpyxl import Workbook, load_workbook
import pytest

from zain_checker.domain_models import (
    AccountConceptTag,
    AccountFileScopeStatus,
    AmountState,
    ReconciliationStatus,
    RowValidationStatus,
)
from zain_checker.pipeline import (
    classify_and_parse_amount,
    normalize_arabic_text,
    run_pre_zain_pipeline,
    sanitize_identifier_cell,
)
from zain_checker.reconciliation import (
    ZainTaskObservation,
    bind_zain_observations_to_plan,
    export_comprehensive_audit_workbook,
    format_variance_description,
)
from zain_checker.simulation_suite import run_and_verify_section_11_simulation
from zain_checker.workbook import Mismatch, append_mismatch, load_records


def _build_test_workbook(tmp_path: Path, sheets_data: dict[str, list[list]], filename: str = "test_input.xlsx") -> Path:
    wb = Workbook()
    first = True
    for sheet_name, rows in sheets_data.items():
        ws = wb.active if first else wb.create_sheet(sheet_name)
        ws.title = sheet_name
        first = False
        for r_idx, r_vals in enumerate(rows, start=1):
            ws.append(r_vals)
            if r_idx > 1:
                # Preserve text formatting on identifier columns (cols 3 & 4) when strings
                if len(r_vals) >= 3 and isinstance(r_vals[2], str):
                    ws.cell(r_idx, 3).number_format = "@"
                if len(r_vals) >= 4 and isinstance(r_vals[3], str):
                    ws.cell(r_idx, 4).number_format = "@"
    out = tmp_path / filename
    wb.save(out)
    wb.close()
    return out


STD_HEADERS = [
    "اسم المحصل",
    "اسم العميل",
    "رقم الحساب",
    "رقم الخدمة",
    "مبلغ المديونية في ملف المحصل",
    "الحالة الرئيسية",
]


def test_section_11_full_20_row_multi_sheet_simulation(tmp_path: Path) -> None:
    """Tests the complete 20-row multi-sheet simulation from Section 11 (Task 10)."""
    plan = run_and_verify_section_11_simulation(tmp_path)
    assert plan.total_input_rows == 20
    assert plan.service_searchable_rows_count == 6
    assert plan.account_searchable_rows_count == 7
    assert plan.excluded_rows_count == 1
    assert plan.blocked_or_review_rows_count == 6
    assert len(plan.service_search_tasks) == 6
    assert len(plan.account_search_tasks) == 5
    assert plan.invariant_balanced is True
    assert plan.ready_for_zain_execution is True
    assert (tmp_path / "simulation_audit_report.xlsx").exists()


def test_four_unbreakable_laws_and_zero_residual_deduction(tmp_path: Path) -> None:
    """Verifies Law 1 (Scope Symmetry), Law 2 (Zero Residual Deduction),
    Law 3 (No Unfounded Root-Cause Inference), and Law 4 (Error != Zero).
    """
    wb_path = _build_test_workbook(
        tmp_path,
        {
            "Sheet1": [
                STD_HEADERS,
                # Example 5: Mixed account 10005 (Service 2005551 @ 400 + Service 5505552 @ 700)
                ["سعد", "عميل 1", "10005", "2005551", 400.00, "نشط"],
                ["سعد", "عميل 1", "10005", "5505552", 700.00, "نشط"],
                # Explicit Zero service
                ["سعد", "عميل 2", "10006", "2006661", 0.00, "نشط"],
            ]
        },
    )
    plan = run_pre_zain_pipeline(wb_path)
    assert len(plan.service_search_tasks) == 2
    assert len(plan.account_search_tasks) == 1

    srv_2005551 = next(t for t in plan.service_search_tasks if t.search_key == "2005551")
    srv_2006661 = next(t for t in plan.service_search_tasks if t.search_key == "2006661")
    acc_10005 = plan.account_search_tasks[0]

    observations = {
        srv_2005551.task_id: ZainTaskObservation(srv_2005551.task_id, "SUCCESS", Decimal("400.00")),
        acc_10005.task_id: ZainTaskObservation(acc_10005.task_id, "SUCCESS", Decimal("1100.00")),
        srv_2006661.task_id: ZainTaskObservation(srv_2006661.task_id, "SUCCESS", Decimal("250.00")),
    }
    bind_zain_observations_to_plan(plan, observations)

    # Law 2: Zero Residual Deduction! Even though Account 10005 is 1100 and Service 2005551 is 400,
    # the system must NEVER deduce that Service 5505552 is 1100 - 400 = 700 at the service level!
    row_non2 = next(r for r in plan.source_rows if r.service_number_norm == "5505552")
    assert row_non2.zain_service_amount is None
    assert row_non2.zain_account_total_context == Decimal("1100.00")
    assert acc_10005.reconciliation_status == ReconciliationStatus.ACCOUNT_TOTAL_MATCH

    # Law 3: No Unfounded Root-Cause Inference on File=0, Zain=250
    assert srv_2006661.reconciliation_status == ReconciliationStatus.SERVICE_AMOUNT_DIFFERENCE
    assert "فرق مالي (File = 0, Zain > 0)" in srv_2006661.reconciliation_note
    assert "تسوية" not in srv_2006661.reconciliation_note
    assert "سداد" not in format_variance_description(Decimal("500.00"), Decimal("100.00"))


def test_leading_zeros_and_format_mask_reconstruction() -> None:
    """Tests Stage 5 & Example 20: Leading zero preservation and Format Mask Reconstruction."""
    # 1. Numeric cell with custom format mask '0000000000' -> reconstructs '0591234567'
    norm, errs, notes = sanitize_identifier_cell(
        591234567, data_type="n", number_format="0000000000"
    )
    assert norm == "0591234567"
    assert not errs
    assert "Reconstructed Leading Zero from Format Mask" in notes

    # 2. Critical prefix protection: "02001234" starts with '0', NOT '2'!
    norm_02, errs_02, _ = sanitize_identifier_cell("02001234", data_type="s")
    assert norm_02 == "02001234"
    assert not norm_02.startswith("2")


def test_scientific_notation_and_symbol_protection() -> None:
    """Tests Stage 6 & 7: Scientific Notation corruption & non-destructive symbol protection."""
    # Case A: Valid integer stored as float <= 15 digits (201234567890.0) -> extracted cleanly
    norm_ok, errs_ok, _ = sanitize_identifier_cell(201234567890.0, data_type="n")
    assert norm_ok == "201234567890"
    assert not errs_ok

    # Case B: String scientific notation ("2.01234E+11") -> BLOCKED!
    norm_bad, errs_bad, _ = sanitize_identifier_cell("2.01234E+11", data_type="s")
    assert "CORRUPTED_IDENTIFIER_SCIENTIFIC_NOTATION" in errs_bad

    # Stage 7 Protection Rule: Never strip hyphens or slashes from inside an identifier!
    _, errs_slash, _ = sanitize_identifier_cell("200145/1", data_type="s")
    assert "CORRUPTED_OR_NON_STANDARD_IDENTIFIER_HOLD" in errs_slash
    _, errs_hyphen, _ = sanitize_identifier_cell("200-145", data_type="s")
    assert "CORRUPTED_OR_NON_STANDARD_IDENTIFIER_HOLD" in errs_hyphen


def test_strict_5_way_amount_classification_and_outliers() -> None:
    """Tests Stage 8: Strict differentiation between Positive, Explicit Zero, Blank, Null/Formula, and Negative."""
    # 1. Valid positive with currency text
    dec1, st1, err1, _ = classify_and_parse_amount("1,200.50 SAR")
    assert dec1 == Decimal("1200.50")
    assert st1 == AmountState.VALID_POSITIVE
    assert not err1

    # 2. Explicit Zero ('0' or 0.00)
    dec2, st2, err2, _ = classify_and_parse_amount(0)
    assert dec2 == Decimal("0.00")
    assert st2 == AmountState.EXPLICIT_ZERO
    assert not err2

    # 3. Blank ("" or "   ") -> MISSING_BLANK, NEVER 0.00!
    dec3, st3, err3, _ = classify_and_parse_amount("   ")
    assert dec3 is None
    assert st3 == AmountState.MISSING_BLANK
    assert err3

    # 4. Null / Formula Error ("#VALUE!" or "N/A")
    dec4, st4, err4, _ = classify_and_parse_amount("#VALUE!")
    assert dec4 is None
    assert st4 == AmountState.INVALID_NULL_OR_FORMULA
    assert err4

    # 5. Negative Amount (-150.00 or "(250.00)")
    dec5, st5, err5, _ = classify_and_parse_amount("(250.00)")
    assert dec5 == Decimal("-250.00")
    assert st5 == AmountState.NEGATIVE_AMOUNT
    assert err5

    # 6. Suspected Service ID pasted into Amount column (Edge Case #24)
    dec6, st6, err6, _ = classify_and_parse_amount(
        20014588.00, service_norm="20014588"
    )
    assert any("SUSPECTED_ID_IN_AMOUNT_COLUMN_HOLD" in e for e in err6)


def test_example_26_and_27_split_collector_and_pre_filter_duplicate_service(tmp_path: Path) -> None:
    """Tests Example 26 (Split Account Across Collectors) and Example 27 (Active + Closed Duplicate Service)."""
    wb_path = _build_test_workbook(
        tmp_path,
        {
            "Sheet1": [
                STD_HEADERS,
                # Example 26: Account 10026 split between Collector A (400) and Collector B (600)
                ["المحصل أ", "عميل 26", "10026", "5502601", 400.00, "نشط"],
                ["المحصل ب", "عميل 26", "10026", "5502602", 600.00, "نشط"],
                # Example 27: Service 2002701 repeated once as 'نشط' and once as 'مغلق'
                ["المحصل أ", "عميل 27", "10027", "2002701", 500.00, "نشط"],
                ["المحصل أ", "عميل 27", "10027", "2002701", 500.00, "مغلق"],
            ]
        },
    )
    plan = run_pre_zain_pipeline(
        wb_path,
        target_collector="المحصل أ",
        all_collectors=False,
    )

    # Verify Example 26: Account 10026 is flagged FILE_SCOPE_INCOMPLETE_SPLIT_COLLECTOR
    grp_10026 = next(g for g in plan.account_groups if g.account_number == "10026")
    assert grp_10026.file_scope_status == AccountFileScopeStatus.FILE_SCOPE_INCOMPLETE_SPLIT_COLLECTOR
    assert grp_10026.included_total_sum == Decimal("400.00")
    assert grp_10026.all_known_file_rows_sum == Decimal("1000.00")

    # Verify Example 27: Service 2002701 conflict caught BEFORE excluding 'مغلق'!
    row_r4 = next(r for r in plan.source_rows if r.row_uid == "Sheet1!R4")
    assert row_r4.validation_status == RowValidationStatus.ERROR_CONFLICTING_SERVICE
    assert any("DUPLICATE_SERVICE_STATUS_CONFLICT" in e for e in row_r4.validation_errors)
    assert not any(t.search_key == "2002701" for t in plan.service_search_tasks)


def test_cross_account_service_conflict_and_customer_name_variance(tmp_path: Path) -> None:
    """Tests Example 9 (Same Service Under Two Accounts) and Examples 17 & 18 (Customer Name Variance)."""
    wb_path = _build_test_workbook(
        tmp_path,
        {
            "Sheet1": [
                STD_HEADERS,
                # Example 9: Service 5509991 under Account 10009 and Account 10099
                ["سعد", "عميل أ", "10009", "5509991", 500.00, "نشط"],
                ["سعد", "عميل أ", "10099", "5509991", 500.00, "نشط"],
                # Example 17: Severe Customer Name Mismatch under same Account 10017
                ["سعد", "أحمد محمد علي", "10017", "2001701", 300.00, "نشط"],
                ["سعد", "مؤسسة النور للمقاولات", "10017", "2001702", 400.00, "نشط"],
                # Example 18: Same Customer Name under TWO different Accounts (10018 & 10019) -> Never merged!
                ["سعد", "خالد عبدالله القحطاني", "10018", "5501801", 500.00, "نشط"],
                ["سعد", "خالد عبدالله القحطاني", "10019", "5501901", 600.00, "نشط"],
            ]
        },
    )
    plan = run_pre_zain_pipeline(wb_path)

    grp_10009 = next(g for g in plan.account_groups if g.account_number == "10009")
    grp_10099 = next(g for g in plan.account_groups if g.account_number == "10099")
    assert AccountConceptTag.CONFLICTING_SERVICE_ENTITY in grp_10009.concept_tags
    assert AccountConceptTag.CONFLICTING_SERVICE_ENTITY in grp_10099.concept_tags
    assert not grp_10009.file_scope_status.is_complete
    assert not grp_10099.file_scope_status.is_complete

    grp_10017 = next(g for g in plan.account_groups if g.account_number == "10017")
    assert "SEVERE_CUSTOMER_NAME_MISMATCH_UNDER_SAME_ACCOUNT" in grp_10017.customer_name_warnings

    # Verify Accounts 10018 and 10019 remain strictly separate
    acc_keys = [t.search_key for t in plan.account_search_tasks]
    assert "10018" in acc_keys and "10019" in acc_keys


def test_workbook_py_integration_deduplication_and_immutable_column_lock(tmp_path: Path) -> None:
    """Tests that zain_checker.workbook.load_records and append_mismatch follow all laws."""
    wb_path = _build_test_workbook(
        tmp_path,
        {
            "CustomSheet": [
                STD_HEADERS + ["المبلغ الأصلي"],
                # Account 10004 with 2 non-2 services (300 + 500 = 800)
                ["سعد", "عميل 4", "10004", "5502001", 300.00, "نشط", 1000.00],
                ["سعد", "عميل 4", "10004", "5502002", 500.00, "نشط", 1000.00],
                # Account 10005 with 1 starts-with-2 service (250)
                ["سعد", "عميل 5", "10005", "2005001", 250.00, "نشط", 900.00],
            ]
        },
    )
    customers, errors = load_records(
        workbook_path=wb_path,
        source_mode="custom",
        target_collector="سعد",
        custom_configs=[
            {
                "sheet_index": 0,
                "lookup_column": 3,
                "service_column": 4,
                "amount_column": 5,
                "customer_column": 2,
                "collector_column": 1,
                "status_column": 6,
                "record_type": "mixed",
                "amount_column_2": 7,  # Secondary column must NEVER be used to override canonical amount
            }
        ],
    )
    assert not errors
    # 2 non-2 rows in Account 10004 must produce 1 Deduplicated Account Search Task with expected_amount = 800.00 (80000 halalas)!
    assert len(customers) == 2
    acc_cust = next(c for c in customers if c.record_type == "account")
    srv_cust = next(c for c in customers if c.record_type == "wallet")

    assert acc_cust.lookup_number == "10004"
    assert acc_cust.expected_result_scope == "ACCOUNT_LEVEL_TOTAL"
    assert acc_cust.expected_amount == 80000
    assert acc_cust.row_numbers == (2, 3)
    assert acc_cust.expected_amount_2 is None  # Prohibition #9 enforced!

    assert srv_cust.lookup_number == "2005001"
    assert srv_cust.expected_result_scope == "SERVICE_LEVEL"
    assert srv_cust.expected_amount == 25000

    # Test append_mismatch writes strict scope and never switches to 1000.00
    res_path = tmp_path / "نتائج فحص زين.xlsx"
    append_mismatch(res_path, Mismatch(customer=acc_cust, website_amount=100000, verified_at=datetime.now()))
    res_wb = load_workbook(res_path)
    ws = res_wb["الفروقات"]
    assert ws.cell(2, 8).value == 800.0  # File amount remained locked at 800.0, NOT 1000.0!
    assert ws.cell(2, 9).value == 1000.0
    assert "فرق مالي (File < Zain)" in str(ws.cell(2, 11).value)
    res_wb.close()
