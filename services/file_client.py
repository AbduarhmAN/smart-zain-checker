"""Explicit workbook boundary; callers hold descriptors, never worksheet objects."""
from __future__ import annotations

import os
from pathlib import Path
import secrets


def remote_files_enabled():
    return os.environ.get('ZAIN_SERVICE_MODE') == '1' and os.environ.get('ZAIN_SERVICE_ROLE') not in {'files', 'file'}


def file_rpc(operation, payload, *, root=None, request_id=None, timeout=None):
    from services.ipc import RpcClient, ServiceUnavailable
    import time
    root = Path(root or os.environ.get('ZAIN_PROJECT_ROOT') or Path(__file__).resolve().parents[1])
    client = RpcClient(service='files', root=root)
    identifier = request_id or 'files-' + secrets.token_hex(16)
    timeout = timeout if timeout is not None else (300 if operation == 'report.export' else 120)
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ServiceUnavailable('File operation remains unconfirmed; request ' + identifier)
        try:
            return client.call(operation, payload, request_id=identifier, timeout=min(120, remaining))
        except ServiceUnavailable:
            # A command accepted before a timeout can still be running. Observe
            # the same ID, never launch another workbook mutation. Fail quickly
            # when the independent process is actually unavailable.
            client.health(timeout=min(2, remaining))
            if time.monotonic() >= deadline:
                raise
            time.sleep(.1)


def atomic_workbook_save(workbook, destination):
    """Publish a complete ZIP using a sibling rename; retain the old file on error."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    pending = destination.with_name('.' + destination.stem + '.' + secrets.token_hex(12) + '.pending' + destination.suffix)
    try:
        workbook.save(pending)
        # Windows FlushFileBuffers requires a writable descriptor.
        with pending.open('r+b') as stream:
            os.fsync(stream.fileno())
        os.replace(pending, destination)
        if os.name != 'nt':
            descriptor = os.open(destination.parent, os.O_DIRECTORY | os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        pending.unlink(missing_ok=True)


class RemoteCell:
    __slots__ = ('value',)

    def __init__(self, value):
        self.value = value


class RemoteSheet:
    """Read-only worksheet reference. Expensive operations execute in File Service."""
    def __init__(self, path, index, title, *, root=None):
        self.path, self.index, self.title, self.root = Path(path), index, title, root
        self._dimensions = None

    def rpc(self, operation, **options):
        return file_rpc('sheet.' + operation, {'path': self.path, 'index': self.index, **options}, root=self.root)

    @property
    def max_row(self):
        if self._dimensions is None:
            self._dimensions = self.rpc('dimensions')
        return self._dimensions['max_row']

    @property
    def max_column(self):
        if self._dimensions is None:
            self._dimensions = self.rpc('dimensions')
        return self._dimensions['max_column']

    def cell(self, row, column):
        rows = self.rpc('rows', min_row=row, max_row=row, min_col=column, max_col=column)
        return RemoteCell(rows[0][0] if rows and rows[0] else None)

    def iter_rows(self, min_row=1, max_row=None, min_col=1, max_col=None, values_only=True):
        end = self.max_row if max_row is None else min(max_row, self.max_row)
        for first in range(min_row, end + 1, 256):
            rows = self.rpc('rows', min_row=first, max_row=min(end, first + 255), min_col=min_col, max_col=max_col)
            for values in rows:
                yield tuple(values) if values_only else tuple(RemoteCell(value) for value in values)


class RemoteWorkbook:
    def __init__(self, path, *, root=None):
        self.path, self.root = Path(path), root
        self.sheetnames = file_rpc('workbook.names', {'path': self.path}, root=root)
        self._sheets = [RemoteSheet(self.path, index, name, root=root) for index, name in enumerate(self.sheetnames)]

    @property
    def worksheets(self):
        return self._sheets

    @property
    def active(self):
        return self._sheets[0]

    def __getitem__(self, key):
        return self._sheets[key if isinstance(key, int) else self.sheetnames.index(key)]

    def close(self):
        pass


def remote_sheet(sheet):
    return isinstance(sheet, RemoteSheet)


class RemoteStreamWriter:
    """Queue consumer delegating XLSX batches; engine's WAL remains authoritative."""
    @staticmethod
    def run(writer):
        import logging
        import queue
        import time
        logger = logging.getLogger('FileServiceStream')
        batch = []
        stopping = False
        last_flush = time.monotonic()
        while True:
            try:
                item = writer.result_queue.get(timeout=0.2)
            except queue.Empty:
                item = None
            if item == 'STOP_SENTINEL':
                stopping = True
            elif item is not None:
                records = item if isinstance(item, list) else [item]
                if not writer.wal_already_persisted:
                    # Standalone pipeline callers still own their WAL in the
                    # engine process; acknowledge locally before file IPC.
                    from services.codec import dumps
                    writer.wal_path.parent.mkdir(parents=True, exist_ok=True)
                    with writer.wal_path.open('a', encoding='utf-8') as stream:
                        for record in records:
                            stream.write(dumps(record) + '\n')
                        stream.flush()
                        os.fsync(stream.fileno())
                batch.extend(records)
            if writer.is_stopping:
                stopping = True
            if batch and (stopping or len(batch) >= writer.batch_size or time.monotonic() - last_flush >= writer.flush_interval_seconds):
                # A lost acknowledgement can be retried with exactly the same key.
                request_id = 'stream-' + secrets.token_hex(16)
                payload = {'path': writer.result_path, 'records': batch}
                for attempt in range(3):
                    try:
                        saved = file_rpc('stream.append', payload, request_id=request_id)
                        writer.total_saved = saved['rows']
                        break
                    except Exception as exc:
                        if attempt == 2:
                            # This is only a preview: final export is rebuilt from
                            # engine WAL/checkpoints, never from this workbook.
                            logger.warning('Stream preview unavailable; authoritative WAL retained (%s)', type(exc).__name__)
                        else:
                            time.sleep(0.25 * (attempt + 1))
                batch = []
                last_flush = time.monotonic()
            if stopping and writer.result_queue.empty():
                break
        writer.is_stopping = True
