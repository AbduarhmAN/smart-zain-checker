"""Stealth Browser Service Engine for Smart Zain Checker.
Simulates a real human browser session to query app.sa.zain.com/ar/quickpay?account={service}
without triggering F5 BIG-IP Bot Defense challenges.
Thread-Safe: Uses a dedicated background worker thread to host the Playwright greenlet,
preventing 'greenlet.error: Cannot switch to a different thread' crashes across multi-worker pools.
"""
from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("StealthServiceEngine")

# Singleton worker thread instance
_WORKER_LOCK = threading.Lock()
_GLOBAL_STEALTH_WORKER: Optional[StealthServiceWorker] = None


class StealthServiceWorker(threading.Thread):
    """Dedicated background worker thread hosting Playwright exclusively."""

    def __init__(self, headless: bool = True):
        super().__init__(daemon=True, name="StealthPlaywrightThread")
        self.headless = headless
        self.work_queue: queue.Queue = queue.Queue()
        self.is_ready_event = threading.Event()
        self.stop_event = threading.Event()

    def run(self):
        """Thread loop where sync_playwright and its greenlet live exclusively."""
        from playwright.sync_api import sync_playwright

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    headless=self.headless,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-infobars",
                    ],
                )
                context = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                    locale="ar-SA",
                    timezone_id="Asia/Riyadh",
                    viewport={"width": 1366, "height": 768},
                    extra_http_headers={
                        "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
                        "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
                        "Sec-Ch-Ua-Mobile": "?0",
                        "Sec-Ch-Ua-Platform": '"Windows"',
                    },
                )
                page = context.new_page()
                page.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                    window.chrome = { runtime: {} };
                """)

                self.is_ready_event.set()
                logger.info("Stealth Playwright Engine initialized safely on dedicated thread.")

                while not self.stop_event.is_set():
                    try:
                        item = self.work_queue.get(timeout=0.5)
                    except queue.Empty:
                        continue

                    if item is None:
                        break

                    service_num, result_queue, timeout_sec = item
                    try:
                        res = self._do_query(page, context, service_num, timeout_sec)
                        result_queue.put(res)
                    except Exception as exc:
                        logger.error(f"Error querying service {service_num}: {exc}")
                        result_queue.put(("error", None, str(exc)))
                    finally:
                        self.work_queue.task_done()

                try:
                    browser.close()
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"Stealth Playwright thread encountered fatal error: {e}")
            self.is_ready_event.set()

    def _do_query(self, page, context, clean_num: str, timeout_seconds: float) -> Tuple[str, Optional[float], str]:
        url = f"https://app.sa.zain.com/ar/quickpay?account={clean_num}"
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=int(timeout_seconds * 1000))
            page.wait_for_timeout(600)
            content = page.content()

            # 1. Check quickpayData variable
            m = re.search(r"var\s+quickpayData\s*=\s*(\{.*?\});", content)
            if m:
                try:
                    data = json.loads(m.group(1))
                    amt = float(data.get("amount", 0.0))
                    return "ok", amt, "تم جلب رصيد الفاتورة بنجاح"
                except Exception as json_err:
                    logger.warning(f"Error parsing quickpayData JSON: {json_err}")

            # 2. Check for zero-due indicators in HTML
            body_text = page.inner_text("body")
            if "لا يوجد مستحقات" in body_text or "لا توجد فواتير" in body_text or "0.00" in body_text:
                return "ok", 0.0, "تم سداد الفاتورة (رصيد 0.00)"

            if "غير موجود" in body_text or "الرقم غير صحيح" in body_text:
                return "not_found", None, "الرقم غير مسجل بنظام زين"

            if "Request Rejected" in body_text or "rejected" in content.lower():
                page.wait_for_timeout(1000)
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=int(timeout_seconds * 1000))
                    page.wait_for_timeout(600)
                    content2 = page.content()
                    m2 = re.search(r"var\s+quickpayData\s*=\s*(\{.*?\});", content2)
                    if m2:
                        d2 = json.loads(m2.group(1))
                        return "ok", float(d2.get("amount", 0.0)), "تم جلب رصيد الفاتورة بنجاح"
                    body2 = page.inner_text("body")
                    if "لا يوجد مستحقات" in body2 or "0.00" in body2:
                        return "ok", 0.0, "تم سداد الفاتورة (رصيد 0.00)"
                except Exception:
                    pass
                return "not_found", None, "لم يتم العثور على بيانات الفاتورة"

            return "not_found", None, "لم يتم العثور على بيانات الفاتورة"

        except Exception as exc:
            return "error", None, str(exc)


def get_stealth_service_worker() -> StealthServiceWorker:
    """Returns the singleton worker thread hosting Playwright."""
    global _GLOBAL_STEALTH_WORKER
    with _WORKER_LOCK:
        if _GLOBAL_STEALTH_WORKER is None or not _GLOBAL_STEALTH_WORKER.is_alive():
            _GLOBAL_STEALTH_WORKER = StealthServiceWorker(headless=True)
            _GLOBAL_STEALTH_WORKER.start()
            _GLOBAL_STEALTH_WORKER.is_ready_event.wait(timeout=15.0)
        return _GLOBAL_STEALTH_WORKER


def query_service_stealth(service_num: str, timeout_seconds: float = 15.0) -> Tuple[str, Optional[float], str]:
    """Public thread-safe function to query a service number via stealth browser."""
    clean_num = str(service_num).strip()
    if not clean_num:
        return "error", None, "رقم خدمة فارغ"

    worker = get_stealth_service_worker()
    resp_q: queue.Queue = queue.Queue()
    worker.work_queue.put((clean_num, resp_q, timeout_seconds))

    try:
        return resp_q.get(timeout=timeout_seconds + 5.0)
    except queue.Empty:
        return "network_error", None, "انتهت مهلة استعلام متصفح الخدمة"
