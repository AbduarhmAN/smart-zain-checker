"""Bounded ETA telemetry tests without files, workers, or network."""
import unittest
from manager.progress_timing import ProgressTiming


class ProgressTimingTests(unittest.TestCase):
    def test_counts_remaining_types_and_resumed_rows(self):
        timing = ProgressTiming(['account','service','service'])
        timing.complete('account',10)
        self.assertEqual(timing.snapshot(15)['remaining_by_type'],{'account':0,'service':2})
        self.assertEqual(timing.snapshot(15)['recent_completions'],[{'age_seconds':5,'kind':'account'}])

    def test_constant_memory_and_no_repeated_iteration(self):
        iterations=[]
        def kinds():
            iterations.append(1)
            yield from ['account']*1000
        timing = ProgressTiming(kinds())
        for i in range(1000):
            timing.complete('account',i)
        for _ in range(100):
            snapshot = timing.snapshot(1000)
            self.assertEqual(len(snapshot['recent_completions']),11)
        self.assertEqual(iterations,[1])
        self.assertEqual(snapshot['remaining_by_type']['account'],0)

    def test_new_round_gets_independent_window(self):
        a,b=ProgressTiming(['account']),ProgressTiming(['service'])
        self.assertNotEqual(a.window_id,b.window_id)
        a.complete('account',10)
        self.assertEqual(b.snapshot(20)['recent_completions'],[])

    def test_automatic_wait_remains_in_wall_clock_intervals(self):
        timing=ProgressTiming(['service']*3)
        timing.complete('service',10)
        timing.complete('service',430)
        events=timing.snapshot(430)['recent_completions']
        self.assertEqual(events[0]['age_seconds']-events[1]['age_seconds'],420)

    def test_explicit_pause_is_excluded_without_resetting_history(self):
        timing=ProgressTiming(['service']*3)
        timing.complete('service',10)
        timing.pause(20)
        timing.resume(440)
        timing.complete('service',450)
        events=timing.snapshot(450)['recent_completions']
        self.assertEqual(events[0]['age_seconds']-events[1]['age_seconds'],20)

    def test_fast_contracts_do_not_evict_service_history(self):
        timing=ProgressTiming(['service']*3+['account']*100)
        timing.complete('service',10);timing.complete('service',430)
        for i in range(100):timing.complete('account',431+i)
        snapshot=timing.snapshot(540)
        self.assertTrue(all(e['kind']=='account' for e in snapshot['recent_completions']))
        self.assertEqual(len(snapshot['recent_by_type']['service']),2)


if __name__ == '__main__':
    unittest.main()
