# -*- coding: utf-8 -*-
"""Unit tests for automatic pre-scan clean sheet deduplication.
Enforces the exact rules of clean_sheet_builder.py inside extract_customer_records:
- An account is kept for its first-seen debt.
- Duplicate rows for the same account with non-2 services are dropped.
- Rows for the same account with independent 2... services are kept.
- Duplicate identical 2... services are dropped.
- In account_only mode, each account is kept strictly once.
"""
import io
import sys
import unittest
from openpyxl import Workbook

# Windows UTF-8 stdout encoding rule
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from domain.workbook import extract_customer_records


class TestCleanDedupPreScan(unittest.TestCase):
    def setUp(self):
        self.wb = Workbook()
        ws = self.wb.active
        ws.title = "TestSheet"
        ws.append(["رقم الحساب", "رقم الخدمة", "المبلغ", "اسم العميل"])
        # Row 2: Account 1001 with non-2 service (Kept: first seen debt)
        ws.append(["1001", "9001", 100, "عميل 1"])
        # Row 3: Account 1001 with another non-2 service (Ignored: duplicate account debt)
        ws.append(["1001", "9002", 150, "عميل 1 مكرر"])
        # Row 4: Account 1001 with 2... service (Kept: independent 2... service)
        ws.append(["1001", "2001", 200, "عميل 1 خدمة 2"])
        # Row 5: Account 1002 with 2... service (Kept: first seen service 2)
        ws.append(["1002", "2002", 300, "عميل 2"])
        # Row 6: Account 1002 with exact same 2... service (Ignored: duplicate service 2)
        ws.append(["1002", "2002", 300, "عميل 2 مكرر"])
        # Row 7: Account 1003 without service (Kept: first seen)
        ws.append(["1003", "", 400, "عميل 3"])
        # Row 8: Account 1003 without service (Ignored: duplicate)
        ws.append(["1003", "", 400, "عميل 3 مكرر"])

    def test_smart_hybrid_clean_dedup(self):
        ws = self.wb.active
        customers = extract_customer_records(
            sheet=ws,
            lookup_col=1,
            amount_col=3,
            service_col=2,
            customer_col=4,
            record_type="mixed",
            clean_dedup=True,
        )
        # Expected kept rows: Row 2, Row 4, Row 5, Row 7 (total 4 rows)
        self.assertEqual(len(customers), 4)
        kept_rows = [c.row_number for c in customers]
        self.assertEqual(kept_rows, [2, 4, 5, 7])
        self.assertEqual([c.lookup_number for c in customers], ["1001", "2001", "2002", "1003"])

    def test_account_only_clean_dedup(self):
        ws = self.wb.active
        customers = extract_customer_records(
            sheet=ws,
            lookup_col=1,
            amount_col=3,
            service_col=2,
            customer_col=4,
            record_type="account",
            clean_dedup=True,
        )
        # Expected kept rows: Row 2 (1001), Row 5 (1002), Row 7 (1003) (total 3 unique accounts)
        self.assertEqual(len(customers), 3)
        kept_rows = [c.row_number for c in customers]
        self.assertEqual(kept_rows, [2, 5, 7])
        self.assertEqual([c.lookup_number for c in customers], ["1001", "1002", "1003"])

    def test_service_only_clean_dedup(self):
        ws = self.wb.active
        customers = extract_customer_records(
            sheet=ws,
            lookup_col=1,
            amount_col=3,
            service_col=2,
            customer_col=4,
            record_type="wallet",
            clean_dedup=True,
        )
        # Expected kept rows: Row 4 (2001), Row 5 (2002) (total 2 unique 2... services)
        self.assertEqual(len(customers), 2)
        kept_rows = [c.row_number for c in customers]
        self.assertEqual(kept_rows, [4, 5])
        self.assertEqual([c.lookup_number for c in customers], ["2001", "2002"])

    def test_clean_dedup_can_be_disabled_if_explicitly_requested(self):
        ws = self.wb.active
        customers = extract_customer_records(
            sheet=ws,
            lookup_col=1,
            amount_col=3,
            service_col=2,
            customer_col=4,
            record_type="mixed",
            clean_dedup=False,
        )
        # All 7 rows are kept when clean_dedup=False
        self.assertEqual(len(customers), 7)


if __name__ == "__main__":
    unittest.main()
