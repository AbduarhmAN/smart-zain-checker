"""Remove Telegram credentials from library retry messages and tracebacks."""
import logging
import re
import traceback

def redact_telegram_credentials(value):
    value=re.sub(r'(/(?:file/)?bot)[^/\s?\'"<>]+',r'\1[REDACTED]',str(value),flags=re.I)
    return re.sub(r'\b\d{6,}:[A-Za-z0-9_-]{20,}\b','[REDACTED]',value)

class TelegramCredentialFilter(logging.Filter):
    def filter(self,record):
        record.msg=redact_telegram_credentials(record.getMessage());record.args=()
        if record.exc_info:
            record.exc_text=redact_telegram_credentials(''.join(traceback.format_exception(*record.exc_info)))
            record.exc_info=None
        elif record.exc_text:record.exc_text=redact_telegram_credentials(record.exc_text)
        return True

def install_telegram_log_filter():
    for name in ('TelegramBot','urllib3.connectionpool','requests.packages.urllib3.connectionpool'):
        logger=logging.getLogger(name)
        if not any(isinstance(item,TelegramCredentialFilter) for item in logger.filters):
            logger.addFilter(TelegramCredentialFilter())
