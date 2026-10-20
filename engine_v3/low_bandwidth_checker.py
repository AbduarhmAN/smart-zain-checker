"""Bounded QuickPay streaming with verified amounts and exact decimal parsing.

The Chrome compatibility profile reproduces the supplied experimental client.
Access challenges stop the route; this client never rotates routes or solves them.
Reported curl body/header counters are not proxy-billed or physical wire bytes.
"""
from __future__ import annotations
import codecs
import json
import re
import time
from datetime import datetime,timezone
from decimal import Decimal,InvalidOperation
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs,urlencode,urlparse,urlunparse

HOST='app.sa.zain.com'
MAX_BODY=128*1024
WRITE_ABORT=0xFFFFFFFF  # libcurl's CURL_WRITEFUNC_ERROR.


def proxy_with_remote_dns(proxy):
    if not proxy:return None
    value=urlparse(proxy)
    if value.scheme not in ('socks5','socks5h','http','https') or not value.hostname or not value.port:
        raise ValueError('Invalid service proxy')
    return urlunparse(value._replace(scheme='socks5h')) if value.scheme=='socks5' else proxy


def amount_decimal(raw):
    if isinstance(raw,bool) or raw is None:return None
    text=str(raw).strip().translate(str.maketrans('٠١٢٣٤٥٦٧٨٩٫٬','0123456789.,'))
    if not re.fullmatch(r'(?:[0-9]{1,15}|[0-9]{1,3}(?:,[0-9]{3}){1,4})(?:\.[0-9]{1,2})?',text):return None
    try:value=Decimal(text.replace(',',''))
    except InvalidOperation:return None
    return value if value.is_finite() and value>=0 else None


def parse_page(text):
    low=text.lower()
    if 'the requested url was rejected' in low:return {'status':'WAF_REJECTED','amount':None,'account':None}
    if 'what code is in the image?' in low or 'captcha required' in low:
        return {'status':'CAPTCHA','amount':None,'account':None}
    decoder=json.JSONDecoder(parse_float=Decimal,parse_constant=lambda _:None)
    for match in re.finditer(r'\bquickpayData\s*=\s*',text):
        try:data,_=decoder.raw_decode(text[match.end():])
        except (ValueError,TypeError):continue
        if not isinstance(data,dict):continue
        amount=amount_decimal(data.get('amount'));account=data.get('account')
        if isinstance(account,bool) or not isinstance(account,(str,int)):continue
        account=str(account)
        if amount is not None and re.fullmatch(r'[0-9]{1,20}',account):
            return {'status':'SUCCESS','amount':format(amount,'.2f'),'account':account}
    # Login, prepaid screens, placeholders and absent amounts are never paid zero results.
    return {'status':'UNVERIFIED','amount':None,'account':None}


def retry_after_seconds(value):
    try:return min(86400,max(60,float(value)))
    except (ValueError,TypeError):pass
    try:return min(86400,max(60,(parsedate_to_datetime(value)-datetime.now(timezone.utc)).total_seconds()))
    except (TypeError,ValueError,OverflowError):return 60.0


class LowBandwidthChecker:
    @staticmethod
    def check_service_number(service_number,proxy_url=None,timeout=20,enable_pjax=True,*,session_factory=None):
        number=str(service_number).strip()
        if not re.fullmatch(r'2[0-9]{9}',number):raise ValueError('Explicit ten-digit service number required')
        if not 0<float(timeout)<=60:raise ValueError('Bounded timeout required')
        proxy=proxy_with_remote_dns(proxy_url)
        if session_factory is None:
            from curl_cffi.requests import Session
            session_factory=Session
        session=session_factory(impersonate='chrome124')
        text='';decoded_bytes=0;found=None;abort_reason=None;response=None;failure=None
        utf8=codecs.getincrementaldecoder('utf-8')(errors='strict')
        started=time.monotonic()
        def consume(chunk):
            nonlocal text,decoded_bytes,found,abort_reason
            decoded_bytes+=len(chunk)
            if decoded_bytes>MAX_BODY:abort_reason='body_limit';return WRITE_ABORT
            try:text+=utf8.decode(chunk)
            except UnicodeDecodeError:abort_reason='invalid_encoding';return WRITE_ABORT
            candidate=parse_page(text)
            if candidate['status']!='UNVERIFIED':found=candidate;return WRITE_ABORT
            return len(chunk)
        headers={'Accept':'text/html, */*; q=0.01','Accept-Language':'ar,en-US;q=0.9,en;q=0.8'}
        if enable_pjax:headers.update({'X-PJAX':'true','X-Requested-With':'XMLHttpRequest'})
        try:
            try:
                response=session.get('https://'+HOST+'/ar/quickpay?'+urlencode({'account':number}),
                    headers=headers,proxy=proxy,timeout=timeout,allow_redirects=False,content_callback=consume)
            except Exception as exc:
                response=getattr(exc,'response',None)
                if not (getattr(exc,'code',None)==23 and (found or abort_reason)):
                    failure='network_unavailable'
            code=getattr(response,'status_code',None)
            final_url=urlparse(getattr(response,'url','') or '')
            valid_origin=(final_url.scheme=='https' and final_url.hostname==HOST and
                parse_qs(final_url.query).get('account')==[number])
            status='ERROR'
            if code==429:status='RATE_LIMITED'
            elif code in (401,403):status='WAF_REJECTED'
            elif failure:status='ERROR'
            elif code!=200 or not valid_origin:status='UNVERIFIED'
            elif abort_reason:status='UNVERIFIED'
            else:status=(found or parse_page(text))['status']
            result=found if found and status=='SUCCESS' else {'amount':None,'account':None}
            counters={}
            for key in ('download_size','header_size','request_size'):
                value=getattr(response,key,None)
                counters[key]=value if type(value) is int and value>=0 else None
            if found and decoded_bytes and counters['download_size']==0:
                counters['download_size']=None  # Aborted transfers can leave curl's counter unset.
            return {'status':status,'amount':result['amount'],'account':result['account'],
                'http_status':code,'latency_ms':round((time.monotonic()-started)*1000,1),
                'decoded_bytes':decoded_bytes,'transfer_counters':counters,
                'retry_after_seconds':retry_after_seconds(getattr(response,'headers',{}).get('Retry-After')) if code==429 else 60,
                'error':failure or abort_reason,'early_abort':bool(found)}
        finally:
            session.close()
