"""Shared HTTP sessions and service-only pacing for each outbound route.

Route identifiers describe configured connections, not measured public IPs.
Locks coordinate threads inside this process.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import timezone
from email.utils import parsedate_to_datetime
import gzip
import http.client
import io
from html.parser import HTMLParser
from http.cookiejar import CookieJar
import json
import math
import re
import socket
import ssl
import threading
import time
from typing import Optional
from urllib.parse import urljoin, urlparse
from urllib.request import Request

HOST = "app.sa.zain.com"
SERVICE_INTERVAL = 3.9
MAX_BODY_BYTES = 2 * 1024 * 1024
_IP_LOCKS_GUARD = threading.RLock()
_IP_SERVICE_LOCKS: dict[str, threading.Lock] = {}
_IP_LAST_QUERY_TIME: dict[str, float] = {}
_IP_RETRY_UNTIL: dict[str, float] = {}
_IP_COOKIES: dict[str, CookieJar] = {}


def normalize_number(number: str) -> str:
    text = str(number or "").strip()
    # Excel may contain Arabic or Persian decimal digits.
    return "".join(str(int(c)) if c.isdecimal() else c for c in text)


def connection_group(proxy_url: Optional[str] = None) -> str:
    if not proxy_url or not str(proxy_url).strip():
        return "local_direct"
    parsed = urlparse(str(proxy_url).strip())
    if parsed.scheme.lower() not in ("socks5", "socks5h", "socks4", "socks4a", "http") or not parsed.hostname:
        raise ValueError("Invalid or unsupported proxy configuration")
    port = parsed.port or (8080 if parsed.scheme.lower() == "http" else 1080)
    return f"proxy_{parsed.hostname.lower()}:{port}"


def get_ip_service_lock(ip_tag: str) -> threading.Lock:
    with _IP_LOCKS_GUARD:
        return _IP_SERVICE_LOCKS.setdefault(ip_tag, threading.Lock())


def get_ip_service_retry_after(ip_tag: str) -> float:
    with _IP_LOCKS_GUARD:
        return max(0.0, _IP_RETRY_UNTIL.get(ip_tag, 0.0) - time.monotonic())


def set_ip_service_cooldown(ip_tag: str, seconds: float) -> None:
    with _IP_LOCKS_GUARD:
        _IP_RETRY_UNTIL[ip_tag] = max(_IP_RETRY_UNTIL.get(ip_tag, 0.0), time.monotonic() + max(0.0, seconds))


@contextmanager
def service_query_slot(proxy_url: Optional[str] = None):
    """Hold the lock for the actual request, including browser requests.

    Contract requests never enter this slot. Every service attempt, including
    session retries, waits at least 3.9 seconds after the preceding attempt.
    """
    ip_tag = connection_group(proxy_url)
    with get_ip_service_lock(ip_tag):
        while True:
            with _IP_LOCKS_GUARD:
                last = _IP_LAST_QUERY_TIME.get(ip_tag)
                deadline = max(
                    last + SERVICE_INTERVAL if last is not None else 0.0,
                    _IP_RETRY_UNTIL.get(ip_tag, 0.0),
                )
            delay = deadline - time.monotonic()
            if delay <= 0:
                break
            time.sleep(delay)
        try:
            yield ip_tag
        finally:
            with _IP_LOCKS_GUARD:
                _IP_LAST_QUERY_TIME[ip_tag] = time.monotonic()


def clear_ip_cookies(ip_tag: str) -> None:
    with _IP_LOCKS_GUARD:
        _IP_COOKIES.pop(ip_tag, None)


def _cookie_jar(ip_tag: str) -> CookieJar:
    # Accessed only while the guard is held; network I/O does not hold it.
    return _IP_COOKIES.setdefault(ip_tag, CookieJar())


def browser_cookies(ip_tag: str) -> list[dict]:
    with _IP_LOCKS_GUARD:
        jar = _cookie_jar(ip_tag)
        jar.clear_expired_cookies()
        result = []
        for cookie in jar:
            item = {"name": cookie.name, "value": cookie.value,
                    "domain": cookie.domain, "path": cookie.path,
                    "secure": cookie.secure,
                    "httpOnly": cookie.has_nonstandard_attr("HttpOnly")}
            same_site = cookie.get_nonstandard_attr("SameSite", "")
            if str(same_site).capitalize() in ("Strict", "Lax", "None"):
                item["sameSite"] = str(same_site).capitalize()
            if cookie.expires is not None:
                item["expires"] = cookie.expires
            result.append(item)
        return result


def store_browser_cookies(ip_tag: str, cookies: list[dict], *, replace: bool = False) -> None:
    from http.cookiejar import Cookie
    with _IP_LOCKS_GUARD:
        if replace:
            _IP_COOKIES[ip_tag] = CookieJar()
        jar = _cookie_jar(ip_tag)
        for item in cookies:
            domain = item.get("domain", "")
            if domain.lstrip(".").lower() not in (HOST, "sa.zain.com", "zain.com"):
                continue
            expires = item.get("expires", -1)
            expires = int(expires) if expires is not None and expires >= 0 else None
            jar.set_cookie(Cookie(
                version=0, name=item["name"], value=item["value"],
                port=None, port_specified=False, domain=domain,
                domain_specified=domain.startswith("."), domain_initial_dot=domain.startswith("."),
                path=item.get("path", "/"), path_specified=True,
                secure=bool(item.get("secure")), expires=expires,
                discard=expires is None, comment=None, comment_url=None,
                rest={**({"HttpOnly": None} if item.get("httpOnly") else {}),
                      **({"SameSite": item["sameSite"]} if item.get("sameSite") else {})},
            ))
        jar.clear_expired_cookies()


def _valid_amount(value) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return amount if math.isfinite(amount) and amount >= 0 else None


class _PageSignals(HTMLParser):
    """Ignore script/template strings when reading rejection text."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = []
        self.suppressed = []
        self.captcha_input = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("script", "style", "template", "noscript"):
            self.suppressed.append(tag)
        if not self.suppressed and tag == "input":
            self.captcha_input |= attrs.get("id") == "ans" and attrs.get("name") == "answer"

    def handle_endtag(self, tag):
        if self.suppressed and self.suppressed[-1] == tag:
            self.suppressed.pop()

    def handle_data(self, data):
        if not self.suppressed:
            self.text.append(data)


