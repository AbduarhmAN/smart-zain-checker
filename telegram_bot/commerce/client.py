"""Restricted server RPC client. No management token or key in logs/errors."""
from __future__ import annotations
from domain import decimal_json as json
from decimal import Decimal
import logging
import re
import socket
import ssl
from urllib.error import HTTPError,URLError
from urllib.request import Request,urlopen
from .secret_store import load_service_key,CredentialUnavailable

PROJECT = 'avuzwbmiiavbuaxmnitp'
logger = logging.getLogger('CommerceClient')


class CommerceUnavailable(RuntimeError):
    def __init__(self,message,*,http_status=None,rpc_name=None,reason=None,database_code=None):
        super().__init__(message)
        self.http_status=http_status
        self.rpc_name=rpc_name
        self.reason=reason
        self.database_code=database_code


def _unavailable(message, name, category, *, http_status=None, database_code=None, reason=None, windows_code=None):
    # Never log request arguments, headers, URLs or exception/provider text.
    logger.warning('Commerce RPC failed: rpc=%s category=%s http=%s database=%s reason=%s windows=%s',
                   name,category,http_status,database_code,reason,windows_code)
    return CommerceUnavailable(message,rpc_name=name,http_status=http_status,
                               database_code=database_code,reason=reason)


def _response_failure(exc):
    cause=exc.reason if isinstance(exc,URLError) else exc
    if isinstance(cause,ssl.SSLError):return 'tls_error'
    if isinstance(cause,socket.gaierror):return 'dns_error'
    if isinstance(cause,TimeoutError):return 'timeout'
    if isinstance(cause,ValueError):return 'invalid_response'
    return 'connection_error'


# Only fixed, known server messages may affect UI explanations. Never expose
# arbitrary provider bodies, request arguments, credentials or SQL detail.
_REASONS={
    'Partner balance changed; review advance again':'partner_balance_changed',
    'Active partner required':'partner_inactive',
    'Partner discount exceeds approved share':'partner_share_limit',
    'Owned partner campaign required':'partner_campaign_owner',
    'Campaign exceeds reviewed administration limits':'campaign_limits',
    'Set explicit administration limits before creating offers':'campaign_policy_missing',
    'Conflicting campaign replay':'campaign_conflict',
    'Reviewed platform campaign rules required':'campaign_invalid',
    'Explicit bounded campaign values required':'campaign_invalid',
    'Reviewed campaign limits required':'campaign_invalid',
    'Explicit positive integer campaign limits required':'campaign_invalid',
    'Reviewed fully funded cash code required':'cash_invalid',
    'Conflicting cash receipt or code replay':'cash_conflict',
    'Receipt or code already assigned, or new code expired':'cash_conflict',
}


def _safe_database_error(exc):
    try:
        payload=json.loads(exc.read(8192))
        if not isinstance(payload,dict):return None,None
        code=payload.get('code')
        if not isinstance(code,str) or not re.fullmatch(r'(?:[0-9A-Z]{5}|PGRST[0-9]{3})',code):code=None
        reason=_REASONS.get(payload.get('message')) if isinstance(payload.get('message'),str) else None
        if code=='23505':reason='duplicate_record'
        if code=='42501':reason='permission_denied'
        return code,reason
    except (OSError,ValueError,TypeError):
        return None,None


class CommerceClient:
    def __init__(self, *, key_provider=load_service_key, opener=urlopen):
        self.key_provider = key_provider
        self.opener = opener

    def rpc(self,name: str,arguments: dict | None = None):
        if not re.fullmatch(r'[a-z_]+',name):
            raise ValueError('Invalid RPC name')
        try:
            key = self.key_provider()
        except CredentialUnavailable as exc:
            raise _unavailable('Commerce credential unavailable',name,'credential_unavailable',
                               reason=exc.reason,windows_code=exc.windows_code) from None
        except (RuntimeError,OSError,ValueError):
            raise _unavailable('Commerce credential unavailable',name,'credential_unavailable',
                               reason='credential_unavailable') from None
        headers = {'apikey':key,'Content-Type':'application/json','Content-Profile':'commerce_rpc',
                   'Accept-Profile':'commerce_rpc'}
        if key.count('.') == 2:
            headers['Authorization'] = f'Bearer {key}'
        request = Request(f'https://{PROJECT}.supabase.co/rest/v1/rpc/{name}',
            data=json.dumps(arguments or {},allow_nan=False).encode('utf-8'),headers=headers,method='POST')
        try:
            with self.opener(request,timeout=15) as response:
                return json.load(response,parse_float=Decimal)
        except HTTPError as exc:
            # Never repeat a failed monetary request automatically here.
            code = exc.code
            database_code,reason=_safe_database_error(exc)
            exc.close()
            raise _unavailable(f'Commerce RPC HTTP {code}',name,'http_error',http_status=code,
                               reason=reason,database_code=database_code) from None
        except (URLError,TimeoutError,OSError,ValueError) as exc:
            category=_response_failure(exc)
            raise _unavailable('Commerce RPC response unavailable',name,category,reason=category) from None
