# -*- coding: utf-8 -*-
"""Unit tests for audit mode selection, row count determination, and contract-only enforcement."""
import io
import sys
import unittest
from openpyxl import Workbook

# Windows UTF-8 stdout encoding rule
if sys.platform == "win32" and hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from domain.models import Customer
from domain.workbook import extract_customer_records
from manager.orchestrator import Orchestrator


class TestAuditModeSelection(unittest.TestCase):
    def setUp(self):
        # Create a sample sheet with 4 rows:
        # Row 2: Both Contract (1000000001) and Service (2000000001)
        # Row 3: Service only (2000000002) - no contract
        # Row 4: Contract only (1000000003) - no service
        # Row 5: Both Contract (1000000004) and Service (2000000004)
        self.wb = Workbook()
        ws = self.wb.active
        ws.title = "TestSheet"
        ws.append(["رقم الحساب", "رقم الخدمة", "المبلغ", "اسم العميل"])
        ws.append(["1000000001", "2000000001", 100, "عميل 1"])
        ws.append(["", "2000000002", 200, "عميل 2"])
        ws.append(["1000000003", "", 300, "عميل 3"])
        ws.append(["1000000004", "2000000004", 400, "عميل 4"])

    def test_account_only_filters_out_service_only_rows_and_uses_contract(self):
        ws = self.wb.active
        customers = extract_customer_records(
            sheet=ws,
            lookup_col=1,
            amount_col=3,
            service_col=2,
            customer_col=4,
            record_type="account",
        )
        # Row 3 (service only) must be excluded; only rows 2, 4, 5 are kept
        self.assertEqual(len(customers), 3)
        self.assertEqual([c.row_number for c in customers], [2, 4, 5])
        self.assertEqual([c.lookup_number for c in customers], ["1000000001", "1000000003", "1000000004"])
        for c in customers:
            self.assertEqual(c.record_type, "account")
            self.assertTrue(c.lookup_number.startswith("1"))

    def test_service_only_filters_out_contract_only_rows_and_uses_service(self):
        ws = self.wb.active
        customers = extract_customer_records(
            sheet=ws,
            lookup_col=1,
            amount_col=3,
            service_col=2,
            customer_col=4,
            record_type="wallet",
        )
        # Row 4 (contract only) must be excluded; only rows 2, 3, 5 are kept
        self.assertEqual(len(customers), 3)
        self.assertEqual([c.row_number for c in customers], [2, 3, 5])
        self.assertEqual([c.lookup_number for c in customers], ["2000000001", "2000000002", "2000000004"])
        for c in customers:
            self.assertEqual(c.record_type, "wallet")
            self.assertTrue(c.lookup_number.startswith("2"))

    def test_smart_hybrid_includes_all_valid_rows(self):
        ws = self.wb.active
        customers = extract_customer_records(
            sheet=ws,
            lookup_col=1,
            amount_col=3,
            service_col=2,
            customer_col=4,
            record_type="mixed",
        )
        # All 4 rows are kept
        self.assertEqual(len(customers), 4)
        self.assertEqual([c.row_number for c in customers], [2, 3, 4, 5])

    def test_orchestrator_payload_account_only(self):
        orch = Orchestrator.__new__(Orchestrator)
        orch.mode = "account_only"
        orch.target_url = "https://business.zain.sa/dashboard/quick-pay"

        cust = Customer(
            row_number=2,
            record_type="account",
            lookup_number="1000000001",
            contract="1000000001",
            expected_amount=10000,
            service_number="2000000001",
        )

        data = {"customer": cust, "index": 0, "worker_id": "worker_1"}
        payload = orch._task_payload("task_1", data)

        self.assertEqual(payload["search_number"], "1000000001")
        self.assertEqual(payload["record_type"], "account")
        self.assertEqual(payload["target_url"], "https://business.zain.sa/dashboard/quick-pay")

    def test_orchestrator_payload_service_only(self):
        orch = Orchestrator.__new__(Orchestrator)
        orch.mode = "service_only"
        orch.target_url = "https://business.zain.sa/dashboard/quick-pay"

        cust = Customer(
            row_number=2,
            record_type="wallet",
            lookup_number="2000000001",
            contract="1000000001",
            expected_amount=10000,
            service_number="2000000001",
        )

        data = {"customer": cust, "index": 0, "worker_id": "worker_1"}
        payload = orch._task_payload("task_1", data)

        self.assertEqual(payload["search_number"], "2000000001")
        self.assertEqual(payload["record_type"], "wallet")
        self.assertIn("https://app.sa.zain.com/ar/quickpay?account=2000000001", payload["target_url"])


if __name__ == "__main__":
    unittest.main()
