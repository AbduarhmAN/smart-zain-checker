"""Bounded timing telemetry. Caller holds the orchestrator lock."""
from collections import Counter, deque
import time
import uuid


class ProgressTiming:
    def __init__(self, kinds=()):
        # One pass when a session starts, never a workbook scan during polling.
        self.remaining = Counter(kinds)
        self.recent = deque(maxlen=11)  # 11 completion timestamps = 10 intervals.
        self.window_id = uuid.uuid4().hex

    def complete(self, kind, now=None):
        self.remaining[kind] = max(0, self.remaining[kind] - 1)
        self.recent.append((time.monotonic() if now is None else now, kind))

    def snapshot(self, now=None):
        now = time.monotonic() if now is None else now
        return {'window_id':self.window_id,
                'remaining_by_type':{kind:self.remaining[kind] for kind in ('account','service')},
                'recent_completions':[{'age_seconds':max(0, now-at), 'kind':kind}
                                      for at,kind in self.recent]}
