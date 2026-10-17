"""Single owner of workbook parsing and writing in the four-process deployment."""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import threading

from services.file_client import atomic_workbook_save


class FileApplication:
    read_only = ('workbook.names',)
    retry_safe = read_only + ('sheet.schema','sheet.validate','sheet.extract','sheet.records',
                 'sheet.quote_rows','sheet.dimensions','sheet.rows','report.export','stream.append','audit.ensure')

    def __init__(self, root):
        self.root = Path(root).resolve()
        # ZIP parsing and writes are bounded independently by the RPC server.
        # Every operation touching a workbook uses this owner lock, preventing
        # read/write races or two updates of a single file within this service.
        self.lock = threading.RLock()
        self.streams = OrderedDict()

    @staticmethod
    def admit():
        from services.resources import admit
        from services.ipc import RetryLater
        if not admit('files'):
            raise RetryLater(3)

    @contextmanager
    def work_slot(self):
        # Rejected memory admission retains the accepted RPC command pending;
        # it cannot hold the workbook owner lock while waiting for resources.
        self.admit()
        with self.lock:
            self.admit()
            yield

    def path(self, value):
        candidate = Path(value)
        candidate = (self.root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        if not candidate.is_relative_to(self.root) or candidate == self.root:
            raise ValueError('Workbook outside configured workspace')
        if candidate.suffix.lower() not in {'.xlsx', '.xlsm', '.xls', '.xlsb'}:
            raise ValueError('Unsupported workbook type')
        return candidate

    def sheet(self, payload, operation):
        from domain.workbook import load_fast_workbook
        with self.work_slot():
            path = self.path(payload['path'])
            before = self.version(path)
            workbook = load_fast_workbook(path)
            try:
                index = payload.get('index', 0)
                if type(index) is not int or not 0 <= index < len(workbook.sheetnames):
                    raise ValueError('Unknown worksheet')
                # FastWorkbook.worksheets eagerly loads every tab. Index only
                # the selected sheet to keep large multi-tab inputs bounded.
                sheet = workbook[index] if hasattr(workbook, 'get_sheet') else workbook.worksheets[index]
                result = operation(sheet)
                if self.version(path) != before:
                    raise ValueError('Workbook changed during processing')
                return result
            finally:
                workbook.close()

    @staticmethod
    def version(path):
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns

    def names(self, payload):
        from domain.workbook import load_fast_workbook
        with self.lock:
            workbook = load_fast_workbook(self.path(payload['path']))
            try:
                return list(workbook.sheetnames)
            finally:
                workbook.close()

    def schema(self, payload):
        from domain.workbook import inspect_sheet_schema
        return self.sheet(payload, lambda sheet: inspect_sheet_schema(sheet,
            has_headers=payload.get('has_headers', True), count_rows=payload.get('count_rows', True)))

    def validate(self, payload):
        from domain.workbook_validation import validate_sheet
        return self.sheet(payload, lambda sheet: validate_sheet(sheet, payload['mapping'],
            payload.get('mode', 'smart_hybrid'), payload.get('amount_target', 'remaining')))

    def extract(self, payload):
        from domain.workbook import extract_session_customers
        return self.sheet(payload, lambda sheet: extract_session_customers(sheet, payload['mapping'],
            payload.get('mode', 'smart_hybrid'), auto_indices=payload.get('auto_indices')))

    def records(self, payload):
        from domain.workbook import extract_customer_records
        allowed = {'lookup_col','amount_col','amount_col_2','service_col','customer_col','collector_col',
                   'case_status_col','main_status_col','sub_status_col','notes_col','source_row_col',
                   'target_collector','record_type','has_headers','clean_dedup'}
        options = payload.get('options', {})
        if not isinstance(options, dict) or options.keys() - allowed:
            raise ValueError('Unsupported extraction options')
        return self.sheet(payload, lambda sheet: extract_customer_records(sheet=sheet, **options))

    def quote_rows(self, payload):
        from telegram_bot.commerce.order_input import quote_rows
        return self.sheet(payload, lambda sheet: quote_rows(sheet, payload['mapping'],
            payload.get('mode', 'smart_hybrid'), payload.get('amount_target', 'remaining')))

    def dimensions(self, payload):
        return self.sheet(payload, lambda sheet: {'max_row': sheet.max_row, 'max_column': sheet.max_column})

    def rows(self, payload):
        first = payload.get('min_row', 1)
        last = payload.get('max_row', first + 255)
        left = payload.get('min_col', 1)
        right = payload.get('max_col')
        if any(type(value) is not int or value < 1 for value in (first, last, left)) or last < first or last - first >= 256:
            raise ValueError('Read rows in batches of at most 256')
        if right is not None and (type(right) is not int or right < left or right > 16384):
            raise ValueError('Invalid column range')
        return self.sheet(payload, lambda sheet: list(sheet.iter_rows(min_row=first, max_row=last,
            min_col=left, max_col=right, values_only=True)))

    def export(self, payload):
        from zain_checker.executive_reporter import export_executive_workbook
        import gc
        with self.work_slot():
            destination = self.path(payload['path'])
            records = payload['records']
            if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
                raise ValueError('Invalid report records')
            result = export_executive_workbook(records, destination, is_repair=payload.get('is_repair',False),
                mawarid_format=payload.get('mawarid_format',False), min_diff_threshold=payload.get('min_diff_threshold',0.20))
            # Workbook/cell graphs contain cycles. Reclaim the finalized report
            # before the next large command reaches memory admission.
            gc.collect()
            return result

    def update(self, payload):
        from zain_checker.sheet_updater import update_source_workbook_after_job
        with self.work_slot():
            path = self.path(payload['path'])
            # Commerce inputs are immutable even if a caller forgets its guard.
            if path.is_relative_to(self.root / '.commerce' / 'inputs'):
                raise ValueError('Frozen commerce input cannot be updated')
            return update_source_workbook_after_job(path, payload['records'],
                sheet_index=payload.get('sheet_index',0), is_repair_session=payload.get('is_repair_session',False),
                has_headers=payload.get('has_headers',True))

    def audit(self, payload, kind):
        from domain.results_exporter import ensure_audit_workbook_exists, append_error_record, append_mismatch_record
        from domain.models import Customer, Mismatch, CheckError
        with self.work_slot():
            path = self.path(payload['path'])
            if kind == 'ensure':
                ensure_audit_workbook_exists(path)
            elif kind == 'mismatch':
                data = dict(payload['record'])
                customer = data['customer']
                data['customer'] = Customer(**customer) if isinstance(customer, dict) else customer
                if isinstance(data.get('detected_at'), str):
                    data['detected_at'] = datetime.fromisoformat(data['detected_at'])
                append_mismatch_record(path, Mismatch(**data))
            else:
                data = dict(payload['record'])
                if isinstance(data.get('detected_at'), str):
                    data['detected_at'] = datetime.fromisoformat(data['detected_at'])
                append_error_record(path, CheckError(**data))
            return {'saved': True}

    def stream(self, payload):
        """Idempotent preview appends; WAL/checkpoint truth belongs to engine."""
        import queue
        from zain_checker.async_pipeline_engine import AsyncExcelWriterThread
        with self.work_slot():
            path = self.path(payload['path'])
            key = str(path)
            if key not in self.streams:
                # Evict before allocating the next large preview, avoiding two
                # complete workbook object graphs during sequential jobs.
                while self.streams:
                    _, (old, _) = self.streams.popitem(last=False)
                    old.wb.close()
                import gc
                gc.collect()
                writer = AsyncExcelWriterThread(queue.Queue(), path, path.with_suffix('.unused-wal'), wal_already_persisted=True)
                writer._init_workbook_and_wal()
                seen = {str(row[0]) for row in writer.ws.iter_rows(min_row=2, max_col=1, values_only=True)}
                self.streams[key] = (writer, seen)
                # One engine executes one sheet at a time. Keeping previous
                # large XLSX object graphs resident would waste VPS memory.
            writer, seen = self.streams[key]
            self.streams.move_to_end(key)
            for record in payload['records']:
                identity = str(record['row'])
                if identity not in seen:
                    writer._process_single_record(record)
                    seen.add(identity)
            if writer.unflushed_count:
                atomic_workbook_save(writer.wb, path)
                writer.unflushed_count = 0
            return {'rows': writer.total_saved}

    def start(self):
        pass

    def stop(self):
        with self.lock:
            for writer, _ in self.streams.values():
                writer.wb.close()
            self.streams.clear()

    def health(self):
        return {'state': 'ready', 'streams': len(self.streams), 'workbook_owner': 'files'}

    def handlers(self):
        return {'workbook.names': self.names, 'sheet.schema': self.schema, 'sheet.validate': self.validate,
                'sheet.extract': self.extract, 'sheet.records': self.records, 'sheet.quote_rows': self.quote_rows,
                'sheet.dimensions': self.dimensions, 'sheet.rows': self.rows,
                'report.export': self.export, 'source.update': self.update, 'stream.append': self.stream,
                'audit.ensure': lambda payload: self.audit(payload, 'ensure'),
                'audit.mismatch': lambda payload: self.audit(payload, 'mismatch'),
                'audit.error': lambda payload: self.audit(payload, 'error')}


def build(root):
    return FileApplication(root)


def handlers(root):
    return build(root).handlers()
