import io
import logging
import unittest
from telegram_bot.logging_safety import install_telegram_log_filter,TelegramCredentialFilter

class TelegramLoggingSafetyTests(unittest.TestCase):
    def test_retry_warning_and_traceback_hide_token_but_keep_error_context(self):
        token='1234567890:'+'x'*35
        stream=io.StringIO();handler=logging.StreamHandler(stream)
        logger=logging.Logger('isolated');logger.addFilter(TelegramCredentialFilter());logger.addHandler(handler)
        logger.warning('Retry timeout: %s','https://api.telegram.org/bot'+token+'/getMe')
        try:raise TimeoutError('Failed /bot'+token+'/sendMessage')
        except TimeoutError:logger.exception('Request failed')
        output=stream.getvalue()
        self.assertNotIn(token,output);self.assertNotIn('x'*35,output)
        self.assertIn('[REDACTED]',output);self.assertIn('/getMe',output);self.assertIn('TimeoutError',output)

    def test_install_is_idempotent_on_library_loggers(self):
        install_telegram_log_filter();install_telegram_log_filter()
        logger=logging.getLogger('urllib3.connectionpool')
        self.assertEqual(sum(isinstance(f,TelegramCredentialFilter) for f in logger.filters),1)
