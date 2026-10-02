"""Offline regression tests: no real sockets, browsers, files, or deletions.

Run: python -B -m unittest discover -s tests -p test_service2_regressions.py -v
"""
from __future__ import annotations

from contextlib import ExitStack
from email.message import Message
import gzip
import io
import json
from pathlib import Path
import queue
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from domain.models import Customer
from manager.orchestrator import Orchestrator
import manager.orchestrator as om
from workers.actor import WorkerActor
from workers.supervisor import WorkerSupervisor
import workers.service_transport as transport
import workers.stealth_service_engine as browser
import workers.zain_api as api
get_browser_worker = browser.get_stealth_service_worker


class Clock:
    def __init__(self):
        self.now = 10000.0
        self.sleeps = []
    def monotonic(self): return self.now
    def time(self): return time.time()
    def strftime(self, fmt): return time.strftime(fmt)
    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class MemorySocket:
    def __init__(self, raw):
        self.raw = raw
        self.request = b""
        self.closed = False
    def makefile(self, mode): return io.BytesIO(self.raw)
    def sendall(self, request): self.request = request
    def close(self): self.closed = True


def response(body='var quickpayData = {"amount": 10.0};', code=200, headers=None, chunked=False):
    body = body.encode() if isinstance(body, str) else body
    headers = list(headers or [])
    if chunked:
        headers.append(("Transfer-Encoding", "chunked"))
        body = f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n"
    else:
        headers.append(("Content-Length", str(len(body))))
    raw = f"HTTP/1.1 {code} Test\r\n".encode()
    raw += "".join(f"{key}: {value}\r\n" for key, value in headers).encode()
    return MemorySocket(raw + b"\r\n" + body)


def service(index=0, kind="wallet"):
    return Customer(index + 2, kind, f"10000{index}", f"10000{index}", 1000, service_number=f"20000{index}")


def contract(index=0):
    return Customer(index + 2, "account", f"10000{index}", f"10000{index}", 1000)


def state(customers):
    root = Path(__file__).resolve().parent.parent
    actors = {name: WorkerActor(name, name, root / "UNUSED_TEST_PROFILE", root / "chrome_extension",
                               use_proxy=name == "worker_3", proxy_url="socks5://proxy.invalid:1080" if name == "worker_3" else None)
              for name in ("worker_1", "worker_2", "worker_3")}
    for actor in actors.values(): actor.status = "ready"
    obj = Orchestrator(SimpleNamespace(get_worker=actors.get), Mock(), root)
    obj.is_running = True
    obj.customers = customers
    obj.result_queue = queue.Queue()
    obj.checkpoint_manager = Mock()
    return obj, actors


class FakePage:
    def __init__(self, clock=None, entered=None, release=None):
        self.clock, self.entered, self.release = clock, entered, release
        self.url = ""
        self.closed = False
        self.closed_with_lock = False
    def goto(self, url, **kwargs):
        self.url = url
        if self.entered:
            self.entered.set()
            if not self.release.wait(2): raise TimeoutError("Test barrier timed out")
        if self.clock: self.clock.now += 5
        return SimpleNamespace(status=200, all_headers=lambda: {})
    def content(self): return 'var quickpayData = {"amount": 10.0};'
    def on(self, event, callback): pass
    def wait_for_function(self, expression, **kwargs): pass
    def wait_for_load_state(self, state, **kwargs): pass
    def is_closed(self): return self.closed
    def remove_listener(self, event, callback): pass
    def close(self):
        self.closed_with_lock = transport.get_ip_service_lock("local_direct").locked()
        self.closed = True


def context(page):
    return SimpleNamespace(new_page=lambda: page, pages=[], clear_cookies=lambda **kwargs: None,
                           add_cookies=lambda cookies: None, cookies=lambda: [])


