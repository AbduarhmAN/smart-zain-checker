"""Financial input selection shares the engine parser; no raw customers in RPCs."""
from domain.workbook import extract_session_customers
from domain.workbook_validation import validate_sheet


def quote_rows(sheet,mapping,mode='smart_hybrid',amount_target='remaining'):
    from services.file_client import RemoteSheet
    if isinstance(sheet, RemoteSheet):
        return sheet.rpc('quote_rows', mapping=mapping, mode=mode, amount_target=amount_target)
    validation = validate_sheet(sheet,mapping,mode,amount_target)
    if not validation['ok']:
        raise ValueError('Sheet must pass validation before pricing')
    customers = extract_session_customers(sheet,mapping,mode)
    if not customers:
        raise ValueError('No executable checks')
    rows,seen = [],set()
    for customer in customers:
        if type(customer.row_number) is not int or customer.row_number <= 0 or customer.row_number in seen:
            raise ValueError('Executable original-row identities must be unique')
        seen.add(customer.row_number)
        number = customer.lookup_number
        if not number.startswith(('1','2')):
            raise ValueError('Unsupported executable number')
        rows.append({'row_number':customer.row_number,'check_type':'service' if number.startswith('2') else 'account'})
    return sorted(rows,key=lambda row:row['row_number'])
