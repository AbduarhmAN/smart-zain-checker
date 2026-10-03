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


if __name__ == '__main__':
    unittest.main()
