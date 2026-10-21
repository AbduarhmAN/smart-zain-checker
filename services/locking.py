"""Kernel locks survive neither crashes nor stale PID files."""
import os
import re
import sys
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


class TokenLease:
    """Exclude the same bot across checkouts, without storing its token.

    Linux abstract sockets share the host network namespace across OS users.
    Windows named kernel objects cover development checkouts in one session.
    Ownership is the handle lifetime, so crashes cannot leave a stale PID lock.
    """
    def __init__(self,fingerprint):
        if not re.fullmatch('[0-9a-f]{64}',fingerprint):raise ValueError('Bot fingerprint required')
        self.name='SmartZainTelegram-'+fingerprint;self.handle=None;self.socket=None

    def acquire(self):
        if os.name=='nt':
            import ctypes
            from ctypes import wintypes
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            kernel.CreateMutexW.argtypes=(ctypes.c_void_p,wintypes.BOOL,wintypes.LPCWSTR)
            kernel.CreateMutexW.restype=wintypes.HANDLE
            kernel.CloseHandle.argtypes=(wintypes.HANDLE,)
            ctypes.set_last_error(0)
            handle=kernel.CreateMutexW(None,False,'Local\\'+self.name)
            error=ctypes.get_last_error()
            if not handle or error==183:
                if handle:kernel.CloseHandle(handle)
                raise RuntimeError('Another process already owns this bot consumer')
            self.handle=handle;self.kernel=kernel
        elif sys.platform.startswith('linux'):
            import socket
            connection=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
            try:connection.bind('\0'+self.name)
            except OSError:
                connection.close();raise RuntimeError('Another process already owns this bot consumer') from None
            self.socket=connection
        else:raise RuntimeError('Token-scoped process ownership supports Linux and Windows')
        return self

    def close(self):
        if self.socket is not None:self.socket.close();self.socket=None
        if self.handle is not None:self.kernel.CloseHandle(self.handle);self.handle=None
