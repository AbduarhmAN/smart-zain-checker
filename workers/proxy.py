"""Proxy Management and Verification for Smart Zain Checker Workers.
"""
from __future__ import annotations

import urllib.request
from typing import Optional, Tuple


def verify_proxy_connectivity(proxy_url: Optional[str], timeout: float = 6.0) -> Tuple[bool, str]:
    """Verifies whether a given proxy URL is reachable (HTTP, HTTPS, or SOCKS5).
    Returns (is_healthy: bool, details_or_ip: str).
    """
    if not proxy_url or not str(proxy_url).strip():
        return True, "Direct Connection (No Proxy)"

    clean_proxy = str(proxy_url).strip()
    
    # Handle SOCKS5 / SOCKS4 via PySocks
    if clean_proxy.lower().startswith("socks5://") or clean_proxy.lower().startswith("socks4://"):
        try:
            import socks
            from urllib.parse import urlparse
            parsed = urlparse(clean_proxy)
            host = parsed.hostname
            port = parsed.port or 1080
            proxy_type = socks.SOCKS5 if "5" in parsed.scheme else socks.SOCKS4
            
            s = socks.socksocket()
            s.set_proxy(proxy_type, host, port, username=parsed.username, password=parsed.password)
            s.settimeout(timeout)
            s.connect(("api.ipify.org", 80))
            s.sendall(b"GET / HTTP/1.1\r\nHost: api.ipify.org\r\nConnection: close\r\n\r\n")
            raw_resp = s.recv(2048).decode("utf-8", errors="ignore")
            s.close()
            
            lines = raw_resp.split("\r\n\r\n", 1)
            live_ip = lines[1].strip() if len(lines) > 1 else "Active"
            return True, f"Proxy Healthy (Egress IP: {live_ip})"
        except Exception as exc:
            return False, f"Proxy Unreachable: {exc}"

    # Handle HTTP / HTTPS
    if not clean_proxy.startswith("http://") and not clean_proxy.startswith("https://"):
        clean_proxy = f"http://{clean_proxy}"

    try:
        proxy_handler = urllib.request.ProxyHandler({
            "http": clean_proxy,
            "https": clean_proxy,
        })
        opener = urllib.request.build_opener(proxy_handler)
        req = urllib.request.Request(
            "https://api.ipify.org?format=text",
            headers={"User-Agent": "ZainChecker-HealthCheck/1.0"},
        )
        with opener.open(req, timeout=timeout) as resp:
            live_ip = resp.read().decode("utf-8").strip()
            return True, f"Proxy Healthy (Egress IP: {live_ip})"
    except Exception as exc:
        return False, f"Proxy Unreachable: {exc}"

