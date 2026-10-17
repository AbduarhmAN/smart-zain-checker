"""Persistent native account API connections, isolated by worker and route.

No account result caching, HTTP keep-alive pings, proxy switching, direct
fallback or periodic socket rotation. A complete HTTP response (including a
business error) leaves its connection reusable. Ambiguous/incomplete responses
must be discarded so that one account can never receive another account's reply.
"""
from __future__ import annotations
import atexit
import base64
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime,timezone
from email.utils import parsedate_to_datetime
import gzip
import hashlib
import http.client
import io
import math
import re
import select
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit,unquote

MAX_BODY_BYTES=2*1024*1024
MAX_SESSIONS=256
_POOL=OrderedDict()
_POOL_LOCK=threading.RLock()


class AccountTransportError(RuntimeError):
    def __init__(self,reason):
        super().__init__(reason)
        self.reason=reason


@dataclass(frozen=True)
class AccountReply:
    status:int
    body:bytes
    reused:bool
    attempts:int


def _retry_seconds(value):
    if not value:return 0.0
    try:
        seconds=float(value)
    except ValueError:
        try:seconds=(parsedate_to_datetime(value)-datetime.now(timezone.utc)).total_seconds()
        except (ValueError,TypeError,OverflowError):return 0.0
    return max(0.0,seconds) if math.isfinite(seconds) else 0.0


def _socket_alive(sock):
    """Process pending TLS control records without a request or a blocking read.

    There is no outstanding response at this point. Any application byte is
    unsolicited, and any EOF is conclusive. SSLWantRead means a healthy idle
    socket; it is not a timed-out buffered HTTP reader.
    """
    if sock is None:return False
    try:
        ready,_,errors=select.select([sock],[],[sock],0)
        if errors:return False
        if not ready and not sock.pending():return True
        timeout=sock.gettimeout()
        try:
            sock.setblocking(False)
            try:data=sock.recv(1)
            except (ssl.SSLWantReadError,ssl.SSLWantWriteError,BlockingIOError):return True
            return False  # EOF or unexpected bytes: never mix response streams.
        finally:sock.settimeout(timeout)
    except (OSError,ValueError):return False


def _new_connection(target,proxy,timeout):
    if target.scheme!='https' or not target.hostname:raise ValueError('HTTPS account target required')
    if not proxy:
        return http.client.HTTPSConnection(target.hostname,target.port or 443,timeout=timeout)
    p=urlsplit(proxy)
    if not p.hostname:raise ValueError('Proxy hostname required')
    if p.scheme.lower() in ('socks5','socks5h','socks4','socks4a'):
        import socks
        from sockshandler import SocksiPyConnectionS
        return SocksiPyConnectionS(socks.SOCKS5 if '5' in p.scheme else socks.SOCKS4,
            p.hostname,p.port or 1080,True,unquote(p.username) if p.username else None,
            unquote(p.password) if p.password else None,
            host=target.hostname,port=target.port or 443,timeout=timeout)
    if p.scheme.lower()!='http':raise ValueError('Unsupported proxy scheme')
    conn=http.client.HTTPSConnection(p.hostname,p.port or 8080,timeout=timeout)
    tunnel_headers={}
    if p.username is not None:
        auth=unquote(p.username)+':'+unquote(p.password or '')
        tunnel_headers['Proxy-Authorization']='Basic '+base64.b64encode(auth.encode()).decode()
    conn.set_tunnel(target.hostname,target.port or 443,headers=tunnel_headers)
    return conn


def _keep_tcp_alive(sock):
    # TCP probes detect dead peers after a long idle period. They send no HTTP
    # request and perform no TLS handshake. A busy connection needs no probes.
    try:
        sock.setsockopt(socket.SOL_SOCKET,socket.SO_KEEPALIVE,1)
        if hasattr(socket,'SIO_KEEPALIVE_VALS'):
            sock.ioctl(socket.SIO_KEEPALIVE_VALS,(1,60000,15000))
        elif hasattr(socket,'TCP_KEEPIDLE'):
            sock.setsockopt(socket.IPPROTO_TCP,socket.TCP_KEEPIDLE,60)
            sock.setsockopt(socket.IPPROTO_TCP,socket.TCP_KEEPINTVL,15)
    except (OSError,AttributeError):pass


