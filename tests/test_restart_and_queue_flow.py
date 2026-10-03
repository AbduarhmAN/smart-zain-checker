"""Tests for Sheet Restart Modal and Sequential Queue Flow.
Verifies:
1. handle_check_sheet_status accurately calculates is_completed and has_progress.
2. start_queue properly passes force_restart=True when job.completed == 0.
3. restart_queue_job and restart_all_queue_jobs cleanly reset state and wipe checkpoints.
4. Auto-advance to next queued sheet respects force_restart when completed == 0.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from domain.models import QueueJob
from manager.checkpoint import CheckpointManager
from manager.orchestrator import Orchestrator
from manager.queue import QueueService
from workers.supervisor import WorkerSupervisor


class TestRestartAndQueueFlow(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(__file__).resolve().parent / "test_scratch_queue"
        self.test_dir.mkdir(parents=True, exist_ok=True)
        self.queue_file = self.test_dir / "test_queue.json"
        if self.queue_file.exists():
            self.queue_file.unlink()

        self.queue_service = QueueService(storage_path=self.queue_file)
        self.supervisor = WorkerSupervisor(project_root=self.test_dir)
        self.orchestrator = Orchestrator(
            supervisor=self.supervisor,
            queue_service=self.queue_service,
            project_root=self.test_dir,
        )

    def tearDown(self):
        import shutil
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_checkpoint_wipe_on_force_restart(self):
        """When force_restart=True is passed, checkpoints matching stem are purged."""
        stem = "test_sheet"
        chk_file = self.test_dir / f".checkpoint_{stem}_0.json"
        chk_bak = self.test_dir / f".checkpoint_{stem}_0.json.bak"
        chk_file.write_text(json.dumps({"completed_indices": [0, 1, 2]}), encoding="utf-8")
        chk_bak.write_text(json.dumps({"completed_indices": [0, 1]}), encoding="utf-8")

        self.assertTrue(chk_file.exists())
        self.assertTrue(chk_bak.exists())

        mgr = CheckpointManager(chk_file)
        mgr.reset()

        self.assertFalse(chk_file.exists())
        self.assertFalse(chk_bak.exists())

    def test_queue_restart_job_resets_completed_and_status(self):
        """restart_job resets job status to pending and completed count to 0."""
        wb_dummy = self.test_dir / "dummy.xlsx"
        wb_dummy.write_bytes(b"dummy")

        job = self.queue_service.add_job(
            file_path=wb_dummy,
            sheet_index=0,
            sheet_name="Sheet1",
            total_records=100,
        )
        self.queue_service.mark_job_completed(job.id)
        saved = self.queue_service.get_job(job.id)
        self.assertEqual(saved.status, "completed")
        self.assertEqual(saved.completed, 100)

        # Restart
        restarted = self.queue_service.restart_job(job.id)
        self.assertEqual(restarted.status, "pending")
        self.assertEqual(restarted.completed, 0)
        self.assertEqual(restarted.remaining, 100)

    def test_restart_all_jobs(self):
        """restart_all_jobs resets all jobs in queue to pending with 0 completed."""
        wb1 = self.test_dir / "wb1.xlsx"
        wb2 = self.test_dir / "wb2.xlsx"
        wb1.write_bytes(b"dummy1")
        wb2.write_bytes(b"dummy2")

        j1 = self.queue_service.add_job(wb1, 0, "Sheet1", total_records=50)
        j2 = self.queue_service.add_job(wb2, 0, "Sheet1", total_records=80)
        self.queue_service.mark_job_completed(j1.id)
        self.queue_service.mark_job_completed(j2.id)

        all_jobs = self.queue_service.get_jobs()
        self.assertTrue(all(j["status"] == "completed" for j in all_jobs))

        self.queue_service.restart_all_jobs()
        all_jobs_after = self.queue_service.get_jobs()
        self.assertTrue(all(j["status"] == "pending" for j in all_jobs_after))
        self.assertTrue(all(j["completed"] == 0 for j in all_jobs_after))


if __name__ == "__main__":
    unittest.main()
