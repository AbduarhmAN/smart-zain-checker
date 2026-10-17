"""Kernel locks survive neither crashes nor stale PID files."""
import os
from pathlib import Path


class ServiceLock:
    def __init__(self, path): self.path=Path(path); self.stream=None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream=self.path.open('a+b')
        try:
            if stream.seek(0, 2)==0: stream.write(b'0'); stream.flush()
            stream.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            stream.close(); raise RuntimeError('Service already owns this state') from None
        self.stream=stream
        return self

    def close(self):
        if self.stream is not None:
            stream,self.stream=self.stream,None
            stream.close()

    def __enter__(self): return self.acquire()
    def __exit__(self,*_): self.close()
