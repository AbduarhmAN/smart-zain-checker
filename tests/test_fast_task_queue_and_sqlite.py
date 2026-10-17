import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from domain.models import Customer, ProgressMismatch
from manager.checkpoint import CheckpointManager
from manager.orchestrator import Orchestrator
from workers.actor import WorkerActor, normalize_task_scope
from workers.supervisor import WorkerSupervisor


def make_cust(row_number: int, search_num: str, expected: int = 1000) -> Customer:
    return Customer(
        row_number=row_number,
        lookup_number=search_num,
        service_number=search_num if search_num.startswith("2") else "",
        contract=search_num if not search_num.startswith("2") else "",
        expected_amount=expected,
        expected_amount_2=None,
        customer_name=f"Customer {row_number}",
        record_type="wallet" if search_num.startswith("2") else "account",
    )


class TestFastTaskQueueAndSqlite(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_fast_queue_"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_in_memory_task_queue_o1_speed(self):
        """50,000 records lease 1,000 tasks in <150ms (O(1) queue)."""
        customers = [make_cust(i, f"1000{i:06d}") for i in range(50000)]
        worker = SimpleNamespace(
            worker_id="w1", status="ready", ip_group="local_direct",
            use_proxy=False, is_in_cooldown=lambda: False,
            assign_task=Mock(), current_task=None, task_scope="both"
        )
        supervisor = SimpleNamespace(
            get_worker=lambda wid: worker if wid == "w1" else None
        )
        orch = Orchestrator(supervisor, Mock(), self.temp_dir)
        orch.customers = customers
        orch.is_running = True

        t0 = time.perf_counter()
        leased_count = 0
        for _ in range(1000):
            task = orch.lease_next_task_for_worker("w1")
            if task:
                leased_count += 1
                orch.completed_indices.add(task["index"])
                orch.active_leases.pop(task["task_id"], None)
        elapsed = time.perf_counter() - t0

        self.assertEqual(leased_count, 1000)
        self.assertLess(elapsed, 0.15, f"1,000 leases took {elapsed:.4f}s, expected < 0.15s")

    def test_in_memory_queue_proxy_service_priority(self):
        """Proxy workers prioritize service numbers from in-memory queue."""
        customers = [
            make_cust(1, "1000000001"), # Account
            make_cust(2, "2000000002"), # Service
            make_cust(3, "1000000003"), # Account
        ]
        w_proxy = SimpleNamespace(
            worker_id="proxy_1", status="ready", ip_group="proxy_route",
            use_proxy=True, is_in_cooldown=lambda: False,
            assign_task=Mock(), current_task=None, task_scope="both"
        )
        supervisor = SimpleNamespace(
            get_worker=lambda wid: w_proxy if wid == "proxy_1" else None
        )
        orch = Orchestrator(supervisor, Mock(), self.temp_dir)
        orch.customers = customers
        orch.is_running = True

        task = orch.lease_next_task_for_worker("proxy_1")
        self.assertIsNotNone(task)
        self.assertEqual(task["search_number"], "2000000002")
        self.assertEqual(task["record_type"], "wallet")

    def test_worker_task_scope_normalization(self):
        """normalize_task_scope properly maps accounts, services, both, and aliases."""
        self.assertEqual(normalize_task_scope("account"), "account")
        self.assertEqual(normalize_task_scope("accounts"), "account")
        self.assertEqual(normalize_task_scope("1"), "account")
        self.assertEqual(normalize_task_scope("contract"), "account")

        self.assertEqual(normalize_task_scope("service"), "service")
        self.assertEqual(normalize_task_scope("services"), "service")
        self.assertEqual(normalize_task_scope("2"), "service")
        self.assertEqual(normalize_task_scope("wallet"), "service")

        self.assertEqual(normalize_task_scope("both"), "both")
        self.assertEqual(normalize_task_scope("all"), "both")
        self.assertEqual(normalize_task_scope("0"), "both")
        self.assertEqual(normalize_task_scope(None), "both")
        self.assertEqual(normalize_task_scope("unknown"), "both")

        actor_acc = WorkerActor("w1", "W1", Path("."), Path("."), task_scope="1")
        self.assertEqual(actor_acc.task_scope, "account")
        self.assertTrue(actor_acc.can_check_accounts)
        self.assertFalse(actor_acc.can_check_services)

        actor_srv = WorkerActor("w2", "W2", Path("."), Path("."), task_scope="service")
        self.assertEqual(actor_srv.task_scope, "service")
        self.assertFalse(actor_srv.can_check_accounts)
        self.assertTrue(actor_srv.can_check_services)

    def test_worker_scope_account_only_never_leases_services(self):
        """Worker with task_scope='account' only leases accounts, never service numbers."""
        customers = [
            make_cust(1, "2000000001"), # Service
            make_cust(2, "2000000002"), # Service
            make_cust(3, "1000000003"), # Account
        ]
        w_acc = WorkerActor("w1", "W1", Path("."), Path("."), task_scope="account")
        w_acc.status = "ready"
        supervisor = SimpleNamespace(get_worker=lambda wid: w_acc if wid == "w1" else None)

        orch = Orchestrator(supervisor, Mock(), self.temp_dir)
        orch.customers = customers
        orch.is_running = True

        # First lease: must skip services 1 & 2 and lease account 3
        task = orch.lease_next_task_for_worker("w1")
        self.assertIsNotNone(task)
        self.assertEqual(task["search_number"], "1000000003")
        orch.completed_indices.add(task["index"])
        orch.active_leases.pop(task["task_id"], None)

        # Second lease: only services remain, worker must return None
        task2 = orch.lease_next_task_for_worker("w1")
        self.assertIsNone(task2)

    def test_worker_scope_service_only_never_leases_accounts(self):
        """Worker with task_scope='service' only leases service numbers, never accounts."""
        customers = [
            make_cust(1, "1000000001"), # Account
            make_cust(2, "1000000002"), # Account
            make_cust(3, "2000000003"), # Service
        ]
        w_srv = WorkerActor("w2", "W2", Path("."), Path("."), task_scope="service")
        w_srv.status = "ready"
        supervisor = SimpleNamespace(get_worker=lambda wid: w_srv if wid == "w2" else None)

        orch = Orchestrator(supervisor, Mock(), self.temp_dir)
        orch.customers = customers
        orch.is_running = True

        # First lease: must skip accounts 1 & 2 and lease service 3
        task = orch.lease_next_task_for_worker("w2")
        self.assertIsNotNone(task)
        self.assertEqual(task["search_number"], "2000000003")
        orch.completed_indices.add(task["index"])
        orch.active_leases.pop(task["task_id"], None)

        # Second lease: only accounts remain, worker must return None
        task2 = orch.lease_next_task_for_worker("w2")
        self.assertIsNone(task2)

    def test_supervisor_loads_task_scope_from_config(self):
        """WorkerSupervisor loads task_scope correctly from configuration dicts."""
        cfgs = [
            {"worker_id": "w1", "name": "Direct", "use_proxy": False, "task_scope": "account"},
            {"worker_id": "w2", "name": "Proxy", "use_proxy": False, "task_scope": "service"},
        ]
        supervisor = WorkerSupervisor(
            project_root=self.temp_dir,
            workers_config=cfgs,
            enable_proxy_workers=True,
        )
        w1 = supervisor.get_worker("w1")
        w2 = supervisor.get_worker("w2")

        self.assertIsNotNone(w1)
        self.assertEqual(w1.task_scope, "account")
        self.assertTrue(w1.can_check_accounts)
        self.assertFalse(w1.can_check_services)

        self.assertIsNotNone(w2)
        self.assertEqual(w2.task_scope, "service")
        self.assertFalse(w2.can_check_accounts)
        self.assertTrue(w2.can_check_services)

    def test_checkpoint_sqlite_persistence_and_reload(self):
        """CheckpointManager saves to SQLite and successfully restores state."""
        chk_path = self.temp_dir / ".checkpoint_test_0.json"
        mgr = CheckpointManager(chk_path)

        completed = {0, 1, 2, 5, 9}
        mismatches = [
            ProgressMismatch(sequence_index=1, expected_amount=10000, website_amount=5000),
            ProgressMismatch(sequence_index=5, expected_amount=20000, website_amount=15000),
        ]
        extra = {
            "errors_count": 1,
            "matches_count": 3,
            "reviews_count": 1,
            "workbook_name": "test.xlsx",
        }

        saved_ok = mgr.save(
            completed_indices=completed,
            mismatches=mismatches,
            last_index=9,
            workbook_name="test.xlsx",
            extra=extra
        )
        self.assertTrue(saved_ok)

        # Verify SQLite DB exists and contains rows
        db_path = chk_path.with_suffix(".db")
        self.assertTrue(db_path.exists())

        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT idx FROM completed_indices ORDER BY idx").fetchall()
        self.assertEqual([r[0] for r in rows], [0, 1, 2, 5, 9])
        mm_count = conn.execute("SELECT COUNT(*) FROM mismatches").fetchone()[0]
        self.assertEqual(mm_count, 2)
        conn.close()

        # Reload state using CheckpointManager.load()
        loaded = mgr.load()
        self.assertEqual(set(loaded["completed_indices"]), completed)
        self.assertEqual(len(loaded["mismatches"]), 2)
        self.assertEqual(loaded["errors_count"], 1)
        self.assertEqual(loaded["matches_count"], 3)
        self.assertEqual(loaded["last_index"], 9)

    def test_checkpoint_reset_archives_sqlite_and_wal(self):
        """mgr.reset() archives both .json and .db files."""
        chk_path = self.temp_dir / ".checkpoint_reset_test_0.json"
        mgr = CheckpointManager(chk_path)
        mgr.save(completed_indices={10, 20}, mismatches=[], last_index=20)

        self.assertTrue(chk_path.exists())
        self.assertTrue(chk_path.with_suffix(".db").exists())

        mgr.reset()
        self.assertFalse(chk_path.exists())
        self.assertFalse(chk_path.with_suffix(".db").exists())


if __name__ == "__main__":
    unittest.main()