class AccountSession:
    def __init__(self,target,proxy):
        self.target=target;self.proxy=proxy;self.lock=threading.RLock()
        self.conn=None;self.failures=0;self.retry_until=0.0;self.idle_until=0.0
        self.last_used=time.monotonic()
        self.last_attempt={}
        self.stats={'logical_queries':0,'wire_requests':0,'connections_opened':0,
                    'reused_responses':0,'responses':0,'transport_errors':0,
                    'stale_retries':0,'peer_closed_before_request':0,'backoff_seconds':0.0,
                    'compressed_response_bytes':0,'server_close_responses':0}

    def _drop(self):
        if self.conn is not None:self.conn.close()
        self.conn=None;self.idle_until=0.0

    def close(self):
        with self.lock:self._drop()

    def request(self,path,headers,timeout):
        if not math.isfinite(timeout) or timeout<=0:raise ValueError('Positive finite timeout required')
        with self.lock:
            self.stats['logical_queries']+=1
            self.last_attempt={'http_status':None,'response_complete':False,'wire_attempts':0}
            deadline=time.monotonic()+timeout
            delay=max(0.0,self.retry_until-time.monotonic())
            if delay:
                if delay>=timeout:raise AccountTransportError('route_cooling_down')
                self.stats['backoff_seconds']+=delay;time.sleep(delay)
            last_reason='connection_failed'
            for attempt in (1,2):
                reused=False
                try:
                    remaining=deadline-time.monotonic()
                    if remaining<=0:raise TimeoutError()
                    if self.conn is not None:
                        expired=self.idle_until and self.idle_until<=time.monotonic()
                        if expired or not _socket_alive(self.conn.sock):
                            self.stats['peer_closed_before_request']+=1;self._drop()
                    reused=self.conn is not None
                    if not reused:
                        self.conn=_new_connection(self.target,self.proxy,min(8.0,remaining))
                        self.conn.connect()
                        self.stats['connections_opened']+=1
                        _keep_tcp_alive(self.conn.sock)
                    remaining=deadline-time.monotonic()
                    if remaining<=0:raise TimeoutError()
                    self.conn.sock.settimeout(remaining)
                    self.stats['wire_requests']+=1
                    self.last_attempt['wire_attempts']+=1
                    self.conn.request('GET',path,headers=headers)
                    response=self.conn.getresponse()
                    status=response.status
                    self.last_attempt.update(http_status=status,server_will_close=response.will_close,
                        connection_header=response.getheader('Connection'),keep_alive_hint=response.getheader('Keep-Alive'))
                    body=response.read(MAX_BODY_BYTES+1)
                    if len(body)>MAX_BODY_BYTES:
                        response.close();self._drop()
                        raise AccountTransportError('response_too_large')
                    compressed=len(body)
                    encoding=(response.getheader('Content-Encoding') or '').strip().lower()
                    try:
                        if encoding=='gzip':
                            with gzip.GzipFile(fileobj=io.BytesIO(body)) as stream:body=stream.read(MAX_BODY_BYTES+1)
                        elif encoding not in ('','identity'):
                            raise AccountTransportError('unsupported_response_encoding')
                        if len(body)>MAX_BODY_BYTES:raise AccountTransportError('response_too_large')
                    finally:
                        response.close()
                    if response.will_close:
                        self.stats['server_close_responses']+=1;self._drop()
                    else:
                        hint=response.getheader('Keep-Alive') or ''
                        match=re.search(r'(?:^|[,;\s])timeout\s*=\s*(\d+)',hint,re.I)
                        self.idle_until=time.monotonic()+max(0,int(match[1])-.1) if match else 0.0
                    self.failures=0;self.retry_until=0.0
                    if status in (429,500,502,503,504):
                        self.retry_until=time.monotonic()+max(1.0,_retry_seconds(response.getheader('Retry-After')))
                    self.last_used=time.monotonic()
                    self.stats['responses']+=1;self.stats['reused_responses']+=int(reused)
                    self.stats['compressed_response_bytes']+=compressed
                    self.last_attempt.update(response_complete=True,response_body_bytes=len(body),
                        wire_body_bytes=compressed,content_encoding=encoding,reused=reused)
                    return AccountReply(status,body,reused,attempt)
                except AccountTransportError:
                    # The HTTP stream was consumed or discarded; never invent
                    # an account outcome from a malformed/oversized response.
                    raise
                except (OSError,http.client.HTTPException) as error:
                    self._drop();self.stats['transport_errors']+=1
                    last_reason='timeout' if isinstance(error,TimeoutError) else 'connection_failed'
                    try:
                        import socks
                        if isinstance(error,socks.SOCKS5AuthError):last_reason='proxy_authentication_failed'
                    except ImportError:pass
                    stale=isinstance(error,(http.client.RemoteDisconnected,BrokenPipeError,ConnectionResetError))
                    if reused and stale and attempt==1 and deadline-time.monotonic()>1:
                        # GET is idempotent. Exactly one retry, on this same
                        # configured proxy; a read timeout is never replayed here.
                        self.stats['stale_retries']+=1
                        continue
                    self.failures+=1
                    self.retry_until=time.monotonic()+min(8.0,2.0**min(self.failures-1,3))
                    raise AccountTransportError(last_reason) from None
            raise AccountTransportError(last_reason)


def account_session(url,proxy=None,connection_key=None):
    target=urlsplit(url)
    clean_proxy=str(proxy).strip() if proxy else None
    if clean_proxy:
        parsed=urlsplit(clean_proxy)
        if parsed.scheme.lower() not in ('socks5','socks5h','socks4','socks4a','http') or not parsed.hostname:
            raise ValueError('Invalid proxy configuration')
        _=parsed.port
    identity=connection_key if connection_key is not None else ('thread',threading.get_ident())
    key=(identity,target.scheme,target.hostname,target.port or 443,
         hashlib.sha256((clean_proxy or 'direct').encode()).digest())
    with _POOL_LOCK:
        session=_POOL.get(key)
        if session is None:
            if len(_POOL)>=MAX_SESSIONS:
                for old_key,old in list(_POOL.items()):
                    if old.lock.acquire(blocking=False):
                        try:old._drop();del _POOL[old_key]
                        finally:old.lock.release()
                        break
                else:raise AccountTransportError('connection_pool_busy')
            session=_POOL[key]=AccountSession(target,clean_proxy)
        _POOL.move_to_end(key)
        return session


def request_account(url,headers,*,proxy=None,connection_key=None,timeout=30.0):
    target=urlsplit(url)
    if target.query or target.fragment:raise ValueError('Unexpected account query URL')
    session=account_session(url,proxy,connection_key)
    final_headers={'Host':target.netloc,**{k.title():v for k,v in headers.items()},
                   'Accept-Encoding':'gzip','Connection':'keep-alive'}
    return session.request(target.path,final_headers,float(timeout))


def close_account_sessions():
    with _POOL_LOCK:
        sessions=list(_POOL.values());_POOL.clear()
    for session in sessions:session.close()


def account_retry_after(url,proxy=None,connection_key=None):
    session=account_session(url,proxy,connection_key)
    with session.lock:return max(0.0,session.retry_until-time.monotonic())


atexit.register(close_account_sessions)
