import unittest
from pathlib import Path
from unittest.mock import MagicMock

from zain_checker.chrome_manager import (
    is_cmdline_for_worker_profile,
    get_worker_profile_dir,
)
from main import (
    normalize_worker_id,
    resolve_worker_proxy,
    CheckerState,
    Customer,
)


class TestWorkerIsolation(unittest.TestCase):
    def test_cmdline_profile_exact_matching(self):
        """Verify that worker_1 and worker_2 profile directories do not cross-match via substrings."""
        prof_1 = Path(r"E:\Projects\smart_zainchecker_gemini\.zain-checker-profile")
        prof_2 = Path(r"E:\Projects\smart_zainchecker_gemini\.zain-checker-profile-proxy")

        cmd_worker_1 = ["chrome.exe", f"--user-data-dir={prof_1}", "--incognito"]
        cmd_worker_2 = ["chrome.exe", f"--user-data-dir={prof_2}", "--incognito"]

        # Worker 1 matches only cmd_worker_1
        self.assertTrue(is_cmdline_for_worker_profile(cmd_worker_1, prof_1))
        self.assertFalse(is_cmdline_for_worker_profile(cmd_worker_2, prof_1))

        # Worker 2 matches only cmd_worker_2
        self.assertTrue(is_cmdline_for_worker_profile(cmd_worker_2, prof_2))
        self.assertFalse(is_cmdline_for_worker_profile(cmd_worker_1, prof_2))

    def test_resolve_worker_proxy(self):
        """Worker 1 without proxy resolves to None (direct network). Worker 2 resolves to configured proxy."""
        # Worker 1 (router / direct)
        proxy_1 = resolve_worker_proxy("worker_1")
        self.assertIsNone(proxy_1)

        # Worker 3 (proxy)
        proxy_3 = resolve_worker_proxy("worker_3")
        self.assertIsNotNone(proxy_3)
        self.assertIn("195.40.62.31", proxy_3)

    def test_per_worker_stalled_handoff(self):
        """Handoff stall on Worker 1 should isolate Worker 1 only, without impacting Worker 2."""
        customers = [
            Customer(
                record_type="account",
                lookup_number="10001",
                expected_amount=10000,
                row_numbers=(2,),
                customer_name="Customer 1",
                main_status="Active",
                sub_status="Normal",
            ),
            Customer(
                record_type="account",
                lookup_number="10002",
                expected_amount=20000,
                row_numbers=(3,),
                customer_name="Customer 2",
                main_status="Active",
                sub_status="Normal",
            ),
        ]
        state = CheckerState(
            customers=customers,
            start_index=0,
            mismatches=[],
            save_progress=MagicMock(),
            write_mismatch=MagicMock(),
            write_error=MagicMock(),
            resolve_prior_redirect=MagicMock(),
        )

        # Start handoffs for both workers
        state._begin_incognito_handoff(timeout_seconds=10, worker_id="worker_1", customer=customers[0])
        state._begin_incognito_handoff(timeout_seconds=60, worker_id="worker_2", customer=customers[1])

        # Initially, neither is stalled
        self.assertEqual(len(state.get_stalled_worker_handoffs()), 0)

        # Simulate Worker 1 handoff expiring (>10s in the past)
        state.worker_handoff_started_at["worker_1"] -= 15.0

        stalled = state.get_stalled_worker_handoffs()
        self.assertEqual(len(stalled), 1)
        self.assertEqual(stalled[0][0], "worker_1")
        self.assertEqual(stalled[0][1].lookup_number, "10001")

        # Worker 1 handoff acknowledged by extension
        ack_res = state.confirm_incognito_handoff({"worker_id": "worker_1", "task_id": "dummy"})
        self.assertEqual(ack_res["status"], "handoff_acknowledged")
        self.assertNotIn("worker_1", state.worker_handoff_started_at)

        # Worker 2 is still actively tracked
        self.assertIn("worker_2", state.worker_handoff_started_at)


if __name__ == "__main__":
    unittest.main()
