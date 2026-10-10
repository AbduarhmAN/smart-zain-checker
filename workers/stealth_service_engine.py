"""Ordinary JavaScript browser transport for the service payment page.

The browser owns the shared service lock through navigation and result reading.
Completed pages are retained; a timed-out navigation is closed before release.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import logging
from pathlib import Path
import queue
import re
import threading
import time
import uuid
from typing import Callable, Optional, Tuple
from urllib.parse import urlparse

from workers.service_transport import (
    HOST, classify_web_response, clear_ip_cookies, connection_group,
    normalize_number, note_web_rejection, service_query_slot, store_browser_cookies,
)

logger = logging.getLogger("StealthServiceEngine")
_WORKER_LOCK = threading.Lock()
_GLOBAL_STEALTH_WORKER: Optional[StealthServiceWorker] = None
_PROXY_BROWSER_WORKERS: dict[str, StealthServiceWorker] = {}


@dataclass
class BrowserJob:
    number: str
    timeout: float
    deadline: float
    cancelled: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    result: tuple = ("network_error", None, "لم يكتمل استعلام المتصفح")
    on_verification: Optional[Callable[[str], None]] = None
    verification_pending: threading.Event = field(default_factory=threading.Event)


_SOCKS5_BRIDGES: dict[str, int] = {}
_SOCKS5_BRIDGE_LOCK = threading.Lock()


def _get_or_create_socks5_bridge(proxy_url: str) -> Optional[str]:
    """Chromium fundamentally rejects SOCKS5 proxies requiring authentication.
    Spawns an internal 127.0.0.1 forwarder that speaks authenticated SOCKS5 upstream
    and serves unauthenticated HTTP CONNECT locally to Chromium.
    """
    if not proxy_url:
        return None
    parsed = urlparse(proxy_url)
    if parsed.scheme not in ("socks5", "socks5h") or not parsed.username:
        return None
    with _SOCKS5_BRIDGE_LOCK:
        if proxy_url in _SOCKS5_BRIDGES:
            return f"http://127.0.0.1:{_SOCKS5_BRIDGES[proxy_url]}"
        import socket
        import socks
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(50)
        port = srv.getsockname()[1]
        _SOCKS5_BRIDGES[proxy_url] = port

        def handle_client(c):
            try:
                buf = b""
                while b"\r\n\r\n" not in buf:
                    chunk = c.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                line = buf.split(b"\r\n")[0].decode("latin1", errors="ignore")
                parts = line.split(" ")
                if len(parts) >= 2 and parts[0].upper() == "CONNECT":
                    target_host, target_port = parts[1].split(":")
                    rem = socks.socksocket()
                    rem.set_proxy(
                        socks.SOCKS5,
                        parsed.hostname,
                        parsed.port or 1080,
                        username=parsed.username,
                        password=parsed.password,
                    )
                    rem.settimeout(20)
                    rem.connect((target_host, int(target_port)))
                    c.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")

                    def pipe(src, dst):
                        try:
                            while True:
                                d = src.recv(8192)
                                if not d:
                                    break
                                dst.sendall(d)
                        except Exception:
                            pass
                        finally:
                            try:
                                src.close()
                            except Exception:
                                pass
                            try:
                                dst.close()
                            except Exception:
                                pass

                    threading.Thread(target=pipe, args=(c, rem), daemon=True).start()
                    threading.Thread(target=pipe, args=(rem, c), daemon=True).start()
            except Exception:
                try:
                    c.close()
                except Exception:
                    pass

        def loop():
            while True:
                try:
                    sock, _ = srv.accept()
                    threading.Thread(target=handle_client, args=(sock,), daemon=True).start()
                except Exception:
                    break

        threading.Thread(target=loop, daemon=True, name=f"Socks5Bridge-{port}").start()
        logger.info("Started internal SOCKS5 bridge on 127.0.0.1:%s for %s", port, parsed.hostname)
        return f"http://127.0.0.1:{port}"


class StealthServiceWorker(threading.Thread):
    def __init__(self, headless: bool = False, proxy_url: Optional[str] = None):
        super().__init__(daemon=True, name="ServiceBrowserThread")
        self.headless = headless
        self.proxy_url = proxy_url
        self.work_queue: queue.Queue = queue.Queue()
        self.is_ready_event = threading.Event()
        self.stop_event = threading.Event()
        self.startup_error = ""
        self.verification_lock = threading.Lock()
        self.verification = None
        self.service_page = None

    @staticmethod
    def _ready_expression(include_challenges: bool = True) -> str:
        payment = """(expectedNumber) => {
            if (location.hostname !== 'app.sa.zain.com') return false;
            if (location.pathname.replace(/\\/$/, '') === '/ar/home') return true;
            const amount = document.querySelector('#customAmount');
            const data = window.quickpayData;
            const valid = data && data.amount !== null && data.amount !== '' &&
                typeof data.amount !== 'boolean' && Number.isFinite(Number(data.amount)) && Number(data.amount) >= 0;
            if (location.pathname.replace(/\\/$/, '') === '/ar/quickpay' && valid &&
                String(data.account) === expectedNumber) return true;
        """
        if include_challenges:
            payment += """
                const text = document.body?.innerText || '';
                if (document.querySelector('input#ans[name=answer]') ||
                    /request rejected|the requested url was rejected|لا يوجد مستحقات|لا توجد فواتير|الرقم غير صحيح|غير موجود|not found/i.test(text)) return true;
            """
        return payment + "return false; }"

    def _wait_until_ready(self, page, job: BrowserJob, *, include_challenges: bool = True) -> None:
        remaining = job.deadline - time.monotonic()
        if job.cancelled.is_set() or remaining <= 0:
            raise TimeoutError("Service browser deadline expired")
        try:
            page.wait_for_load_state("domcontentloaded", timeout=max(1, min(2000, int(remaining * 1000))))
        except Exception:
            pass
        remaining = job.deadline - time.monotonic()
        if job.cancelled.is_set() or remaining <= 0:
            raise TimeoutError("Service browser deadline expired")
        page.wait_for_function(self._ready_expression(include_challenges), arg=job.number,
                               timeout=max(1, int(remaining * 1000)))

    def _launch_context(self, pw):
        """Use installed Chrome with a dedicated persistent profile per route.

        Never open or copy the user's personal Chrome profile. A profile that
        is already in use fails closed rather than switching routes/browsers.
        """
        route = connection_group(self.proxy_url)
        profile_key = hashlib.sha256(route.encode("utf-8")).hexdigest()[:20]
        profile = Path(__file__).resolve().parent.parent / ".zain-service-profiles" / profile_key
        options = {
            "channel": "chrome",
            "headless": self.headless,
            "locale": "ar-SA",
            "timeout": 20000,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
            "ignore_default_args": ["--enable-automation"],
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        }
        if self.proxy_url:
            bridge_server = _get_or_create_socks5_bridge(self.proxy_url)
            if bridge_server:
                options["proxy"] = {"server": bridge_server}
            else:
                proxy = urlparse(self.proxy_url)
                scheme = "socks5" if proxy.scheme in ("socks5", "socks5h") else ("socks4" if "4" in proxy.scheme else "http")
                options["proxy"] = {"server": f"{scheme}://{proxy.hostname}:{proxy.port or (8080 if scheme == 'http' else 1080)}"}
                if proxy.username:
                    options["proxy"]["username"] = proxy.username
                if proxy.password:
                    options["proxy"]["password"] = proxy.password
        ctx = pw.chromium.launch_persistent_context(str(profile), **options)
        if hasattr(ctx, "add_init_script"):
            ctx.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'languages', { get: () => ['ar-SA', 'ar', 'en-US', 'en'] });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                window.chrome = { runtime: {} };
            """)
        # The dedicated route profile retains its own session across recovery.
        # Cookies are cleared only after a classified session failure/rejection.
        return ctx

    def _payment_ready(self, page, job: BrowserJob) -> bool:
        """Observe a human completing verification in the visible window."""
        return bool(page.evaluate(self._ready_expression(False), job.number))

    def _manual_verification(self, page, job: BrowserJob) -> bool:
        """Keep this page and its route lock while a human enters the code."""
        if job.on_verification is None or job.cancelled.is_set():
            return False
        if not self.headless:
            page.bring_to_front()
        deadline = time.monotonic() + 180.0
        job.deadline = deadline + job.timeout
        job.verification_pending.set()
        pending = {"id": uuid.uuid4().hex, "image": page.screenshot(full_page=True),
                   "answers": queue.Queue(maxsize=1), "deadline": deadline,
                   "route": "proxy" if self.proxy_url else "direct", "submitted": False,
                   "cancel": job.cancelled}
        with self.verification_lock:
            self.verification = pending
        try:
            job.on_verification("تطلب جلسة زين تحققًا؛ أكمله في نافذة Chrome المفتوحة أو أدخل رمز الصورة في لوحة المتابعة")
            while not job.cancelled.is_set() and time.monotonic() < deadline:
                if not self.headless:
                    from playwright.sync_api import Error as PlaywrightError
                    try:
                        if self._payment_ready(page, job):
                            return not job.cancelled.is_set()
                    except PlaywrightError:
                        if page.is_closed():
                            return False
                        # Human navigation may replace the JavaScript context.
                try:
                    answer = pending["answers"].get(timeout=0.2)
                    break
                except queue.Empty:
                    continue
            else:
                return False
            # Only the explicit answer submitted by the user/operator is used.
            job.deadline = time.monotonic() + job.timeout
            page.locator("input#ans[name=answer]").fill(answer)
            page.locator("button#jar").click(timeout=int(job.timeout * 1000))
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            try:
                self._wait_until_ready(page, job, include_challenges=False)
            except PlaywrightTimeoutError:
                return False
            return not job.cancelled.is_set()
        finally:
            with self.verification_lock:
                self.verification = None

    def _is_context_alive(self, context) -> bool:
        if context is None:
            return False
        try:
            if hasattr(context, "browser") and context.browser and not context.browser.is_connected():
                return False
            _ = context.pages
            return True
        except Exception:
            return False

    def _heal_context(self, pw, old_context):
        self.service_page = None
        if old_context:
            try:
                old_context.close()
            except Exception:
                pass
        time.sleep(1.0)
        # Chrome manages its profile locks. Do not remove another live context's locks.
        return self._launch_context(pw)

    def run(self):
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                context = self._launch_context(pw)
                try:
                    self.is_ready_event.set()
                    while not self.stop_event.is_set():
                        try:
                            job = self.work_queue.get(timeout=0.5)
                        except queue.Empty:
                            continue
                        try:
                            if job is None:
                                break
                            if not self._is_context_alive(context):
                                logger.warning("Stealth browser context is closed or disconnected. Self-healing...")
                                context = self._heal_context(pw, context)
                            if not job.cancelled.is_set():
                                # Attempt 1: Re-use the existing instance and tab
                                try:
                                    res = self._do_query(context, job)
                                except Exception as exc:
                                    exc_name = type(exc).__name__
                                    err_msg = str(exc)
                                    is_target_closed = "TargetClosed" in exc_name or ("target" in err_msg.lower() and "closed" in err_msg.lower())
                                    if job.verification_pending.is_set():
                                        res = ("verification_required", None, "تعذر إكمال التحقق؛ أعد المحاولة عند جاهزية جلسة زين")
                                    elif is_target_closed:
                                        res = ("network_error", None, "أُغلقت جلسة المتصفح؛ يجري استعادتها (TargetClosedError)")
                                    else:
                                        res = ("network_error", None, f"تعذر استكمال المتصفح: {exc_name}")

                                # Attempt 2 (Smart Instance Retry):
                                # If attempt 1 did not produce a clean result (not "ok" and not "not_found"),
                                # and not blocked (blocked needs router cooldown, not immediate retry on same route):
                                status = res[0] if res else "error"
                                if status not in ("ok", "not_found", "verification_required", "blocked") and not job.cancelled.is_set():
                                    logger.warning(
                                        "Service query for %s returned '%s' on existing instance. "
                                        "Closing old instance and retrying with a fresh instance...",
                                        job.number, status
                                    )
                                    try:
                                        context = self._heal_context(pw, context)
                                        if job.deadline - time.monotonic() < 20.0:
                                            job.deadline = time.monotonic() + 25.0
                                        if not job.cancelled.is_set():
                                            res = self._do_query(context, job)
                                    except Exception as retry_exc:
                                        logger.warning("Retry on fresh instance failed: %s", retry_exc)

                                job.result = res
                        except Exception as exc:
                            self.service_page = None
                            exc_name = type(exc).__name__
                            err_msg = str(exc)
                            is_target_closed = "TargetClosed" in exc_name or ("target" in err_msg.lower() and "closed" in err_msg.lower())
                            if job is not None:
                                if job.verification_pending.is_set():
                                    job.result = ("verification_required", None, "تعذر إكمال التحقق؛ أعد المحاولة عند جاهزية جلسة زين")
                                elif is_target_closed:
                                    job.result = ("network_error", None, "أُغلقت جلسة المتصفح؛ يجري استعادتها (TargetClosedError)")
                                else:
                                    job.result = ("network_error", None, f"تعذر استكمال المتصفح: {exc_name}")
                            # Immediately heal context if target or context was closed/disconnected
                            if is_target_closed or not self._is_context_alive(context):
                                if job is not None:
                                    job.done.set()
                                logger.warning("TargetClosed or invalid browser context. Healing context...")
                                try:
                                    context = self._heal_context(pw, context)
                                except Exception as heal_err:
                                    logger.error("Failed to heal browser context: %s", heal_err)
                        finally:
                            if job is not None:
                                job.done.set()
                            self.work_queue.task_done()
                finally:
                    self.service_page = None
                    try:
                        context.close()
                    except Exception:
                        pass
        except Exception as exc:
            self.startup_error = type(exc).__name__
            logger.error("Service browser unavailable: %s", self.startup_error)
        finally:
            self.is_ready_event.set()

    def _do_query(self, context, job: BrowserJob):
        if job.cancelled.is_set() or time.monotonic() >= job.deadline:
            return "network_error", None, "انتهت مهلة المتصفح قبل بدء الطلب"
        url = f"https://{HOST}/ar/quickpay?account={job.number}"
        with service_query_slot(self.proxy_url) as ip_tag:
            remaining = min(job.timeout, job.deadline - time.monotonic())
            if job.cancelled.is_set() or remaining <= 0:
                return "network_error", None, "انتهت مهلة المتصفح قبل بدء الطلب"
            page = self.service_page
            if page is None or page.is_closed():
                try:
                    pages = [p for p in context.pages if not p.is_closed()]
                    page = pages[0] if pages else context.new_page()
                except Exception:
                    page = context.new_page()
                self.service_page = page
            keep_page = False
            responses = []
            def on_response(resp):
                if resp.request.is_navigation_request() and resp.frame == page.main_frame:
                    responses.append(resp)
            page.on("response", on_response)
            try:
                # Chrome's persistent context owns cookies and local storage.
                # Replacing them from an HTTP jar would discard browser state.
                response = page.goto(url, wait_until="domcontentloaded", timeout=max(1, int(remaining * 1000)))
                if job.cancelled.is_set():
                    return "network_error", None, "انتهت مهلة استعلام المتصفح"
                # Let normal page JavaScript finish. Never click payment or
                # CAPTCHA controls, or alter browser security fingerprints.
                # A timeout must never fall through to parse a partial page.
                self._wait_until_ready(page, job)
                if job.cancelled.is_set():
                    return "network_error", None, "انتهت مهلة استعلام المتصفح"
                body = page.content()
                if responses:
                    response = responses[-1]
                headers = response.all_headers() if response else {}
                # Normalize header lookup for the common HTTP classifier.
                from email.message import Message
                header_message = Message()
                for key, value in headers.items():
                    header_message[key] = value
                status = response.status if response else 0
                target = urlparse(page.url)
                if target.hostname == HOST and target.path.rstrip("/") == "/ar/home":
                    status = 302
                    header_message["Location"] = page.url
                elif target.hostname != HOST or (target.path.rstrip("/") != "/ar/quickpay" and not target.path.startswith("/TSPD/")):
                    return "unknown_response", None, "انتقل المتصفح إلى صفحة غير متوقعة"
                result = classify_web_response(status, header_message, body, url)
                if result[0] == "verification_required" and self._manual_verification(page, job):
                    target = urlparse(page.url)
                    if target.hostname != HOST or target.path.rstrip("/") not in ("/ar/quickpay", "/ar/home"):
                        return "unknown_response", None, "انتقل المتصفح إلى صفحة غير متوقعة"
                    if responses:
                        response = responses[-1]
                        status = response.status
                        header_message = Message()
                        for key, value in response.all_headers().items():
                            header_message[key] = value
                    if target.path.rstrip("/") == "/ar/home":
                        status = 302
                        header_message["Location"] = page.url
                    result = classify_web_response(status, header_message, page.content(), url)
                # A full context snapshot also propagates cookie deletions.
                store_browser_cookies(ip_tag, context.cookies(), replace=True)
                if result[0] in ("session_expired", "blocked"):
                    context.clear_cookies(domain=re.compile(r"(^|\.)zain\.com$", re.I))
                    clear_ip_cookies(ip_tag)
                if result[0] == "blocked":
                    note_web_rejection(ip_tag, self.proxy_url, header_message)
                keep_page = result[0] in ("ok", "not_found") and not job.cancelled.is_set()
                return result
            finally:
                try:
                    page.remove_listener("response", on_response)
                except Exception:
                    pass
                if not keep_page:
                    # Stop incomplete navigation before releasing the route lock.
                    try:
                        if not page.is_closed():
                            page.close()
                    except Exception:
                        pass
                    self.service_page = None


