"""Bounded, ordered background delivery; Telegram acknowledgments stay explicit."""
from __future__ import annotations
import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.request

logger=logging.getLogger('TelegramNotifier')


class NotificationDelivery:
    def _init_delivery(self):
        self._delivery_lock=threading.RLock()
        self._send_lock=threading.Lock()
        self._delivery_queue=queue.Queue(maxsize=256)
        self._delivery_thread=None
        self._delivery_disabled=threading.Event()
        self._generation=0
        self._delivered=0
        self._failed=0
        self._in_flight=False
        self._last_error=None
        self._last_delivery_at=None

    def delivery_status(self):
        with self._delivery_lock:
            return {'pending':self._delivery_queue.qsize(),'sending':self._in_flight,
                    'delivered':self._delivered,'failed':self._failed,
                    'last_error':self._last_error,'last_delivery_at':self._last_delivery_at}

    def _submit_delivery(self, operation, wait):
        if self._delivery_disabled.is_set():return False
        if wait:return self._deliver(operation)
        with self._delivery_lock:
            try:self._delivery_queue.put_nowait((self._generation,operation))
            except queue.Full:
                self._failed+=1;self._last_error='notification_queue_full';return False
            if not self._delivery_thread or not self._delivery_thread.is_alive():
                self._delivery_thread=threading.Thread(target=self._delivery_loop,name='TelegramDelivery',daemon=True)
                self._delivery_thread.start()
        return True  # Accepted for background delivery, not a Telegram acknowledgment.

    def _delivery_loop(self):
        while True:
            generation,operation=self._delivery_queue.get()
            try:
                if generation==self._generation and not self._delivery_disabled.is_set():self._deliver(operation)
                else:
                    with self._delivery_lock:self._failed+=1;self._last_error='delivery_cancelled'
            finally:self._delivery_queue.task_done()

    def _deliver(self,operation):
        if not self._send_lock.acquire(timeout=10):
            with self._delivery_lock:self._failed+=1;self._last_error='delivery_busy'
            return False
        try:
            if self._delivery_disabled.is_set():return False
            with self._delivery_lock:self._in_flight=True
            accepted=bool(operation())
            with self._delivery_lock:
                if accepted:self._delivered+=1;self._last_delivery_at=time.time();self._last_error=None
                else:self._failed+=1;self._last_error=self._last_error or 'telegram_rejected_delivery'
            return accepted
        except Exception as error:
            with self._delivery_lock:self._failed+=1;self._last_error=type(error).__name__
            logger.warning('Telegram delivery failed (%s)',type(error).__name__)
            return False
        finally:
            with self._delivery_lock:self._in_flight=False
            self._send_lock.release()

    def _request_delivery(self, request, timeout, *, on_receipt=None, on_rejection=None):
        for attempt in range(3):
            if self._delivery_disabled.is_set():return False
            try:
                with urllib.request.urlopen(request,timeout=timeout) as response:
                    data=json.loads(response.read().decode('utf-8'))
            except urllib.error.HTTPError as error:
                try:data=json.loads(error.read().decode('utf-8'))
                except (ValueError,UnicodeError):data={'ok':False,'error_code':error.code}
            except Exception as error:
                self._last_error=type(error).__name__
                return False  # A timed-out send may already have been accepted: do not duplicate it.
            if not isinstance(data,dict):self._last_error='invalid_telegram_response';return False
            if data.get('ok') is True:
                if on_receipt is not None:
                    try:
                        on_receipt(data.get('result'))
                    except Exception:
                        self._last_error='receipt_persistence_failed'
                        return False  # Telegram may have delivered it; never resend blindly.
                return True
            try:code=int(data.get('error_code',0));delay=float(data.get('parameters',{}).get('retry_after',1))
            except (ValueError,TypeError,AttributeError):code=0;delay=0
            if code==429 and attempt<2 and 0<delay<=15:
                if self._delivery_disabled.wait(delay):return False
                continue
            self._last_error='telegram_rate_limit' if code==429 else 'telegram_rejected_delivery'
            if on_rejection is not None and 400<=code<500 and code!=408:
                on_rejection()
            return False
        return False
