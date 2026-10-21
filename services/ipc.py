"""Loopback RPC with authenticated, durable acceptance and stable request IDs.

An HTTP timeout is not cancellation. Reuse the same request ID to observe the
accepted command. Unreconciled running mutations become 'uncertain' on restart.
"""
from __future__ import annotations
import hashlib
import hmac
import http.client
import os
import sqlite3
import threading
import time
import uuid
import re
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from .codec import dumps, loads

PORTS={'engine':18701,'files':18702,'browser':18703,'telegram':18704}
MAX_BODY=64*1024*1024
MAX_CONTROL_BODY=256*1024


def control_operation(operation,payload):
    return (operation in {'queue.request_customer_stop','engine.apply_stop','query_cancel',
        'verification_cancel','verification_submit','worker_stop'} or
        operation in {'engine.action','engine.session_action'} and isinstance(payload,dict)
        and payload.get('action') in {'pause','resume','pause_session','resume_session','apply_customer_stop'})


class BoundedHTTPServer(ThreadingHTTPServer):
    """Bound connections before creating threads, including header/body readers."""
    daemon_threads=True

    def __init__(self,*args,**kwargs):
        self.connections=threading.BoundedSemaphore(64)
        super().__init__(*args,**kwargs)

    def process_request(self,request,address):
        if not self.connections.acquire(False):
            try:
                body=b'{"error":"connection_capacity"}'
                request.settimeout(.1)
                request.sendall(b'HTTP/1.0 503 Service Unavailable\r\nConnection: close\r\nContent-Type: application/json\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body)
            except OSError:pass
            finally:self.shutdown_request(request)
            return
        try:super().process_request(request,address)
        except BaseException:
            self.connections.release()
            raise

    def process_request_thread(self,request,address):
        try:super().process_request_thread(request,address)
        finally:self.connections.release()


class ServiceUnavailable(RuntimeError): pass
class RequestPending(ServiceUnavailable): pass
class RemoteError(RuntimeError): pass
class RequestConflict(RemoteError): pass
class RetryLater(RuntimeError):
    def __init__(self,delay=1): self.delay=max(.1,min(float(delay),60))


def token():
    value=os.environ.get('ZAIN_IPC_TOKEN','')
    if len(value)<32 or not value.isascii() or any(character.isspace() for character in value): raise ValueError('ZAIN_IPC_TOKEN must contain at least 32 non-whitespace ASCII characters')
    return value


def port(service): return int(os.environ.get('ZAIN_'+service.upper()+'_PORT', PORTS[service]))


class RpcClient:
    def __init__(self, service, root=None):
        if service not in PORTS: raise ValueError('Unknown service')
        self.service=service; self.root=Path(root or os.environ.get('ZAIN_PROJECT_ROOT',Path.cwd())).resolve()

    def call(self, operation, payload=None, request_id=None, timeout=30):
        identifier=str(request_id or uuid.uuid4())
        body=dumps({'id':identifier,'operation':operation,'payload':payload or {}}).encode('utf-8')
        from .circuit import dependency_circuit
        circuit=dependency_circuit((self.service,str(self.root),port(self.service)))
        allowed,probe=circuit.before()
        if not allowed: raise ServiceUnavailable('Dependency circuit OPEN; request '+identifier+' not submitted')
        if probe:
            try: self.health()
            except ServiceUnavailable:
                circuit.failure();raise ServiceUnavailable('Dependency recovery probe failed; request '+identifier+' not submitted') from None
        deadline=time.monotonic()+timeout
        try:
            while True:
                remaining=deadline-time.monotonic()
                if remaining<=0:
                    circuit.success()
                    raise RequestPending('Request '+identifier+' is still pending; observe the same request ID')
                connection=http.client.HTTPConnection('127.0.0.1',port(self.service),timeout=min(remaining,125))
                try:
                    headers={'Authorization':'Bearer '+token(),'Content-Type':'application/json','X-Zain-Root':hashlib.sha256(str(self.root).encode()).hexdigest()}
                    if control_operation(operation,payload):headers['X-Zain-Control']='1'
                    connection.request('POST','/rpc',body,headers)
                    response=connection.getresponse(); raw=response.read(MAX_BODY+1)
                    if len(raw)>MAX_BODY: raise RemoteError('Oversized service response')
                    data=loads(raw)
                    if response.status==409: raise RequestConflict('Request ID reused with different arguments')
                    if response.status in (400,401,403,404,413):
                        circuit.success()
                        raise RemoteError(data.get('error','request_rejected'))
                    if response.status!=200: raise ServiceUnavailable('Service temporarily unavailable; request '+identifier)
                    state=data.get('state')
                    if state=='succeeded':circuit.success();return data.get('result')
                    if state in ('pending','running'):circuit.success();continue
                    if state=='uncertain': raise RequestPending('Request '+identifier+' requires reconciliation; retrying cannot create another effect')
                    circuit.success();raise RemoteError(data.get('error','operation_failed'))
                finally: connection.close()
        except TimeoutError:
            # A bounded observation timeout is not evidence that an accepted
            # operation failed. Its stable ID remains the reconciliation key.
            circuit.success()
            raise ServiceUnavailable('Observation timed out; inspect request '+identifier) from None
        except (OSError, http.client.HTTPException, ValueError):
            circuit.failure()
            raise ServiceUnavailable('Service '+self.service+' unavailable; request '+identifier) from None
        except RequestPending:
            circuit.success();raise
        except ServiceUnavailable:
            circuit.failure();raise

    def health(self,timeout=2):
        connection=http.client.HTTPConnection('127.0.0.1',port(self.service),timeout=timeout)
        try:
            connection.request('GET','/health',headers={'Authorization':'Bearer '+token()})
            response=connection.getresponse()
            if response.status!=200: raise ServiceUnavailable('Health unavailable')
            return loads(response.read(MAX_BODY))
        except (OSError,http.client.HTTPException,ValueError): raise ServiceUnavailable('Health unavailable') from None
        finally: connection.close()


class CommandStore:
    def __init__(self,path,namespace='test'):
        if not re.fullmatch('[a-z_]+',namespace): raise ValueError('Invalid table namespace')
        self.table='ipc_'+namespace+'_commands'
        self.path=Path(path); self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.execute('pragma journal_mode=PERSIST')
            db.execute(f'create table if not exists {self.table} (id text primary key, operation text not null, payload text not null, digest text not null, state text not null, result text, error text, created real not null, updated real not null, ready_at real not null default 0, priority integer not null default 0)')

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=5); db.row_factory=sqlite3.Row
        db.execute('pragma synchronous=FULL'); db.execute('pragma busy_timeout=5000')
        try:
            with db: yield db
        finally: db.close()

    def recover(self,retry_safe):
        with self.connect() as db:
            db.execute(f"update {self.table} set state='uncertain',error='interrupted_requires_reconciliation' where state='running'")
            for operation in retry_safe:
                db.execute(f"update {self.table} set state='pending',error=null where state='uncertain' and operation=?",(operation,))

    def accept(self,identifier,operation,payload,priority=False):
        if not isinstance(identifier,str) or not 1<=len(identifier)<=160: raise ValueError('Invalid request ID')
        encoded=dumps(payload); digest=hashlib.sha256((operation+'\0'+encoded).encode()).hexdigest(); now=time.time()
        with self.connect() as db:
            db.execute('begin immediate')
            old=db.execute(f'select digest from {self.table} where id=?',(identifier,)).fetchone()
            if old is not None and old['digest']!=digest: raise RequestConflict('Conflicting request replay')
            if old is None:
                backlog=db.execute(f"select count(*) from {self.table} where state in ('pending','running')").fetchone()[0]
                if backlog>=(272 if priority else 256): raise ServiceUnavailable('Service backlog full')
                db.execute(f'insert into {self.table} (id,operation,payload,digest,state,result,error,created,updated,priority) values (?,?,?,?,?,?,?,?,?,?)',(identifier,operation,encoded,digest,'pending',None,None,now,now,int(priority)))
        return self.get(identifier)

    def get(self,identifier):
        with self.connect() as db: row=db.execute(f'select id,state,result,error from {self.table} where id=?',(identifier,)).fetchone()
        if row is None: return None
        return {'id':row['id'],'state':row['state'],'result':loads(row['result']) if row['result'] is not None else None,'error':row['error']}

    def claim(self,control_only=False):
        with self.connect() as db:
            # Idle consumers must not repeatedly acquire SQLite's single writer
            # lock. Recheck the candidate after acquiring it for a real claim.
            query=f"select * from {self.table} where state='pending' and ready_at<=? {'and priority=1' if control_only else ''} order by priority desc,created,id limit 1"
            if db.execute(query,(time.time(),)).fetchone() is None:return None
            db.execute('begin immediate')
            row=db.execute(query,(time.time(),)).fetchone()
            if row is None: return None
            db.execute(f"update {self.table} set state='running',updated=? where id=?",(time.time(),row['id']))
            return row['id'],row['operation'],loads(row['payload'])

    def finish(self,identifier,result=None,error=None):
        encoded=None if error else dumps(result)
        with self.connect() as db:
            db.execute(f'update {self.table} set state=?,result=?,error=?,updated=? where id=?',('failed' if error else 'succeeded',encoded,error,time.time(),identifier))

    def defer(self,identifier,delay):
        with self.connect() as db:
            db.execute(f"update {self.table} set state='pending',ready_at=?,updated=? where id=?",(time.time()+delay,time.time(),identifier))


