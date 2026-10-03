"""Telegram Event Subscriber and Notifier.
Listens to domain events and dispatches updates and Excel reports to Telegram.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Optional

from manager.event_bus import EVENT_BUS

logger = logging.getLogger("TelegramNotifier")

DEFAULT_BOT_TOKEN = "8613566630:AAFy7P3H7wiwpWzp2yiQ0KLtBtECtbmr7Gs"
DEFAULT_CHAT_ID = 1085138908


class TelegramNotifier:
    def __init__(
        self,
        bot_token: str = DEFAULT_BOT_TOKEN,
        chat_id: int = DEFAULT_CHAT_ID,
    ) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.api_url = f"https://api.telegram.org/bot{self.bot_token}"
        self._listening = False

    def start_listening(self) -> None:
        """Registers listener callbacks on the central EventBus."""
        if self._listening:
            return
        self._listening = True
        EVENT_BUS.subscribe("session_started", self._on_session_started)
        EVENT_BUS.subscribe("session_completed", self._on_session_completed)
        EVENT_BUS.subscribe("network_alert", self._on_network_alert)
        logger.info("TelegramNotifier subscribed to EventBus.")

    def stop_listening(self) -> None:
        for event, callback in (("session_started", self._on_session_started),
                                ("session_completed", self._on_session_completed),
                                ("network_alert", self._on_network_alert)):
            EVENT_BUS.unsubscribe(event, callback)
        self._listening = False

    def check_connection(self) -> dict:
        """Read bot identity without sending a message or exposing credentials."""
        try:
            with urllib.request.urlopen(f"{self.api_url}/getMe", timeout=12) as response:
                data = json.loads(response.read().decode("utf-8"))
            if data.get("ok") is not True:
                return {"connected": False, "message": "لم يؤكد تلقرام اتصال البوت"}
            return {"connected": True, "username": data.get("result", {}).get("username", ""),
                    "message": "أكد تلقرام اتصال البوت بنجاح"}
        except Exception:
            return {"connected": False, "message": "تعذر الاتصال بتلقرام؛ راجع الشبكة وإعدادات البوت"}

    def send_message(self, text: str, parse_mode: str = "Markdown", *, wait: bool = False) -> bool:
        """Sends a text message silently in background."""
        def _send():
            try:
                url = f"{self.api_url}/sendMessage"
                payload = json.dumps({
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                }).encode("utf-8")
                req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    return data.get("ok") is True
            except Exception as exc:
                logger.warning("Failed to send Telegram message: %s", type(exc).__name__)
                return False

        if wait:
            return _send()
        threading.Thread(target=_send, daemon=True).start()
        return True

    def send_excel_document(self, file_path: Path, caption: str = "", *, wait: bool = False, parse_mode: str = "Markdown") -> bool:
        """Dispatches an Excel workbook as a Telegram document."""
        if not file_path.exists():
            return False

        def _send_doc():
            try:
                file_bytes = file_path.read_bytes()
                file_name = file_path.name
                boundary = f"----WebKitFormBoundary{uuid.uuid4().hex}"
                body = bytearray()

                def add_field(name: str, value: str):
                    body.extend(f"--{boundary}\r\n".encode("utf-8"))
                    body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
                    body.extend(f"{value}\r\n".encode("utf-8"))

                add_field("chat_id", str(self.chat_id))
                add_field("caption", caption)
                add_field("parse_mode", parse_mode)

                body.extend(f"--{boundary}\r\n".encode("utf-8"))
                body.extend(
                    f'Content-Disposition: form-data; name="document"; filename="{file_name}"\r\n'.encode("utf-8")
                )
                body.extend(b"Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n")
                body.extend(file_bytes)
                body.extend(b"\r\n")
                body.extend(f"--{boundary}--\r\n".encode("utf-8"))

                req = urllib.request.Request(
                    f"{self.api_url}/sendDocument",
                    data=bytes(body),
                    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                )
                with urllib.request.urlopen(req, timeout=40) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    return data.get("ok") is True
            except Exception as exc:
                logger.error("Failed to send Excel document to Telegram: %s", type(exc).__name__)
                return False

        if wait:
            return _send_doc()
        threading.Thread(target=_send_doc, daemon=True).start()
        return True

    def _on_session_started(self, event_type: str, payload: Any) -> None:
        from telegram_bot.views import safe, count
        wb = payload.get("workbook", "ملف إكسل")
        total = payload.get("total_records", 0)
        self.send_message(
            f"<b>تشيك. | بدأ الفحص</b>\n\n"
            f"📁 {safe(wb)}\n"
            f"السجلات المستهدفة: {count(total)}\n\n"
            "افتح حالة الفحص من قائمة البوت لمتابعة التقدم.", parse_mode="HTML"
        )

    def _on_session_completed(self, event_type: str, payload: Any) -> None:
        from telegram_bot.views import safe, count
        wb = payload.get("workbook", "ملف إكسل")
        if payload.get("round_pending"):
            self.send_message(f"<b>✓ اكتمل ملف</b>\n{safe(wb)}\n\nننتقل إلى الملف التالي. النتائج تُرسل بعد اكتمال الجولة.", parse_mode="HTML")
            return
        total = payload.get("total", 0)
        matches = payload.get("matches", 0)
        mismatches = payload.get("mismatches", 0)
        errors = payload.get("errors", 0)
        res_file = Path(payload.get("result_file", "نتائج فحص زين.xlsx"))

        caption = (
            f"<b>تشيك. | انتهت المعالجة</b>\n\n"
            f"📁 {safe(wb)}\n"
            f"السجلات: {count(total)}\n"
            f"مطابقات تامة: {count(matches)}\n"
            f"فروقات الرصيد: {count(mismatches)}\n"
            f"تحتاج مراجعة: {count(payload.get('needs_review'))}\n"
            f"غير موجودة: {count(payload.get('not_found'))}\n"
            f"مهلات وأخطاء: {count(errors)}\n\n"
            "<i>راجع حالات المراجعة والأخطاء في الملف المرفق.</i>"
        )
        files = payload.get("round_result_files") or [str(res_file)]
        for index, filename in enumerate(files,1):
            final_caption = caption if len(files) == 1 else (
                f"<b>✓ اكتملت الجولة</b>\nنتائج الملف {index} من {len(files)}\n{safe(Path(filename).name)}")
            self.send_excel_document(Path(filename), caption=final_caption, parse_mode="HTML")

    def _on_network_alert(self, event_type: str, payload: Any) -> None:
        from telegram_bot.views import safe
        reason = payload.get("reason", "تنبيه شبكة")
        self.send_message(f"<b>⚠ تنبيه اتصال</b>\n\n{safe(reason, 1000)}\n\nراجع حالة الجلسة من قائمة البوت.", parse_mode="HTML")
