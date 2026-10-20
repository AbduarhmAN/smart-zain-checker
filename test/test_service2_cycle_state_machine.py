# -*- coding: utf-8 -*-
import io
import sys
import unittest
from unittest.mock import patch

# Ensure UTF-8 stdout on Windows (User Global Rule)
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
elif hasattr(sys.stdout, "buffer") and getattr(sys.stdout, "encoding", "").lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from main import CheckerState
from zain_checker.workbook import Customer


def make_customer(idx: int, record_type: str, lookup_number: str, expected_sar: float = 150.0) -> Customer:
    is_srv2 = record_type == "wallet" or lookup_number.startswith("2")
    return Customer(
        record_type="wallet" if is_srv2 else "account",
        lookup_number=lookup_number,
        expected_amount=int(round(expected_sar * 100)),
        row_numbers=(idx + 1,),
        customer_name=f"Customer {idx + 1}",
        main_status="",
        sub_status="",
        source_key="custom",
        service_number=lookup_number if is_srv2 else f"9665{idx:08d}",
        original_account_number="" if is_srv2 else lookup_number,
    )


class TestService2CycleStateMachine(unittest.TestCase):
    @patch("time.sleep", return_value=None)
    def test_full_7_step_cycle_sequence(self, _mock_sleep):
        """Tests the exact 7-record sequence:
        #1: Service 2011111111 (Start SERVICE_2 Cycle #1 -> 1 fresh Incognito tab, refresh ONCE)
        #2: Service 2022222222 (Consecutive SERVICE_2 -> reuse SAME tab, NO open_incognito, NO refresh)
        #3: Service 2033333333 (Consecutive SERVICE_2 -> reuse SAME tab, NO open_incognito, NO refresh)
        #4: Account 1000444444 (Breaks SERVICE_2 Cycle #1 -> switch to ACCOUNT mode, reuse same tab)
        #5: Account 8000555555 (Consecutive ACCOUNT -> reuse SAME tab in ACCOUNT mode)
        #6: Service 2066666666 (Cycle was broken by Account -> Close all Incognito, open 1 fresh Incognito tab, Cycle #2, refresh ONCE)
        #7: Service 2077777777 (Consecutive SERVICE_2 in Cycle #2 -> reuse SAME fresh tab from #6, NO refresh)
        """
        customers = [
            make_customer(0, "wallet", "2011111111", 100.0),
            make_customer(1, "wallet", "2022222222", 200.0),
            make_customer(2, "wallet", "2033333333", 300.0),
            make_customer(3, "account", "1000444444", 400.0),
            make_customer(4, "account", "8000555555", 500.0),
            make_customer(5, "wallet", "2066666666", 600.0),
            make_customer(6, "wallet", "2077777777", 700.0),
        ]

        state = CheckerState(
            customers=customers,
            start_index=0,
            mismatches=[],
            save_progress=lambda idx, m, c: None,
            write_mismatch=lambda cust, amt: None,
            write_error=lambda err: None,
            resolve_prior_redirect=lambda cust: None,
        )

        # --- RECORD #1: Service 2011111111 (Start of Cycle #1) ---
        task1 = state.get_task()
        self.assertEqual(task1["status"], "check")
        self.assertEqual(task1["search_number"], "2011111111")
        self.assertEqual(task1["cycle_mode"], "SERVICE_2")
        self.assertEqual(task1["service_cycle_id"], 1)
        self.assertTrue(task1["refresh_once"], "First Service 2... in Cycle #1 must request refresh_once=True")

        # Extension performs the 1 single refresh and confirms /cycle-refreshed
        ack = state.confirm_cycle_refreshed({"service_cycle_id": 1})
        self.assertEqual(ack["status"], "cycle_refresh_acknowledged")
        self.assertTrue(ack["service_cycle_refreshed"])

        # Subsequent poll for Record #1 after the 1 refresh must have refresh_once=False
        task1_after_refresh = state.get_task()
        self.assertFalse(task1_after_refresh["refresh_once"], "After 1 refresh, refresh_once must be False")

        # Submit Record #1 -> Transition to Record #2 (also Service 2...)
        res1 = state.submit_result({
            "task_id": task1["task_id"],
            "row_number": task1["row_number"],
            "contract": task1["contract"],
            "record_type": task1["record_type"],
            "status": "ok",
            "website_amount": "100.00",
        })
        self.assertEqual(res1["status"], "waiting", "Consecutive Service 2... (#1 -> #2) must NOT return open_incognito")
        self.assertTrue(res1["reuse_same_tab"])
        self.assertEqual(res1["cycle_mode"], "SERVICE_2")
        self.assertEqual(res1["service_cycle_id"], 1)
        self.assertFalse(res1["refresh_once"])

        # --- RECORD #2: Service 2022222222 (Consecutive Service 2... in Cycle #1) ---
        task2 = state.get_task()
        self.assertEqual(task2["status"], "check")
        self.assertEqual(task2["search_number"], "2022222222")
        self.assertEqual(task2["cycle_mode"], "SERVICE_2")
        self.assertEqual(task2["service_cycle_id"], 1)
        self.assertFalse(task2["refresh_once"], "2nd consecutive Service 2... must NOT refresh")

        # Submit Record #2 -> Transition to Record #3 (also Service 2...)
        res2 = state.submit_result({
            "task_id": task2["task_id"],
            "row_number": task2["row_number"],
            "contract": task2["contract"],
            "record_type": task2["record_type"],
            "status": "ok",
            "website_amount": "200.00",
        })
        self.assertEqual(res2["status"], "waiting", "Consecutive Service 2... (#2 -> #3) must NOT return open_incognito")
        self.assertTrue(res2["reuse_same_tab"])
        self.assertEqual(res2["service_cycle_id"], 1)
        self.assertFalse(res2["refresh_once"])

        # --- RECORD #3: Service 2033333333 (Consecutive Service 2... in Cycle #1) ---
        task3 = state.get_task()
        self.assertEqual(task3["search_number"], "2033333333")
        self.assertEqual(task3["service_cycle_id"], 1)
        self.assertFalse(task3["refresh_once"])

        # Submit Record #3 -> Transition to Record #4 (Account 1000444444 BREAKS Cycle #1)
        res3 = state.submit_result({
            "task_id": task3["task_id"],
            "row_number": task3["row_number"],
            "contract": task3["contract"],
            "record_type": task3["record_type"],
            "status": "ok",
            "website_amount": "300.00",
        })
        self.assertEqual(res3["status"], "waiting")
        self.assertEqual(res3["cycle_mode"], "ACCOUNT", "Account #4 must break SERVICE_2 cycle and switch to ACCOUNT")
        self.assertTrue(res3["reuse_same_tab"])
        self.assertFalse(res3["refresh_once"])

        # --- RECORD #4: Account 1000444444 ---
        task4 = state.get_task()
        self.assertEqual(task4["search_number"], "1000444444")
        self.assertEqual(task4["cycle_mode"], "ACCOUNT")
        self.assertFalse(task4["refresh_once"])

        # Submit Record #4 -> Transition to Record #5 (Consecutive Account 8000555555)
        res4 = state.submit_result({
            "task_id": task4["task_id"],
            "row_number": task4["row_number"],
            "contract": task4["contract"],
            "record_type": task4["record_type"],
            "status": "ok",
            "website_amount": "400.00",
        })
        self.assertEqual(res4["status"], "waiting")
        self.assertEqual(res4["cycle_mode"], "ACCOUNT")
        self.assertTrue(res4["reuse_same_tab"], "Consecutive Account #5 must reuse the same tab")

        # --- RECORD #5: Account 8000555555 ---
        task5 = state.get_task()
        self.assertEqual(task5["search_number"], "8000555555")
        self.assertEqual(task5["cycle_mode"], "ACCOUNT")

        # Submit Record #5 -> Transition to Record #6 (Service 2066666666 AFTER cycle was broken!)
        res5 = state.submit_result({
            "task_id": task5["task_id"],
            "row_number": task5["row_number"],
            "contract": task5["contract"],
            "record_type": task5["record_type"],
            "status": "ok",
            "website_amount": "500.00",
        })
        self.assertEqual(
            res5["status"],
            "open_incognito",
            "Returning to Service 2... (#6) after Account (#5) MUST close all Incognito tabs and open 1 fresh Incognito session!",
        )
        self.assertEqual(res5["cycle_mode"], "SERVICE_2")
        self.assertEqual(res5["service_cycle_id"], 2, "Must increment to Service Cycle #2")
        self.assertTrue(res5["refresh_once"], "New Service Cycle #2 must request refresh_once=True")

        # --- RECORD #6: Service 2066666666 (Start of Cycle #2) ---
        task6 = state.get_task()
        self.assertEqual(task6["search_number"], "2066666666")
        self.assertEqual(task6["cycle_mode"], "SERVICE_2")
        self.assertEqual(task6["service_cycle_id"], 2)
        self.assertTrue(task6["refresh_once"])

        # Confirm the 1x refresh for Cycle #2
        state.confirm_cycle_refreshed({"service_cycle_id": 2})
        task6_after = state.get_task()
        self.assertFalse(task6_after["refresh_once"])

        # Submit Record #6 -> Transition to Record #7 (Consecutive Service 2077777777 in Cycle #2)
        res6 = state.submit_result({
            "task_id": task6["task_id"],
            "row_number": task6["row_number"],
            "contract": task6["contract"],
            "record_type": task6["record_type"],
            "status": "ok",
            "website_amount": "600.00",
        })
        self.assertEqual(res6["status"], "waiting", "Consecutive Service 2... (#6 -> #7) in Cycle #2 must reuse same tab")
        self.assertTrue(res6["reuse_same_tab"])
        self.assertEqual(res6["service_cycle_id"], 2)
        self.assertFalse(res6["refresh_once"])

        # --- RECORD #7: Service 2077777777 (Consecutive in Cycle #2) ---
        task7 = state.get_task()
        self.assertEqual(task7["search_number"], "2077777777")
        self.assertEqual(task7["service_cycle_id"], 2)
        self.assertFalse(task7["refresh_once"])

        res7 = state.submit_result({
            "task_id": task7["task_id"],
            "row_number": task7["row_number"],
            "contract": task7["contract"],
            "record_type": task7["record_type"],
            "status": "ok",
            "website_amount": "700.00",
        })
        self.assertEqual(res7["status"], "complete")
        print("✅ All 7 state-machine transitions verified successfully!")


if __name__ == "__main__":
    unittest.main()