def classify_web_response(status: int, headers, body: str, url: str):
    """Classify transport outcomes without treating missing data as payment."""
    lower = body.lower()
    location = headers.get("Location", "")
    signals = _PageSignals()
    signals.feed(body)
    visible = " ".join(signals.text).lower()
    challenge = any(signature in lower or signature in str(location).lower()
                    for signature in ("/tspd/", "bobcmn", "ts_challenge"))
    rejected = any(signature in visible for signature in
                   ("request rejected", "the requested url was rejected"))
    if status == 429 or rejected:
        return "blocked", None, f"رفض الطلب (HTTP {status})"
    if signals.captcha_input and any(text in visible for text in
                                    ("what code is in the image", "human visitor", "captcha")):
        return "verification_required", None, "تطلب صفحة زين تحققًا بشريًا؛ أُوقف الفحص لتجنب تكرار الطلبات"
    if status == 403 and not challenge:
        return "blocked", None, "رفض الطلب (HTTP 403)"
    if status in (301, 302, 303, 307, 308):
        if challenge:
            return "session_expired", None, "تحتاج جلسة الويب إلى تجديد"
        target = urlparse(urljoin(url, location)) if location else None
        if target and target.hostname == HOST and target.path.rstrip("/") == "/ar/home":
            return "not_found", None, "تحويل إلى الرئيسية؛ لا يوجد مبلغ مؤكد"
        return "unknown_response", None, "تحويل غير معروف أو دون Location"
    if status >= 500:
        return "network_error", None, f"تعذر استكمال الطلب (HTTP {status})"
    if status < 200 or status >= 300:
        if challenge:
            return "session_expired", None, "تحتاج جلسة الويب إلى تجديد"
        return "unknown_response", None, f"استجابة غير مؤكدة (HTTP {status})"
    match = re.search(r"\b(?:var|let|const)\s+quickpayData\s*=\s*", body)
    if match:
        try:
            data, _ = json.JSONDecoder().raw_decode(body[match.end():])
            amount = _valid_amount(data.get("amount")) if isinstance(data, dict) else None
        except (ValueError, TypeError):
            amount = None
        if amount is not None:
            return "ok", amount, "تمت قراءة المبلغ"
        return "unknown_response", None, "بيانات الفاتورة لا تحتوي مبلغًا صالحًا"
    match_bill = re.search(r"\b(?:var|let|const)\s+billDetails\s*=\s*(\{.*?\});", body, re.DOTALL)
    if match_bill:
        total = re.search(r"\btotal\s*:\s*([\d.]+)", match_bill.group(1))
        amount = _valid_amount(total.group(1)) if total else None
        if amount is not None:
            return "ok", amount, "تمت قراءة المبلغ"
    # Healthy payment pages also load /TSPD/ scripts. Valid bill data above
    # takes precedence over a dormant security loader.
    if challenge:
        return "session_expired", None, "تحتاج جلسة الويب إلى تجديد"
    if "support id" in visible:
        return "blocked", None, f"رفض الطلب؛ Support ID ظاهر دون بيانات فاتورة (HTTP {status})"
    if "غير موجود" in visible or "الرقم غير صحيح" in visible or "not found" in visible:
        return "not_found", None, "الرقم غير موجود؛ لا يوجد مبلغ مؤكد"
    if "لا يوجد مستحقات" in visible or "لا توجد فواتير" in visible:
        return "ok", 0.0, "لا توجد مستحقات وفق الاستجابة"
    return "unknown_response", None, "لم تتضمن الاستجابة مبلغًا مؤكدًا"


