"""In-memory regression checks: no workbook writes or network requests."""
import unittest
from dataclasses import replace
from openpyxl import Workbook
from zain_checker.workbook import Customer, _deduplicate_records, _read_sheet_records


def record(row, service="", amount=100):
    return Customer("account", "1012024276", amount, (row,), "Customer", "", "",
                    source_key="custom", service_number=service)


class DuplicateServiceTests(unittest.TestCase):
    def test_distinct_services_keep_distinct_amounts(self):
        clean, errors = _deduplicate_records([
            record(3, "582860852", 35000), record(4, "582904852", 21890)])
        self.assertFalse(errors)
        self.assertEqual([r.lookup_number for r in clean], ["582860852", "582904852"])
        self.assertEqual([r.expected_amount for r in clean], [35000, 21890])
        self.assertTrue(all(r.original_account_number == "1012024276" for r in clean))
        self.assertTrue(all(r.record_type_ar == "رقم خدمة" for r in clean))

    def test_equal_amounts_do_not_merge_distinct_services(self):
        clean, errors = _deduplicate_records([record(3, "582860852"), record(4, "582904852")])
        self.assertEqual(len(clean), 2)
        self.assertFalse(errors)

    def test_missing_service_remains_conflict(self):
        clean, errors = _deduplicate_records([record(3, "582860852"), record(4, amount=200)])
        self.assertFalse(clean)
        self.assertEqual(errors[0].source_key, "custom")

    def test_same_service_conflict_not_hidden(self):
        clean, errors = _deduplicate_records([record(3, "582860852"), record(4, "582860852", 200)])
        self.assertFalse(clean)
        self.assertEqual(len(errors), 1)

    def test_single_account_unchanged(self):
        original = record(3, "582860852")
        self.assertEqual(_deduplicate_records([original]), ([original], []))

    def test_all_source_rows_preserved(self):
        clean, errors = _deduplicate_records([replace(record(3), row_numbers=(3, 4)), record(5)])
        self.assertEqual(clean[0].row_numbers, (3, 4, 5))
        self.assertFalse(errors)

    def test_custom_service_header_discovery(self):
        sheet = Workbook().active
        sheet.append(["رقم الحساب", "المبلغ", "اسم العميل", "رقم الخدمة"])
        sheet.append(["1012024276", 350, "Customer", "582860852"])
        sheet.append(["1012024276", 218.9, "Customer", "582904852"])
        records, errors = _read_sheet_records(
            sheet, record_type="account", lookup_column=1, source_key="custom",
            target_collector="", amount_column=2, customer_column=3, no_collector=True)
        self.assertFalse(errors)
        self.assertEqual([r.service_number for r in records], ["582860852", "582904852"])
        self.assertEqual(len(_deduplicate_records(records)[0]), 2)


if __name__ == "__main__":
    unittest.main()