class ServiceRegressions(unittest.TestCase):
    def setUp(self):
        with transport._IP_LOCKS_GUARD:
            transport._IP_SERVICE_LOCKS.clear()
            transport._IP_LAST_QUERY_TIME.clear()
            transport._IP_RETRY_UNTIL.clear()
            transport._IP_COOKIES.clear()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        # Accidental live network/browser use fails immediately.
        self.stack.enter_context(patch("socket.create_connection", side_effect=AssertionError("Live network forbidden")))
        self.stack.enter_context(patch("urllib.request.urlopen", side_effect=AssertionError("Live network forbidden")))
        self.stack.enter_context(patch.object(browser, "get_stealth_service_worker", side_effect=AssertionError("Live browser forbidden")))

    def test_services_never_reach_business_api_even_via_helper(self):
        with patch.object(browser, "query_service_stealth", return_value=("ok", 10.0, "mock")) as web:
            self.assertEqual(api.query_direct_business_api("20001")[0], "ok")
            self.assertEqual(api.query_contract_due_amount("٢٠٠٠٢")[0], "ok")
        self.assertEqual([call.args[0] for call in web.call_args_list], ["20001", "20002"])

    def test_real_payment_structure_with_security_loader_is_ok(self):
        body = (Path(__file__).parent / "fixtures/quickpay_rendered_payment.html").read_text(encoding="utf-8")
        self.assertEqual(transport.classify_web_response(200, {}, body, "https://app.sa.zain.com/ar/quickpay?account=20001")[:2], ("ok", 286.26))

    def test_real_captcha_structure_is_not_a_ban(self):
        body = (Path(__file__).parent / "fixtures/quickpay_human_verification.html").read_text(encoding="utf-8")
        self.assertEqual(transport.classify_web_response(200, {}, body, "https://app.sa.zain.com/ar/quickpay?account=20001")[:2], ("verification_required", None))

    def test_session_support_id_does_not_override_session_marker(self):
        body = "<script>window.bobcmn = 'session';</script><p>Your support ID is: 123456</p>"
        self.assertEqual(transport.classify_web_response(200, {}, body, "https://app.sa.zain.com/ar/quickpay?account=20001")[0], "session_expired")

    def test_rejection_strings_in_scripts_do_not_override_amount(self):
        body = '<script>const message = "Request Rejected Support ID not found";</script><script>var quickpayData = {"amount": 0};</script>'
        self.assertEqual(transport.classify_web_response(200, {}, body, "https://app.sa.zain.com/ar/quickpay?account=20001")[:2], ("ok", 0.0))

    def test_human_verification_pauses_and_preserves_row_without_retries(self):
        obj, actors = state([service(), service(1)])
        task = obj.lease_next_task_for_worker("worker_3")
        obj.handle_verification_required(task["task_id"], "human check", "worker_1")
        self.assertFalse(obj.is_paused)  # Wrong owner cannot pause the session.
        obj.handle_verification_required(task["task_id"], "human check", "worker_3")
        self.assertTrue(obj.is_paused)
        self.assertEqual(obj.verification_notice, "human check")
        self.assertEqual(obj.deferred_indices, [0])
        self.assertIsNone(obj.lease_next_task_for_worker("worker_1"))
        self.assertFalse(actors["worker_3"].is_in_cooldown())
        self.assertEqual(obj.reviews_count, 0)
        self.assertEqual(obj.errors_count, 0)
        self.assertFalse(obj.completed_indices)
        self.assertEqual(obj.retry_attempts_by_status[0], {"verification_required": 1})

    def test_successful_verification_resumes_without_manual_resume_click(self):
        obj, _ = state([service(), service(1)])
        task = obj.lease_next_task_for_worker("worker_1")
        obj.notify_pending_verification(task["task_id"], "human check", "worker_1")
        self.assertTrue(obj.is_paused)
        obj.record_task_outcome(task["task_id"], 10.0, "match", worker_id="worker_1")
        self.assertFalse(obj.is_paused)
        self.assertEqual(obj.verification_notice, "")
        self.assertFalse(obj.verification_pause_tasks)
        self.assertIsNotNone(obj.lease_next_task_for_worker("worker_2"))

    def test_successful_verification_respects_explicit_manual_pause(self):
        obj, _ = state([service()])
        task = obj.lease_next_task_for_worker("worker_1")
        obj.notify_pending_verification(task["task_id"], "human check", "worker_1")
        obj.pause_session()
        obj.record_task_outcome(task["task_id"], 10.0, "match", worker_id="worker_1")
        self.assertTrue(obj.is_paused)

    def test_two_routes_resume_only_after_both_verifications_succeed(self):
        obj, _ = state([service(), service(1), service(2)])
        first = obj.lease_next_task_for_worker("worker_1")
        second = obj.lease_next_task_for_worker("worker_3")
        obj.notify_pending_verification(first["task_id"], "direct check", "worker_1")
        obj.notify_pending_verification(second["task_id"], "proxy check", "worker_3")
        obj.record_task_outcome(first["task_id"], 10.0, "match", worker_id="worker_1")
        self.assertTrue(obj.is_paused)
        obj.record_task_outcome(second["task_id"], 10.0, "match", worker_id="worker_3")
        self.assertFalse(obj.is_paused)

    def test_expired_verification_cannot_leave_a_stale_resume_guard(self):
        obj, _ = state([service()])
        task = obj.lease_next_task_for_worker("worker_1")
        obj.notify_pending_verification(task["task_id"], "human check", "worker_1")
        obj.handle_verification_required(task["task_id"], "expired", "worker_1")
        self.assertTrue(obj.is_paused)
        self.assertFalse(obj.verification_pause_tasks)
        obj.resume_session()
        self.assertFalse(obj.is_paused)

    def test_browser_proxy_has_own_lock_and_never_uses_direct_backoff(self):
        clock = Clock()
        proxy = "socks5://proxy.invalid:1080"
        worker = browser.StealthServiceWorker(proxy_url=proxy)
        page = FakePage()
        page.content = lambda: "Request Rejected"
        closed_proxy_lock = []
        page.close = lambda: closed_proxy_lock.append(transport.get_ip_service_lock(transport.connection_group(proxy)).locked())
        with patch.object(browser, "time", clock), patch.object(transport, "time", clock):
            result = worker._do_query(context(page), browser.BrowserJob("20001", 15, clock.now + 20))
        self.assertEqual(result[0], "blocked")
        self.assertEqual(closed_proxy_lock, [True])
        self.assertEqual(transport.get_ip_service_retry_after(transport.connection_group(proxy)), 0)
        self.assertNotIn("local_direct", transport._IP_LAST_QUERY_TIME)

    def test_service_metadata_overrides_inconsistent_record_type(self):
        obj, _ = state([service(kind="account")])
        task = obj.lease_next_task_for_worker("worker_1")
        self.assertEqual(task["search_number"], "200000")
        self.assertEqual(task["record_type"], "wallet")
        self.assertEqual(task["target_url"], "https://app.sa.zain.com/ar/quickpay?account=200000")

    def test_normal_and_session_retry_pacing(self):
        clock = Clock()
        first = response("<script src='/TSPD/test.js'></script>", code=403,
                         headers=[("Set-Cookie", "TSold=value; Path=/")])
        second = response()
        with patch.object(transport, "time", clock), patch.object(transport, "_open_tls_socket", side_effect=[first, second]):
            self.assertEqual(transport.query_web_number("20001")[0], "session_expired")
            self.assertEqual(transport.query_web_number("20001")[0], "ok")
        self.assertGreaterEqual(sum(clock.sleeps) + 1e-9, 3.9)
        self.assertNotIn(b"Cookie:", second.request)
        self.assertEqual(transport.get_ip_service_retry_after("local_direct"), 0)

    def test_contracts_do_not_enter_service_pacing(self):
        clock = Clock()
        transport._IP_LAST_QUERY_TIME["local_direct"] = clock.now
        with patch.object(transport, "time", clock), patch.object(api, "query_direct_business_api", return_value=("ok", 10.0, "mock")):
            api.query_contract_due_amount("10001")
        self.assertEqual(clock.sleeps, [])
        key = transport.connection_group("socks5://proxy.invalid:1080")
        transport._IP_LAST_QUERY_TIME[key] = clock.now
        with patch.object(transport, "time", clock), patch.object(transport, "_open_tls_socket", return_value=response()):
            self.assertEqual(api.query_contract_due_amount("10001", "socks5://proxy.invalid:1080")[0], "ok")
        self.assertEqual(clock.sleeps, [])

    def test_proxy_failure_never_uses_direct_browser_or_business_api(self):
        with patch.object(api, "query_app_zain_web_engine", return_value=("network_error", None, "mock")), \
             patch.object(api, "query_direct_business_api", side_effect=AssertionError("Direct API forbidden")), \
             patch.object(browser, "query_service_stealth", return_value=("network_error", None, "mock")) as query:
            for number in ("10001", "20001"):
                self.assertEqual(api.query_contract_due_amount(number, "socks5://proxy.invalid:1080")[0], "network_error")
            query.assert_called_once_with("20001", timeout_seconds=45.0, proxy_url="socks5://proxy.invalid:1080")

    def test_actual_same_route_requests_are_mutually_exclusive(self):
        barrier = threading.Barrier(3)
        guard = threading.Lock()
        active = 0
        maximum = 0
        results = []
        def query(*args):
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.01)
            with guard: active -= 1
            return "ok", 10.0, "mock"
        def run(number):
            barrier.wait()
            results.append(transport.query_web_number(number))
        with patch.object(transport, "SERVICE_INTERVAL", 0.01), patch.object(transport, "_query_web_once", side_effect=query):
            threads = [threading.Thread(target=run, args=(str(20001 + i),)) for i in range(2)]
            for thread in threads: thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(2)
                self.assertFalse(thread.is_alive())
        self.assertEqual(maximum, 1)
        self.assertEqual(len(results), 2)

    def test_parallel_scheduler_excludes_shared_ip_but_allows_proxy(self):
        obj, _ = state([service(0), service(1), service(2)])
        barrier = threading.Barrier(3)
        results = []
        def lease(worker):
            barrier.wait()
            results.append(obj.lease_next_task_for_worker(worker))
        threads = [threading.Thread(target=lease, args=(worker,)) for worker in ("worker_1", "worker_2")]
        for thread in threads: thread.start()
        barrier.wait()
        for thread in threads: thread.join(2)
        self.assertEqual(sum(task is not None for task in results), 1)
        self.assertIsNotNone(obj.lease_next_task_for_worker("worker_3"))

    def test_cookie_expiry_domain_and_path(self):
        sockets = [response(headers=[("Set-Cookie", "TSdemo=value; Path=/"),
                                     ("Set-Cookie", "private=value; Path=/unrelated"),
                                     ("Set-Cookie", "foreign=value; Domain=example.com; Path=/")]),
                   response(headers=[("Set-Cookie", "TSdemo=removed; Max-Age=0; Path=/")]), response()]
        with patch.object(transport, "time", Clock()), patch.object(transport, "_open_tls_socket", side_effect=sockets):
            for number in ("20001", "20002", "20003"):
                self.assertEqual(transport.query_web_number(number)[0], "ok")
        self.assertIn(b"Cookie: TSdemo=value", sockets[1].request)
        self.assertNotIn(b"private=value", sockets[1].request)
        self.assertNotIn(b"foreign=value", sockets[1].request)
        self.assertNotIn(b"Cookie:", sockets[2].request)
        self.assertTrue(all(sock.closed for sock in sockets))

    def test_chunked_and_compressed_http_are_decoded(self):
        sockets = [response(chunked=True), response(gzip.compress(b'var quickpayData = {"amount": 10};'), headers=[("Content-Encoding", "gzip")])]
        with patch.object(transport, "time", Clock()), patch.object(transport, "_open_tls_socket", side_effect=sockets):
            self.assertEqual(transport.query_web_number("20001")[:2], ("ok", 10.0))
            self.assertEqual(transport.query_web_number("20002")[:2], ("ok", 10.0))

    def test_concurrent_cookie_responses_merge_without_lost_updates(self):
        barrier = threading.Barrier(2)
        results = []
        def open_socket(*args):
            name = threading.current_thread().name
            sock = response(headers=[("Set-Cookie", f"{name}=value; Path=/")])
            barrier.wait(timeout=2)
            return sock
        def run(number): results.append(transport.query_web_number(number))
        with patch.object(transport, "_open_tls_socket", side_effect=open_socket):
            threads = [threading.Thread(target=run, args=(str(10001 + i),), name=f"TS{i}") for i in range(2)]
            for thread in threads: thread.start()
            for thread in threads:
                thread.join(2)
                self.assertFalse(thread.is_alive())
        self.assertEqual([result[0] for result in results], ["ok", "ok"])
        self.assertEqual({cookie.name for cookie in transport._IP_COOKIES["local_direct"]}, {"TS0", "TS1"})

    def test_truncated_response_is_not_a_zero_balance(self):
        sock = MemorySocket(b'HTTP/1.1 200 OK\r\nContent-Length: 999\r\n\r\nvar quickpayData = {"amount":0};')
        with patch.object(transport, "_open_tls_socket", return_value=sock):
            self.assertEqual(transport.query_web_number("20001")[0], "network_error")
        self.assertTrue(sock.closed)

    def test_missing_invalid_or_incidental_amount_is_unknown(self):
        for body in ('var quickpayData = {};', 'var quickpayData = {"amount": null};',
                     'var quickpayData = {"amount": -1};', 'var quickpayData = {"amount": "NaN"};',
                     '<div>Footer 0.00</div>'):
            with self.subTest(body=body):
                self.assertEqual(transport.classify_web_response(200, {}, body, "https://app.sa.zain.com/ar/quickpay?account=20001")[:2], ("unknown_response", None))
        self.assertEqual(transport.classify_web_response(200, {}, 'var quickpayData = {"nested": {}, "amount": 0};', "https://app.sa.zain.com/ar/quickpay?account=20001")[:2], ("ok", 0.0))

    def test_session_case_redirect_location_and_rejection_priority(self):
        url = "https://app.sa.zain.com/ar/quickpay?account=20001"
        self.assertEqual(transport.classify_web_response(403, {}, "BOBCMN", url)[0], "session_expired")
        self.assertEqual(transport.classify_web_response(403, {}, "Request Rejected /TSPD/", url)[0], "blocked")
        self.assertEqual(transport.classify_web_response(429, {}, "/TSPD/", url)[0], "blocked")
        self.assertEqual(transport.classify_web_response(302, {"Location": "/TSPD/start"}, "", url)[0], "session_expired")
        self.assertEqual(transport.classify_web_response(302, {"Location": "/ar/home"}, "", url)[0], "not_found")
        for headers in ({}, {"Location": "https://example.com/ar/home"}, {"Location": "/ar/homepage"}):
            self.assertEqual(transport.classify_web_response(302, headers, "", url)[0], "unknown_response")

    def test_retry_after_keeps_proxy_actor_available(self):
        proxy = "socks5://proxy.invalid:1080"
        clock = Clock()
        with patch.object(transport, "time", clock), patch.object(transport, "_open_tls_socket", return_value=response("Too many requests", 429, [("Retry-After", "30")])):
            self.assertEqual(transport.query_web_number("20001", proxy)[0], "blocked")
            self.assertEqual(transport.get_ip_service_retry_after(transport.connection_group(proxy)), 30)
        _, actors = state([])
        actors["worker_3"].set_cooldown(100)
        self.assertFalse(actors["worker_3"].is_in_cooldown())
        self.assertNotEqual(actors["worker_3"].ip_group, actors["worker_1"].ip_group)
        for invalid in ("NaN", "Infinity", "not-a-date"):
            self.assertEqual(transport._retry_after_seconds({"Retry-After": invalid}), 0.0)

    def test_shared_direct_backoff_still_allows_contracts(self):
        obj, _ = state([service(), contract(1)])
        with patch.object(transport, "time", Clock()):
            transport.set_ip_service_cooldown("local_direct", 12)
            task = obj.lease_next_task_for_worker("worker_2")
            self.assertEqual(task["index"], 1)
            self.assertIsNone(obj.lease_next_task_for_worker("worker_1"))
            self.assertIsNotNone(obj.lease_next_task_for_worker("worker_3"))

    def test_expired_lease_unique_id_ownership_and_heartbeat(self):
        obj, _ = state([service()])
        clock = Clock()
        with patch.object(om, "time", clock):
            old = obj.lease_next_task_for_worker("worker_1")
            clock.now += 100
            self.assertTrue(obj.renew_task_lease(old["task_id"], "worker_1"))
            clock.now += 30
            self.assertIsNone(obj.lease_next_task_for_worker("worker_2"))
            clock.now += 121
            new = obj.lease_next_task_for_worker("worker_2")
        self.assertNotEqual(old["task_id"], new["task_id"])
        obj.record_task_outcome(old["task_id"], 10, "match", worker_id="worker_1")
        obj.record_task_outcome(new["task_id"], 10, "match", worker_id="worker_1")
        self.assertEqual(obj.completed_indices, set())
        obj.record_task_outcome(new["task_id"], 10, "match", worker_id="worker_2")
        self.assertEqual(obj.matches_count, 1)

    def test_service_lease_does_not_expire_during_network_slot(self):
        obj, _ = state([service()])
        task = obj.lease_next_task_for_worker("worker_1")
        obj.active_leases[task["task_id"]]["leased_at"] -= 121
        with transport.get_ip_service_lock("local_direct"):
            self.assertIsNone(obj.lease_next_task_for_worker("worker_2"))
        self.assertIn(task["task_id"], obj.active_leases)

    def test_result_commit_never_exposes_unleased_uncompleted_row(self):
        obj, _ = state([service()])
        task = obj.lease_next_task_for_worker("worker_1")
        real_parse = om.parse_money_to_halalas
        overlapping = []
        def parse(value):
            overlapping.append(obj.lease_next_task_for_worker("worker_2"))
            return real_parse(value)
        with patch.object(om, "parse_money_to_halalas", side_effect=parse):
            obj.record_task_outcome(task["task_id"], 10, "match", worker_id="worker_1")
        self.assertEqual(overlapping, [None])
        self.assertFalse(obj.active_leases)
        obj.record_task_outcome(task["task_id"], 10, "match", worker_id="worker_1")
        self.assertEqual(obj.matches_count, 1)
        self.assertEqual(obj.result_queue.qsize(), 1)

    def test_index_zero_prevents_premature_deferred_retries(self):
        obj, _ = state([service(), contract(1), service(2)])
        obj.deferred_indices = [1]
        obj.active_leases["busy"] = {"index": 2, "customer": obj.customers[2], "worker_id": "worker_1",
                                     "leased_at": time.monotonic(), "ip_group": "local_direct"}
        self.assertIsNone(obj.lease_next_task_for_worker("worker_2"))

    def test_simultaneous_duplicate_results_commit_once(self):
        obj, _ = state([service()])
        task = obj.lease_next_task_for_worker("worker_1")
        barrier = threading.Barrier(8)
        def submit():
            barrier.wait(timeout=2)
            obj.record_task_outcome(task["task_id"], 10, "match", worker_id="worker_1")
        threads = [threading.Thread(target=submit) for _ in range(8)]
        for thread in threads: thread.start()
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(obj.matches_count, 1)
        self.assertEqual(obj.result_queue.qsize(), 1)
        self.assertEqual(obj.completed_indices, {0})

    def test_third_block_becomes_review_not_error_and_counters_are_separate(self):
        obj, actors = state([service()])
        clock = Clock()
        with patch.object(om, "time", clock):
            for category in ("session_expired", "blocked", "blocked", "blocked"):
                task = obj.lease_next_task_for_worker("worker_3")
                self.assertIsNotNone(task)
                obj.defer_task_to_end(task["task_id"], "mock", worker_id="worker_3", category=category)
                clock.now += 4
        self.assertEqual(obj.errors_count, 0)
        self.assertEqual(obj.reviews_count, 1)
        self.assertEqual(obj.recent_results[0]["status"], "needs_review")
        self.assertIsNone(obj.recent_results[0]["live_amount"])
        self.assertFalse(actors["worker_3"].is_in_cooldown())
        self.assertFalse(obj.deferred_indices)
        saved = obj.checkpoint_manager.save.call_args.kwargs
        self.assertEqual(saved["extra"]["reviews_count"], 1)

    def test_home_not_found_and_invalid_amount_never_mark_paid(self):
        for status, amount in (("not_found", None), ("match", None), ("match", "NaN"), ("match", float("inf"))):
            with self.subTest(status=status, amount=amount):
                obj, _ = state([service()])
                task = obj.lease_next_task_for_worker("worker_1")
                obj.record_task_outcome(task["task_id"], amount, status, worker_id="worker_1")
                self.assertEqual(obj.errors_count, 0)
                self.assertEqual(obj.matches_count, 0)
                self.assertEqual(len(obj.mismatches), 0)
                self.assertIsNone(obj.all_completed_records[0]["live_sar"])
                self.assertNotEqual(obj.all_completed_records[0]["status_label"], "مسدد بالكامل")

    def test_missing_proxy_fails_closed(self):
        _, actors = state([])
        for proxy in (None, "", "invalid", "socks5://proxy.invalid:bad"):
            with self.subTest(proxy=proxy):
                actor = actors["worker_3"]
                actor.proxy_url = proxy
                with patch.object(api, "query_contract_due_amount", side_effect=AssertionError("Direct fallback forbidden")):
                    self.assertEqual(actor.execute_task_api("20001")[0], "error")

    def test_browser_fallback_obeys_pacing_and_closes_inside_lock(self):
        clock = Clock()
        page = FakePage(clock)
        worker = browser.StealthServiceWorker()
        job = browser.BrowserJob("20001", 15, clock.now + 30)
        def fallback(*args, **kwargs): return worker._do_query(context(page), job)
        sock = response()
        with patch.object(transport, "time", clock), patch.object(browser, "time", clock), \
             patch.object(transport, "_open_tls_socket", side_effect=[OSError("mock"), sock]), \
             patch.object(browser, "query_service_stealth", side_effect=fallback):
            self.assertEqual(api.query_contract_due_amount("20001")[0], "ok")
            browser_end = clock.now
            self.assertEqual(api.query_contract_due_amount("20002")[0], "ok")
            self.assertGreaterEqual(clock.now - browser_end + 1e-9, 3.9)
        self.assertFalse(page.closed)

    def test_cancelled_browser_keeps_service_lock_until_closed(self):
        entered, release = threading.Event(), threading.Event()
        page = FakePage(entered=entered, release=release)
        worker = browser.StealthServiceWorker()
        job = browser.BrowserJob("20001", 15, time.monotonic() + 30)
        thread = threading.Thread(target=worker._do_query, args=(context(page), job))
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            job.cancelled.set()
            self.assertTrue(transport.get_ip_service_lock("local_direct").locked())
            obj, _ = state([service(1)])
            self.assertIsNone(obj.lease_next_task_for_worker("worker_2"))
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(page.closed_with_lock)
        self.assertFalse(transport.get_ip_service_lock("local_direct").locked())

    def test_browser_caller_timeout_cancels_job(self):
        job = browser.BrowserJob("20001", 15, time.monotonic() - 1)
        job.done = Mock(wait=Mock(return_value=False))
        ready = threading.Event()
        ready.set()
        worker = SimpleNamespace(is_ready_event=ready, startup_error="", is_alive=lambda: True, work_queue=queue.Queue())
        with patch.object(browser, "get_stealth_service_worker", return_value=worker), patch.object(browser, "BrowserJob", return_value=job):
            self.assertEqual(browser.query_service_stealth("20001")[0], "network_error")
        self.assertTrue(job.cancelled.is_set())

    def test_manual_verification_keeps_same_page_and_lock_until_submission(self):
        captcha = (Path(__file__).parent / "fixtures/quickpay_human_verification.html").read_text(encoding="utf-8")
        page = FakePage()
        phase = {"verified": False}
        page.content = lambda: ('var quickpayData = {"amount": 286.26};' if phase["verified"] else captcha)
        page.screenshot = lambda **kwargs: b"MOCK_PNG"
        filled = []
        page.locator = lambda selector: SimpleNamespace(fill=lambda text: filled.append(text),
                            click=lambda **kwargs: phase.update(verified=True))
        worker = browser.StealthServiceWorker(headless=True)
        started = threading.Event()
        job = browser.BrowserJob("20001", 15, time.monotonic() + 20)
        job.on_verification = lambda message: started.set()
        results = []
        with patch.object(browser, "_GLOBAL_STEALTH_WORKER", worker), patch.object(browser, "_PROXY_BROWSER_WORKERS", {}):
            thread = threading.Thread(target=lambda: results.append(worker._do_query(context(page), job)))
            thread.start()
            try:
                self.assertTrue(started.wait(1))
                self.assertTrue(transport.get_ip_service_lock("local_direct").locked())
                pending = browser.pending_verifications()
                self.assertEqual(len(pending), 1)
                self.assertEqual(browser.verification_image(pending[0]["id"]), b"MOCK_PNG")
                self.assertFalse(browser.submit_verification("wrong_id", "CODE"))
                self.assertTrue(browser.submit_verification(pending[0]["id"], "CODE"))
                self.assertFalse(browser.submit_verification(pending[0]["id"], "CODE"))
            finally:
                pending = worker.verification
                if not phase["verified"] and not (pending and pending["submitted"]):
                    job.cancelled.set()
                thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(browser.pending_verifications(), [])
        self.assertEqual(results[0][:2], ("ok", 286.26))
        self.assertEqual(filled, ["CODE"])
        self.assertFalse(page.closed)
        self.assertFalse(transport.get_ip_service_lock("local_direct").locked())

    def test_persistent_chrome_profiles_separate_direct_and_proxy(self):
        pw = SimpleNamespace(chromium=Mock())
        direct = browser.StealthServiceWorker()
        proxy = browser.StealthServiceWorker(proxy_url="http://operator:secret@proxy.invalid:8080")
        direct._launch_context(pw)
        direct._launch_context(pw)
        proxy._launch_context(pw)
        calls = pw.chromium.launch_persistent_context.call_args_list
        self.assertEqual(calls[0].args[0], calls[1].args[0])
        self.assertNotEqual(calls[0].args[0], calls[2].args[0])
        self.assertTrue(Path(calls[0].args[0]).is_absolute())
        self.assertIn(".zain-service-profiles", calls[0].args[0])
        self.assertNotIn("secret", calls[2].args[0])
        self.assertEqual({k: calls[0].kwargs[k] for k in ("channel", "headless", "locale")},
                         {"channel": "chrome", "headless": False, "locale": "ar-SA"})
        self.assertEqual(calls[2].kwargs["proxy"]["server"], "http://proxy.invalid:8080")
        pw.chromium.launch.assert_not_called()

    def test_persistent_profile_launch_failure_never_falls_back(self):
        pw = SimpleNamespace(chromium=Mock())
        pw.chromium.launch_persistent_context.side_effect = RuntimeError("Profile locked")
        worker = browser.StealthServiceWorker(proxy_url="socks5://proxy.invalid:1080")
        with self.assertRaises(RuntimeError):
            worker._launch_context(pw)
        self.assertEqual(pw.chromium.launch_persistent_context.call_count, 1)
        pw.chromium.launch.assert_not_called()

    def test_direct_only_mode_never_registers_proxy_worker(self):
        cfgs = [{"worker_id": "direct", "use_proxy": False},
                {"worker_id": "proxy", "proxy": "socks5://proxy.invalid:1080"}]
        supervisor = WorkerSupervisor(workers_config=cfgs, enable_proxy_workers=False)
        self.assertEqual([actor.worker_id for actor in supervisor.get_all_workers()], ["direct"])
        self.assertEqual(len(cfgs), 2)
        self.assertIsNone(supervisor.get_worker("proxy"))

    def test_success_keeps_existing_browser_cookies_and_storage(self):
        page = FakePage()
        ctx = context(page)
        ctx.clear_cookies = Mock()
        ctx.add_cookies = Mock()
        ctx.cookies = lambda: [{"name": "TSbrowser", "value": "retained", "domain": "app.sa.zain.com", "path": "/"}]
        transport.store_browser_cookies("local_direct", [{"name": "TSstale", "value": "stale", "domain": "app.sa.zain.com", "path": "/"}])
        result = browser.StealthServiceWorker()._do_query(ctx, browser.BrowserJob("20001", 15, time.monotonic() + 20))
        self.assertEqual(result[0], "ok")
        ctx.clear_cookies.assert_not_called()
        ctx.add_cookies.assert_not_called()
        self.assertEqual([item["name"] for item in transport.browser_cookies("local_direct")], ["TSbrowser"])

    def test_human_can_complete_verification_in_visible_browser(self):
        captcha = (Path(__file__).parent / "fixtures/quickpay_human_verification.html").read_text(encoding="utf-8")
        page = FakePage()
        phase = {"verified": False}
        page.content = lambda: ('var quickpayData = {"amount": 286.26};' if phase["verified"] else captcha)
        page.screenshot = lambda **kwargs: b"MOCK_PNG"
        page.bring_to_front = Mock()
        page.evaluate = lambda expression, number: phase["verified"]
        page.locator = Mock(side_effect=AssertionError("No automated CAPTCHA interaction expected"))
        worker = browser.StealthServiceWorker()
        job = browser.BrowserJob("20001", 15, time.monotonic() + 20)
        def human_action(message):
            self.assertTrue(transport.get_ip_service_lock("local_direct").locked())
            phase["verified"] = True
        job.on_verification = human_action
        self.assertEqual(worker._do_query(context(page), job)[:2], ("ok", 286.26))
        page.bring_to_front.assert_called_once()
        page.locator.assert_not_called()
        self.assertIsNone(worker.verification)
        self.assertFalse(page.closed)
        self.assertFalse(transport.get_ip_service_lock("local_direct").locked())

    def test_loading_timeout_never_parses_partial_amount_and_closes_with_lock(self):
        page = FakePage()
        page.content = Mock(return_value='var quickpayData = {"amount": 0};')
        page.wait_for_load_state = Mock(side_effect=TimeoutError("Loading incomplete"))
        worker = browser.StealthServiceWorker()
        with self.assertRaises(TimeoutError):
            worker._do_query(context(page), browser.BrowserJob("20001", 45, time.monotonic() + 50))
        page.content.assert_not_called()
        self.assertTrue(page.closed_with_lock)
        self.assertIsNone(worker.service_page)

    def test_amount_wait_timeout_never_parses_unready_bill(self):
        page = FakePage()
        page.content = Mock(return_value='var quickpayData = {"amount": 0};')
        page.wait_for_function = Mock(side_effect=TimeoutError("Bill not ready"))
        with self.assertRaises(TimeoutError):
            browser.StealthServiceWorker()._do_query(context(page), browser.BrowserJob("20001", 45, time.monotonic() + 50))
        page.content.assert_not_called()
        self.assertTrue(page.closed_with_lock)

    def test_one_tab_reused_after_loading_before_next_service(self):
        events = []
        page = FakePage()
        page.wait_for_load_state = lambda state, **kwargs: events.append("load")
        page.wait_for_function = lambda expression, **kwargs: events.append("bill_ready")
        page.content = lambda: events.append("read") or 'var quickpayData = {"amount": 10.0};'
        ctx = context(page)
        ctx.pages = [page]
        ctx.new_page = Mock(side_effect=AssertionError("Unexpected second tab"))
        clock = Clock()
        worker = browser.StealthServiceWorker()
        with patch.object(browser, "time", clock), patch.object(transport, "time", clock):
            for number in ("20001", "20002"):
                self.assertEqual(worker._do_query(ctx, browser.BrowserJob(number, 45, clock.now + 50))[0], "ok")
        self.assertEqual(events, ["load", "bill_ready", "read"] * 2)
        self.assertIs(worker.service_page, page)
        self.assertFalse(page.closed)
        self.assertGreaterEqual(sum(clock.sleeps) + 1e-9, 3.9)

    def test_two_direct_callers_share_one_browser_thread(self):
        ready = threading.Event()
        ready.set()
        fake = SimpleNamespace(is_alive=lambda: True, start=Mock(), is_ready_event=ready)
        with patch.object(browser, "get_stealth_service_worker", get_browser_worker), \
             patch.object(browser, "_GLOBAL_STEALTH_WORKER", None), \
             patch.object(browser, "StealthServiceWorker", return_value=fake) as factory:
            results = []
            threads = [threading.Thread(target=lambda: results.append(browser.get_stealth_service_worker())) for _ in range(2)]
            for thread in threads: thread.start()
            for thread in threads: thread.join(2)
            self.assertEqual(results, [fake, fake])
            factory.assert_called_once_with()
            fake.start.assert_called_once()

    def test_expired_verification_is_never_submitted(self):
        worker = browser.StealthServiceWorker()
        worker.verification = {"id": "expired", "deadline": time.monotonic() - 1,
                               "answers": queue.Queue(), "submitted": False, "route": "direct", "image": b"PNG"}
        with patch.object(browser, "_GLOBAL_STEALTH_WORKER", worker), patch.object(browser, "_PROXY_BROWSER_WORKERS", {}):
            self.assertEqual(browser.pending_verifications(), [])
            self.assertIsNone(browser.verification_image("expired"))
            self.assertFalse(browser.submit_verification("expired", "CODE"))
        self.assertTrue(worker.verification["answers"].empty())

    def test_cancelled_verification_is_never_submitted_or_displayed(self):
        worker = browser.StealthServiceWorker()
        cancelled = threading.Event()
        worker.verification = {"id": "cancelled", "deadline": time.monotonic() + 60,
                               "answers": queue.Queue(), "submitted": False, "route": "direct", "image": b"PNG", "cancel": cancelled}
        with patch.object(browser, "_GLOBAL_STEALTH_WORKER", worker), patch.object(browser, "_PROXY_BROWSER_WORKERS", {}):
            browser.cancel_pending_verifications()
            self.assertTrue(cancelled.is_set())
            self.assertEqual(browser.pending_verifications(), [])
            self.assertIsNone(browser.verification_image("cancelled"))
            self.assertFalse(browser.submit_verification("cancelled", "CODE"))
        self.assertTrue(worker.verification["answers"].empty())

    def test_uncertain_records_only_enter_report_notes(self):
        from zain_checker.executive_reporter import ExactTemplateReporter
        record = {"row": 2, "expected_sar": 100, "live_sar": None,
                  "status": "needs_review", "error": "mock rejection"}
        report = ExactTemplateReporter([record], Path("UNUSED_REPORT.xlsx"))
        self.assertEqual(report.tab4_notes[0][1], "mock rejection")
        self.assertFalse(report.tab1_net_diffs)
        self.assertFalse(report.tab3_all_diffs)

    def test_worker_loop_uses_service_and_records_not_found_separately(self):
        obj, actors = state([service(kind="account")])
        called = []
        def execute(number):
            called.append(number)
            obj.is_running = False
            return "not_found", None, "home redirect"
        actors["worker_1"].execute_task_api = execute
        with patch.object(om, "time", Clock()):
            obj._run_worker_api_loop("worker_1")
        self.assertEqual(called, ["200000"])
        self.assertEqual(obj.not_found_count, 1)
        self.assertEqual(obj.errors_count, 0)
        self.assertEqual(obj.recent_results[0]["status"], "not_found")

    def test_bridge_preserves_explicit_zero_and_defers_rejections(self):
        from server.extension_bridge import ExtensionBridgeHandler
        obj, _ = state([service()])
        task = obj.lease_next_task_for_worker("worker_1")
        handler = ExtensionBridgeHandler.__new__(ExtensionBridgeHandler)
        handler.orchestrator = obj
        handler.send_cors_json = Mock()
        handler.handle_task_result({"task_id": task["task_id"], "status": "match", "live_amount": 0, "amount": 99}, "worker_1")
        self.assertEqual(obj.all_completed_records[0]["live_sar"], 0.0)
        obj, _ = state([service()])
        task = obj.lease_next_task_for_worker("worker_1")
        handler.orchestrator = obj
        handler.handle_task_result({"task_id": task["task_id"], "status": "blocked"}, "worker_1")
        self.assertEqual(obj.deferred_indices, [0])
        self.assertEqual(obj.errors_count, 0)
        self.assertFalse(obj.completed_indices)

    def test_browser_cookie_snapshot_propagates_deletion_and_parent_domain(self):
        transport.store_browser_cookies("local_direct", [{"name": "TSold", "value": "value", "domain": "app.sa.zain.com", "path": "/"}])
        transport.store_browser_cookies("local_direct", [{"name": "TSnew", "value": "value", "domain": ".zain.com", "path": "/", "httpOnly": True}], replace=True)
        cookies = transport.browser_cookies("local_direct")
        self.assertEqual([cookie["name"] for cookie in cookies], ["TSnew"])
        self.assertTrue(cookies[0]["httpOnly"])

    def test_worker_loop_challenge_has_no_route_or_actor_cooldown(self):
        obj, actors = state([service()])
        def execute(number):
            obj.is_running = False
            return "session_expired", None, "mock challenge"
        actors["worker_1"].execute_task_api = execute
        clock = Clock()
        with patch.object(om, "time", clock), patch.object(transport, "time", clock):
            obj._run_worker_api_loop("worker_1")
            self.assertFalse(actors["worker_1"].is_in_cooldown())
            self.assertEqual(transport.get_ip_service_retry_after("local_direct"), 0)
        self.assertEqual(obj.deferred_indices, [0])
        self.assertIsNone(actors["worker_1"].current_task)

    def test_proxy_worker_loop_network_failures_do_not_cool_actor(self):
        obj, actors = state([service()])
        calls = []
        def execute(number):
            calls.append(number)
            if len(calls) == 3:
                obj.is_running = False
            return "network_error", None, "mock failure"
        actors["worker_3"].execute_task_api = execute
        with patch.object(om, "time", Clock()):
            obj._run_worker_api_loop("worker_3")
        self.assertEqual(len(calls), 3)
        self.assertEqual(obj.reviews_count, 1)
        self.assertEqual(obj.errors_count, 0)
        self.assertFalse(actors["worker_3"].is_in_cooldown())


if __name__ == "__main__":
    unittest.main()
