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

    def start_listening(self) -> None:
        """Registers listener callbacks on the central EventBus."""
        EVENT_BUS.subscribe("session_started", self._on_session_started)
        EVENT_BUS.subscribe("session_completed", self._on_session_completed)
        EVENT_BUS.subscribe("network_alert", self._on_network_alert)
        logger.info("TelegramNotifier subscribed to EventBus.")

    def send_message(self, text: str, parse_mode: str = "Markdown") -> bool:
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
                    pass
            except Exception as exc:
                logger.warning(f"Failed to send Telegram message: {exc}")

        threading.Thread(target=_send, daemon=True).start()
        return True

    def send_excel_document(self, file_path: Path, caption: str = "") -> bool:
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
                add_field("parse_mode", "Markdown")

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
                    pass
            except Exception as exc:
                logger.error(f"Failed to send Excel document to Telegram: {exc}")

        threading.Thread(target=_send_doc, daemon=True).start()
        return True

    def _on_session_started(self, event_type: str, payload: Any) -> None:
        wb = payload.get("workbook", "ملف إكسل")
        total = payload.get("total_records", 0)
        self.send_message(
            f"🚀 **بدء جلسة فحص جديدة عبر المنظومة**\n\n"
            f"• **الملف:** `{wb}`\n"
            f"• **إجمالي السجلات المستهدفة:** `{total:,}` سجل\n"
            f"• **وقت البدء:** {time.strftime('%I:%M:%S %p')}"
        )

    def _on_session_completed(self, event_type: str, payload: Any) -> None:
        wb = payload.get("workbook", "ملف إكسل")
        total = payload.get("total", 0)
        matches = payload.get("matches", 0)
        mismatches = payload.get("mismatches", 0)
        errors = payload.get("errors", 0)
        res_file = Path(payload.get("result_file", "نتائج فحص زين.xlsx"))

        caption = (
            f"✅ **اكتمل فحص الشيت بنجاح!**\n\n"
            f"• **الملف المفحوص:** `{wb}`\n"
            f"• **إجمالي السجلات:** `{total:,}`\n"
            f"• **المتطابق التام:** `{matches:,}` عميل ✔\n"
            f"• **فروقات المديونية:** `{mismatches:,}` عميل ⚠️\n"
            f"• **المهلات/الأخطاء:** `{errors:,}` ✖\n"
            f"• **وقت الاكتمال:** {time.strftime('%I:%M:%S %p')}"
        )
        self.send_excel_document(res_file, caption=caption)

    def _on_network_alert(self, event_type: str, payload: Any) -> None:
        reason = payload.get("reason", "تنبيه شبكة")
        self.send_message(f"⚠️ **تنبيه أمان شبكة من الراوتر / البروكسي**\n\n{reason}")
