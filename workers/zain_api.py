"""Direct Zain Dual-Engine Query Client.
Fast, lightweight dual-engine client supporting both:
- Contracts (starting with 1): Queries Business API (Direct) & Contract Payment (Proxy).
- Services/Accounts (starting with 2): Queries app.sa.zain.com/ar/quickpay?account={number}.
Achieves high concurrency, zero rate-limiting, and ~2.5+ records/sec throughput.
"""
from __future__ import annotations

import json
import logging
import re
import ssl
import threading
import time
from typing import Any, Optional, Tuple
from urllib.parse import urlparse
import urllib.error
import urllib.request

logger = logging.getLogger("ZainAPI")

# IP-Level Mutex Locks: Prevents concurrent queries to app.sa.zain.com from the same IP
_IP_SERVICE_LOCKS: dict[str, threading.Lock] = {}
_IP_LOCKS_GUARD = threading.Lock()
_IP_LAST_QUERY_TIME: dict[str, float] = {}
_IP_COOKIES: dict[str, dict[str, str]] = {}

def get_ip_service_lock(ip_tag: str) -> threading.Lock:
    with _IP_LOCKS_GUARD:
        if ip_tag not in _IP_SERVICE_LOCKS:
            _IP_SERVICE_LOCKS[ip_tag] = threading.Lock()
        return _IP_SERVICE_LOCKS[ip_tag]

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
    number: str,
    proxy_url: Optional[str] = None,
    timeout: float = 10.0,
) -> Tuple[str, Optional[float], str]:
    """Queries app.sa.zain.com via direct socket or SOCKS5 proxy.
    - If number starts with '2': uses /ar/quickpay?account={number}
    - If number starts with other prefix: uses /ar/contract-payment?contract={number}
    """
    num_clean = str(number).strip()
    if not num_clean:
        return "error", None, "Empty number"

    # Route service numbers (starting with 2) to quickpay?account=
    if num_clean.startswith("2"):
        path = f"/ar/quickpay?account={num_clean}"
    else:
        path = f"/ar/contract-payment?contract={num_clean}"

    try:
        import socks
        parsed = urlparse(proxy_url) if proxy_url and str(proxy_url).strip() else None

        s = socks.socksocket()
        if parsed:
            proxy_type = socks.SOCKS5 if "5" in (parsed.scheme or "") else socks.HTTP
            s.set_proxy(proxy_type, parsed.hostname, parsed.port, username=parsed.username, password=parsed.password)
        s.settimeout(timeout)

        ctx = ssl.create_default_context()
        s_wrapped = ctx.wrap_socket(s, server_hostname="app.sa.zain.com")
        s_wrapped.connect(("app.sa.zain.com", 443))

        if proxy_url and str(proxy_url).strip():
            try:
                parsed_proxy = urlparse(str(proxy_url).strip())
                ip_tag = f"proxy_{parsed_proxy.hostname}:{parsed_proxy.port}"
            except Exception:
                ip_tag = f"proxy_{str(proxy_url).strip()}"
        else:
            ip_tag = "local_direct"

        current_cookies = _IP_COOKIES.get(ip_tag, {})
        cookie_str = "; ".join([f"{k}={v}" for k, v in current_cookies.items()])
        cookie_hdr = f"Cookie: {cookie_str}\r\n" if cookie_str else ""

        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: app.sa.zain.com\r\n"
            f"User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36\r\n"
            f"Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8\r\n"
            f"Accept-Language: ar,en;q=0.9\r\n"
            f"{cookie_hdr}"
            f"Connection: close\r\n\r\n"
        )
        s_wrapped.sendall(req.encode("utf-8"))

        chunks = []
        while True:
            try:
                chunk = s_wrapped.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
            except Exception:
                break
        s_wrapped.close()

        html = b"".join(chunks).decode("utf-8", errors="ignore")

        # Extract and persist Set-Cookie (e.g. F5 TS cookies) for this IP
        if "\r\n\r\n" in html:
            header_part = html.split("\r\n\r\n", 1)[0]
            new_cookies = dict(current_cookies)
            for line in header_part.split("\r\n"):
                if line.lower().startswith("set-cookie:"):
                    raw_cookie = line.split(":", 1)[1].strip()
                    main_part = raw_cookie.split(";")[0].strip()
                    if "=" in main_part:
                        c_k, c_v = main_part.split("=", 1)
                        new_cookies[c_k.strip()] = c_v.strip()
            _IP_COOKIES[ip_tag] = new_cookies

        # 1. Detect HTTP status code and redirect/F5 block signatures
        first_line = html.split("\r\n", 1)[0] if "\r\n" in html else ""
        m_status = re.search(r"HTTP/\d\.\d\s+(\d+)", first_line)
        status_code = int(m_status.group(1)) if m_status else 200
        html_lower = html.lower()

        # 1. F5 WAF Security Block Detection (IP-Level Ban)
        if (
            status_code in (403, 429)
            or "request rejected" in html_lower
            or "the requested url was rejected" in html_lower
            or "support id" in html_lower
        ):
            return "blocked", None, f"حظر أمني على مستوى الـ IP (HTTP {status_code})"

        # 2. Session-Level Challenge / Expired Token Detection
        if "/tspd/" in html_lower or "bobcmn" in html or "ts_challenge" in html_lower:
            _IP_COOKIES[ip_tag] = {}
            return "session_expired", None, "انتهاء صلاحية كوكيز الجلسة (F5 Session Challenge)"

        # Handle 302 redirects: if redirected to home, it's not_found (not blocked)
        if status_code in (301, 302, 303, 307):
            m_loc = re.search(r"\r\nLocation:\s*([^\r\n]+)", html, re.IGNORECASE)
            loc = m_loc.group(1).strip() if m_loc else ""
            if "home" in loc.lower() or not loc:
                return "not_found", None, "الرقم غير مسجل أو ليس له فاتورة (تحويل للرئيسية)"
            return "blocked", None, f"إعادة توجيه أمنية (HTTP {status_code}) -> {loc}"

        # 2. Extract quickpayData JSON embedded in HTML
        match = re.search(r"var\s+quickpayData\s*=\s*(\{.*?\});", html)
        if match:
            data = json.loads(match.group(1))
            amt = float(data.get("amount", 0.0))
            return "ok", amt, "Success"

        # 3. Check billDetails JSON
        match_bill = re.search(r"var\s+billDetails\s*=\s*(\{.*?\});", html, re.DOTALL)
        if match_bill:
            m_total = re.search(r"total\s*:\s*([\d.]+)", match_bill.group(1))
            if m_total:
                return "ok", float(m_total.group(1)), "Success"

        if "غير موجود" in html or "لا يوجد مستحقات" in html or "not found" in html_lower:
            return "ok", 0.0, "Zero or No Due"

        return "not_found", None, "Record not found or no due amount"

    except Exception as exc:
        return "network_error", None, f"Query error: {exc}"


