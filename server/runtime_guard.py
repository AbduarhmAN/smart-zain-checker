"""Keep one application process per workspace, before starting Telegram."""
import atexit
import os
from pathlib import Path
import socket


class AppRuntimeGuard:
    def __init__(self, root):
        self.path = Path(root) / '.commerce' / 'app-runtime.lock'
        self.stream = None

    def acquire(self, host, ports):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open('a+b')
        try:
            if stream.seek(0, 2) == 0:
                stream.write(b'0'); stream.flush()
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            raise RuntimeError('التطبيق يعمل بالفعل لهذا المشروع. افتح نافذته الحالية بدل تشغيل نسخة ثانية.') from None
        self.stream = stream
        try:
            # Older application versions do not hold the workspace lock.
            for port in ports:
                family = socket.AF_INET6 if host == '::1' else socket.AF_INET
                with socket.socket(family, socket.SOCK_STREAM) as probe:
                    if os.name == 'nt':
                        probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                    probe.bind((host, port))
        except OSError:
            self.close()
            raise RuntimeError('منفذ التطبيق مستخدم بالفعل. أغلق النسخة السابقة قبل إعادة التشغيل.') from None
        atexit.register(self.close)
        return self

    def close(self):
        if self.stream is not None:
            stream, self.stream = self.stream, None
            stream.close()
