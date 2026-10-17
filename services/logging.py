"""JSON service records for journald, with credential redaction."""
import json
import logging
import os
import re
from datetime import datetime,timezone


class ServiceFormatter(logging.Formatter):
    def __init__(self,service):super().__init__();self.service=service
    def format(self,record):
        message=record.getMessage()
        message=re.sub(r'(https?://)([^\s/@:]+:[^\s/@]+)@',r'\1[redacted]@',message)
        message=re.sub(r'(api\.telegram\.org/bot)[^/\s]+',r'\1[redacted]',message)
        message=re.sub(r'\b\d{6,}:[A-Za-z0-9_-]{20,}\b','[redacted]',message)
        value={'time':datetime.fromtimestamp(record.created,timezone.utc).isoformat(),'service':self.service,
            'pid':os.getpid(),'thread':record.threadName,'level':record.levelname,'logger':record.name,'message':message}
        if record.exc_info:value['exception_type']=record.exc_info[0].__name__
        return json.dumps(value,ensure_ascii=False)


def configure(service):
    handler=logging.StreamHandler();handler.setFormatter(ServiceFormatter(service))
    logging.basicConfig(level=logging.INFO,handlers=[handler],force=True)
