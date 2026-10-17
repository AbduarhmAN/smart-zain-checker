"""Exercise capture, restart, delivery uncertainty and settlement ordering offline.

All filesystem fixtures are retained. Telegram acknowledgements are synthetic.
"""
import concurrent.futures
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4
from unittest.mock import Mock,patch
from telegram_bot.commerce.bridge import CommerceBridge,file_sha256
from telegram_bot.commerce.outbox import Outbox
from telegram_bot.notifier import TelegramNotifier


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='zain-bridge-retained-'))
        self.result = self.root/'result.xlsx'
        self.result.write_bytes(b'PK\x03\x04synthetic-result')
        self.order = str(uuid4())
        self.client = Mock()
        self.client.rpc.return_value = {'ok':True}
        self.bridge = CommerceBridge(self.root,client=self.client)
        self.payload = {'commerce_order_id':self.order,'job_id':'test-job','telegram_chat_id':123,
            'result_file':str(self.result),'total':3,'commerce_records':[
                {'row':2,'status':'match','live_sar':0},
                {'row':3,'status':'not_found'}, {'row':4,'status':'error'}]}
        self.message = {'chat':{'id':123},'message_id':456,'document':{'file_id':'synthetic-file'}}
        self.bot = TelegramNotifier(bot_token='test-only',chat_id=999)
        self.bot.commerce_bridge = self.bridge
        self.network = patch('urllib.request.urlopen',side_effect=AssertionError('No live network'))
        self.urlopen = self.network.start()
        self.addCleanup(self.network.stop)

    def _ack(self):
        self.urlopen.side_effect = None
        self.urlopen.return_value = io.BytesIO(json.dumps({'ok':True,'result':self.message}).encode())

    def _send(self):
        self.bot._on_session_completed('session_completed',self.payload)
        self.bot._delivery_queue.join()

    def _drain(self,bridge=None):
        bridge = bridge or self.bridge
        for _ in range(10):
            if not bridge.worker.run_once(): break

    def test_completion_alone_cannot_settle(self):
        self.bridge.capture_completion(self.payload)
        self._drain()
        self.assertEqual([c.args[0] for c in self.client.rpc.call_args_list],
                         ['bind_checker_job','record_terminal_results'])
        self.assertEqual(self.bridge.outbox.status(),{'done':2})

    def test_snapshot_is_independent_of_original_reexport(self):
        completion = self.bridge.capture_completion(self.payload)
        self.result.write_bytes(b'other export')
        self.assertEqual(file_sha256(completion['result_file']),completion['sha256'])
        self._ack();self._send()
        self.assertEqual(self.bridge.outbox.delivery_state(self.order),'confirmed')

    def test_receipt_replay_and_restart_keep_rpc_order(self):
        completion = self.bridge.capture_completion(self.payload)
        self.bridge.capture_delivery(completion,self.message)
        restarted = CommerceBridge(self.root,client=self.client)
        restarted.recover()
        restarted.capture_delivery(completion,self.message)
        self._drain(restarted)
        self.assertEqual([c.args[0] for c in self.client.rpc.call_args_list],
            ['bind_checker_job','record_terminal_results','confirm_result_delivery','settle_order'])
        self.assertEqual(restarted.outbox.status(),{'done':4})
        self.assertEqual(restarted.outbox.delivery_state(self.order),'confirmed')

    def test_durable_receipt_recovers_enqueue_storage_failure(self):
        completion = self.bridge.capture_completion(self.payload)
        with patch.object(self.bridge,'_enqueue_receipt',side_effect=sqlite3.OperationalError('locked')):
            with self.assertRaises(sqlite3.Error):self.bridge.capture_delivery(completion,self.message)
        self.assertTrue(self.bridge._recovery_needed.is_set())
        self.assertTrue((self.bridge.storage/f'receipt_{self.order}.json').exists())
        self.bridge.recover();self._drain()
        self.assertFalse(self.bridge._recovery_needed.is_set())
        self.assertEqual(self.bridge.outbox.status(),{'done':4})

    def test_completion_spool_recovers_enqueue_failure(self):
        with patch.object(self.bridge,'_enqueue_completion',side_effect=sqlite3.OperationalError('locked')):
            with self.assertRaises(sqlite3.Error):self.bridge.capture_completion(self.payload)
        self.assertTrue(self.bridge._recovery_needed.is_set())
        self.bridge.recover();self._drain()
        self.assertEqual(self.bridge.outbox.status(),{'done':2})

    def test_incomplete_rows_and_outside_result_refused(self):
        for payload in [dict(self.payload,total=4),dict(self.payload,result_file=__file__)]:
            with self.assertRaises(ValueError):self.bridge.capture_completion(payload)
        self.assertEqual(self.bridge.outbox.status(),{})

    def test_wrong_owner_or_uncaptured_completion_never_enqueues_settlement(self):
        completion = self.bridge.capture_completion(self.payload)
        wrong = dict(self.message,chat={'id':999})
        with self.assertRaises(ValueError):self.bridge.capture_delivery(completion,wrong)
        with self.assertRaises(ValueError):self.bridge.capture_delivery(dict(completion,job_id='other'),self.message)
        self.assertEqual(self.bridge.outbox.status(),{'pending':2})

    def test_changed_receipt_refused(self):
        completion = self.bridge.capture_completion(self.payload)
        self.bridge.capture_delivery(completion,self.message)
        with self.assertRaises(ValueError):
            self.bridge.capture_delivery(completion,dict(self.message,message_id=999))

    def test_repeated_completion_sends_one_document_to_owner(self):
        self._ack();self._send();self._send()
        self.assertEqual(self.urlopen.call_count,1)
        self.assertIn(b'name="chat_id"\r\n\r\n123',self.urlopen.call_args.args[0].data)
        self.assertEqual(self.bridge.outbox.delivery_state(self.order),'confirmed')
        self.assertEqual(self.bridge.outbox.status(),{'pending':4})

    def test_duplicate_queued_events_claim_only_at_upload(self):
        operations = []
        self._ack()
        with patch.object(self.bot,'_submit_delivery',side_effect=lambda op,wait: operations.append(op) or True):
            self.bot._on_session_completed('session_completed',self.payload)
            self.bot._on_session_completed('session_completed',self.payload)
        self.assertIsNone(self.bridge.outbox.delivery_state(self.order))
        self.assertEqual(len(operations),2)
        self.assertTrue(operations[0]());self.assertFalse(operations[1]())
        self.assertEqual(self.urlopen.call_count,1)

    def test_timeout_never_auto_resends_or_settles_after_restart(self):
        self.urlopen.side_effect = TimeoutError()
        self._send()
        restarted = CommerceBridge(self.root,client=self.client)
        restarted.recover();self.bot.commerce_bridge = restarted
        self._send();self._drain(restarted)
        self.assertEqual(self.urlopen.call_count,1)
        self.assertEqual(restarted.outbox.delivery_state(self.order),'ambiguous')
        self.assertEqual(restarted.outbox.status(),{'done':2})

    def test_confirmed_rejection_permits_new_attempt(self):
        self.urlopen.side_effect = None
        self.urlopen.return_value = io.BytesIO(json.dumps({'ok':False,'error_code':403}).encode())
        self._send()
        self.assertEqual(self.bridge.outbox.delivery_state(self.order),'rejected')
        self._ack();self._send()
        self.assertEqual(self.urlopen.call_count,2)
        self.assertEqual(self.bridge.outbox.delivery_state(self.order),'confirmed')

    def test_receipt_failure_after_ack_is_ambiguous_and_not_retried(self):
        self._ack()
        with patch.object(self.bridge,'capture_delivery',side_effect=OSError('storage unavailable')):
            self._send()
        self._send()
        self.assertEqual(self.urlopen.call_count,1)
        self.assertEqual(self.bridge.outbox.delivery_state(self.order),'ambiguous')
        self.assertEqual(self.bridge.outbox.status(),{'pending':2})

    def test_corrupted_snapshot_never_uploads_or_claims(self):
        completion = self.bridge.capture_completion(self.payload)
        Path(completion['result_file']).write_bytes(b'corrupted')
        self._send()
        self.urlopen.assert_not_called()
        self.assertIsNone(self.bridge.outbox.delivery_state(self.order))

    def test_only_one_process_claims_delivery_and_crash_is_not_reclaimed(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            tokens = list(pool.map(lambda _:Outbox(self.bridge.outbox.path).claim_delivery(self.order),range(16)))
        self.assertEqual(sum(t is not None for t in tokens),1)
        self.assertIsNone(Outbox(self.bridge.outbox.path).claim_delivery(self.order))
        self.assertFalse(self.bridge.outbox.delivery_failed(self.order,'stale',rejected=True))

    def _completed_queue(self,*,signature='input-v1',row_signature='input-v1'):
        checkpoint = '.checkpoint_test_0_test-job.json'
        (self.root/checkpoint).write_text(json.dumps({'input_signature':signature}),encoding='utf-8')
        records = [dict(row,input_signature=row_signature) for row in self.payload['commerce_records']]
        (self.root/'.wal_test_0_test-job.jsonl').write_text(
            '\n'.join(json.dumps(row) for row in records),encoding='utf-8')
        queue = Mock()
        queue.get_jobs.return_value = [{'id':'test-job','checkpoint_file':checkpoint,
            'status':'completed','result_file':self.result.name,'total_records':3,
            'column_mapping':{'commerce_order_id':self.order,'telegram_chat_id':123}}]
        return queue

    def test_lost_completion_event_recovers_from_own_job_journal(self):
        bridge = CommerceBridge(self.root,client=self.client,queue_service=self._completed_queue())
        bridge.recover();self._drain(bridge)
        self.assertEqual(bridge.outbox.status(),{'done':2})
        self.assertEqual(bridge.completion_for(self.order)['rows'],[
            {'row_number':2,'state':'succeeded','verified_result':True},
            {'row_number':3,'state':'not_found','verified_result':True},
            {'row_number':4,'state':'failed','verified_result':False}])

    def test_other_input_journal_cannot_trigger_financial_completion(self):
        bridge = CommerceBridge(self.root,client=self.client,
            queue_service=self._completed_queue(row_signature='other-input'))
        bridge.recover()
        self.assertEqual(bridge.outbox.status(),{})
        self.assertIsNone(bridge.completion_for(self.order))
        self.assertEqual(bridge.last_error,'completed_job_recovery_requires_review')

    def test_disabled_notifications_do_not_claim_and_can_resume(self):
        self.bridge.capture_completion(self.payload)
        self.bot.stop_listening();self._ack()
        self.bot.recover_commerce_deliveries()
        self.assertIsNone(self.bridge.outbox.delivery_state(self.order))
        self.bot._delivery_disabled.clear()
        self.bot.recover_commerce_deliveries();self.bot._delivery_queue.join()
        self.assertEqual(self.urlopen.call_count,1)
        self.assertEqual(self.bridge.outbox.delivery_state(self.order),'confirmed')

    def test_competing_completion_captures_cannot_overwrite_identity(self):
        def capture(index):
            bridge = CommerceBridge(self.root,client=self.client)
            try:
                result = bridge.capture_completion(dict(self.payload,job_id=f'competing-{index%2}'))
                return result['job_id']
            except ValueError:return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            captured = [value for value in pool.map(capture,range(16)) if value is not None]
        self.assertTrue(captured)
        self.assertEqual(len(set(captured)),1)
        self.assertEqual(self.bridge.completion_for(self.order)['job_id'],captured[0])


if __name__ == '__main__':unittest.main()
