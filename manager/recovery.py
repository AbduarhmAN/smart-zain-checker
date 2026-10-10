"""Crash recovery helpers. No workbook reads during live polling, no file deletion."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


def input_signature(customers, mapping, mode, amount_target, sheet_index):
    digest = hashlib.sha256()
    digest.update(json.dumps([mapping, mode, amount_target, sheet_index], sort_keys=True,
                             ensure_ascii=False, default=str).encode('utf-8'))
    for customer in customers:
        digest.update(json.dumps([customer.row_number, customer.lookup_number,
            customer.service_number, customer.contract, customer.expected_amount,
            customer.expected_amount_2, customer.customer_name, customer.collector_name,
            customer.original_account_number, customer.main_status, customer.sub_status,
            customer.notes], ensure_ascii=False, default=str).encode('utf-8'))
        digest.update(b'\n')
    return digest.hexdigest()


def read_journal(path: Path):
    records = {}
    if path.exists():
        with path.open(encoding='utf-8') as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                    records[int(record['row'])] = record
                except (ValueError, TypeError, KeyError):
                    continue  # A partially written last line is not a completed row.
    return records


def append_journal(path: Path, record):
    # Leading newline also isolates an incomplete line left by a previous crash.
    with path.open('a', encoding='utf-8') as handle:
        handle.write('\n' + json.dumps(record, ensure_ascii=False) + '\n')
        handle.flush()


def restore_records(customers, saved, records):
    """Only durable rows with matching identifiers/amounts may be skipped."""
    restored = {}
    claimed = {i for i in saved.get('completed_indices', []) if isinstance(i, int) and not isinstance(i, bool)}
    for index, customer in enumerate(customers):
        record = records.get(customer.row_number)
        if record is None:
            continue
        numbers = {str(customer.lookup_number or ''), str(customer.service_number or ''), str(customer.contract or '')} - {''}
        if str(record.get('account') or '') not in numbers and str(record.get('service') or '') not in numbers and str(record.get('contract') or '') not in numbers:
            continue
        try:
            expected = round(float(record.get('expected_sar', -1))*100)
        except (ValueError, TypeError, OverflowError):
            continue
        if expected not in {customer.expected_amount, customer.expected_amount_2}:
            continue
        # Modern journals carry the input signature; legacy journals are accepted
        # only for rows explicitly recorded in their checkpoint.
        same_input = bool(record.get('input_signature') and record.get('input_signature') == saved.get('input_signature'))
        if same_input or index in claimed:
            restored[index] = record
    return restored
