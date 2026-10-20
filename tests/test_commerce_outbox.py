"""Retained SQLite fixtures, no cleanup/deletion, no live network."""
import concurrent.futures
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock
from telegram_bot.commerce.outbox import Outbox,OutboxWorker


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp(prefix='zain-outbox-retained-'))/'outbox.sqlite'
        self.box = Outbox(self.path)

    def test_replay_and_conflict(self):
        self.box.enqueue('order:bind','bind_checker_job',{'p_order_id':'id','p_job_id':'job'})
        Outbox(self.path).enqueue('order:bind','bind_checker_job',{'p_job_id':'job','p_order_id':'id'})
        self.assertEqual(self.box.status(),{'pending':1})
        with self.assertRaises(ValueError): self.box.enqueue('order:bind','bind_checker_job',{'p_job_id':'other'})

    def test_dependencies_and_restart(self):
        self.box.enqueue_many([('bind','bind_checker_job',{},None),('terminal','record_terminal_results',{},'bind')])
        command = self.box.lease()
        self.assertEqual(command['key'],'bind')
        self.assertIsNone(Outbox(self.path).lease())
        self.box.complete(command,{'ok':True})
        self.assertEqual(Outbox(self.path).lease()['key'],'terminal')

    def test_expired_lease_recovered_and_stale_writer_rejected(self):
        self.box.enqueue('bind','bind_checker_job',{})
        old = self.box.lease(now=10,lease_seconds=5)
        current = Outbox(self.path).lease(now=16)
        self.assertFalse(self.box.complete(old,{'stale':True}))
        self.assertTrue(self.box.complete(current,{'ok':True}))

    def test_competing_consumers_receive_one_lease(self):
        self.box.enqueue('bind','bind_checker_job',{})
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            leases = list(executor.map(lambda _: Outbox(self.path).lease(),range(16)))
        self.assertEqual(sum(lease is not None for lease in leases),1)

    def test_transient_retry_and_permanent_block_are_visible(self):
        self.box.enqueue('bind','bind_checker_job',{})
        command = self.box.lease(now=10)
        self.box.failed(command,'network',now=10)
        self.assertIsNone(self.box.lease(now=11))
        command = self.box.lease(now=13)
        self.box.failed(command,'validation',permanent=True)
        self.assertEqual(self.box.status(),{'blocked':1})
        self.assertTrue(self.box.retry_blocked('bind'))
        self.assertIsNotNone(self.box.lease())

    def test_partial_batch_rolls_back(self):
        self.box.enqueue('bind','bind_checker_job',{'id':1})
        with self.assertRaises(ValueError):
            self.box.enqueue_many([('new','settle_order',{},None),('bind','bind_checker_job',{'id':2},None)])
        self.assertEqual(self.box.status(),{'pending':1})

    def test_worker_replays_unknown_network_result_without_losing_command(self):
        self.box.enqueue('bind','bind_checker_job',{})
        client = Mock();client.rpc.side_effect = TimeoutError('sensitive detail')
        worker = OutboxWorker(self.box,client)
        self.assertTrue(worker.run_once())
        self.assertEqual(self.box.status(),{'pending':1})
        command = self.box.lease(now=time.time()+500)
        self.assertEqual(command['attempts'],2)

    def test_corrupted_payload_is_blocked_without_rpc(self):
        self.box.enqueue('bind','bind_checker_job',{})
        connection = self.box._connect()
        try:
            connection.execute("update commands set payload='{} ' where key='bind'");connection.commit()
        finally: connection.close()
        self.assertIsNone(self.box.lease())
        self.assertEqual(self.box.status(),{'blocked':1})


if __name__ == '__main__': unittest.main()
