"""Retention selection and safety checks. Fixtures retained; no deletion runs."""
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from maintenance.retention_review import review
from maintenance.collect_retention_inventory import collect

NOW = datetime(2026, 10, 19, 12, tzinfo=timezone.utc).timestamp()
ORDER = '20000000-0000-0000-0000-000000000001'
DIGEST = 'a' * 64


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


class RetentionReviewTests(unittest.TestCase):
    def setUp(self):
        archive = {'kind': 'original', 'state': 'confirmed', 'sha256': DIGEST,
                   'file_id': 'fixture-document', 'message_id': 1, 'file_size': 50,
                   'retention_until': iso(NOW + 86400)}
        self.order = {'order_id': ORDER, 'input_sha256': DIGEST, 'result_sha256': DIGEST,
                      'execution_state': 'terminal', 'delivery_state': 'confirmed', 'payment_state': 'settled',
                      'financial_completed_at': iso(NOW - 86400), 'final_charge_minor': 10,
                      'archives': [archive, {**archive, 'kind': 'result'}]}
        self.snapshot = {'observed_at': iso(NOW), 'complete': True, 'orders': [self.order]}
        self.path = '.commerce/inputs/' + DIGEST + '.xlsx'
        self.file = {'path': self.path, 'absolute_path': '/workspace/' + self.path, 'bytes': 50,
                     'mtime': NOW - 86400, 'link_count': 2, 'sha256': DIGEST, 'unsafe': False}
        self.inventory = {'observed_at': iso(NOW), 'complete': True, 'references_complete': True,
                          'files': [self.file], 'jobs': [], 'drafts': [], 'payments': {}}

    def run_review(self):
        return review(self.inventory, self.snapshot, now=NOW)

    def test_exact_24_hour_boundary_is_reviewable_without_claiming_disk_space_recovered(self):
        result = self.run_review()
        self.assertEqual(len(result['candidate_files']), 1)
        self.assertEqual(result['candidate_logical_bytes'], 50)
        self.assertIsNone(result['physical_bytes_recoverable'])
        self.assertFalse(result['files_deleted'])
        self.assertFalse(result['payments_changed'])

    def test_planning_does_not_modify_either_input(self):
        before = copy.deepcopy((self.inventory, self.snapshot))
        self.run_review()
        self.assertEqual((self.inventory, self.snapshot), before)

    def test_old_file_is_protected_when_order_was_only_recently_settled(self):
        self.order['financial_completed_at'] = iso(NOW - 86399)
        self.assertFalse(self.run_review()['candidate_files'])

    def test_recently_reused_file_is_protected_even_after_old_settlement(self):
        self.file['mtime'] = NOW - 10
        self.assertFalse(self.run_review()['candidate_files'])

    def test_unfinished_execution_delivery_or_payment_never_qualifies(self):
        for key, value in (('execution_state', 'running'), ('execution_state', 'queued'),
                           ('delivery_state', 'pending'), ('payment_state', 'reserved')):
            with self.subTest(key=key, value=value):
                original = self.order[key]
                self.order[key] = value
                self.assertFalse(self.run_review()['candidate_files'])
                self.order[key] = original

    def test_shared_input_is_protected_if_one_order_is_running(self):
        other = {**self.order, 'order_id': '20000000-0000-0000-0000-000000000002', 'execution_state': 'running'}
        self.snapshot['orders'].append(other)
        self.assertFalse(self.run_review()['candidate_files'])

    def test_local_draft_and_active_job_references_are_protected(self):
        self.inventory['drafts'] = [{'commerce_input': '/workspace/' + self.path}]
        self.assertFalse(self.run_review()['candidate_files'])
        self.inventory['drafts'] = []
        self.inventory['jobs'] = [{'status': 'active', 'filename': '/workspace/' + self.path,
                                  'column_mapping': {'commerce_order_id': ORDER}}]
        self.assertFalse(self.run_review()['candidate_files'])

    def test_missing_archive_ambiguous_archive_or_expired_archive_is_protected(self):
        baseline = copy.deepcopy(self.order['archives'][0])
        for change in ({'state': 'pending'}, {'state': 'ambiguous'}, {'file_id': None},
                       {'retention_until': iso(NOW)}, {'sha256': 'b' * 64}):
            with self.subTest(change=change):
                self.order['archives'][0] = {**baseline, **change}
                self.assertFalse(self.run_review()['candidate_files'])

    def test_wrong_file_hash_unsafe_path_and_unknown_server_reference_are_protected(self):
        self.file['sha256'] = 'b' * 64
        self.assertFalse(self.run_review()['candidate_files'])
        self.file['sha256'] = DIGEST
        self.file['unsafe'] = True
        self.assertFalse(self.run_review()['candidate_files'])
        self.file['unsafe'] = False
        self.snapshot['orders'] = []
        self.assertFalse(self.run_review()['candidate_files'])

    def test_missing_local_reference_inventory_blocks_files(self):
        self.inventory['references_complete'] = False
        result = self.run_review()
        self.assertEqual(result['blocked_files'][0]['reason'], 'local_references_unavailable')

    def test_stale_incomplete_and_duplicate_server_snapshots_are_rejected(self):
        self.snapshot['observed_at'] = iso(NOW - 301)
        with self.assertRaises(ValueError):
            self.run_review()
        self.snapshot['observed_at'] = iso(NOW)
        self.snapshot['complete'] = False
        with self.assertRaises(ValueError):
            self.run_review()
        self.snapshot['complete'] = True
        self.snapshot['orders'].append(copy.deepcopy(self.order))
        with self.assertRaises(ValueError):
            self.run_review()

    def test_pending_payment_with_submitted_transaction_requires_review_after_20_hours(self):
        self.inventory['payments'] = {'fixture': {'created_at': iso(NOW - 72000), 'status': 'pending', 'tx_id': True}}
        result = self.run_review()
        self.assertFalse(result['payment_actions'])
        self.assertEqual(result['protected_payments'][0]['reason'], 'submitted_transfer_requires_review')

    def test_unpaid_review_at_exact_20_hour_boundary_has_only_a_non_destructive_proposal(self):
        self.inventory['payments'] = {'fixture': {'created_at': iso(NOW - 72000), 'status': 'ready',
                                                'draft_info': {'rows': True, 'validation': True}}}
        result = self.run_review()
        self.assertEqual(result['payment_actions'][0]['proposed_action'], 'expire_unpaid_review')
        self.assertIn('idempotency', result['payment_actions'][0]['keep'])
        self.assertFalse(result['payments_changed'])
        self.assertNotIn('fixture', json.dumps(result))

    def test_approved_or_unsettled_payments_do_not_have_metadata_compaction_proposals(self):
        self.inventory['payments'] = {'fixture': {'created_at': iso(NOW - 80000), 'status': 'approved'}}
        self.assertFalse(self.run_review()['payment_actions'])
        self.inventory['payments']['fixture']['status'] = 'ready'
        self.inventory['payments']['fixture']['draft_info'] = {'commerce_order_id': ORDER}
        self.order['payment_state'] = 'reserved'
        self.assertFalse(self.run_review()['payment_actions'])

    def test_rejected_and_fulfilled_payments_use_resolution_not_creation_time(self):
        self.inventory['payments'] = {'rejected': {'created_at': iso(NOW - 100000), 'resolved_at': iso(NOW - 1), 'status': 'rejected'},
                                      'fulfilled': {'created_at': iso(NOW - 100000), 'status': 'fulfilled',
                                                    'draft_info': {'commerce_order_id': ORDER}}}
        self.order['financial_completed_at'] = iso(NOW - 1)
        self.assertFalse(self.run_review()['payment_actions'])

    def test_missing_payment_time_does_not_fall_back_to_file_age(self):
        self.inventory['payments'] = {'fixture': {'status': 'fulfilled', 'created_at': iso(NOW - 100000)}}
        self.assertEqual(self.run_review()['protected_payments'][0]['reason'], 'payment_time_unknown')

    def test_result_copy_has_its_own_matching_archive_requirement(self):
        self.file['path'] = '.commerce/result_' + ORDER + '.xlsx'
        self.assertEqual(len(self.run_review()['candidate_files']), 1)
        self.order['archives'] = [self.order['archives'][0]]
        self.assertFalse(self.run_review()['candidate_files'])

    def test_collector_redacts_raw_transactions_and_rows_without_changing_files(self):
        root = Path(tempfile.mkdtemp(prefix='retention-review-retained-'))
        (root / '.zain-queue.json').write_text('[]', encoding='utf-8')
        (root / '.zain-telegram-drafts.json').write_text('{"version":1,"drafts":{}}', encoding='utf-8')
        encoded = json.dumps({'fixture': {'status': 'pending', 'tx_id': 'SECRET-TRANSFER',
                                          'draft_info': {'rows': [{'number': 'PRIVATE-NUMBER'}]}}})
        path = root / 'pending_payments.json'
        path.write_text(encoded, encoding='utf-8')
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        result = collect(root)
        self.assertTrue(result['references_complete'])
        self.assertNotIn('SECRET-TRANSFER', json.dumps(result))
        self.assertNotIn('PRIVATE-NUMBER', json.dumps(result))
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)


if __name__ == '__main__':
    unittest.main()
