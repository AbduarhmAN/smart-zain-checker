"""Unit tests for checker state and queue management."""
import unittest
import tempfile
import shutil
from pathlib import Path
from zain_checker.queue_manager import SheetQueueManager, QueueJob


class CheckerStateTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_queue_lifecycle(self):
        qm = SheetQueueManager(storage_dir=Path(self.test_dir))
        self.assertEqual(len(qm.get_jobs()), 0)

        # Add jobs
        j1 = qm.add_job(
            file_path="test1.xlsx",
            sheet_index=0,
            sheet_name="Sheet1",
            mode="smart_hybrid",
            total_records=100,
            column_mapping={"lookup_col": "L", "amount_col": "P"}
        )
        self.assertIsNotNone(j1)
        self.assertEqual(j1.status, "pending")

        j2 = qm.add_job(
            file_path="test2.xlsx",
            sheet_index=0,
            sheet_name="Sheet1",
            mode="account_only",
            total_records=200,
            column_mapping={"lookup_col": "L", "amount_col": "P"}
        )
        self.assertEqual(len(qm.get_jobs()), 2)

        # Pop next job
        next_job = qm.get_next_pending_job()
        self.assertIsNotNone(next_job)
        self.assertEqual(next_job.id, j1.id)
        
        # Mark running
        qm.mark_job_running(next_job.id)
        self.assertEqual(qm.get_job(j1.id).status, "running")

        # Mark completed
        qm.mark_job_completed(j1.id)
        self.assertEqual(qm.get_job(j1.id).status, "completed")

        # Next pending
        next_job2 = qm.get_next_pending_job()
        self.assertIsNotNone(next_job2)
        self.assertEqual(next_job2.id, j2.id)


if __name__ == "__main__":
    unittest.main()
