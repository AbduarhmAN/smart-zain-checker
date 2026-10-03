"""Unit tests for 'صيانة الأخطاء' (repair_errors) feature."""
from __future__ import annotations

from pathlib import Path
import queue
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from domain.models import Customer
from manager.orchestrator import Orchestrator
import manager.orchestrator as om
from workers.actor import WorkerActor
import workers.service_transport as transport


def create_customer(row=2, expected=1000):
    return Customer(row, "wallet", f"10000{row}", f"10000{row}", expected, service_number=f"20000{row}")


def setup_orchestrator(customers):
    root = Path(__file__).resolve().parent.parent
    actors = {
        name: WorkerActor(
            name, name, root / "UNUSED_TEST_PROFILE", root / "chrome_extension",
            use_proxy=name == "worker_3",
            proxy_url="socks5://proxy.invalid:1080" if name == "worker_3" else None,
        )
        for name in ("worker_1", "worker_2", "worker_3")
    }
    for a in actors.values():
        a.status = "ready"
    obj = Orchestrator(SimpleNamespace(get_worker=actors.get, get_all_workers=lambda: list(actors.values()), start_all=Mock()), Mock(), root)
    obj.is_running = False
    obj.customers = customers
    obj.result_queue = queue.Queue()
    obj.checkpoint_manager = Mock()
    obj._run_worker_api_loop = Mock()
    return obj, actors


class TestRepairErrors(unittest.TestCase):
    def setUp(self):
        with transport._IP_LOCKS_GUARD:
            transport._IP_SERVICE_LOCKS.clear()
            transport._IP_LAST_QUERY_TIME.clear()
            transport._IP_RETRY_UNTIL.clear()

    def test_repair_errors_finds_and_reenqueues_error_and_review_rows(self):
        customers = [create_customer(row=2), create_customer(row=3), create_customer(row=4)]
        obj, actors = setup_orchestrator(customers)

        # Simulate initial run:
        # Row 0 (row 2) matched
        # Row 1 (row 3) error
        # Row 2 (row 4) needs_review
        obj.completed_indices = {0, 1, 2}
        obj.matches_count = 1
        obj.errors_count = 1
        obj.reviews_count = 1
        obj.all_completed_records = {
            0: {"row": 2, "status": "match", "live_sar": 10.0},
            1: {"row": 3, "status": "error", "error": "timeout"},
            2: {"row": 4, "status": "needs_review", "error": "TargetClosedError"},
        }

        # Call repair_errors
        res = obj.repair_errors()
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["repaired_count"], 2)

        # Row 0 must remain in completed_indices
        self.assertIn(0, obj.completed_indices)
        # Rows 1 and 2 must be removed from completed_indices to be re-checked
        self.assertNotIn(1, obj.completed_indices)
        self.assertNotIn(2, obj.completed_indices)

        # Error and review counts should be decremented
        self.assertEqual(obj.errors_count, 0)
        self.assertEqual(obj.reviews_count, 0)
        # Session must be running to process them
        self.assertTrue(obj.is_running)

    def test_repair_single_row(self):
        customers = [create_customer(row=2), create_customer(row=3)]
        obj, actors = setup_orchestrator(customers)

        obj.completed_indices = {0, 1}
        obj.errors_count = 2
        obj.all_completed_records = {
            0: {"row": 2, "status": "error", "error": "failed"},
            1: {"row": 3, "status": "error", "error": "failed"},
        }

        # Repair only row 3 (which is index 1)
        res = obj.repair_errors(row=3)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["repaired_count"], 1)

        self.assertIn(0, obj.completed_indices)
        self.assertNotIn(1, obj.completed_indices)
        self.assertEqual(obj.errors_count, 1)

    def test_repair_errors_no_errors_found(self):
        customers = [create_customer(row=2)]
        obj, actors = setup_orchestrator(customers)
        obj.completed_indices = {0}
        obj.matches_count = 1
        obj.all_completed_records = {0: {"row": 2, "status": "match"}}

        res = obj.repair_errors()
        self.assertEqual(res["status"], "info")
        self.assertEqual(res["repaired_count"], 0)
        self.assertIn("لا توجد أخطاء", res["message"])


if __name__ == "__main__":
    unittest.main()
