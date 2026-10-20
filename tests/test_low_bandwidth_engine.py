"""Offline acceptance: exact amounts, streaming, resource bounds and route isolation."""
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from engine_v3.low_bandwidth_checker import LowBandwidthChecker,parse_page,amount_decimal,WRITE_ABORT
from engine_v3.proxy_scheduler import CentralizedCooldownScheduler,RouteNotReady,route_key
import workers.low_bandwidth_transport as transport

NUMBER='2000000001'
GOOD=b'var quickpayData = {"amount":"91.39","account":"1000000001"};'


class WriteStopped(Exception):
    code=23
    def __init__(self,response):self.response=response


class FakeSession:
    def __init__(self,chunks=None,code=200,url=None,error=None):
        self.chunks=chunks or [GOOD];self.code=code;self.url=url or 'https://app.sa.zain.com/ar/quickpay?account='+NUMBER
        self.error=error;self.closed=False;self.received=0;self.kwargs=None
    def get(self,url,**kwargs):
        self.kwargs=kwargs
        if self.error:raise self.error
        response=SimpleNamespace(status_code=self.code,url=self.url,headers={'Retry-After':'120'},download_size=4000,header_size=500,request_size=300)
        for chunk in self.chunks:
            self.received+=1
            if kwargs['content_callback'](chunk)==WRITE_ABORT:raise WriteStopped(response)
        return response
    def close(self):self.closed=True


class CheckerTests(unittest.TestCase):
    def check(self,session,**kwargs):
        factory=Mock(return_value=session)
        result=LowBandwidthChecker.check_service_number(NUMBER,session_factory=factory,**kwargs)
        self.assertTrue(session.closed)
        return result
    def test_verified_bill_aborts_before_footer_and_closes_session(self):
        session=FakeSession([GOOD,b'footer'*10000]);result=self.check(session)
        self.assertEqual((result['status'],result['amount']),('SUCCESS','91.39'))
        self.assertEqual(session.received,1);self.assertTrue(result['early_abort'])
        self.assertNotIn('wire_bytes',result)
    def test_only_authoritative_numeric_zero_is_success(self):
        self.assertEqual(parse_page('var quickpayData={"amount":0,"account":"1"};')['amount'],'0.00')
        for page in ('0.00 ر.س','لا توجد فواتير مستحقة','تسجيل الدخول أو إنشاء حساب','لا تعرف رقمك',''):
            with self.subTest(page=page):self.assertEqual(parse_page(page)['status'],'UNVERIFIED')
    def test_invalid_amounts_and_missing_account_never_produce_a_bill(self):
        for amount in (None,False,True,'','NaN','Infinity','-1','12.345','1e100','no amount'):
            with self.subTest(amount=amount):
                body='var quickpayData='+json.dumps({'amount':amount,'account':'1'})+';'
                self.assertEqual(parse_page(body)['status'],'UNVERIFIED')
        self.assertEqual(parse_page('var quickpayData={"amount":0};')['status'],'UNVERIFIED')
    def test_split_utf8_and_split_json_preserve_exact_amount(self):
        raw=('اختبار '+'var quickpayData={"account":"1","amount":123456789.99};').encode()
        result=self.check(FakeSession([raw[:1],raw[1:7],raw[7:39],raw[39:]]))
        self.assertEqual(result['amount'],'123456789.99')
    def test_nested_json_and_escaped_braces(self):
        body='var quickpayData={"meta":{"label":"}"},"amount":"1,234.50","account":"1"};'
        self.assertEqual(parse_page(body)['amount'],'1234.50')
    def test_http_errors_or_wrong_response_origin_cannot_be_paid_successes(self):
        for code,status in ((302,'UNVERIFIED'),(401,'WAF_REJECTED'),(403,'WAF_REJECTED'),(429,'RATE_LIMITED'),(500,'UNVERIFIED')):
            result=self.check(FakeSession(code=code));self.assertEqual(result['status'],status);self.assertIsNone(result['amount'])
        result=self.check(FakeSession(url='https://example.invalid/?account='+NUMBER))
        self.assertEqual(result['status'],'UNVERIFIED')
    def test_network_failure_hides_credentials_and_is_not_retried(self):
        session=FakeSession(error=RuntimeError('secret credential'))
        result=self.check(session);self.assertEqual(result['error'],'network_unavailable')
        self.assertIsNone(result['amount']);self.assertNotIn('secret',str(result))
    def test_challenges_stop_without_a_zero_balance(self):
        for body,expected in ((b'The requested URL was rejected','WAF_REJECTED'),(b'What code is in the image?','CAPTCHA')):
            result=self.check(FakeSession([body,GOOD]));self.assertEqual(result['status'],expected);self.assertIsNone(result['amount'])
    def test_body_bound_and_invalid_utf8_stop_the_transfer(self):
        for body in (b'x'*(128*1024+1),b'\xff'):
            session=FakeSession([body,GOOD]);result=self.check(session)
            self.assertEqual(result['status'],'UNVERIFIED');self.assertEqual(session.received,1)
    def test_remote_dns_preserves_configured_proxy_and_disallows_direct_fallback(self):
        session=FakeSession();self.check(session,proxy_url='socks5://user:password@proxy.invalid:1080')
        self.assertEqual(session.kwargs['proxy'],'socks5h://user:password@proxy.invalid:1080')
        self.assertFalse(session.kwargs['allow_redirects'])
    def test_service_input_is_strict_and_validation_is_decimal(self):
        for number in ('1000000001','2','2000000001&other=1'):
            with self.assertRaises(ValueError):LowBandwidthChecker.check_service_number(number,session_factory=Mock())
        self.assertEqual(str(amount_decimal('١٢٣٫٤٥')),'123.45')

    def test_unset_abort_download_counter_is_unknown_rather_than_zero_traffic(self):
        class Session(FakeSession):
            def get(self,*args,**kwargs):
                try:return super().get(*args,**kwargs)
                except WriteStopped as exc:
                    exc.response.download_size=0;raise
        result=self.check(Session())
        self.assertIsNone(result['transfer_counters']['download_size'])


