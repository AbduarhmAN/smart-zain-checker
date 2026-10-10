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
import urllib.error
import urllib.request

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
    timeout: float = 15.0,
    proxy_url: Optional[str] = None,
) -> Tuple[str, Optional[float], str]:
    """Direct connection via manage.business.zain.sa REST API with optional proxy support."""
    num_clean = normalize_number(num_clean)
    if not re.fullmatch(r"[0-9]+", num_clean):
        return "error", None, "Invalid lookup number"
    if num_clean.startswith("2"):
        return query_contract_due_amount(num_clean, proxy_url=proxy_url, timeout=int(timeout))
    url = f"{API_BASE_URL}/{num_clean}"
    req = urllib.request.Request(url, headers=DEFAULT_HEADERS)

    try:
        if proxy_url and str(proxy_url).strip():
            from urllib.parse import urlparse
            parsed = urlparse(str(proxy_url).strip())
            if parsed.scheme.startswith("socks"):
                import socks
                from sockshandler import SocksiPyHandler
                opener = urllib.request.build_opener(
                    SocksiPyHandler(
                        socks.SOCKS5 if "5" in parsed.scheme else socks.SOCKS4,
                        parsed.hostname,
                        parsed.port or 1080,
                        True,
                        parsed.username,
                        parsed.password,
                    )
                )
            else:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": str(proxy_url).strip(), "https": str(proxy_url).strip()})
                )
        else:
            opener = urllib.request.build_opener()

        with opener.open(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("is_successful"):
                bill_data = data.get("data", {})
                due = bill_data.get("due_amount")
                try:
                    due_float = float(due)
                except (ValueError, TypeError):
                    return "unknown_response", None, "Missing or invalid due amount"
                if not math.isfinite(due_float) or due_float < 0:
                    return "unknown_response", None, "Invalid due amount"
                return "ok", due_float, data.get("message", {}).get("message", "Success")
            else:
                msg = data.get("message", {}).get("message", "Zain API returned failure")
                return "error", None, msg

    except urllib.error.HTTPError as http_err:
        try:
            err_data = json.loads(http_err.read().decode("utf-8"))
            msg = err_data.get("message", {}).get("message") or err_data.get("message", {}).get("title") or str(http_err)
        except Exception:
            msg = f"HTTP {http_err.code}"
        return "error", None, msg

    except Exception as exc:
        return "network_error", None, str(exc)


def query_contract_due_amount(
    lookup_number: str, proxy_url: Optional[str] = None, timeout: Optional[int] = None,
    on_verification=None,
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
    return query_direct_business_api(num_clean, timeout=timeout if timeout is not None else 12, proxy_url=proxy_url)

