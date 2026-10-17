"""Retained offline fixtures for paid-job persistence and concurrent retries."""
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from manager.queue import QueueService


class TelegramQueueDurabilityTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='telegram_queue_retained_'))
        self.path = self.root / 'queue.json'
        self.queue = QueueService(self.path)
        bus = patch('manager.queue.EVENT_BUS.publish')
        self.publish = bus.start()
        self.addCleanup(bus.stop)

    def add(self, token='payment-1', filename='customers.xlsx', **overrides):
        mapping = {'account_col': 1, 'owner_chat_id': 123}
        if token is not None:
            mapping['payment_token'] = token
        params = {'file_path': self.root / filename, 'sheet_index': 0,
                  'sheet_name': 'Customers', 'total_records': 10, 'column_mapping': mapping}
        params.update(overrides)
        return self.queue.add_job(**params)

    def test_successful_add_survives_reload(self):
        job = self.add()
        loaded = QueueService(self.path)
        self.assertEqual(loaded.get_jobs()[0]['id'], job.id)
        self.assertEqual(loaded.get_jobs()[0]['column_mapping']['payment_token'], 'payment-1')
        self.assertEqual(len(list(self.root.glob('queue.json.*.tmp'))), 0)

    def test_failed_replace_raises_and_rolls_back_memory_without_losing_disk(self):
        original = self.add('payment-original')
        disk_before = self.path.read_bytes()
        self.publish.reset_mock()
        with patch('manager.queue.os.replace', side_effect=PermissionError('locked')):
            with self.assertRaises(PermissionError):
                self.add('payment-new')
        self.assertEqual([job['id'] for job in self.queue.get_jobs()], [original.id])
        self.assertEqual(self.path.read_bytes(), disk_before)
        self.assertEqual(len(list(self.root.glob('queue.json.*.tmp'))), 1)
        self.publish.assert_not_called()

    def test_failed_first_add_keeps_memory_and_disk_empty(self):
        with patch('manager.queue.os.fsync', side_effect=OSError('disk error')):
            with self.assertRaises(OSError):
                self.add()
        self.assertEqual(self.queue.get_jobs(), [])
        self.assertFalse(self.path.exists())
        self.assertEqual(len(list(self.root.glob('queue.json.*.tmp'))), 1)

    def test_json_serialization_failure_does_not_enqueue_or_publish(self):
        with self.assertRaises(TypeError):
            self.add(column_mapping={'payment_token': 'p', 'invalid': object()})
        self.assertEqual(self.queue.get_jobs(), [])
        self.publish.assert_not_called()

    def test_same_payment_retry_reuses_job_and_does_not_publish_twice(self):
        first = self.add()
        retry = self.add()
        self.assertIs(retry, first)
        self.assertEqual(len(self.queue.get_jobs()), 1)
        self.assertEqual(self.publish.call_count, 1)

    def test_same_payment_after_reload_reuses_existing_job(self):
        first = self.add()
        self.queue = QueueService(self.path)
        retry = self.add()
        self.assertEqual(retry.id, first.id)
        self.assertEqual(len(self.queue.get_jobs()), 1)

    def test_concurrent_payment_retries_create_one_durable_job(self):
        barrier = threading.Barrier(8)
        def submit(_):
            barrier.wait()
            return self.add().id
        with ThreadPoolExecutor(max_workers=8) as executor:
            ids = list(executor.map(submit, range(8)))
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(len(QueueService(self.path).get_jobs()), 1)
        self.assertEqual(self.publish.call_count, 1)

    def test_distinct_payments_and_ordinary_rechecks_are_not_deduplicated(self):
        ids = [self.add('p1').id, self.add('p2').id, self.add(None).id, self.add(None).id]
        self.assertEqual(len(set(ids)), 4)
        self.assertEqual(len(QueueService(self.path).get_jobs()), 4)

    def test_concurrent_distinct_jobs_do_not_lose_any_disk_snapshot(self):
        with ThreadPoolExecutor(max_workers=6) as executor:
            ids = list(executor.map(lambda i: self.add('p-' + str(i)).id, range(20)))
        loaded_ids = {job['id'] for job in QueueService(self.path).get_jobs()}
        self.assertEqual(loaded_ids, set(ids))
        self.assertEqual(len(loaded_ids), 20)

    def test_token_collision_with_different_file_or_owner_is_rejected(self):
        original = self.add()
        with self.assertRaises(ValueError):
            self.add(filename='other.xlsx')
        with self.assertRaises(ValueError):
            self.add(column_mapping={'account_col': 1, 'owner_chat_id': 456, 'payment_token': 'payment-1'})
        self.assertEqual([job['id'] for job in self.queue.get_jobs()], [original.id])

    def test_column_mapping_is_snapshot_not_callers_mutable_reference(self):
        mapping = {'payment_token': 'payment-1', 'nested': {'owner': 123}}
        job = self.add(column_mapping=mapping)
        mapping['nested']['owner'] = 456
        self.assertEqual(job.column_mapping['nested']['owner'], 123)

    def test_explicit_fork_creates_fresh_round_without_reusing_invoice(self):
        source = self.add()
        fork = self.queue.fork_job(source.id)
        self.assertNotEqual(fork.id, source.id)
        self.assertNotIn('payment_token', fork.column_mapping)
        self.assertEqual(source.column_mapping['payment_token'], 'payment-1')
        self.assertEqual(source.status, 'archived')

    def test_price_update_failure_preserves_old_disk_and_price(self):
        job = self.add(pricing={'mode': 'sheet', 'unit_price': '30'})
        old_bytes = self.path.read_bytes()
        with patch('manager.queue.os.replace', side_effect=OSError('locked')):
            with self.assertRaises(OSError):
                self.queue.update_job_pricing(job.id, {'mode': 'sheet', 'unit_price': '50'})
        self.assertEqual(job.pricing['unit_minor'], 3000)
        self.assertEqual(self.path.read_bytes(), old_bytes)

    def test_mapping_edit_preserves_paid_invoice_and_owner_metadata(self):
        job = self.add(column_mapping={'account_col': 1, 'payment_token': 'telegram:123:p1',
                                       'telegram_chat_id': 123})
        mapping = {'account_col': 2, 'remaining_col': 3}
        self.assertTrue(self.queue.update_pending_mapping(job.id, column_mapping=mapping,
            mode='account_only', amount_target='remaining', total_records=12))
        self.assertEqual(job.column_mapping['payment_token'], 'telegram:123:p1')
        self.assertEqual(job.column_mapping['telegram_chat_id'], 123)
        self.assertEqual(job.column_mapping['account_col'], 2)
        self.assertNotIn('payment_token', mapping)
        loaded = QueueService(self.path).get_jobs()[0]
        self.assertEqual(loaded['column_mapping']['telegram_chat_id'], 123)
        self.assertEqual(loaded['total_records'], 12)

    def test_mapping_edit_rejects_paid_invoice_or_owner_replacement(self):
        job = self.add(column_mapping={'account_col': 1, 'payment_token': 'telegram:123:p1',
                                       'telegram_chat_id': 123})
        original = job.to_dict()
        disk_before = self.path.read_bytes()
        self.publish.reset_mock()
        for conflict in ({'payment_token': 'other'}, {'telegram_chat_id': 456},
                         {'payment_token': None}, {'telegram_chat_id': None}):
            with self.subTest(conflict=conflict), self.assertRaises(ValueError):
                self.queue.update_pending_mapping(job.id, column_mapping={'account_col': 2, **conflict},
                    mode='account_only', amount_target='remaining', total_records=12)
        self.assertEqual(job.to_dict(), original)
        self.assertEqual(self.path.read_bytes(), disk_before)
        self.publish.assert_not_called()

    def test_failed_mapping_save_rolls_back_all_fields_and_publishes_no_event(self):
        job = self.add(column_mapping={'account_col': 1, 'payment_token': 'telegram:123:p1',
                                       'telegram_chat_id': 123})
        original = job.to_dict()
        disk_before = self.path.read_bytes()
        self.publish.reset_mock()
        with patch('manager.queue.os.replace', side_effect=OSError('locked')):
            with self.assertRaises(OSError):
                self.queue.update_pending_mapping(job.id, column_mapping={'account_col': 2},
                    mode='account_only', amount_target='remaining', total_records=12)
        self.assertEqual(job.to_dict(), original)
        self.assertEqual(self.path.read_bytes(), disk_before)
        self.publish.assert_not_called()


if __name__ == '__main__':
    unittest.main()
