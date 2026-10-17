"""Direct Zain Dual-Engine Query Client.
Fast, lightweight dual-engine client supporting both:
- Contracts (starting with 1): Queries Business API (Direct) & Contract Payment (Proxy).
- Services/Accounts (starting with 2): Queries app.sa.zain.com/ar/quickpay?account={number}.
Service attempts are serialized and paced per configured outbound route.
"""
from __future__ import annotations

import json
import logging
import math
import re
from typing import Any, Optional, Tuple

logger = logging.getLogger("ZainAPI")

from workers.service_transport import (
    _IP_COOKIES, _IP_LAST_QUERY_TIME, get_ip_service_lock,
    normalize_number, query_web_number,
)

API_BASE_URL = "https://manage.business.zain.sa/api/v1/customers/contracts/explore-due-amount"
API_AUTH_KEY = "X3tREqGZT0WNDApC"
DEFAULT_HEADERS = {
    "api-auth-key": API_AUTH_KEY,
    "x-device-platform": "pwa",
    "x-device-platform-version": "2.1.9",
    "accept": "application/json",
    "accept-language": "ar",
    "origin": "https://business.zain.sa",
    "referer": "https://business.zain.sa/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}


def query_app_zain_web_engine(
    number: str, proxy_url: Optional[str] = None, timeout: float = 10.0,
) -> Tuple[str, Optional[float], str]:
    """Query the web route with shared cookies and service-only pacing."""
    return query_web_number(number, proxy_url=proxy_url, timeout=timeout)


def query_direct_business_api(
    num_clean: str,
    timeout: float = 30.0,
    proxy_url: Optional[str] = None,
    *,connection_key=None,
) -> Tuple[str, Optional[float], str]:
    """Direct connection via manage.business.zain.sa REST API with optional proxy support."""
    num_clean = normalize_number(num_clean)
    if not re.fullmatch(r"[0-9]+", num_clean):
        return "error", None, "Invalid lookup number"
    if num_clean.startswith("2"):
        return query_contract_due_amount(num_clean, proxy_url=proxy_url, timeout=int(timeout))
    url = f"{API_BASE_URL}/{num_clean}"
    try:
        from .account_transport import request_account
        reply=request_account(url,DEFAULT_HEADERS,proxy=proxy_url,connection_key=connection_key,timeout=timeout)
        if reply.status in (429,500,502,503,504):
            return 'network_error',None,'Account API temporarily unavailable; retry after route cooldown'
        try:
            data=json.loads(reply.body)
        except (ValueError,UnicodeError):
            return 'unknown_response',None,'Account API returned a non-JSON response'
        if not isinstance(data,dict):return 'unknown_response',None,'Invalid account API response'
        if reply.status in (400,404) and data.get('is_successful') is False and data.get('response_code')=='EZB5029':
            return 'not_found',None,'رقم العقد لا يطابق أي سجل (EZB5029)'
        if reply.status!=200 or data.get('is_successful') is not True:
            return 'error',None,'Account API rejected the request (HTTP '+str(reply.status)+')'
        bill_data=data.get('data')
        due=bill_data.get('due_amount') if isinstance(bill_data,dict) else None
        try:
            if isinstance(due,bool):raise ValueError()
            due_float=float(due)
        except (ValueError,TypeError):return 'unknown_response',None,'Missing or invalid due amount'
        if not math.isfinite(due_float) or due_float<0:return 'unknown_response',None,'Invalid due amount'
        return 'ok',due_float,'Success'
    except ValueError:
        return 'error',None,'Invalid account connection configuration'
    except Exception as exc:
        reason=getattr(exc,'reason',None)
        if reason=='proxy_authentication_failed':return 'error',None,'Proxy authentication failed'
        if reason=='route_cooling_down':return 'account_backoff',None,'Account route is waiting for server Retry-After'
        return 'network_error',None,'Account connection unavailable ('+(reason or type(exc).__name__)+')'


def query_contract_due_amount(
    lookup_number: str, proxy_url: Optional[str] = None, timeout: Optional[int] = None,
    on_verification=None,
    *,connection_key=None,
) -> Tuple[str, Optional[float], str]:
    """Use a normal JS browser for services, with one context per route.

    Raw service HTTP cannot render Zain's session bootstrap. Services never
    fall back to contracts or switch between proxy and direct connections.
    """
    num_clean = normalize_number(lookup_number)
    if not re.fullmatch(r"[0-9]+", num_clean):
        return "error", None, "Invalid lookup number"
    if num_clean.startswith("2"):
        from .stealth_service_engine import query_service_stealth
        kwargs = {"timeout_seconds": float(timeout if timeout is not None else 12), "proxy_url": proxy_url}
        if on_verification is not None:
            kwargs["on_verification"] = on_verification
        return query_service_stealth(num_clean, **kwargs)
    kwargs={'timeout':timeout if timeout is not None else 30,'proxy_url':proxy_url}
    if connection_key is not None:kwargs['connection_key']=connection_key
    return query_direct_business_api(num_clean,**kwargs)

