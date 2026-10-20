"""Tests for automatic detection, parsing, and auditing of Errors and Results Sheets.
Ensures exported workbooks (like نتائج فحص زين.xlsx) and standalone error sheets
can be loaded, analyzed, mapped, and audited immediately with 100% precision.
"""
import io
import sys
from pathlib import Path
import pytest
import openpyxl

from domain.workbook import (
    detect_smart_sheet_columns,
    inspect_sheet_schema,
    extract_customer_records,
    parse_account_and_service,
)
from zain_checker.executive_reporter import export_executive_workbook


def test_parse_account_and_service_variations():
    """Verify robust parsing of compound and single numbers."""
    # Compound cell with Arabic labels
    acc, srv = parse_account_and_service("حساب: 1006659013 | خدمة: 2006695911")
    assert acc == "1006659013"
    assert srv == "2006695911"

    # Compound cell with phone number
    acc, srv = parse_account_and_service("عقد: 1001485949 | جوال: 0599662119")
    assert acc == "1001485949"
    assert srv == "0599662119"

    # Single service number starting with 2
    acc, srv = parse_account_and_service("2008084328")
    assert acc == ""
    assert srv == "2008084328"

    # Single account/contract number starting with 10
    acc, srv = parse_account_and_service("1010624047")
    assert acc == "1010624047"
    assert srv == ""

    # Delimited numbers without labels
    acc, srv = parse_account_and_service("1001485949 / 2008084328")
    assert acc == "1001485949"
    assert srv == "2008084328"


