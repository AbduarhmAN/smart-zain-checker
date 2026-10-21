"""Populated wallet/service priority, independent accounts, and cancelled columns."""
from itertools import permutations
import unittest
from openpyxl import Workbook
from domain.workbook import (CalamineSheetAdapter, inspect_sheet_schema,
    extract_session_customers)
from domain.workbook_validation import validate_sheet


class LookupSchemaPriorityTests(unittest.TestCase):
    def schema(self, headers, values, *, adapter=False):
        rows=[headers, values]
        if adapter:sheet=CalamineSheetAdapter(rows)
        else:
            book=Workbook();self.addCleanup(book.close);sheet=book.active
            for row in rows:sheet.append(row)
        return sheet,inspect_sheet_schema(sheet)

    def test_each_wallet_alias_is_a_primary_lookup_and_preserves_amounts(self):
        for header in ('رقم المحفظة','المحفظة','محفظة','رقم المحفظه','المحفظه','محفظه'):
            for adapter in (False,True):
                with self.subTest(header=header,adapter=adapter):
                    sheet,schema=self.schema(['اسم العميل',header,'المبلغ'],['عميل تجريبي','2000000001',0],adapter=adapter)
                    self.assertEqual(schema['letters']['lookup_col'],'B')
                    self.assertEqual(schema['letters']['service_col'],'B')
                    self.assertEqual(schema['letters']['amount_col'],'C')
                    self.assertTrue(schema['is_acceptable'])
                    self.assertTrue(validate_sheet(sheet,schema['letters'])['ok'])
                    rows=extract_session_customers(sheet,schema['letters'])
                    self.assertEqual(len(rows),1);self.assertEqual(rows[0].service_number,'2000000001')
                    self.assertEqual(rows[0].expected_amount,0)

    def test_wallet_then_service_priority_is_independent_of_column_order(self):
        data={'رقم الحساب':'1000000001','رقم الخدمة':'2000000001','رقم المحفظة':'2000000002'}
        for headers in permutations(data):
            with self.subTest(headers=headers):
                _,schema=self.schema(list(headers)+['المبلغ'],[data[h] for h in headers]+[35])
                self.assertEqual(schema['indices']['lookup_col'],headers.index('رقم الحساب')+1)
                self.assertEqual(schema['indices']['service_col'],headers.index('رقم المحفظة')+1)

    def test_service_and_account_keep_their_own_columns_in_either_order(self):
        for headers,values,expected in ((['رقم الحساب','رقم الخدمة','المبلغ'],['1000000001','2000000001',35],'B'),
                                       (['رقم الخدمة','رقم الحساب','المبلغ'],['2000000001','1000000001',35],'A')):
            _,schema=self.schema(headers,values)
            self.assertEqual(schema['letters']['lookup_col'],'A' if expected=='B' else 'B')
            self.assertEqual(schema['letters']['service_col'],expected)

    def test_empty_wallet_cannot_shadow_populated_service_or_account(self):
        for headers in permutations(('رقم الحساب','رقم الخدمة','رقم المحفظة')):
            for adapter in (False, True):
                with self.subTest(headers=headers,adapter=adapter):
                    data={'رقم الحساب':'1000000001','رقم الخدمة':'2000000001','رقم المحفظة':None}
                    sheet,schema=self.schema(list(headers)+['المبلغ'],[data[h] for h in headers]+[35],adapter=adapter)
                    self.assertEqual(schema['indices']['lookup_col'],headers.index('رقم الحساب')+1)
                    self.assertEqual(schema['indices']['service_col'],headers.index('رقم الخدمة')+1)
                    self.assertTrue(validate_sheet(sheet,schema['letters'])['ok'])
                    customer=extract_session_customers(sheet,schema['letters'])[0]
                    self.assertEqual(customer.original_account_number,'1000000001')
                    self.assertEqual(customer.service_number,'2000000001')
                    self.assertEqual(customer.lookup_number,'2000000001')
                    accounts=extract_session_customers(sheet,schema['letters'],mode='account_only')
                    self.assertEqual(accounts[0].lookup_number,'1000000001')

    def test_populated_wallet_does_not_replace_original_account(self):
        sheet,schema=self.schema(['رقم المحفظة','رقم الحساب','رقم الخدمة','المبلغ'],
                                ['2000000002','1000000001','2000000001',35])
        self.assertEqual(schema['letters']['lookup_col'],'B')
        self.assertEqual(schema['letters']['service_col'],'A')
        customer=extract_session_customers(sheet,schema['letters'])[0]
        self.assertEqual(customer.original_account_number,'1000000001')
        self.assertEqual(customer.service_number,'2000000002')
        self.assertEqual(customer.lookup_number,'2000000002')

    def test_sparse_service_beyond_initial_five_rows_is_still_detected(self):
        rows=[['رقم المحفظة','رقم الخدمة','رقم الحساب','المبلغ']]
        rows += [[None,None,'1000000001',35] for _ in range(8)]
        rows.append([None,'2000000001','1000000001',35])
        schema=inspect_sheet_schema(CalamineSheetAdapter(rows))
        self.assertEqual(schema['letters']['lookup_col'],'C')
        self.assertEqual(schema['letters']['service_col'],'B')

    def test_primary_service_wins_over_secondary_service_in_either_order(self):
        for headers in permutations(('رقم الخدمة 2','رقم الخدمة','رقم المحفظة','رقم الحساب')):
            with self.subTest(headers=headers):
                data={'رقم الخدمة 2':'2000000002','رقم الخدمة':'2000000001',
                      'رقم المحفظة':None,'رقم الحساب':'1000000001'}
                _,schema=self.schema(list(headers)+['المبلغ'],[data[h] for h in headers]+[35],adapter=True)
                self.assertEqual(schema['indices']['lookup_col'],headers.index('رقم الحساب')+1)
                self.assertEqual(schema['indices']['service_col'],headers.index('رقم الخدمة')+1)

    def test_plural_service_and_account_headers_keep_distinct_roles(self):
        _,schema=self.schema(['ارقام الحساب','ارقام الخدمة','رقم المحفظة','المبلغ'],
                            ['1000000001','2000000001',None,35],adapter=True)
        self.assertEqual(schema['letters']['lookup_col'],'A')
        self.assertEqual(schema['letters']['service_col'],'B')

    def test_empty_identifier_headers_do_not_pass_row_validation(self):
        sheet,schema=self.schema(['رقم المحفظة','رقم الخدمة','رقم الحساب','المبلغ'],
                            [None,'','   ',35],adapter=True)
        self.assertEqual(schema['letters']['lookup_col'],'C')
        validation=validate_sheet(sheet,schema['letters'])
        self.assertFalse(validation['ok'])
        self.assertEqual(validation['invalid_rows'],1)

    def test_explicit_mapping_is_not_replaced_by_automatic_detection(self):
        sheet,_=self.schema(['رقم الحساب','رقم الخدمة','رقم المحفظة','المبلغ'],
                            ['1000000001','2000000001','2000000002',35],adapter=True)
        mapping={'lookup_col':'A','service_col':'B','amount_col':'D'}
        customer=extract_session_customers(sheet,mapping)[0]
        self.assertEqual(customer.original_account_number,'1000000001')
        self.assertEqual(customer.service_number,'2000000001')

    def test_all_cancelled_forms_are_excluded_from_headers_and_sample_fallback(self):
        for suffix in ('الملغية','ملغية','الملغي','ملغي','الملغيه','ملغيه'):
            for base in ('رقم الخدمة','رقم المحفظة','رقم الحساب','رقم البحث'):
                with self.subTest(base=base,suffix=suffix):
                    _,schema=self.schema([base+' '+suffix,'المبلغ'],['2000000001',35])
                    self.assertIsNone(schema['letters']['lookup_col'])
                    self.assertIsNone(schema['letters']['service_col'])
                    self.assertFalse(schema['is_acceptable'])

    def test_cancelled_service_never_shadows_valid_wallet_or_legacy_account(self):
        for valid,number in (('رقم المحفظة','2000000002'),('رقم الخدمة','2000000002'),('رقم الحساب','1000000001')):
            _,schema=self.schema(['رقم الخدمة الملغية',valid,'المبلغ'],['2000000001',number,35])
            self.assertEqual(schema['letters']['lookup_col'],'B')
            self.assertEqual(schema['letters']['service_col'],'B')

    def test_legacy_account_contract_and_combined_headers_still_work(self):
        for header in ('رقم الحساب','رقم العقد','رقم الحساب ورقم الخدمة','Account','Contract','Number'):
            with self.subTest(header=header):
                sheet,schema=self.schema([header,'المبلغ'],['1000000001',35])
                self.assertEqual(schema['letters']['lookup_col'],'A')
                self.assertEqual(schema['letters']['service_col'],'A')
                self.assertTrue(validate_sheet(sheet,schema['letters'],'account_only')['ok'])
                rows=extract_session_customers(sheet,schema['letters'],mode='account_only')
                self.assertEqual(rows[0].contract,'1000000001')
                self.assertEqual(rows[0].expected_amount,3500)

    def test_explicit_service_wins_over_a_combined_header(self):
        _,schema=self.schema(['رقم الحساب ورقم الخدمة','رقم الخدمة','المبلغ'],['1000000001','2000000001',35])
        self.assertEqual(schema['letters']['lookup_col'],'B')

    def test_wallet_name_and_wallet_value_do_not_shadow_a_valid_number(self):
        _,schema=self.schema(['اسم العميل','اسم المحفظة','قيمة المحفظة','رقم الحساب'],
                            ['عميل تجريبي','محفظة تجريبية',35,'1000000001'])
        self.assertEqual(schema['letters']['lookup_col'],'D')
        self.assertEqual(schema['letters']['service_col'],'D')
        self.assertEqual(schema['letters']['amount_col'],'C')

    def test_cancelled_identifiers_are_not_inferred_as_amounts(self):
        _,schema=self.schema(['رقم الخدمة الملغية','رقم المحفظة','غير محدد'],['2000000001','2000000002','وصف'])
        self.assertIsNone(schema['letters']['amount_col'])
        self.assertFalse(schema['is_acceptable'])

    def test_headerless_input_keeps_existing_unambiguous_detection(self):
        sheet=CalamineSheetAdapter([['2000000001',0],['2000000002',35]])
        schema=inspect_sheet_schema(sheet,has_headers=False)
        self.assertEqual(schema['letters']['lookup_col'],'A')
        self.assertEqual(schema['letters']['amount_col'],'B')
        self.assertEqual(schema['estimated_rows'],2)


if __name__=='__main__':unittest.main()
