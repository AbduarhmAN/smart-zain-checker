"""python -m services <engine|telegram|files|browser> --root <workspace>."""
import argparse
import importlib
import logging
import os
from pathlib import Path
import signal
import threading
from .ipc import PORTS, RpcServer, token
from .locking import ServiceLock


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('service',choices=PORTS)
    parser.add_argument('--root',type=Path,required=True)
    args=parser.parse_args(); root=args.root.resolve()
    if not root.is_dir(): parser.error('Workspace root must exist')
    token()  # Validate before loading settings or touching queue state.
    os.environ['ZAIN_SERVICE_MODE']='1'; os.environ['ZAIN_SERVICE_ROLE']=args.service
    os.environ['ZAIN_PROJECT_ROOT']=str(root)
    from .logging import configure
    configure(args.service)
    lock=ServiceLock(root/'.services'/'locks'/(args.service+'.lock')).acquire()
    runtime_guard=None; application=None; server=None; stopped=threading.Event()
    try:
        if args.service=='engine':
            from server.runtime_guard import AppRuntimeGuard
            runtime_guard=AppRuntimeGuard(root).acquire('127.0.0.1',())
        module=importlib.import_module('services.'+args.service)
        application=module.build(root) if hasattr(module,'build') else None
        handlers=application.handlers() if application else module.handlers(root)
        read_only={'engine.snapshot','engine.records','queue.get_jobs','queue.get_job','event.list',
            'query_status','verification_list','verification_image','worker_status','health','telegram.status'}
        server=RpcServer(args.service,root,handlers,
            health=application.health if application else lambda:{'ready':True},
            read_only=(read_only | set(getattr(application or module,'read_only',()))) & handlers.keys(),workers=2,
            retry_safe=getattr(application or module,'retry_safe',()))
        def shutdown(*_): stopped.set()
        signal.signal(signal.SIGINT,shutdown); signal.signal(signal.SIGTERM,shutdown)
        server.start()
        if application: application.start()
        logging.getLogger('Services').info('service=%s pid=%s ready',args.service,os.getpid())
        while not stopped.wait(.5): pass
    finally:
        if server: server.stop()
        if application: application.stop()
        elif 'module' in locals() and hasattr(module,'shutdown'): module.shutdown()
        if runtime_guard: runtime_guard.close()
        lock.close()


if __name__=='__main__': main()
