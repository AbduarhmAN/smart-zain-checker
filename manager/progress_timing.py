"""Bounded timing telemetry. Caller holds the orchestrator lock."""
from collections import Counter, deque
import time
import uuid


class ProgressTiming:
    def __init__(self, kinds=()):
        # One pass when a session starts, never a workbook scan during polling.
        self.remaining = Counter(kinds)
        self.recent = deque(maxlen=11)  # 11 completion timestamps = 10 intervals.
        self.by_type = {kind: deque(maxlen=11) for kind in ('account', 'service')}
        self.by_phase = {phase: deque(maxlen=11) for phase in ('regular', 'retry')}
        self.pause_started = None
        self.paused_seconds = 0.0
        self.started = time.monotonic()
        self.window_id = uuid.uuid4().hex

    def _clock(self, now):
        return now - self.paused_seconds - (max(0, now-self.pause_started) if self.pause_started is not None else 0)

    def pause(self, now=None):
        if self.pause_started is None:
            self.pause_started = time.monotonic() if now is None else now

    def resume(self, now=None):
        if self.pause_started is not None:
            now = time.monotonic() if now is None else now
            self.paused_seconds += max(0, now-self.pause_started)
            self.pause_started = None

    def complete(self, kind, now=None, phase='regular'):
        self.remaining[kind] = max(0, self.remaining[kind] - 1)
        at = self._clock(time.monotonic() if now is None else now)
        event = (at, kind)
        self.recent.append(event)
        self.by_type.setdefault(kind, deque(maxlen=11)).append(event)
        self.by_phase.setdefault(phase, deque(maxlen=11)).append(event)

    def snapshot(self, now=None):
        now = time.monotonic() if now is None else now
        active_now = self._clock(now)
        def events(items):
            return [{'age_seconds':max(0, active_now-at), 'kind':kind} for at,kind in items]
        return {'window_id':self.window_id,
                'active_age_seconds':max(0, active_now-self.started),
                'remaining_by_type':{kind:self.remaining[kind] for kind in ('account','service')},
                'recent_completions':events(self.recent),
                'recent_by_type':{kind:events(items) for kind,items in self.by_type.items()},
                'recent_by_phase':{phase:events(items) for phase,items in self.by_phase.items()}}
