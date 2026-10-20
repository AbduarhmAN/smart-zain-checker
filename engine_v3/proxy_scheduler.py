"""One in-process coordinator shared by files, with no automatic route rotation."""
import hashlib
import heapq
import threading
import time
from contextlib import contextmanager
from urllib.parse import urlparse


def route_key(proxy=None):
    if not proxy:return 'service_http_direct'
    value=urlparse(proxy)
    if value.scheme not in ('socks5','socks5h','http','https') or not value.hostname or not value.port:
        raise ValueError('Invalid service route')
    # Configured route identity, never a claim that the public IP was measured.
    canonical=str((value.scheme.replace('socks5h','socks5'),value.hostname.lower(),value.port,value.username,value.password))
    return 'service_http_'+hashlib.sha256(canonical.encode()).hexdigest()[:24]


class RouteNotReady(RuntimeError):
    def __init__(self,seconds,blocked=False):self.seconds=max(.2,seconds);self.blocked=blocked


class CentralizedCooldownScheduler:
    def __init__(self,cooldown_seconds=60,max_concurrent=28,target_qps=5,*,clock=time.monotonic):
        if cooldown_seconds<60 or not 1<=max_concurrent<=28 or not 0<target_qps<=5:
            raise ValueError('Reviewed bounded service scheduling required')
        self.cooldown=cooldown_seconds;self.limit=max_concurrent;self.interval=1/target_qps;self.clock=clock
        self.lock=threading.RLock();self.active=set();self.ready_at={};self.blocked=set();self.heap=[];self.next_dispatch=0

    def acquire(self,key):
        with self.lock:
            now=self.clock()
            while self.heap and self.heap[0][0]<=now:
                deadline,route=heapq.heappop(self.heap)
                if self.ready_at.get(route)==deadline:self.ready_at.pop(route,None)
            if key in self.blocked:raise RouteNotReady(60,True)
            delay=max(self.ready_at.get(key,0)-now,self.next_dispatch-now)
            if key in self.active or len(self.active)>=self.limit:delay=max(delay,.2)
            if delay>0:raise RouteNotReady(delay)
            self.active.add(key);self.next_dispatch=now+self.interval

    def release(self,key,*,retry_after=0,blocked=False):
        with self.lock:
            if key not in self.active:raise ValueError('Service route is not leased')
            self.active.remove(key);deadline=self.clock()+max(self.cooldown,retry_after)
            self.ready_at[key]=deadline;heapq.heappush(self.heap,(deadline,key))
            if blocked:self.blocked.add(key)

    def unblock(self,key):
        with self.lock:self.blocked.discard(key)

    def retry_after(self,key):
        with self.lock:return max(0,self.ready_at.get(key,0)-self.clock())
