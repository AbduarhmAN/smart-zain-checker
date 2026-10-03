"""Telegram Interactive Bot Controller.
Handles remote commands, status requests, and automatic sheet uploads into Queue.
"""
from __future__ import annotations

import json
import re
import uuid
import logging
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional

from manager.orchestrator import Orchestrator
from manager.queue import QueueService
from telegram_bot.ui import TelegramUiMixin
from telegram_bot.sheet_workflow import SheetWorkflowMixin

logger = logging.getLogger("TelegramBot")

DEFAULT_BOT_TOKEN = "8613566630:AAFy7P3H7wiwpWzp2yiQ0KLtBtECtbmr7Gs"
AUTHORIZED_USER_ID = 1085138908


class TelegramBotRunner(SheetWorkflowMixin, TelegramUiMixin):
    def __init__(
        self,
        orchestrator: Orchestrator,
        queue_service: QueueService,
        project_root: Path,
        bot_token: str = DEFAULT_BOT_TOKEN,
        authorized_id: int = AUTHORIZED_USER_ID,
    ) -> None:
        self.orchestrator = orchestrator
        self.queue_service = queue_service
        self.project_root = project_root
        self.bot_token = bot_token
        self.authorized_id = authorized_id
        self.api_url = f"https://api.telegram.org/bot{self.bot_token}"

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ui_lock = threading.RLock()
        self._confirmations = {}
        self._sheet_drafts = {}
        self._last_update_id = 0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logger.info("TelegramBotRunner polling loop started.")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=22)

    def _api_call(self, method: str, data: Optional[dict[str, Any]] = None, timeout: int = 25) -> Optional[dict[str, Any]]:
        try:
            url = f"{self.api_url}/{method}"
            if data is not None:
                payload = json.dumps(data).encode("utf-8")
                req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
            else:
                req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None

    def _poll_loop(self) -> None:
        self._api_call("setMyCommands", {"scope":{"type":"chat", "chat_id":self.authorized_id},
            "commands":[{"command":"start", "description":"القائمة الرئيسية"},
                        {"command":"status", "description":"متابعة تقدم الفحص"},
                        {"command":"queue", "description":"عرض طابور الملفات"},
                        {"command":"results", "description":"استلام نتائج الجلسة"},
                        {"command":"upload", "description":"إضافة ملف العملاء"},
                        {"command":"help", "description":"طريقة الاستخدام"}]})
        while not self._stop_event.is_set():
            try:
                res = self._api_call("getUpdates", {"offset": self._last_update_id + 1, "timeout": 15}, timeout=20)
                if self._stop_event.is_set():
                    break
                if not res or not res.get("ok"):
                    time.sleep(2.0)
                    continue

                for update in res.get("result", []):
                    self._last_update_id = max(self._last_update_id, update.get("update_id", 0))
                    self._handle_update(update)
            except Exception as exc:
                time.sleep(3.0)

    def _handle_update(self, update: dict[str, Any]) -> None:
        if update.get("callback_query"):
            self._handle_callback(update["callback_query"])
            return
        msg = update.get("message")
        if not msg:
            return

        from_user = msg.get("from", {})
        sender_id = from_user.get("id")
        if sender_id != self.authorized_id:
            return  # Strict zero-trust authorization check

        chat_id = msg.get("chat", {}).get("id", self.authorized_id)
        if chat_id != self.authorized_id:
            return
        text = str(msg.get("text", "")).strip()
        text = {
            "📊 حالة الفحص": "/status", "📁 طابور الملفات": "/queue",
            "⏸ إيقاف مؤقت": "/pause", "▶ استئناف الفحص": "/resume",
            "📎 إضافة ملف": "/upload", "💡 طريقة الاستخدام": "/help",
            "🏠 الرئيسية": "/start", "📥 نتائج الجلسة": "/results",
            "📥 نتائج الجولة": "/results",
        }.get(text, text)

        # 1. Handle Document Upload (.xlsx)
        doc = msg.get("document")
        if doc and str(doc.get("file_name", "")).lower().endswith(".xlsx"):
            self._handle_document_upload(doc, chat_id)
            return
        if doc:
            self._dispatch_ui("upload", chat_id)
            return

        command = text.split()[0].split("@")[0] if text else ""
        route = {"/start":"home", "/help":"help", "/upload":"upload", "/status":"status",
                 "/queue":"queue:0", "/pause":"pause", "/stop":"pause", "/resume":"resume", "/results":"results"}.get(command)
        if route:
            if route == "home":
                self._reply(chat_id, "أهلًا بك في تشيك. افتح لوحتك من الأزرار أدناه.")
            self._dispatch_ui(route, chat_id)
            return

        self._dispatch_ui("help", chat_id)

    def _handle_document_upload(self, doc: dict[str, Any], chat_id: int) -> None:
        file_id = doc.get("file_id")
        original_name = str(doc.get("file_name") or "ملف_عملاء.xlsx")
        file_name = re.sub(r'[\\/<>:"|?*\x00-\x1f]', '_', original_name).strip(' .')[:160]
        if not file_name.lower().endswith('.xlsx'):
            file_name = 'ملف_عملاء.xlsx'

        res = self._api_call("getFile", {"file_id": file_id})
        if not res or not res.get("ok"):
            self._reply(chat_id, f"⚠️ تعذر تنزيل الملف `{file_name}` من سيرفرات تلقرام.")
            return

        file_path_tg = res.get("result", {}).get("file_path")
        download_url = f"https://api.telegram.org/file/bot{self.bot_token}/{file_path_tg}"

        try:
            target_path = self.project_root / file_name
            if target_path.exists():
                target_path = target_path.with_name(f"{target_path.stem}_{uuid.uuid4().hex[:8]}.xlsx")
            with urllib.request.urlopen(download_url, timeout=30) as resp:
                file_bytes = resp.read()
            with target_path.open('xb') as target_file:
                target_file.write(file_bytes)

            self._open_sheet_review(target_path, chat_id)
        except Exception:
            self._reply(chat_id, "⚠ تعذر قراءة الملف أو إضافته إلى الطابور. تأكد من صيغة XLSX وراجع لوحة التحكم.")

    def _reply(self, chat_id: int, text: str) -> None:
        self._api_call("sendMessage", {
            "chat_id": chat_id,
            "text": text.replace("**", "").replace("`", ""),
            "reply_markup": {
                "keyboard": [["📊 حالة الفحص", "📁 طابور الملفات"],
                             ["🏠 الرئيسية", "📥 نتائج الجولة"],
                             ["📎 إضافة ملف", "💡 طريقة الاستخدام"]],
                "resize_keyboard": True, "is_persistent": True,
                "input_field_placeholder": "اختر إجراءً أو أرسل ملف XLSX",
            },
        })
