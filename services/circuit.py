"""Small transport circuit. Financial state/retries remain in commerce code."""
import threading
import time


class CircuitBreaker:
    def __init__(self,threshold=3,recovery_seconds=5,clock=time.monotonic):
        if threshold<1 or recovery_seconds<=0: raise ValueError('Invalid circuit settings')
        self.threshold=threshold;self.recovery_seconds=recovery_seconds;self.clock=clock
        self.lock=threading.Lock();self.failures=0;self.opened=None;self.probing=False

    def before(self):
        """Return (allowed, health_probe_required); at most one half-open probe."""
        with self.lock:
            if self.opened is None:return True,False
            if self.clock()-self.opened<self.recovery_seconds or self.probing:return False,False
            self.probing=True;return True,True

    def success(self):
        with self.lock:self.failures=0;self.opened=None;self.probing=False

    def failure(self):
        with self.lock:
            self.failures+=1
            if self.probing or self.failures>=self.threshold:self.opened=self.clock()
            self.probing=False

    def status(self):
        with self.lock:
            return {'state':'HALF_OPEN' if self.probing else 'OPEN' if self.opened is not None else 'CLOSED','failures':self.failures,
                'retry_after':max(0,self.recovery_seconds-(self.clock()-self.opened)) if self.opened is not None else 0}


_circuits={}
_lock=threading.Lock()


def dependency_circuit(key):
    import os
    with _lock:
        if key not in _circuits:
            _circuits[key]=CircuitBreaker(int(os.environ.get('ZAIN_IPC_FAILURE_THRESHOLD','3')),float(os.environ.get('ZAIN_IPC_RECOVERY_SECONDS','5')))
        return _circuits[key]
