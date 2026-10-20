"""Credential diagnostics with retained fixtures and no external requests."""
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from telegram_bot.commerce.client import CommerceClient,CommerceUnavailable
from telegram_bot.commerce.secret_store import CredentialUnavailable,load_service_key,_crypt


class CredentialDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(tempfile.mkdtemp(prefix='credential-diagnostics-retained-'))
        self.path=self.root/'fixture.dpapi'
        environment=patch.dict(os.environ,{'ZAIN_COMMERCE_SERVICE_KEY':'','SUPABASE_SERVICE_ROLE_KEY':''})
        environment.start();self.addCleanup(environment.stop)
        location=patch('telegram_bot.commerce.secret_store.secret_path',return_value=self.path)
        location.start();self.addCleanup(location.stop)
        self.fallback=self.root/'workspace.dpapi'
        fallback=patch('telegram_bot.commerce.secret_store.workspace_secret_path',return_value=self.fallback)
        fallback.start();self.addCleanup(fallback.stop)

    def reason(self,expected):
        with self.assertRaises(CredentialUnavailable) as raised:load_service_key()
        self.assertEqual(raised.exception.reason,expected)
        self.assertNotIn(str(self.root),str(raised.exception))

    def test_missing_file_is_distinct_from_decryption_failure(self):
        self.reason('credential_file_missing')

    def test_permission_denial_is_distinct_from_missing_file(self):
        with patch.object(Path,'read_bytes',side_effect=PermissionError('private-file-detail')):
            self.reason('credential_file_denied')

    def test_other_read_failure_is_classified_without_exception_text(self):
        with patch.object(Path,'read_bytes',side_effect=OSError('private-disk-detail')):
            self.reason('credential_file_unreadable')

    def test_empty_file_is_rejected_before_decryption(self):
        self.path.write_bytes(b'')
        with patch('telegram_bot.commerce.secret_store._crypt') as crypt:
            self.reason('credential_file_empty')
        crypt.assert_not_called()

    def test_invalid_plaintext_is_distinct_from_dpapi_failure(self):
        self.path.write_bytes(b'fixture-encrypted')
        with patch('telegram_bot.commerce.secret_store._crypt',return_value=b'\xff'):
            self.reason('credential_invalid_encoding')
        with patch('telegram_bot.commerce.secret_store._crypt',return_value=b' '):
            self.reason('credential_empty')

    def test_environment_credential_retains_precedence(self):
        with patch.dict(os.environ,{'ZAIN_COMMERCE_SERVICE_KEY':'fixture-key'}):
            self.assertEqual(load_service_key(),'fixture-key')

    @unittest.skipUnless(os.name=='nt','Windows DPAPI only')
    def test_real_encrypted_workspace_fallback_loads_when_appdata_file_is_missing(self):
        encrypted=_crypt(b'fixture-not-a-real-credential',decrypt=False)
        self.fallback.write_bytes(encrypted)
        self.assertEqual(load_service_key(),'fixture-not-a-real-credential')
        self.assertEqual(self.fallback.read_bytes(),encrypted)
        self.assertFalse(self.path.exists())

    def test_existing_appdata_credential_has_precedence_over_fallback(self):
        self.path.write_bytes(b'primary-fixture');self.fallback.write_bytes(b'fallback-fixture')
        with patch('telegram_bot.commerce.secret_store._crypt',return_value=b'primary-key') as decrypt:
            self.assertEqual(load_service_key(),'primary-key')
        decrypt.assert_called_once_with(b'primary-fixture',decrypt=True)

    def test_denied_primary_file_does_not_silently_use_another_key(self):
        self.fallback.write_bytes(b'fallback-fixture')
        with patch.object(Path,'read_bytes',side_effect=PermissionError('private-detail')):
            self.reason('credential_file_denied')

    def test_corrupt_primary_file_does_not_silently_use_another_key(self):
        self.path.write_bytes(b'');self.fallback.write_bytes(b'fallback-fixture')
        self.reason('credential_file_empty')

    def test_dpapi_code_reaches_logs_without_sending_a_request(self):
        self.path.write_bytes(b'fixture-encrypted')
        opener=Mock()
        with patch('telegram_bot.commerce.secret_store._crypt',side_effect=CredentialUnavailable(
                'credential_decryption_failed',windows_code=13)):
            with self.assertLogs('CommerceClient',level='WARNING') as logs:
                with self.assertRaises(CommerceUnavailable) as raised:
                    CommerceClient(opener=opener).rpc('list_partners')
        self.assertEqual(raised.exception.reason,'credential_decryption_failed')
        self.assertIn('windows=13',' '.join(logs.output))
        self.assertNotIn(str(self.root),' '.join(logs.output))
        opener.assert_not_called()

    @unittest.skipUnless(os.name=='nt','Windows DPAPI only')
    def test_real_dpapi_rejects_invalid_data_with_numeric_code(self):
        with self.assertRaises(CredentialUnavailable) as raised:_crypt(b'invalid-fixture',decrypt=True)
        self.assertEqual(raised.exception.reason,'credential_decryption_failed')
        self.assertIsInstance(raised.exception.windows_code,int)
        self.assertNotEqual(raised.exception.windows_code,0)


if __name__=='__main__':unittest.main()
