import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.circuit import CircuitBreaker
from services.ipc import CommandStore
from services.resources import admit


class ResilienceTests(unittest.TestCase):
    def test_only_one_half_open_probe_then_recovery(self):
        now=[0.0];breaker=CircuitBreaker(threshold=2,recovery_seconds=5,clock=lambda:now[0])
        self.assertEqual(breaker.before(),(True,False))
        breaker.failure();breaker.failure()
        self.assertEqual(breaker.status()['state'],'OPEN')
        self.assertEqual(breaker.before(),(False,False))
        now[0]=5
        self.assertEqual(breaker.before(),(True,True))
        self.assertEqual(breaker.before(),(False,False))
        breaker.failure();self.assertEqual(breaker.status()['state'],'OPEN')
        now[0]=10;self.assertEqual(breaker.before(),(True,True))
        breaker.success();self.assertEqual(breaker.status()['state'],'CLOSED')

    def test_kernel_pressure_and_calibrated_estimate_defer_without_guessing_quota(self):
        with patch.dict(os.environ,{'ZAIN_MEMORY_RESERVE_BYTES':'0','ZAIN_BROWSER_TASK_MEMORY_BYTES':'0'}):
            with patch('services.resources.memory_snapshot',return_value={'current':100,'high':100,'max':200,'available':1000}):self.assertFalse(admit('browser'))
            with patch('services.resources.memory_snapshot',return_value={'current':90,'high':100,'max':200,'available':1000}):self.assertTrue(admit('browser'))
            with patch.dict(os.environ,{'ZAIN_BROWSER_TASK_MEMORY_BYTES':'15'}),patch('services.resources.memory_snapshot',return_value={'current':90,'high':100,'max':200,'available':1000}):self.assertFalse(admit('browser'))

    def test_deferred_work_stays_durable_and_control_uses_reserved_queue_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            store=CommandStore(Path(directory)/'fixture.sqlite')
            store.accept('heavy','report.export',{})
            self.assertEqual(store.claim()[0],'heavy')
            store.defer('heavy',10)
            store.accept('stop','query_cancel',{'task_id':'one'},priority=True)
            self.assertEqual(store.claim(control_only=True)[0],'stop')
            self.assertEqual(store.get('heavy')['state'],'pending')
            self.assertIsNone(store.claim())
            self.assertEqual(CommandStore(store.path).get('heavy')['state'],'pending')


if __name__=='__main__':unittest.main()
