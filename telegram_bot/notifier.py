"""Telegram Event Subscriber and Notifier.
Listens to domain events and dispatches updates and Excel reports to Telegram.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Optional

from manager.event_bus import EVENT_BUS
from telegram_bot.config import settings
from telegram_bot.notification_delivery import NotificationDelivery

logger = logging.getLogger("TelegramNotifier")


class TelegramNotifier(NotificationDelivery):
    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[int] = None,
    ) -> None:
        cfg = settings()
        self.bot_token = bot_token if bot_token is not None else cfg['bot_token']
        self.chat_id = chat_id if chat_id is not None else cfg['chat_id']
        self.api_url = f"https://api.telegram.org/bot{self.bot_token}"
        self._listening = False
        self._init_delivery()

    @property
    def admin_targets(self) -> list[int]:
        cfg = settings()
        targets = list(cfg.get("admin_targets", []))
        if self.chat_id and self.chat_id not in cfg.get('revoked_admin_ids',[]) and self.chat_id not in targets:
            targets.insert(0, self.chat_id)
        return targets

    def start_listening(self) -> None:
        with self._delivery_lock:
            if self._listening:
                return
            self._delivery_disabled.clear()
            for event, callback in self._subscriptions():
                EVENT_BUS.subscribe(event, callback)
            self._listening = True

    def _subscriptions(self):
        return (
            ("session_started", self._on_session_started),
            ("session_completed", self._on_session_completed),
            ("network_alert", self._on_network_alert),
        )

    def recover_commerce_deliveries(self):
        bridge = getattr(self,'commerce_bridge',None)
        if bridge is None: return
        try:
            for completion in bridge.pending_deliveries():
                self._on_session_completed('session_completed',{'commerce_order_id':completion['order_id']})
        except (ValueError,KeyError,OSError,TypeError,sqlite3.Error):
            self._last_error = 'commerce_delivery_recovery_requires_review'

    def stop_listening(self) -> None:
        with self._delivery_lock:
            self._delivery_disabled.set()
            self._generation += 1
            for event, callback in self._subscriptions():
                EVENT_BUS.unsubscribe(event, callback)
            self._listening = False

    def check_connection(self) -> dict:
        """Read bot identity without sending a message or exposing credentials."""
        try:
            with urllib.request.urlopen(f"{self.api_url}/getMe", timeout=12) as response:
                data = json.loads(response.read().decode("utf-8"))
            if data.get("ok") is not True:
                return {"connected": False, "message": "لم يؤكد تلقرام اتصال البوت"}
            return {
                "connected": True,
                "username": data.get("result", {}).get("username", ""),
                "message": "أكد تلقرام اتصال البوت بنجاح",
            }
        except Exception:
            return {"connected": False, "message": "تعذر الاتصال بتلقرام؛ راجع الشبكة وإعدادات البوت"}

    def send_message(
        self, text: str, parse_mode: Optional[str] = None, *, wait: bool = False, chat_id: Optional[int] = None
    ) -> bool:
        target = self.chat_id if chat_id is None else chat_id
        if not self.bot_token or not target or not isinstance(text, str) or not text or len(text) > 4096:
            return False
        data = {"chat_id": target, "text": text}
        if parse_mode:
            data['parse_mode'] = parse_mode
        request = urllib.request.Request(
            f"{self.api_url}/sendMessage",
            data=json.dumps(data).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
        )
        return self._submit_delivery(lambda: self._request_delivery(request, 10), wait)

    def send_excel_document(
        self,
        file_path: Path,
        caption: str = "",
        *,
        wait: bool = False,
        parse_mode: Optional[str] = None,
        chat_id: Optional[int] = None,
        on_receipt=None,
        expected_sha256: Optional[str] = None,
        claim_delivery=None,
        finish_delivery=None,
    ) -> bool:
        target = self.chat_id if chat_id is None else chat_id
        if not self.bot_token or not target or len(caption) > 1024 or not file_path.exists():
            return False

        def upload():
            file_bytes = file_path.read_bytes()
            if expected_sha256 is not None:
                import hashlib
                if hashlib.sha256(file_bytes).hexdigest() != expected_sha256:
                    self._last_error = 'result_changed_before_delivery'
                    return False
            if len(file_bytes) > 50 * 1024 * 1024:
                self._last_error = 'document_too_large'
                return False
            filename = file_path.name.replace('"', '_').replace('\r', '_').replace('\n', '_')
            boundary = '----ZainDocument' + uuid.uuid4().hex
            body = bytearray()

            def field(name, value):
                body.extend(
                    f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode(
                        'utf-8'
                    )
                )

            field('chat_id', str(target))
            field('caption', caption)
            if parse_mode:
                field('parse_mode', parse_mode)
            body.extend(
                f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{filename}"\r\nContent-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n'.encode(
                    'utf-8'
                )
            )
            body.extend(file_bytes)
            body.extend(f'\r\n--{boundary}--\r\n'.encode('utf-8'))
            request = urllib.request.Request(
                f'{self.api_url}/sendDocument',
                data=bytes(body),
                headers={'Content-Type': f'multipart/form-data; boundary={boundary}'},
            )
            if claim_delivery is not None:
                token = claim_delivery()
                if token is None:
                    self._last_error = 'commerce_delivery_already_claimed'
                    return False
                rejected = [False]
                try:
                    return self._request_delivery(request,40,on_receipt=on_receipt,
                        on_rejection=lambda: rejected.__setitem__(0,True))
                finally:
                    if finish_delivery is not None: finish_delivery(token,rejected[0])
            if on_receipt is not None:
                return self._request_delivery(request,40,on_receipt=on_receipt)
            return self._request_delivery(request, 40)

        return self._submit_delivery(upload, wait)

    def broadcast_message(
        self, text: str, parse_mode: Optional[str] = "HTML", *, wait: bool = False
    ) -> None:
        """Send a message to all admin groups, authorized admins, and the default chat_id."""
        for target in self.admin_targets:
            self.send_message(text, parse_mode=parse_mode, wait=wait, chat_id=target)

    def broadcast_excel_document(
        self,
        file_path: Path,
        caption: str = "",
        parse_mode: Optional[str] = "HTML",
        *,
        wait: bool = False,
    ) -> None:
        """Send an Excel document to all admin groups, authorized admins, and the default chat_id."""
        for target in self.admin_targets:
            self.send_excel_document(file_path, caption=caption, parse_mode=parse_mode, wait=wait, chat_id=target)

    def _on_session_started(self, event_type: str, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        restored=payload.get('previously_completed',0)
        if type(restored) is int and restored>0:
            return  # Loading saved progress must not resend the customer's start notice.
        from telegram_bot.views import safe, count

        wb = payload.get("workbook", "ملف إكسل")
        total = payload.get("total_records", 0)
        msg = (
            f"<b>تشيك. | بدأ الفحص</b>\n\n"
            f"📁 {safe(wb)}\n"
            f"السجلات المستهدفة: {count(total)}\n\n"
            "افتح حالة الفحص من قائمة البوت لمتابعة التقدم."
        )
        self.broadcast_message(msg, parse_mode="HTML")
        owner = payload.get('telegram_chat_id')
        if isinstance(owner, int) and owner > 0 and owner not in self.admin_targets:
            self.send_message(
                f'<b>بدأ فحص ملفك</b>\n{safe(wb)}\nالسجلات: {count(total)}\nسنرسل ملف النتائج عند اكتمال الجولة.',
                parse_mode='HTML',
                chat_id=owner,
            )

    def _on_session_completed(self, event_type: str, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        if payload.get('commerce_order_id'):
            bridge = getattr(self,'commerce_bridge',None)
            if bridge is None:
                self._last_error = 'commerce_bridge_unavailable'
                return
            try:
                completion = bridge.completion_for(payload['commerce_order_id'])
                if completion is None:
                    completion = bridge.capture_completion(payload)
                owner = completion['user_id']
                order = completion['order_id']
                if bridge.outbox.delivery_state(order) in {'started','ambiguous','confirmed'}:
                    return
                path = Path(completion['result_file'])
                stopped=any(row.get('state')=='cancelled' for row in completion.get('rows',[]))
                self.send_excel_document(path,
                    caption=('تقرير إيقاف طلبك والنتيجة المنجزة' if stopped else 'نتائج فحصك')+f"\nرقم الطلب: {completion['order_id']}",chat_id=owner,
                    expected_sha256=completion['sha256'],
                    on_receipt=lambda message: bridge.capture_delivery(completion,message),
                    claim_delivery=lambda: bridge.outbox.claim_delivery(order),
                    finish_delivery=lambda token,rejected: bridge.outbox.delivery_failed(order,token,rejected=rejected))
            except (ValueError,KeyError,OSError,TypeError,sqlite3.Error):
                self._last_error = 'commerce_delivery_requires_review'
            return
        from telegram_bot.views import safe, count

        wb = payload.get("workbook", "ملف إكسل")
        res_file_raw = payload.get("result_file")
        res_file = Path(res_file_raw) if res_file_raw else None
        admin_targets = self.admin_targets

        # Case 1: An intermediate file in a multi-file queue has completed
        if payload.get("round_pending"):
            notice = (
                f"<b>✓ تشيك. | اكتمل ملف</b>\n\n"
                f"📁 {safe(wb)}\n"
                f"السجلات: {count(payload.get('total', 0))}\n"
                f"مطابقات تامة: {count(payload.get('matches', 0))}\n"
                f"فروقات الرصيد: {count(payload.get('mismatches', 0))}\n"
                f"تحتاج مراجعة: {count(payload.get('needs_review', 0))}\n"
                f"غير موجودة: {count(payload.get('not_found', 0))}\n"
                f"مهلات وأخطاء: {count(payload.get('errors', 0))}\n\n"
                f"⏳ <i>ننتقل إلى الملف التالي في الطابور...</i>"
            )
            # 1. Send file completion message to all admin groups & admin targets
            self.broadcast_message(notice, parse_mode="HTML")

            # 2. Send the result file of this completed file to all admin groups & admin targets
            if res_file and res_file.exists():
                file_caption = f"<b>📄 نتائج فحص:</b> {safe(wb)}"
                self.broadcast_excel_document(res_file, caption=file_caption, parse_mode="HTML")

            # 3. If submitted by an external customer, also send them notice and result file
            owner = payload.get('telegram_chat_id')
            if isinstance(owner, int) and owner > 0 and owner not in admin_targets:
                self.send_message(
                    ('<b>تقرير إيقاف طلبك والنتيجة المنجزة</b>' if payload.get('customer_stop') else '<b>✓ اكتمل فحص ملفك</b>')+f"\n📁 {safe(wb)}\nالنتائج مرفقة أدناه.",
                    parse_mode="HTML",
                    chat_id=owner,
                )
                if res_file and res_file.exists():
                    self.send_excel_document(
                        res_file,
                        caption=('<b>تقرير إيقاف طلبك والنتيجة المنجزة</b>' if payload.get('customer_stop') else '<b>نتائج فحص ملفك</b>')+f"\n{safe(wb)}",
                        parse_mode="HTML",
                        chat_id=owner,
                    )
            return

        # Case 2: Round completed / single file fully completed
        total = payload.get("total", 0)
        matches = payload.get("matches", 0)
        mismatches = payload.get("mismatches", 0)
        errors = payload.get("errors", 0)
        res_file_default = res_file or Path("نتائج فحص زين.xlsx")

        caption = (
            f"<b>تشيك. | انتهت المعالجة</b>\n\n"
            f"📁 {safe(wb)}\n"
            f"السجلات: {count(total)}\n"
            f"مطابقات تامة: {count(matches)}\n"
            f"فروقات الرصيد: {count(mismatches)}\n"
            f"تحتاج مراجعة: {count(payload.get('needs_review', 0))}\n"
            f"غير موجودة: {count(payload.get('not_found', 0))}\n"
            f"مهلات وأخطاء: {count(errors)}\n\n"
            "<i>راجع حالات المراجعة والأخطاء في الملف المرفق.</i>"
        )

        # 1. Send final completion message to all admin groups & admin targets
        self.broadcast_message(caption, parse_mode="HTML")

        # 2. Send result files to all admin groups & admin targets
        raw_files = payload.get("round_result_files") or ([str(res_file)] if res_file and res_file.exists() else [str(res_file_default)])
        valid_files = [Path(f) for f in raw_files if Path(f).exists()]
        if not valid_files and res_file and res_file.exists():
            valid_files = [res_file]

        for index, file_path in enumerate(valid_files, 1):
            doc_caption = (
                f"<b>✓ اكتملت الجولة</b>\nنتائج الملف {index} من {len(valid_files)}\n{safe(file_path.name)}"
                if len(valid_files) > 1
                else f"<b>📄 نتائج فحص:</b> {safe(file_path.name)}"
            )
            self.broadcast_excel_document(file_path, caption=doc_caption, parse_mode="HTML")

        # 3. Customer submitters deliverables
        seen = set()
        for delivery in payload.get('round_deliveries', []):
            if not isinstance(delivery, dict):
                continue
            owner = delivery.get('chat_id')
            filename = delivery.get('file')
            if not isinstance(owner, int) or owner <= 0 or owner in admin_targets or not filename:
                continue
            fpath = Path(filename)
            if not fpath.exists():
                continue
            key = (owner, str(filename))
            if key in seen:
                continue
            seen.add(key)
            self.send_excel_document(
                fpath,
                caption=('<b>تقرير إيقاف طلبك والنتيجة المنجزة</b>'
                    if payload.get('customer_stop') and owner==payload.get('telegram_chat_id') and fpath==res_file
                    else '<b>نتائج فحص ملفك</b>')+f"\n{safe(fpath.name)}\nراجع الأرصدة وحالات المراجعة داخل الملف.",
                parse_mode="HTML",
                chat_id=owner,
            )

    def _on_network_alert(self, event_type: str, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        from telegram_bot.views import safe

        reason = payload.get("reason", "تنبيه شبكة")
        msg = f"<b>⚠ تنبيه اتصال</b>\n\n{safe(reason, 1000)}\n\nراجع حالة الجلسة من قائمة البوت."
        self.broadcast_message(msg, parse_mode="HTML")