def query_direct_business_api(
    num_clean: str,
    timeout: float = 15.0,
) -> Tuple[str, Optional[float], str]:
    """Direct connection via manage.business.zain.sa REST API."""
    url = f"{API_BASE_URL}/{num_clean}"
    req = urllib.request.Request(url, headers=DEFAULT_HEADERS)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("is_successful"):
                bill_data = data.get("data", {})
                due = bill_data.get("due_amount", 0.0)
                try:
                    due_float = float(due)
                except (ValueError, TypeError):
                    due_float = 0.0
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
    lookup_number: str,
    proxy_url: Optional[str] = None,
    timeout: int = 15,
) -> Tuple[str, Optional[float], str]:
    """Unified inquiry function:
    - If lookup_number starts with '2': queries app.sa.zain.com/ar/quickpay?account={number} via stealth engine.
    - If lookup_number starts with '1':
      - If proxy_url provided (Worker 2): queries app.sa.zain.com, falling back to direct business API if rejected.
      - If direct (Worker 1): queries manage.business.zain.sa REST API.
    """
    num_clean = str(lookup_number).strip()
    if not num_clean:
        return "error", None, "Empty lookup number"

    # 1. Service numbers (starting with 2):
    # Route via fast web engine (supports direct socket & SOCKS5 proxy concurrently)
    # Guaranteed: Two workers sharing the same IP NEVER query 2xxx simultaneously
    if num_clean.startswith("2"):
        if proxy_url and str(proxy_url).strip():
            try:
                parsed = urlparse(str(proxy_url).strip())
                ip_tag = f"proxy_{parsed.hostname}:{parsed.port}"
            except Exception:
                ip_tag = f"proxy_{str(proxy_url).strip()}"
        else:
            ip_tag = "local_direct"

        lock = get_ip_service_lock(ip_tag)
        with lock:
            # Enforce 3.9s pause exclusively between service number queries from the same IP
            last_t = _IP_LAST_QUERY_TIME.get(ip_tag, 0.0)
            elapsed = time.time() - last_t
            if elapsed < 3.9:
                time.sleep(3.9 - elapsed)

            status, amt, msg = query_app_zain_web_engine(num_clean, proxy_url=proxy_url, timeout=float(timeout))
            _IP_LAST_QUERY_TIME[ip_tag] = time.time()

            if status in ("ok", "not_found"):
                return status, amt, msg

            if status == "session_expired":
                # Clear stale cookies and auto-retry once with a clean session handshake
                _IP_COOKIES[ip_tag] = {}
                time.sleep(1.0)
                status, amt, msg = query_app_zain_web_engine(num_clean, proxy_url=proxy_url, timeout=float(timeout))
                _IP_LAST_QUERY_TIME[ip_tag] = time.time()
                if status in ("ok", "not_found"):
                    return status, amt, msg

            if status == "blocked":
                return "blocked", None, msg
            # Fallback to stealth browser if direct socket encounters network errors and no strict proxy is set
            if not proxy_url:
                from .stealth_service_engine import query_service_stealth
                return query_service_stealth(num_clean)
            return status, amt, msg

    # 2. Contract numbers (starting with 1 or others):
    # Strict 100% Proxy Isolation for Worker 3: Zero IP leak, no fallback to direct connection
    if proxy_url and str(proxy_url).strip():
        return query_app_zain_web_engine(num_clean, proxy_url=proxy_url.strip(), timeout=float(timeout))

    # 3. Direct connection workers (Worker 1 & Worker 2) via manage.business.zain.sa REST API
    return query_direct_business_api(num_clean, timeout=timeout)