def test_executive_errors_sheet_detection_and_extraction(tmp_path):
    """Create a simulated executive errors sheet and verify schema inspection and row extraction."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "الأخطاء والملاحظات للمحصل"

    # Headers matching executive_reporter.py
    headers = [
        "رقم الصف في الشيت",
        "اسم العميل",
        "رقم الحساب ورقم الخدمة",
        "المبلغ في الشيت",
        "المشكلة باختصار",
        "رابط صفحة زين",
        "الحالة الرئيسية بالملف",
        "الحالة الفرعية بالملف",
    ]
    ws.append(headers)

    # Add sample error rows
    ws.append([54, "احمد الشايع", "حساب: 1006659013 | خدمة: 2006695911", 143.33, "تعذر استكمال المتصفح: TargetClosedError", "link", "عدم توصل", "لا يرد"])
    ws.append([79, "ETIDAL ALJOHANI", "حساب: 1007853624 | خدمة: 2008105565", 89.94, "تعذر استكمال المتصفح: TargetClosedError", "link", "توصل ايجابي", "مراجعة"])

    # 1. Schema Inspection
    schema = inspect_sheet_schema(ws)
    assert schema["is_acceptable"] is True
    assert "أخطاء" in schema["document_type"]
    assert "شيت أخطاء صالح ومكتمل" in schema["verification_status"]
    assert schema["estimated_rows"] == 2

    # Check mapped letters
    letters = schema["letters"]
    assert letters["source_row_col"] == "A"
    assert letters["customer_col"] == "B"
    assert letters["lookup_col"] == "C"
    assert letters["amount_col"] == "D"
    assert letters["notes_col"] == "E"
    assert letters["main_status_col"] == "G"
    assert letters["sub_status_col"] == "H"

    # 2. Extract Customer Records
    inds = schema["indices"]
    customers = extract_customer_records(
        sheet=ws,
        lookup_col=inds["lookup_col"],
        amount_col=inds["amount_col"],
        amount_col_2=inds["amount_col_2"],
        service_col=inds["service_col"],
        customer_col=inds["customer_col"],
        notes_col=inds["notes_col"],
        source_row_col=inds["source_row_col"],
        main_status_col=inds["main_status_col"],
        sub_status_col=inds["sub_status_col"],
    )

    assert len(customers) == 2

    c0 = customers[0]
    assert c0.row_number == 54  # Preserves master row number!
    assert c0.customer_name == "احمد الشايع"
    assert c0.contract == "1006659013"
    assert c0.service_number == "2006695911"
    assert c0.lookup_number == "2006695911"
    assert c0.record_type == "wallet"
    assert c0.expected_amount == 14333  # 143.33 SAR in Halalas
    assert "TargetClosedError" in c0.error_or_review_details
    assert c0.main_status == "عدم توصل"
    assert c0.sub_status == "لا يرد"

    c1 = customers[1]
    assert c1.row_number == 79
    assert c1.customer_name == "ETIDAL ALJOHANI"
    assert c1.contract == "1007853624"
    assert c1.service_number == "2008105565"
    assert c1.expected_amount == 8994


def test_executive_results_sheet_detection_and_extraction(tmp_path):
    """Create a simulated executive results sheet and verify schema inspection and row extraction."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "الفروقات الايجابيه"

    headers = [
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
    ws.append(headers)

    ws.append([
        "برقم الخدمة",
        "حساب: 1003991108 | خدمة: 2006532121",
        "link",
        "IBRAHIM ALHEJAILI",
        176,
        1478.78,
        978.78,
        500.0,
        "وعد سداد",
        "وعد سداد جزئي",
        "0590994388 تم التواصل",
    ])

    # 1. Schema Inspection
    schema = inspect_sheet_schema(ws)
    assert schema["is_acceptable"] is True
    assert "نتائج" in schema["document_type"]
    assert "شيت نتائج صالح ومكتمل" in schema["verification_status"]
    assert schema["estimated_rows"] == 1

    letters = schema["letters"]
    assert letters["lookup_col"] == "B"
    assert letters["customer_col"] == "D"
    assert letters["source_row_col"] == "E"
    assert letters["amount_col"] == "F"
    assert letters["amount_col_2"] == "G"
    assert letters["main_status_col"] == "I"
    assert letters["sub_status_col"] == "J"
    assert letters["notes_col"] == "K"

    # 2. Extract Customer Records
    inds = schema["indices"]
    customers = extract_customer_records(
        sheet=ws,
        lookup_col=inds["lookup_col"],
        amount_col=inds["amount_col"],
        amount_col_2=inds["amount_col_2"],
        service_col=inds["service_col"],
        customer_col=inds["customer_col"],
        notes_col=inds["notes_col"],
        source_row_col=inds["source_row_col"],
        main_status_col=inds["main_status_col"],
        sub_status_col=inds["sub_status_col"],
    )

    assert len(customers) == 1
    c = customers[0]
    assert c.row_number == 176  # Extracted from source_row_col E
    assert c.customer_name == "IBRAHIM ALHEJAILI"
    assert c.contract == "1003991108"
    assert c.service_number == "2006532121"
    assert c.lookup_number == "2006532121"
    assert c.record_type == "wallet"
    assert c.expected_amount == 147878  # 1478.78 SAR in Halalas
    assert c.expected_amount_2 == 97878  # 978.78 SAR in Halalas
    assert c.main_status == "وعد سداد"
    assert c.sub_status == "وعد سداد جزئي"
    assert "0590994388" in c.notes


def test_start_repair_session_from_sheet(tmp_path):
    """Verify Orchestrator can start a repair session directly from an exported results/errors workbook."""
    from manager.orchestrator import Orchestrator
    from workers.supervisor import WorkerSupervisor
    from manager.queue import QueueService

    # Create dummy errors workbook
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "الأخطاء والملاحظات للمحصل"
    ws.append([
        "رقم الصف في الشيت", "اسم العميل", "رقم الحساب ورقم الخدمة",
        "المبلغ في الشيت", "المشكلة باختصار", "رابط صفحة زين", "الحالة الرئيسية بالملف", "الحالة الفرعية بالملف"
    ])
    ws.append([54, "احمد الشايع", "حساب: 1006659013 | خدمة: 2006695911", 143.33, "TargetClosedError", "link", "عدم توصل", "لا يرد"])
    ws.append([79, "ETIDAL ALJOHANI", "حساب: 1007853624 | خدمة: 2008105565", 89.94, "TargetClosedError", "link", "توصل ايجابي", "مراجعة"])
    wb_file = tmp_path / "نتائج_فحص_تجريبي.xlsx"
    wb.save(wb_file)
    wb.close()

    supervisor = WorkerSupervisor(tmp_path, tmp_path)
    queue_service = QueueService(tmp_path / ".queue.json")
    orchestrator = Orchestrator(supervisor, queue_service, tmp_path)

    try:
        res = orchestrator.start_repair_session_from_sheet(wb_file, sheet_index=0)
        assert res["status"] == "ok"
        assert res["total_records"] == 2
        assert orchestrator.is_running is True
        assert len(orchestrator.customers) == 2
        assert orchestrator.errors_count == 2
        assert len(orchestrator.recent_results) == 2

        # Verify recent_results preview
        r0 = orchestrator.recent_results[0]
        assert r0["row"] == 54
        assert r0["name"] == "احمد الشايع"
        assert r0["status"] == "error"
        assert r0["number"] == "2006695911"
        assert r0["expected_sar"] == 143.33
    finally:
        orchestrator.cancel_session()
        supervisor.stop_all()


def test_detect_results_sheet_api_endpoint(tmp_path):
    """Verify the /api/detect-results-sheet endpoint via ServerGateway."""
    from manager.orchestrator import Orchestrator
    from workers.supervisor import WorkerSupervisor
    from manager.queue import QueueService
    from server.gateway import ServerGateway
    import urllib.request
    import json

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "الأخطاء والملاحظات للمحصل"
    ws.append([
        "رقم الصف في الشيت", "اسم العميل", "رقم الحساب ورقم الخدمة",
        "المبلغ في الشيت", "المشكلة باختصار", "رابط صفحة زين", "الحالة الرئيسية بالملف", "الحالة الفرعية بالملف"
    ])
    ws.append([12, "خالد محمد", "حساب: 1001234567 | خدمة: 2009876543", 250.0, "خطأ سابق", "link", "عدم توصل", "لا يرد"])
    wb_file = tmp_path / "نتائج_تجريبية.xlsx"
    wb.save(wb_file)
    wb.close()

    supervisor = WorkerSupervisor(tmp_path, tmp_path)
    queue_service = QueueService(tmp_path / ".queue.json")
    orchestrator = Orchestrator(supervisor, queue_service, tmp_path)

    # Start test gateway on ephemeral port
    gateway = ServerGateway(
        orchestrator=orchestrator,
        queue_service=queue_service,
        web_ui_dir=tmp_path,
        project_root=tmp_path,
        web_port=5099,
        bridge_port=8799,
    )
    gateway.start()
    port = gateway.web_port

    from urllib.parse import quote
    try:
        url = f"http://127.0.0.1:{port}/api/detect-results-sheet?workbook={quote(wb_file.name)}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        assert data["status"] == "ok"
        assert data["is_results_or_errors_sheet"] is True
        assert data["sheet_name"] == "الأخطاء والملاحظات للمحصل"
        assert data["error_count"] == 1
        assert len(data["rows"]) == 1
        assert data["rows"][0]["row"] == 12
        assert data["rows"][0]["name"] == "خالد محمد"
        assert data["rows"][0]["number"] == "2009876543"
        assert data["rows"][0]["expected_sar"] == 250.0
    finally:
        gateway.stop()
        supervisor.stop_all()