class SchedulerTests(unittest.TestCase):
    def setUp(self):self.now=1000.;self.s=CentralizedCooldownScheduler(clock=lambda:self.now)
    def test_full_sixty_seconds_start_after_completion_and_not_dispatch(self):
        self.s.acquire('route');self.now+=4;self.s.release('route');self.now+=59.999
        with self.assertRaises(RouteNotReady):self.s.acquire('route')
        self.now+=.001;self.s.acquire('route')
    def test_global_bound_of_twenty_eight_and_no_same_route_parallelism(self):
        for index in range(28):self.s.acquire(str(index));self.now+=.201
        with self.assertRaises(RouteNotReady):self.s.acquire('29')
        with self.assertRaises(RouteNotReady):self.s.acquire('1')
        self.s.release('0');self.now+=.201;self.s.acquire('29')
    def test_server_retry_after_and_operator_unblock(self):
        self.s.acquire('a');self.s.release('a',retry_after=120,blocked=True);self.now+=121
        with self.assertRaises(RouteNotReady) as caught:self.s.acquire('a')
        self.assertTrue(caught.exception.blocked);self.s.unblock('a');self.s.acquire('a')
    def test_route_keys_are_secret_free_and_do_not_conflate_credentials(self):
        first=route_key('socks5://user:secret@proxy.invalid:1080')
        self.assertEqual(first,route_key('socks5h://user:secret@proxy.invalid:1080'))
        self.assertNotEqual(first,route_key('socks5://other:secret@proxy.invalid:1080'))
        self.assertNotIn('secret',first);self.assertNotIn('user',first)
    def test_releasing_an_unleased_route_is_rejected(self):
        with self.assertRaises(ValueError):self.s.release('not-leased')


class AdapterTests(unittest.TestCase):
    def setUp(self):self.s=CentralizedCooldownScheduler(clock=lambda:1000.)
    def result(self,status='SUCCESS',amount='91.39'):
        return {'status':status,'amount':amount,'retry_after_seconds':60}
    def test_unverified_response_is_never_a_verified_zero(self):
        with patch.object(transport,'SCHEDULER',self.s),patch.object(transport.LowBandwidthChecker,'check_service_number',return_value=self.result('UNVERIFIED',None)):
            status,amount,_=transport.query_service(NUMBER)
        self.assertEqual(status,'unknown_response');self.assertIsNone(amount)
    def test_challenge_is_quarantined_and_no_fallback_request_is_made(self):
        callback=Mock()
        with patch.object(transport,'SCHEDULER',self.s),patch.object(transport.LowBandwidthChecker,'check_service_number',return_value=self.result('CAPTCHA',None)) as query:
            result=transport.query_service(NUMBER,on_verification=callback)
            again=transport.query_service(NUMBER)
        self.assertEqual(result[0],'verification_required');self.assertEqual(again[0],'verification_required')
        self.assertEqual(query.call_count,1);callback.assert_called_once()
    def test_account_worker_and_remote_browser_routing_are_separate(self):
        from workers.actor import WorkerActor
        actor=WorkerActor('test','test',Path('unused'),Path('unused'),task_scope='service')
        with patch.dict(os.environ,{'ZAIN_SERVICE_ENGINE':'low_bandwidth','ZAIN_SERVICE_HTTP_ALLOW_DIRECT':'1'}),patch.object(transport,'query_service',return_value=('ok',91.39,'verified')) as query,patch('services.browser_client.query') as browser:
            self.assertEqual(actor.execute_task_api(NUMBER)[0],'ok');query.assert_called_once();browser.assert_not_called()
            with self.assertRaises(PermissionError):actor.execute_task_api('1000000001')
    def test_account_query_does_not_enter_http_service_transport(self):
        from workers.zain_api import query_contract_due_amount
        with patch.dict(os.environ,{'ZAIN_SERVICE_ENGINE':'low_bandwidth'}),patch('workers.zain_api.query_direct_business_api',return_value=('ok',1,'verified')) as account,patch.object(transport,'query_service') as service:
            self.assertEqual(query_contract_due_amount('1000000001')[0],'ok');account.assert_called_once();service.assert_not_called()
    def test_default_engine_stays_browser(self):
        with patch.dict(os.environ,{'ZAIN_SERVICE_ENGINE':'browser'}):self.assertFalse(transport.enabled())

    def test_only_configured_tested_routes_are_enabled_and_direct_is_opt_in(self):
        proxy='socks5://user:password@proxy.invalid:1080'
        with patch.dict(os.environ,{'ZAIN_SERVICE_ENGINE':'low_bandwidth','ZAIN_SERVICE_HTTP_ROUTES':route_key(proxy)}):
            self.assertTrue(transport.enabled(proxy));self.assertFalse(transport.enabled())
            self.assertFalse(transport.enabled('socks5://other:password@proxy.invalid:1080'))

    def test_invalid_route_is_rejected_without_exception_or_network_attempt(self):
        with patch.dict(os.environ,{'ZAIN_SERVICE_ENGINE':'low_bandwidth','ZAIN_SERVICE_HTTP_ROUTES':'reviewed'}):
            self.assertFalse(transport.enabled('invalid'))
            self.assertEqual(transport.query_service(NUMBER,proxy_url='invalid')[0],'error')


if __name__=='__main__':unittest.main()
