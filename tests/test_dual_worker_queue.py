import io
import sys
import unittest

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
elif hasattr(sys.stdout, "buffer") and getattr(sys.stdout, "encoding", "").lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from main import CheckerState, Customer


class TestDualWorkerQueue(unittest.TestCase):
    def setUp(self):
        self.customers = [
            Customer(
                record_type="account",
                lookup_number=f"100040559{i}",
                expected_amount=15000,
                row_numbers=(i + 2,),
                customer_name=f"Customer {i}",
                main_status="",
                sub_status="",
            )
            for i in range(10)
        ]
        self.saved_progress = []
        self.mismatches = []
        self.errors = []

        def save_progress(idx, mismatches, completed, *args):
            self.saved_progress.append((idx, len(mismatches), completed))

        def write_mismatch(cust, amt):
            self.mismatches.append((cust, amt))

        def write_error(err):
            self.errors.append(err)

        def resolve_redirect(cust):
            pass

        self.state = CheckerState(
            customers=self.customers,
            start_index=0,
            mismatches=[],
            save_progress=save_progress,
            write_mismatch=write_mismatch,
            write_error=write_error,
            resolve_prior_redirect=resolve_redirect,
        )

    def test_worker_leasing_and_no_skip_on_repeated_poll(self):
        w1 = "Worker 1 (Router)"
        w2 = "Worker 2 (Proxy)"

        # 1. Worker 1 requests a task
        task1 = self.state.get_task(worker_id=w1)
        self.assertEqual(task1["status"], "check")
        self.assertEqual(task1["row_number"], 2)
        self.assertEqual(task1["worker_id"], w1)

        # 2. Worker 1 polls again 5 times (as happens during tab loading/navigation)
        for _ in range(5):
            repeated = self.state.get_task(worker_id=w1)
            self.assertEqual(repeated["status"], "check")
            self.assertEqual(repeated["row_number"], 2)
            self.assertEqual(repeated["task_id"], task1["task_id"])

        # 3. Worker 2 requests a task
        task2 = self.state.get_task(worker_id=w2)
        self.assertEqual(task2["status"], "check")
        self.assertEqual(task2["row_number"], 3)
        self.assertEqual(task2["worker_id"], w2)

        # 4. Worker 2 polls again 3 times
        for _ in range(3):
            repeated2 = self.state.get_task(worker_id=w2)
            self.assertEqual(repeated2["status"], "check")
            self.assertEqual(repeated2["row_number"], 3)
            self.assertEqual(repeated2["task_id"], task2["task_id"])

        # 5. Worker 1 finishes row 2 and submits result
        res1 = self.state.submit_result({
            "task_id": task1["task_id"],
            "worker_id": w1,
            "status": "ok",
            "website_amount": "150.00",
        })
        self.assertEqual(res1["status"], "waiting")
        self.assertEqual(len(self.state.completed_indices), 1)

        # 6. Worker 1 requests its next task: should now get row 4 (since row 3 was assigned to Worker 2)
        task3 = self.state.get_task(worker_id=w1)
        self.assertEqual(task3["status"], "check")
        self.assertEqual(task3["row_number"], 4)
        self.assertEqual(task3["worker_id"], w1)

        # 7. Worker 2 finishes row 3 and submits result
        res2 = self.state.submit_result({
            "task_id": task2["task_id"],
            "worker_id": w2,
            "status": "ok",
            "website_amount": "150.00",
        })
        self.assertEqual(res2["status"], "waiting")
        self.assertEqual(len(self.state.completed_indices), 2)

        # 8. Worker 2 requests next task: should get row 5
        task4 = self.state.get_task(worker_id=w2)
        self.assertEqual(task4["status"], "check")
        self.assertEqual(task4["row_number"], 5)
        self.assertEqual(task4["worker_id"], w2)

    def test_lease_timeout_reclamation(self):
        w1 = "Worker 1 (Router)"
        w2 = "Worker 2 (Proxy)"

        # Worker 1 takes row 2
        task1 = self.state.get_task(worker_id=w1)
        self.assertEqual(task1["row_number"], 2)

        # Simulate 50 seconds passing on task1 lease
        tid = task1["task_id"]
        self.state.in_flight[tid]["leased_at"] -= 50.0

        # Worker 2 now requests a task: it should reclaim row 2 from timeout and give it to Worker 2!
        task_reclaimed = self.state.get_task(worker_id=w2)
        self.assertEqual(task_reclaimed["status"], "check")
        self.assertEqual(task_reclaimed["row_number"], 2)
        self.assertEqual(task_reclaimed["worker_id"], w2)


if __name__ == "__main__":
    unittest.main()