def get_stealth_service_worker(proxy_url: Optional[str] = None) -> StealthServiceWorker:
    global _GLOBAL_STEALTH_WORKER
    with _WORKER_LOCK:
        if proxy_url:
            key = connection_group(proxy_url)
            worker = _PROXY_BROWSER_WORKERS.get(key)
            if worker is None or not worker.is_alive():
                worker = StealthServiceWorker(proxy_url=proxy_url)
                _PROXY_BROWSER_WORKERS[key] = worker
                worker.start()
        else:
            if _GLOBAL_STEALTH_WORKER is None or not _GLOBAL_STEALTH_WORKER.is_alive():
                _GLOBAL_STEALTH_WORKER = StealthServiceWorker()
                _GLOBAL_STEALTH_WORKER.start()
            worker = _GLOBAL_STEALTH_WORKER
    worker.is_ready_event.wait(timeout=15.0)
    return worker


def query_service_stealth(service_num: str, timeout_seconds: float = 45.0,
                          proxy_url: Optional[str] = None,
                          on_verification: Optional[Callable[[str], None]] = None) -> Tuple[str, Optional[float], str]:
    number = normalize_number(service_num)
    if not re.fullmatch(r"2[0-9]*", number):
        return "error", None, "هذا المسار مخصص لأرقام الخدمة التي تبدأ بـ2"
    worker = get_stealth_service_worker(proxy_url) if proxy_url else get_stealth_service_worker()
    if not worker.is_ready_event.is_set() or worker.startup_error or not worker.is_alive():
        return "network_error", None, "محرك المتصفح غير متاح"
    job = BrowserJob(number, timeout_seconds, time.monotonic() + timeout_seconds + 5.0)
    job.on_verification = on_verification
    worker.work_queue.put(job)
    while not job.done.wait(timeout=0.2):
        if time.monotonic() >= job.deadline:
            job.cancelled.set()
            # The browser continues to own the lock until the page is closed.
            return "network_error", None, "انتهت مهلة استعلام المتصفح"
    return job.result


