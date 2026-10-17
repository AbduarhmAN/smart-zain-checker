import unittest
from openpyxl import Workbook
from domain.workbook import extract_customer_records,extract_session_customers
from telegram_bot.commerce.order_input import quote_rows


class OrderInputTests(unittest.TestCase):
    def sheet(self,rows):
        wb = Workbook()
        for row in rows: wb.active.append(row)
        return wb.active

    def test_quote_rows_match_engine_deduplication_exactly(self):
        sheet = self.sheet([['رقم العقد','المبلغ'],['1000200989',100],['1000200989',100],['2000000001',50]])
        mapping = {'lookup_col':'A','amount_col':'B','has_headers':True}
        customers = extract_session_customers(sheet,mapping)
        quoted = quote_rows(sheet,mapping)
        self.assertEqual([c.row_number for c in customers],[r['row_number'] for r in quoted])
        self.assertEqual(quoted,[{'row_number':2,'check_type':'account'},{'row_number':4,'check_type':'service'}])

    def test_shared_helper_preserves_previous_manual_parser_selection(self):
        sheet = self.sheet([['رقم','مبلغ'],['1000200989',100],['2000000001',50],['2000000001',50]])
        mapping = {'lookup_col':'A','service_col':None,'amount_col':'B','has_headers':True}
        self.assertEqual(extract_session_customers(sheet,mapping),
                         extract_customer_records(sheet,lookup_col=1,amount_col=2,has_headers=True,record_type='mixed'))

    def test_row_one_without_headers_is_priced(self):
        sheet = self.sheet([['2000000001',0],['1000200989',80]])
        self.assertEqual(quote_rows(sheet,{'lookup_col':'A','amount_col':'B','has_headers':False})[0]['row_number'],1)

    def test_invalid_amount_prevents_any_quote(self):
        sheet = self.sheet([['رقم','مبلغ'],['1000200989','invalid']])
        with self.assertRaises(ValueError): quote_rows(sheet,{'lookup_col':'A','amount_col':'B','has_headers':True})

    def test_service_only_uses_selected_service_column(self):
        sheet = self.sheet([['حساب','خدمة','مبلغ'],['1000200989','2000000001',50]])
        result = quote_rows(sheet,{'lookup_col':'A','service_col':'B','amount_col':'C','has_headers':True},mode='service_only')
        self.assertEqual(result,[{'row_number':2,'check_type':'service'}])


if __name__ == '__main__': unittest.main()
