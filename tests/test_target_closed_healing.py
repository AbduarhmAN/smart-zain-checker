"""Tests for TargetClosedError handling, self-healing, and persistent cooldown loop."""
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
import workers.stealth_service_engine as browser


class Clock:
    def __init__(self):
        self.now = 10000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def time(self):
        return time.time()

    def strftime(self, fmt):
        return time.strftime(fmt)

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def service(index=0):
    return Customer(index + 2, "wallet", f"10000{index}", f"10000{index}", 1000, service_number=f"20000{index}")


def create_orchestrator(customers):
    root = Path(__file__).resolve().parent.parent
    actors = {
        name: WorkerActor(
            name, name, root / "UNUSED_TEST_PROFILE", root / "chrome_extension",
            use_proxy=name == "worker_3",
            proxy_url="socks5://proxy.invalid:1080" if name == "worker_3" else None,
        )
        for name in ("worker_1", "worker_2", "worker_3")
    }
    for actor in actors.values():
        actor.status = "ready"
    obj = Orchestrator(SimpleNamespace(get_worker=actors.get), Mock(), root)
    obj.is_running = True
    obj.customers = customers
    obj.result_queue = queue.Queue()
    obj.checkpoint_manager = Mock()
    return obj, actors


class TestTargetClosedHealing(unittest.TestCase):
    def setUp(self):
        with transport._IP_LOCKS_GUARD:
            transport._IP_SERVICE_LOCKS.clear()
            transport._IP_LAST_QUERY_TIME.clear()
            transport._IP_RETRY_UNTIL.clear()

    def test_stealth_engine_detects_target_closed_and_heals_context(self):
        """When TargetClosedError happens, engine marks result as blocked and detects context liveness."""
        worker = browser.StealthServiceWorker()

        dead_browser = SimpleNamespace(is_connected=lambda: False)
        dead_context = SimpleNamespace(browser=dead_browser, pages=[])
        self.assertFalse(worker._is_context_alive(dead_context))

        alive_browser = SimpleNamespace(is_connected=lambda: True)
        alive_context = SimpleNamespace(browser=alive_browser, pages=[])
        self.assertTrue(worker._is_context_alive(alive_context))

    def test_actor_normalizes_target_closed_to_blocked(self):
        """WorkerActor normalizes any TargetClosed error into a 'blocked' status."""
        actor = WorkerActor("worker_1", "w1", Path("."), Path("."))
        with patch("workers.zain_api.query_contract_due_amount", return_value=("network_error", None, "تعذر استكمال المتصفح: TargetClosedError")):
            status, amount, msg = actor.execute_task_api("20001")
            self.assertEqual(status, "blocked")
            self.assertIn("TargetClosedError", msg)

    def test_orchestrator_worker_loop_defers_target_closed_with_cooldown(self):
        """When TargetClosedError occurs in worker loop, orchestrator defers row with cooldown."""
        obj, actors = create_orchestrator([service(0)])

        def execute(number):
            obj.is_running = False
            return "network_error", None, "تعذر استكمال المتصفح: TargetClosedError"

        actors["worker_1"].execute_task_api = execute
        clock = Clock()
        with patch.object(om, "time", clock), patch.object(transport, "time", clock):
            obj._run_worker_api_loop("worker_1")
            # The task must be deferred to the end, NOT converted to needs_review
            self.assertEqual(obj.reviews_count, 0)
            self.assertEqual(obj.errors_count, 0)
            self.assertEqual(obj.deferred_indices, [0])
            # Direct actor must be placed in cooldown for recovery
            self.assertTrue(actors["worker_1"].is_in_cooldown())
            self.assertGreater(transport.get_ip_service_retry_after("local_direct"), 0)

    def test_worker_loop_keeps_retrying_blocked_tasks_until_all_completed(self):
        """Deferred blocked tasks can be retried across cooldowns until completed without dropping to needs_review."""
        obj, actors = create_orchestrator([service(0)])
        clock = Clock()

        with patch.object(om, "time", clock), patch.object(transport, "time", clock):
            # Attempt 1: Hits block / TargetClosedError
            task1 = obj.lease_next_task_for_worker("worker_1")
            self.assertIsNotNone(task1)
            obj.defer_task_to_end(task1["task_id"], "TargetClosedError block", worker_id="worker_1",
                                   retry_after=15.0, category="blocked", max_attempts=50)
            self.assertEqual(obj.deferred_indices, [0])
            self.assertEqual(obj.reviews_count, 0)

            # While cooldown is active, task cannot be leased
            clock.now += 5.0
            self.assertIsNone(obj.lease_next_task_for_worker("worker_1"))

            # Cooldown passes -> re-leased for retry
            clock.now += 15.0
            task2 = obj.lease_next_task_for_worker("worker_1")
            self.assertIsNotNone(task2)
            # Attempt 2 succeeds (10.00 SAR matches 1000 halalas)
            obj.record_task_outcome(task2["task_id"], 10.00, "match", worker_id="worker_1")

            # Must be completed successfully with 0 errors and 0 needs_review
            self.assertEqual(obj.matches_count, 1)
            self.assertEqual(obj.reviews_count, 0)
            self.assertEqual(obj.errors_count, 0)
            self.assertEqual(obj.completed_indices, {0})
            self.assertEqual(len(obj.deferred_indices), 0)


if __name__ == "__main__":
    unittest.main()