def _existing_browser_workers():
    with _WORKER_LOCK:
        return ([_GLOBAL_STEALTH_WORKER] if _GLOBAL_STEALTH_WORKER else []) + list(_PROXY_BROWSER_WORKERS.values())


def _verification_active(pending) -> bool:
    return bool(pending and pending["deadline"] > time.monotonic()
                and not (pending.get("cancel") and pending["cancel"].is_set()))


def pending_verifications() -> list[dict]:
    result = []
    for worker in _existing_browser_workers():
        with worker.verification_lock:
            pending = worker.verification
            if _verification_active(pending):
                result.append({"id": pending["id"], "route": pending["route"],
                               "seconds_remaining": max(0, int(pending["deadline"] - time.monotonic())),
                               "submitted": pending["submitted"]})
    return result


def cancel_pending_verifications() -> None:
    for worker in _existing_browser_workers():
        with worker.verification_lock:
            if worker.verification:
                worker.verification["cancel"].set()


def stop_service_browsers() -> None:
    """Close owned Chrome contexts normally so persistent state is flushed."""
    workers = _existing_browser_workers()
    cancel_pending_verifications()
    for worker in workers:
        worker.stop_event.set()
        worker.work_queue.put(None)
    deadline = time.monotonic() + 20.0
    for worker in workers:
        if worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=max(0.0, deadline - time.monotonic()))


def verification_image(challenge_id: str) -> Optional[bytes]:
    for worker in _existing_browser_workers():
        with worker.verification_lock:
            pending = worker.verification
            if _verification_active(pending) and pending["id"] == challenge_id:
                return pending["image"]
    return None


def submit_verification(challenge_id: str, answer: str) -> bool:
    if not isinstance(answer, str) or not answer.strip() or len(answer) > 64:
        return False
    for worker in _existing_browser_workers():
        with worker.verification_lock:
            pending = worker.verification
            if _verification_active(pending) and pending["id"] == challenge_id and not pending["submitted"]:
                pending["answers"].put_nowait(answer.strip())
                pending["submitted"] = True
                return True
    return False
