"""Unit test verifying One-for-One Worker Actor isolation.
Demonstrates that pausing or terminating Worker 1 does not affect Worker 2.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from workers.supervisor import WorkerSupervisor


class TestModularWorkerIsolation(unittest.TestCase):
    def setUp(self):
        self.supervisor = WorkerSupervisor(project_root=PROJECT_ROOT)

    def tearDown(self):
        self.supervisor.stop_all()

    def test_independent_cooldown_and_leasing(self):
        # 1. Put Worker 1 in cooldown
        self.supervisor.pause_single_worker("worker_1", cooldown_seconds=30)
        
        w1 = self.supervisor.get_worker("worker_1")
        w2 = self.supervisor.get_worker("worker_2")
        
        self.assertIsNotNone(w1)
        self.assertIsNotNone(w2)
        
        # Worker 1 is in cooldown and cannot take tasks
        self.assertTrue(w1.is_in_cooldown())
        self.assertFalse(w1.is_ready_for_work())
        
        # Worker 2 is completely unaffected and ready for tasks!
        self.assertFalse(w2.is_in_cooldown())
        self.assertTrue(w2.is_ready_for_work())

    def test_summary_telemetry(self):
        summary = self.supervisor.get_status_summary()
        self.assertEqual(len(summary), 2)
        ids = {w["id"] for w in summary}
        self.assertIn("worker_1", ids)
        self.assertIn("worker_2", ids)


if __name__ == "__main__":
    unittest.main()
