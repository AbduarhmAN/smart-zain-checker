"""Account connection lifetime and result correctness. No external I/O."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import gzip
import http.client
import json
import ssl
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import Mock,patch
from urllib.parse import urlsplit
from workers import account_transport as transport
from workers import zain_api as api
from workers.actor import WorkerActor

URL='https://example.invalid/accounts/10001'
PROXY='socks5://user:private@proxy.invalid:1080'


def payload(amount='12.34'):
    return json.dumps({'is_successful':True,'data':{'due_amount':amount}}).encode()


class Reply:
    def __init__(self,body=None,status=200,headers=None,will_close=False):
        self.body=payload() if body is None else body;self.status=status
        self.headers=headers or {};self.will_close=will_close;self.closed=False
    def read(self,limit):return self.body[:limit]
    def getheader(self,key):return self.headers.get(key)
    def close(self):self.closed=True


class Connection:
    def __init__(self,replies=()):
        self.replies=deque(replies);self.requests=[];self.sock=Mock();self.closed=0;self.connected=0
    def connect(self):self.connected+=1;self.sock=Mock()
    def request(self,method,path,headers):
        self.requests.append((method,path,headers))
        if self.replies and isinstance(self.replies[0],BaseException):raise self.replies.popleft()
    def getresponse(self):return self.replies.popleft() if self.replies else Reply()
    def close(self):self.closed+=1;self.sock=None


class Clock:
    def __init__(self):self.now=100.0;self.sleeps=[]
    def monotonic(self):return self.now
    def sleep(self,value):self.sleeps.append(value);self.now+=value


class AccountTransportTests(unittest.TestCase):
    def setUp(self):
        transport.close_account_sessions()
        self.addCleanup(transport.close_account_sessions)
        self.real_factory=transport._new_connection
        self.factory=patch.object(transport,'_new_connection',side_effect=lambda *a:Connection()).start()
        self.alive=patch.object(transport,'_socket_alive',return_value=True).start()
        patch.object(transport,'_keep_tcp_alive').start()
        patch('socket.create_connection',side_effect=AssertionError('External I/O forbidden')).start()
        self.addCleanup(patch.stopall)

    def request(self,key='worker',proxy=PROXY):
        return transport.request_account(URL,{'accept':'application/json'},proxy=proxy,connection_key=key)

    def test_100_queries_reuse_one_connection_without_pings_or_rotation(self):
        for _ in range(100):self.assertEqual(self.request().status,200)
        self.assertEqual(self.factory.call_count,1)
        s=transport.account_session(URL,PROXY,'worker')
        self.assertEqual(len(s.conn.requests),100);self.assertEqual(s.stats['reused_responses'],99)
        self.assertTrue(all(r[2]['Connection']=='keep-alive' for r in s.conn.requests))

    def test_10_parallel_workers_100_each_have_exactly_10_connections(self):
        barrier=threading.Barrier(10)
        def worker(index):
            barrier.wait()
            for _ in range(100):self.request('worker_'+str(index))
        with ThreadPoolExecutor(max_workers=10) as executor:list(executor.map(worker,range(10)))
        self.assertEqual(self.factory.call_count,10)
        self.assertEqual(sum(s.stats['responses'] for s in transport._POOL.values()),1000)

    def test_same_worker_is_serialized_and_other_workers_do_not_share_results(self):
        with ThreadPoolExecutor(max_workers=4) as executor:list(executor.map(lambda _:self.request(),range(20)))
        self.assertEqual(self.factory.call_count,1)
        self.assertFalse(self.request('another').reused)
        self.assertEqual(self.factory.call_count,2)

    def test_credentials_and_routes_are_separate_pool_keys(self):
        self.request();self.request(proxy='socks5://other:other@proxy.invalid:1080')
        self.request(proxy='socks5://user:private@elsewhere.invalid:1080')
        self.assertEqual(self.factory.call_count,3)

    def test_http_400_is_consumed_and_connection_is_kept(self):
        conn=Connection([Reply(b'{"is_successful":false}',400),Reply()]);self.factory.return_value=conn;self.factory.side_effect=None
        self.assertEqual(self.request().status,400);self.assertTrue(self.request().reused)
        self.assertEqual(conn.connected,1);self.assertEqual(conn.closed,0)

    def test_server_close_header_is_respected(self):
        first=Connection([Reply(will_close=True)]);second=Connection()
        self.factory.side_effect=[first,second]
        self.request();self.assertFalse(self.request().reused)
        self.assertEqual(self.factory.call_count,2);self.assertTrue(first.closed)

    def test_peer_eof_is_detected_before_sending_another_query(self):
        first=Connection();second=Connection();self.factory.side_effect=[first,second]
        self.request();self.alive.return_value=False
        self.request()
        self.assertEqual(len(first.requests),1);self.assertEqual(len(second.requests),1)

    def test_stale_reused_socket_gets_only_one_retry_on_the_same_proxy(self):
        first=Connection([Reply(),BrokenPipeError()]);second=Connection()
        self.factory.side_effect=[first,second]
        self.request();reply=self.request()
        self.assertEqual(reply.attempts,2);self.assertEqual(self.factory.call_count,2)
        self.assertTrue(all(c.args[1]==PROXY for c in self.factory.call_args_list))
        self.assertEqual(transport.account_session(URL,PROXY,'worker').stats['stale_retries'],1)

    def test_second_failed_connection_is_not_retried_again(self):
        first=Connection([Reply(),ConnectionResetError()]);second=Connection([ConnectionResetError()])
        self.factory.side_effect=[first,second];self.request()
        with self.assertRaises(transport.AccountTransportError):self.request()
        self.assertEqual(self.factory.call_count,2)

    def test_read_timeout_is_not_replayed_or_reused(self):
        first=Connection([Reply(),TimeoutError()]);self.factory.side_effect=[first,Connection()]
        self.request()
        with self.assertRaises(transport.AccountTransportError):self.request()
        self.assertEqual(self.factory.call_count,1);self.assertTrue(first.closed)
        clock=Clock()
        with patch.object(transport,'time',clock):
            session=transport.account_session(URL,PROXY,'worker');session.retry_until=clock.now+2
            self.request()
        self.assertEqual(clock.sleeps,[2]);self.assertEqual(self.factory.call_count,2)

    def test_retry_after_waits_without_reconnecting(self):
        conn=Connection([Reply(b'{}',429,{'Retry-After':'3'}),Reply()])
        self.factory.side_effect=None;self.factory.return_value=conn;clock=Clock()
        with patch.object(transport,'time',clock):self.request();self.assertTrue(self.request().reused)
        self.assertEqual(clock.sleeps,[3]);self.assertEqual(self.factory.call_count,1)

    def test_long_retry_after_does_not_close_or_send_another_request(self):
        conn=Connection([Reply(b'{}',429,{'Retry-After':'120'}),Reply()])
        self.factory.side_effect=None;self.factory.return_value=conn;clock=Clock()
        with patch.object(transport,'time',clock):
            self.request()
            with self.assertRaisesRegex(transport.AccountTransportError,'route_cooling_down'):self.request()
            self.assertEqual(transport.account_retry_after(URL,PROXY,'worker'),120)
        self.assertEqual(len(conn.requests),1);self.assertEqual(conn.closed,0)

    def test_valid_gzip_is_decoded_and_connection_reused(self):
        conn=Connection([Reply(gzip.compress(payload()),headers={'Content-Encoding':'gzip'}),Reply()])
        self.factory.side_effect=None;self.factory.return_value=conn
        self.assertEqual(json.loads(self.request().body)['data']['due_amount'],'12.34')
        self.assertTrue(self.request().reused)

    def test_gzip_bomb_is_bounded_and_never_becomes_a_result(self):
        conn=Connection([Reply(gzip.compress(b'x'*(transport.MAX_BODY_BYTES+10)),headers={'Content-Encoding':'gzip'})])
        self.factory.side_effect=None;self.factory.return_value=conn
        with self.assertRaisesRegex(transport.AccountTransportError,'response_too_large'):self.request()

    def test_invalid_proxy_never_falls_back_to_direct(self):
        for proxy in ('invalid','socks5://proxy.invalid:wrong','ftp://proxy.invalid:21'):
            with self.subTest(proxy=proxy),self.assertRaises(ValueError):self.request(proxy=proxy)
        self.factory.assert_not_called()

    def test_http_proxy_uses_authenticated_connect(self):
        with patch.object(http.client,'HTTPSConnection') as factory:
            self.real_factory(urlsplit(URL),'http://u:p@proxy.invalid:80',12)
        factory.assert_called_once_with('proxy.invalid',80,timeout=12)
        self.assertEqual(factory.return_value.set_tunnel.call_args.args,('example.invalid',443))
        self.assertIn('Proxy-Authorization',factory.return_value.set_tunnel.call_args.kwargs['headers'])


class AccountClassificationTests(unittest.TestCase):
    def query(self,status,body):
        reply=transport.AccountReply(status,json.dumps(body).encode(),True,1)
        with patch.object(transport,'request_account',return_value=reply):return api.query_direct_business_api('10001')

    def test_confirmed_provider_not_found_code_is_a_negative_result_not_zero(self):
        result=self.query(400,{'is_successful':False,'response_code':'EZB5029'})
        self.assertEqual(result[:2],('not_found',None))

    def test_arbitrary_400_and_auth_failures_are_never_not_found(self):
        for status,body in ((400,{'is_successful':False,'response_code':'OTHER'}),
                            (403,{'is_successful':False,'response_code':'EZB5029'}),
                            (400,{'is_successful':True,'response_code':'EZB5029'})):
            self.assertEqual(self.query(status,body)[0],'error')

    def test_invalid_amounts_never_become_zero_balance(self):
        for amount in (None,True,False,'NaN','Infinity','-1'):
            with self.subTest(amount=amount):
                self.assertEqual(self.query(200,{'is_successful':True,'data':{'due_amount':amount}})[0],'unknown_response')

    def test_zero_is_valid_and_distinct_from_not_found(self):
        self.assertEqual(self.query(200,{'is_successful':True,'data':{'due_amount':'0'}})[:2],('ok',0.0))

    def test_html_503_is_transient_not_a_permanent_bad_account(self):
        with patch.object(transport,'request_account',return_value=transport.AccountReply(503,b'<html>busy</html>',True,1)):
            self.assertEqual(api.query_direct_business_api('10001')[0],'network_error')

    def test_network_exception_does_not_expose_credentials(self):
        with patch.object(transport,'request_account',side_effect=OSError('private-password-and-proxy')):
            result=api.query_direct_business_api('10001')
        self.assertEqual(result[0],'network_error');self.assertNotIn('private-password',result[2])

    def test_account_worker_with_missing_proxy_cannot_use_direct_route(self):
        actor=WorkerActor('worker','worker',Path('.'),Path('.'),use_proxy=True,proxy_url=None)
        with patch.object(api,'query_contract_due_amount') as query:
            self.assertEqual(actor.execute_task_api('10001')[0],'error')
        query.assert_not_called()

    def test_worker_passes_stable_connection_identity(self):
        actor=WorkerActor('worker','worker',Path('.'),Path('.'),use_proxy=True,proxy_url=PROXY)
        with patch.object(api,'query_contract_due_amount',return_value=('ok',0,'ok')) as query:
            actor.execute_task_api('10001')
        self.assertEqual(query.call_args.kwargs['connection_key'],'worker')

    def test_long_route_cooldown_is_deferred_not_reported_as_bad_account(self):
        with patch.object(transport,'request_account',side_effect=transport.AccountTransportError('route_cooling_down')):
            self.assertEqual(api.query_direct_business_api('10001')[0],'account_backoff')


class IdleSocketTests(unittest.TestCase):
    def test_idle_healthy_socket_causes_no_probe_request(self):
        sock=Mock();sock.pending.return_value=0
        with patch.object(transport.select,'select',return_value=([],[],[])):
            self.assertTrue(transport._socket_alive(sock))
        sock.recv.assert_not_called()

    def test_tls_control_record_want_read_preserves_socket_and_timeout(self):
        sock=Mock();sock.gettimeout.return_value=30;sock.recv.side_effect=ssl.SSLWantReadError()
        with patch.object(transport.select,'select',return_value=([sock],[],[])):
            self.assertTrue(transport._socket_alive(sock))
        sock.settimeout.assert_called_once_with(30)

    def test_peer_eof_is_conclusive_without_a_get_request(self):
        sock=Mock();sock.recv.return_value=b''
        with patch.object(transport.select,'select',return_value=([sock],[],[])):
            self.assertFalse(transport._socket_alive(sock))


if __name__=='__main__':unittest.main()