class RpcServer:
    def __init__(self,service,root,handlers,health=None,workers=2,retry_safe=(),read_only=()):
        self.service=service; self.root=Path(root).resolve(); self.handlers=dict(handlers)
        self.health_callback=health or (lambda:{})
        from .persistence import database_path
        self.store=CommandStore(database_path(self.root),namespace=service)
        self.store.recover(retry_safe)
        self.stop_event=threading.Event(); self.wake=threading.Event(); self.threads=[]
        self.auth='Bearer '+token(); self.worker_count=workers
        self.read_only=frozenset(read_only)
        self.admission=threading.BoundedSemaphore(32)
        self.control_admission=threading.BoundedSemaphore(8)
        self.storage_unavailable=False
        self.started=False
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                self.request.settimeout(10)
                super().setup()
            def log_message(self,*_): pass
            def reply(self,status,data):
                encoded=dumps(data).encode('utf-8')
                try:
                    self.send_response(status); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(encoded))); self.end_headers(); self.wfile.write(encoded)
                except OSError: pass
            def authorized(self): return hmac.compare_digest(self.headers.get('Authorization',''),owner.auth)
            def do_GET(self):
                if not self.authorized(): self.reply(401,{'error':'unauthorized'}); return
                if self.path!='/health': self.reply(404,{}); return
                try: detail=owner.health_callback()
                except Exception: detail={'ready':False,'error':'health_dependency_unavailable'}
                self.reply(200,{'service':service,'pid':os.getpid(),'workers':owner.worker_count,'storage_unavailable':owner.storage_unavailable,**detail})
            def do_POST(self):
                if not self.authorized(): self.reply(401,{'error':'unauthorized'}); return
                expected=hashlib.sha256(str(owner.root).encode()).hexdigest()
                if self.headers.get('X-Zain-Root')!=expected: self.reply(403,{'error':'wrong_workspace'}); return
                if self.path!='/rpc': self.reply(404,{}); return
                admission=None
                try:
                    length=int(self.headers.get('Content-Length','0'))
                    if not 0<length<=MAX_BODY: raise ValueError('Invalid request size')
                    control_requested=self.headers.get('X-Zain-Control')=='1'
                    if control_requested and length>MAX_CONTROL_BODY:raise ValueError('Oversized control request')
                    admission=owner.control_admission if control_requested else owner.admission
                    if not admission.acquire(False):admission=None;self.reply(503,{'error':'busy'});return
                    data=loads(self.rfile.read(length))
                    if not isinstance(data,dict) or set(data)!={'id','operation','payload'}: raise ValueError('Invalid envelope')
                    operation=data['operation']
                    if not isinstance(operation,str) or operation not in owner.handlers or not isinstance(data['payload'],dict): raise ValueError('Unknown operation')
                    priority=control_operation(operation,data['payload'])
                    if control_requested and not priority:raise ValueError('Control lane requires control operation')
                    if operation in owner.read_only:
                        self.reply(200,{'id':data['id'],'state':'succeeded','result':owner.handlers[operation](data['payload'])}); return
                    state=owner.store.accept(data['id'],operation,data['payload'],priority=priority); owner.wake.set()
                    deadline=time.monotonic()+120
                    while state['state'] in ('pending','running') and time.monotonic()<deadline and not owner.stop_event.is_set():
                        owner.stop_event.wait(.025); state=owner.store.get(data['id'])
                    self.reply(200,state)
                except RequestConflict: self.reply(409,{'error':'idempotency_conflict'})
                except (ValueError,TypeError,KeyError): self.reply(400,{'error':'invalid_request'})
                except (OSError,sqlite3.Error,ServiceUnavailable): self.reply(503,{'error':'storage_or_capacity_unavailable'})
                except Exception: self.reply(500,{'error':'operation_unavailable'})
                finally:
                    if admission is not None: admission.release()
        self.http=BoundedHTTPServer(('127.0.0.1',port(service)),Handler)

    def _work(self,control_only=False):
        while not self.stop_event.is_set():
            try: task=self.store.claim(control_only=control_only)
            except (OSError,sqlite3.Error): self.storage_unavailable=True;self.stop_event.wait(.25);continue
            if task is None: self.wake.wait(.1); self.wake.clear(); continue
            identifier,operation,payload=task
            try: result=self.handlers[operation](payload)
            except RetryLater as exc:
                delay=exc.delay
                persist=lambda:self.store.defer(identifier,delay)
            except Exception as exc:
                # No exception messages: they can contain tokens, records or proxies.
                error=type(exc).__name__
                persist=lambda:self.store.finish(identifier,error=error)
            else:persist=lambda:self.store.finish(identifier,result)
            # A failed acknowledgement write must not re-execute the handler or
            # kill its worker. Keep the known outcome until persistence returns.
            while not self.stop_event.is_set():
                try:persist();self.storage_unavailable=False;break
                except (OSError,sqlite3.Error):self.storage_unavailable=True;self.stop_event.wait(.25)

    def start(self):
        self.started=True
        for index in range(self.worker_count):
            thread=threading.Thread(target=self._work,name=f'{self.service}-rpc-{index}',daemon=True); thread.start(); self.threads.append(thread)
        thread=threading.Thread(target=self._work,args=(True,),name=f'{self.service}-control',daemon=True);thread.start();self.threads.append(thread)
        thread=threading.Thread(target=self.http.serve_forever,name=f'{self.service}-http',daemon=True); thread.start(); self.threads.append(thread)

    def stop(self):
        self.stop_event.set(); self.wake.set()
        if self.started:self.http.shutdown()
        self.http.server_close()
        for thread in self.threads: thread.join(timeout=2)
