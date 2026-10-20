"""Prepare a bounded retention review. Never expires payments or removes files."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

POLICY_PATH = Path(__file__).with_name('retention_policy.json')
FINAL_PAYMENTS = {'settled', 'partial_refund', 'refunded'}


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError('Explicit timestamp required')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Timezone required')
    return parsed.timestamp()


def policy():
    value = json.loads(POLICY_PATH.read_text(encoding='utf-8'))
    if (value.get('version') != 1 or value.get('mode') != 'review_only'
            or value.get('automatic_deletion') is not False
            or value.get('payment_metadata_hours') != 20 or value.get('completed_file_hours') != 24
            or any(value.get(key) is not True for key in
                   ('preserve_financial_records', 'protect_unfinished_orders', 'require_confirmed_archive'))):
        raise ValueError('Only the approved non-destructive review policy is supported')
    return value


def settled(order):
    return (order.get('execution_state') == 'terminal' and order.get('delivery_state') == 'confirmed'
            and order.get('payment_state') in FINAL_PAYMENTS and order.get('financial_completed_at')
            and order.get('final_charge_minor') is not None)


def archive_ok(order, kind, digest, now):
    if not settled(order):
        return False
    for item in order.get('archives', []):
        if item.get('kind') != kind or item.get('state') != 'confirmed' or item.get('sha256') != digest:
            continue
        if not item.get('file_id') or not item.get('message_id') or item.get('file_size') is None:
            continue
        try:
            if timestamp(item['retention_until']) > now:
                return True
        except (ValueError, KeyError, TypeError):
            pass
    return False


def review(inventory, snapshot, *, now=None):
    cfg = policy()
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    observed = timestamp(snapshot['observed_at'])
    if not -60 <= now - observed <= 300 or snapshot.get('complete') is not True:
        raise ValueError('A complete fresh server snapshot is required')
    if not -60 <= now - timestamp(inventory['observed_at']) <= 300:
        raise ValueError('A fresh local inventory is required')
    if inventory.get('complete') is not True:
        raise ValueError('Incomplete local inventory')
    orders = snapshot['orders']
    if len({item['order_id'] for item in orders}) != len(orders):
        raise ValueError('Duplicate server order identity')
    by_id = {item['order_id']: item for item in orders}
    inputs = {}
    for order in orders:
        digest = order.get('input_sha256')
        if digest:
            inputs.setdefault(digest, []).append(order)
    # Local references survive an unacknowledged/unfinished server operation.
    protected = set()
    references_complete = inventory.get('references_complete') is True
    for job in inventory.get('jobs', []):
        mapping = job.get('column_mapping') or {}
        order = by_id.get(mapping.get('commerce_order_id'))
        if job.get('status') not in {'completed', 'archived', 'cancelled'} or not order or not settled(order):
            for key in ('filename', 'result_file', 'checkpoint_file'):
                if job.get(key):
                    protected.add(job[key])
            if mapping.get('commerce_input'):
                protected.add(mapping['commerce_input'])
    for draft in inventory.get('drafts', []):
        order = by_id.get(draft.get('commerce_order_id'))
        if not order or not settled(order):
            for key in ('path', 'commerce_input'):
                if draft.get(key):
                    protected.add(draft[key])
    candidates, blocked = [], []
    for item in inventory['files']:
        name = item['path']
        posix = PurePosixPath(name)
        original = re.fullmatch(r'\.commerce/inputs/([a-f0-9]{64})\.xlsx', name)
        result = re.fullmatch(r'\.commerce/result_([0-9a-f-]{36})\.xlsx', name)
        if not original and not result:
            continue
        reason = None
        digest = original[1] if original else None
        refs = inputs.get(digest, []) if original else [by_id.get(result[1])]
        if posix.is_absolute() or '..' in posix.parts or item.get('unsafe'):
            reason = 'unsafe_path'
        elif not references_complete:
            reason = 'local_references_unavailable'
        elif name in protected or item.get('absolute_path') in protected:
            reason = 'local_request_still_references_file'
        elif not refs or any(order is None for order in refs):
            reason = 'unknown_server_reference'
        else:
            for order in refs:
                expected = order.get('input_sha256') if original else order.get('result_sha256')
                kind = 'original' if original else 'result'
                if not settled(order):
                    reason = 'execution_delivery_or_settlement_incomplete'
                elif not expected or item.get('sha256') != expected:
                    reason = 'content_hash_unverified'
                else:
                    try:
                        completed = timestamp(order['financial_completed_at'])
                        if completed > now or now - completed < cfg['completed_file_hours'] * 3600:
                            reason = 'completed_less_than_24_hours_ago'
                        elif not archive_ok(order, kind, expected, now):
                            reason = 'matching_archive_not_confirmed_or_expired'
                    except (ValueError, TypeError):
                        reason = 'completion_time_unavailable'
                if reason:
                    break
        if not reason and (type(item.get('mtime')) not in (int, float) or now - item['mtime'] < 86400):
            reason = 'file_created_or_changed_within_24_hours'
        if reason:
            blocked.append({'path': name, 'reason': reason, 'bytes': item['bytes']})
        else:
            candidates.append({'path': name, 'bytes': item['bytes'], 'sha256': item['sha256'],
                               'order_ids': [order['order_id'] for order in refs],
                               'link_count': item.get('link_count'), 'reason': 'settled_archived_and_older_than_24_hours'})
    # Keep a compact financial tombstone; never suggest removing receipts,
    # wallet journals, transaction IDs, idempotency keys or owner identity.
    payment_actions, protected_payments = [], []
    for token, payment in inventory.get('payments', {}).items():
        label = hashlib.sha256(token.encode()).hexdigest()[:12]
        draft = payment.get('draft_info') or {}
        order = by_id.get(draft.get('commerce_order_id'))
        status = payment.get('status')
        try:
            base = (order.get('financial_completed_at') if order else None) if status == 'fulfilled' else (
                payment.get('resolved_at') if status == 'rejected' else payment.get('created_at'))
            age = now - timestamp(base)
        except (ValueError, KeyError, TypeError):
            protected_payments.append({'reference_hash': label, 'reason': 'payment_time_unknown'})
            continue
        if age < cfg['payment_metadata_hours'] * 3600:
            continue
        if status == 'pending' and payment.get('tx_id'):
            reason = 'submitted_transfer_requires_review'
        elif status == 'approved':
            reason = 'approved_payment_must_finish_queue_recovery'
        elif order and not settled(order):
            reason = 'related_order_not_settled'
        elif status == 'fulfilled' and not order:
            reason = 'legacy_payment_requires_settlement_evidence'
        elif status not in {'ready', 'pending', 'rejected', 'fulfilled'}:
            reason = 'unknown_payment_state'
        else:
            reason = None
        if reason:
            protected_payments.append({'reference_hash': label, 'reason': reason})
        else:
            removable = [key for key in ('rows', 'commerce_rows', 'validation', 'preview', 'mapping', 'version') if key in draft]
            payment_actions.append({'reference_hash': label, 'state': status, 'age_hours': round(age / 3600, 2),
                                    'proposed_action': 'expire_unpaid_review' if status in {'ready', 'pending'} else 'compact_closed_metadata',
                                    'removable_draft_fields': removable,
                                    'keep': 'owner, amounts, transaction references, idempotency and minimal order/input identity'})
    return {'policy': cfg, 'observed_at': datetime.fromtimestamp(now, timezone.utc).isoformat(),
            'production_modified': False, 'files_deleted': False, 'payments_changed': False,
            'candidate_files': candidates, 'blocked_files': blocked,
            'candidate_logical_bytes': sum(item['bytes'] for item in candidates),
            'physical_bytes_recoverable': None,
            'physical_size_note': 'Hard links and filesystem allocation prevent inferring reclaimed disk space from logical bytes.',
            'payment_actions': payment_actions, 'protected_payments': protected_payments,
            'limits': ['This is a review, not an executable deletion manifest.',
                       'Unknown files, local-only legacy requests, checkpoints, spools and financial databases are preserved.',
                       'All references and file identities must be rechecked at any later cleanup activation.']}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--inventory', required=True, type=Path)
    parser.add_argument('--snapshot', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    report = review(json.loads(args.inventory.read_text(encoding='utf-8')),
                    json.loads(args.snapshot.read_text(encoding='utf-8')))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps({'candidate_files': len(report['candidate_files']),
                      'candidate_logical_bytes': report['candidate_logical_bytes'],
                      'blocked_files': len(report['blocked_files']), 'payment_actions': len(report['payment_actions']),
                      'protected_payments': len(report['protected_payments']), 'files_deleted': False}))


if __name__ == '__main__':
    main()