def _retry_after_seconds(headers) -> float:
    raw = str(headers.get("Retry-After", "")).strip()
    try:
        seconds = float(raw) if raw else 0.0
        return max(0.0, seconds) if math.isfinite(seconds) else 0.0
    except ValueError:
        try:
            date = parsedate_to_datetime(raw)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(0.0, date.timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return 0.0


def note_web_rejection(ip_tag: str, proxy_url: Optional[str], headers) -> None:
    # Keep proxy actors available, while honoring explicit server Retry-After.
    delay = _retry_after_seconds(headers)
    if not proxy_url:
        delay = max(12.0, delay)
    if delay:
        set_ip_service_cooldown(ip_tag, delay)


def _open_tls_socket(proxy_url: Optional[str], timeout: float):
    if proxy_url:
        import socks
        parsed = urlparse(proxy_url)
        connection_group(proxy_url)  # Validate before opening any connection.
        kind = socks.HTTP if parsed.scheme == "http" else (socks.SOCKS4 if "4" in parsed.scheme else socks.SOCKS5)
        raw = socks.socksocket()
        raw.set_proxy(kind, parsed.hostname, parsed.port or (8080 if kind == socks.HTTP else 1080),
                      username=parsed.username, password=parsed.password)
        raw.settimeout(timeout)
        try:
            raw.connect((HOST, 443))
        except Exception:
            raw.close()
            raise
    else:
        raw = socket.create_connection((HOST, 443), timeout=timeout)
    try:
        return ssl.create_default_context().wrap_socket(raw, server_hostname=HOST)
    except Exception:
        raw.close()
        raise


def _query_web_once(number: str, proxy_url: Optional[str], timeout: float):
    ip_tag = connection_group(proxy_url)
    path = f"/ar/quickpay?account={number}" if number.startswith("2") else f"/ar/contract-payment?contract={number}"
    url = f"https://{HOST}{path}"
    request = Request(url, headers={
        "Host": HOST, "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml", "Accept-Language": "ar,en;q=0.9",
        "Accept-Encoding": "gzip", "Connection": "close",
    })
    with _IP_LOCKS_GUARD:
        _cookie_jar(ip_tag).add_cookie_header(request)
    connection = _open_tls_socket(proxy_url, timeout)
    try:
        message = f"GET {path} HTTP/1.1\r\n" + "".join(f"{key}: {value}\r\n" for key, value in request.header_items()) + "\r\n"
        connection.sendall(message.encode("utf-8"))
        with http.client.HTTPResponse(connection) as response:
            response.begin()
            with _IP_LOCKS_GUARD:
                _cookie_jar(ip_tag).extract_cookies(response, request)
            raw = response.read(MAX_BODY_BYTES + 1)
            if len(raw) > MAX_BODY_BYTES:
                return "unknown_response", None, "استجابة أكبر من الحد المسموح"
            if response.length is not None and response.length > 0:
                raise http.client.IncompleteRead(raw, response.length)
            if response.headers.get("Content-Encoding", "").lower() == "gzip":
                with gzip.GzipFile(fileobj=io.BytesIO(raw)) as compressed:
                    raw = compressed.read(MAX_BODY_BYTES + 1)
                if len(raw) > MAX_BODY_BYTES:
                    return "unknown_response", None, "استجابة أكبر من الحد المسموح"
            body = raw.decode(response.headers.get_content_charset() or "utf-8", errors="replace")
            result = classify_web_response(response.status, response.headers, body, url)
            if result[0] == "blocked":
                note_web_rejection(ip_tag, proxy_url, response.headers)
            elif result[0] == "session_expired":
                clear_ip_cookies(ip_tag)
            return result
    finally:
        connection.close()


def query_web_number(number: str, proxy_url: Optional[str] = None, timeout: float = 10.0):
    number = normalize_number(number)
    if not re.fullmatch(r"[0-9]+", number):
        return "error", None, "رقم البحث غير صالح"
    try:
        ip_tag = connection_group(proxy_url)
        if number.startswith("2"):
            with service_query_slot(proxy_url):
                return _query_web_once(number, proxy_url, timeout)
        if get_ip_service_retry_after(ip_tag):
            return "blocked", None, "انتظار مهلة إعادة المحاولة المحددة للاتصال"
        return _query_web_once(number, proxy_url, timeout)
    except Exception as exc:
        return "network_error", None, f"تعذر استكمال الطلب: {type(exc).__name__}"
