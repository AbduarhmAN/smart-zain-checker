"""Standalone read-only VPS/local inventory; stdout contains private references."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat


def stable_json(path, default):
    if not path.exists():
        return default
    before = path.stat()
    if before.st_size > 64 * 1024 * 1024 or path.is_symlink():
        raise ValueError('Unbounded or linked state file')
    data = json.loads(path.read_text(encoding='utf-8'))
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError('State changed during inventory; retry later')
    return data


def collect(root):
    root = Path(root).resolve()
    queue_paths = [root / name for name in ('.zain-queue.json', '.sheet_queue.json') if (root / name).is_file()]
    queue = []
    for queue_path in queue_paths:
        batch = stable_json(queue_path, [])
        if not isinstance(batch, list):
            raise ValueError('Local queue is invalid')
        queue.extend(batch)
    drafts = stable_json(root / '.zain-telegram-drafts.json', {'version': 1, 'drafts': {}})
    payments = stable_json(root / 'pending_payments.json', {})
    if (not isinstance(queue, list) or not isinstance(drafts, dict) or drafts.get('version') != 1
            or not isinstance(drafts.get('drafts'), dict) or not isinstance(payments, dict)):
        raise ValueError('Local reference state is invalid')
    # Keep only reference fields; no wallet secrets, transaction IDs or raw rows.
    jobs = []
    for job in queue:
        value = {key: job.get(key) for key in ('id', 'filename', 'result_file', 'checkpoint_file', 'status')}
        value['column_mapping'] = {key: job.get('column_mapping', {}).get(key) for key in ('commerce_order_id', 'commerce_input')}
        jobs.append(value)
    kept_drafts = [{key: draft.get(key) for key in ('path', 'commerce_input', 'commerce_order_id')}
                   for draft in drafts['drafts'].values()]
    for records, keys in ((jobs, ('filename', 'result_file', 'checkpoint_file')), (kept_drafts, ('path', 'commerce_input'))):
        for record in records:
            for key in keys:
                if record.get(key):
                    candidate = Path(record[key])
                    record[key] = str((candidate if candidate.is_absolute() else root / candidate).resolve())
    kept_payments = {}
    for token, payment in payments.items():
        value = {key: payment.get(key) for key in ('status', 'created_at', 'resolved_at')}
        value['tx_id'] = bool(payment.get('tx_id'))
        draft = payment.get('draft_info') or {}
        value['draft_info'] = {key: draft[key] for key in ('commerce_order_id', 'rows', 'commerce_rows', 'validation', 'preview', 'mapping', 'version') if key in draft}
        # Presence is enough to plan compaction; do not transmit row contents.
        value['draft_info'] = {key: item if key == 'commerce_order_id' else True for key, item in value['draft_info'].items()}
        kept_payments[token] = value
    paths = list((root / '.commerce').glob('*'))
    if (root / '.commerce').is_symlink():
        raise ValueError('Linked commerce state is unsupported')
    inputs = root / '.commerce/inputs'
    if inputs.exists() and not inputs.is_symlink():
        paths += list(inputs.glob('*'))
    if len(paths) > 100000:
        raise ValueError('Bounded inventory exceeded')
    files, digests = [], {}
    for path in paths:
        if not path.is_file() and not path.is_symlink():
            continue
        info = path.lstat()
        unsafe = path.is_symlink() or not path.resolve().is_relative_to(root) or not stat.S_ISREG(info.st_mode)
        identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        digest = None
        if not unsafe and path.suffix == '.xlsx':
            if identity not in digests:
                result = hashlib.sha256()
                with path.open('rb') as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        result.update(chunk)
                after = path.lstat()
                if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                    raise ValueError('File changed during inventory; retry later')
                digests[identity] = result.hexdigest()
            digest = digests[identity]
        files.append({'path': path.relative_to(root).as_posix(), 'absolute_path': str(path.absolute()),
                      'bytes': info.st_size, 'mtime': info.st_mtime, 'link_count': info.st_nlink,
                      'inode': info.st_ino, 'device': info.st_dev, 'sha256': digest, 'unsafe': unsafe})
    return {'observed_at': datetime.now(timezone.utc).isoformat(), 'complete': True,
            'references_complete': bool(queue_paths) and (root / '.zain-telegram-drafts.json').is_file(),
            'root': str(root), 'jobs': jobs, 'drafts': kept_drafts, 'payments': kept_payments, 'files': files,
            'files_deleted': False, 'payment_records_changed': False}


if __name__ == '__main__':
    import sys
    if len(sys.argv) != 2:
        raise SystemExit('Explicit workspace root required')
    print(json.dumps(collect(sys.argv[1]), ensure_ascii=False))
