import ctypes
import io
import json
import os
import socket
import ssl
import unittest
from urllib.error import HTTPError,URLError
from telegram_bot.commerce.client import CommerceClient,CommerceUnavailable
from telegram_bot.commerce.secret_store import _crypt


class ClientTests(unittest.TestCase):
    def test_restricted_rpc_headers_and_no_management_token(self):
        captured = []
        def opener(request,timeout):
            captured.append((request,timeout))
            return io.BytesIO(b'{"status":"ok"}')
        client = CommerceClient(key_provider=lambda:'sb_secret_fixture',opener=opener)
        self.assertEqual(client.rpc('health'),{'status':'ok'})
        request,timeout = captured[0]
        self.assertEqual(request.get_header('Content-profile'),'commerce_rpc')
        self.assertEqual(request.get_header('Apikey'),'sb_secret_fixture')
        self.assertIsNone(request.get_header('Authorization'))
        self.assertEqual(timeout,15)

    def test_errors_do_not_expose_key_or_provider_body(self):
        attempts = []
        def opener(request,timeout):
            attempts.append(1)
            raise HTTPError(request.full_url,403,'denied',{},io.BytesIO(b'sensitive body'))
        with self.assertRaises(CommerceUnavailable) as raised:
            CommerceClient(key_provider=lambda:'secret-fixture',opener=opener).rpc('health')
        self.assertNotIn('secret-fixture',str(raised.exception))
        self.assertNotIn('sensitive body',str(raised.exception))
        self.assertEqual(len(attempts),1)

    def test_credentials_fail_closed(self):
        def missing(): raise RuntimeError('missing-key-detail')
        with self.assertRaises(CommerceUnavailable) as raised:
            CommerceClient(key_provider=missing).rpc('health')
        self.assertNotIn('missing-key-detail',str(raised.exception))
        self.assertEqual(raised.exception.reason,'credential_unavailable')
        self.assertEqual(raised.exception.rpc_name,'health')

    def test_transport_failures_have_safe_categories_and_never_retry_a_payment(self):
        failures=[(URLError(socket.gaierror(-1,'private-host-detail')),'dns_error'),
                  (URLError(ssl.SSLError('private-certificate-detail')),'tls_error'),
                  (TimeoutError('private-timeout-detail'),'timeout'),
                  (ConnectionError('private-connection-detail'),'connection_error')]
        for failure,category in failures:
            with self.subTest(category=category):
                calls=[]
                def opener(request,timeout):
                    calls.append(1);raise failure
                with self.assertLogs('CommerceClient',level='WARNING') as logs:
                    with self.assertRaises(CommerceUnavailable) as raised:
                        CommerceClient(key_provider=lambda:'secret-fixture',opener=opener).rpc('reserve_order',{'private':'customer'})
                self.assertEqual(raised.exception.reason,category)
                self.assertEqual(raised.exception.rpc_name,'reserve_order')
                self.assertEqual(calls,[1])
                output=' '.join(logs.output)+repr(vars(raised.exception))
                for forbidden in ('private-host','private-certificate','private-timeout','private-connection','secret-fixture','customer'):
                    self.assertNotIn(forbidden,output)

    def test_invalid_json_has_a_safe_response_category(self):
        def opener(request,timeout):return io.BytesIO(b'private-invalid-response')
        with self.assertLogs('CommerceClient',level='WARNING') as logs:
            with self.assertRaises(CommerceUnavailable) as raised:
                CommerceClient(key_provider=lambda:'fixture',opener=opener).rpc('list_partners')
        self.assertEqual(raised.exception.reason,'invalid_response')
        self.assertEqual(raised.exception.rpc_name,'list_partners')
        self.assertNotIn('private-invalid-response',' '.join(logs.output))

    def test_http_diagnostics_include_safe_rpc_and_codes_only(self):
        def opener(request,timeout):
            raise HTTPError(request.full_url,403,'private-provider-reason',{},io.BytesIO(
                b'{"code":"42501","message":"private-provider-body"}'))
        with self.assertLogs('CommerceClient',level='WARNING') as logs:
            with self.assertRaises(CommerceUnavailable):
                CommerceClient(key_provider=lambda:'secret-fixture',opener=opener).rpc('list_partners',{'private':'customer'})
        output=' '.join(logs.output)
        self.assertIn('rpc=list_partners',output);self.assertIn('http=403',output);self.assertIn('database=42501',output)
        for forbidden in ('private-provider','secret-fixture','customer','https://'):
            self.assertNotIn(forbidden,output)

    def test_known_validation_reason_is_safe_and_not_retried(self):
        attempts=[]
        def opener(request,timeout):
            attempts.append(1)
            body=json.dumps({'code':'22023','message':'Campaign exceeds reviewed administration limits',
                'details':'private-customer-detail','hint':'secret-fixture'}).encode()
            raise HTTPError(request.full_url,400,'denied',{},io.BytesIO(body))
        with self.assertRaises(CommerceUnavailable) as raised:
            CommerceClient(key_provider=lambda:'secret-fixture',opener=opener).rpc('create_platform_campaign',{})
        error=raised.exception
        self.assertEqual(error.reason,'campaign_limits');self.assertEqual(error.database_code,'22023')
        self.assertEqual(error.rpc_name,'create_platform_campaign');self.assertEqual(error.http_status,400)
        self.assertEqual(len(attempts),1)
        self.assertNotIn('private-customer-detail',repr(vars(error)));self.assertNotIn('secret-fixture',repr(vars(error)))

    def test_unknown_error_body_is_not_retained(self):
        def opener(request,timeout):
            raise HTTPError(request.full_url,400,'denied',{},io.BytesIO(b'{"code":"secret","message":"secret-fixture"}'))
        with self.assertRaises(CommerceUnavailable) as raised:
            CommerceClient(key_provider=lambda:'fixture',opener=opener).rpc('health')
        self.assertIsNone(raised.exception.reason);self.assertIsNone(raised.exception.database_code)
        self.assertNotIn('secret-fixture',repr(vars(raised.exception)))

    def test_database_permission_denial_is_classified_without_private_detail(self):
        def opener(request,timeout):
            raise HTTPError(request.full_url,403,'denied',{},io.BytesIO(
                b'{"code":"42501","message":"private-schema-detail"}'))
        with self.assertRaises(CommerceUnavailable) as raised:
            CommerceClient(key_provider=lambda:'fixture',opener=opener).rpc('issue_cash_credit_code',{})
        self.assertEqual(raised.exception.reason,'permission_denied')
        self.assertNotIn('private-schema-detail',repr(vars(raised.exception)))

    @unittest.skipUnless(os.name == 'nt','Windows DPAPI only')
    def test_dpapi_encrypts_and_roundtrips_in_memory(self):
        plain = b'fixture-not-a-real-credential'
        encrypted = _crypt(plain,decrypt=False)
        self.assertNotIn(plain,encrypted)
        self.assertEqual(_crypt(encrypted,decrypt=True),plain)


if __name__ == '__main__': unittest.main()
