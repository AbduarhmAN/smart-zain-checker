"""Telegram Interactive Bot Controller.
Handles remote commands, status requests, and automatic sheet uploads into Queue.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional

from manager.orchestrator import Orchestrator
from manager.queue import QueueService

logger = logging.getLogger("TelegramBot")

DEFAULT_BOT_TOKEN = "8613566630:AAFy7P3H7wiwpWzp2yiQ0KLtBtECtbmr7Gs"
AUTHORIZED_USER_ID = 1085138908


class TelegramBotRunner:
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

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logger.info("TelegramBotRunner polling loop started.")

    def stop(self) -> None:
        self._stop_event.set()

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
        last_update_id = 0
        while not self._stop_event.is_set():
            try:
                res = self._api_call("getUpdates", {"offset": last_update_id + 1, "timeout": 15}, timeout=20)
                if not res or not res.get("ok"):
                    time.sleep(2.0)
                    continue

                for update in res.get("result", []):
                    last_update_id = max(last_update_id, update.get("update_id", 0))
                    self._handle_update(update)
            except Exception as exc:
                time.sleep(3.0)

    def _handle_update(self, update: dict[str, Any]) -> None:
        msg = update.get("message")
        if not msg:
            return

        from_user = msg.get("from", {})
        sender_id = from_user.get("id")
        if sender_id != self.authorized_id:
            return  # Strict zero-trust authorization check

        chat_id = msg.get("chat", {}).get("id", self.authorized_id)
        text = str(msg.get("text", "")).strip()

        # 1. Handle Document Upload (.xlsx)
        doc = msg.get("document")
        if doc and str(doc.get("file_name", "")).endswith(".xlsx"):
            self._handle_document_upload(doc, chat_id)
            return

        # 2. Handle Text Commands
        if text.startswith("/start"):
            self._reply(chat_id, (
                "👋 **أهلاً بك في بوت إدارة فحص زين الذكي**\n\n"
                "• أرسل أي ملف إكسل `.xlsx` هنا لإضافته فوراً إلى طابور الفحص.\n"
                "• استخدم الأوامر:\n"
                "  - `/status` : عرض حالة الفحص الحية والعدادات.\n"
                "  - `/queue` : استعراض الشيتات في الطابور.\n"
                "  - `/pause` : إيقاف الفحص مؤقتاً.\n"
                "  - `/resume` : استئناف الفحص."
            ))
        elif text.startswith("/status"):
            st = self.orchestrator.get_live_status()
            k = st.get("kpis", {})
            state_str = "متوقف مؤقتاً ⏸️" if st.get("paused") else ("نشط ويعمل 🟢" if st.get("running") else "خامل ⚪")
            self._reply(chat_id, (
                f"📊 **حالة المنظومة الحالية: {state_str}**\n\n"
                f"• **الملف الحالي:** `{st.get('workbook') or 'لا يوجد'}`\n"
                f"• **المفحوص:** `{k.get('completed', 0):,}` من `{k.get('total', 0):,}`\n"
                f"• **المتطابق:** `{k.get('matches', 0):,}` ✔\n"
                f"• **الفروقات الصافية:** `{k.get('mismatches', 0):,}` ⚠️ (`{k.get('mismatch_total', 0):,.2f}` ر.س)\n"
                f"• **المهلات/الأخطاء:** `{k.get('errors', 0):,}` ✖"
            ))
        elif text.startswith("/queue"):
            jobs = self.queue_service.get_jobs()
            if not jobs:
                self._reply(chat_id, "📋 الطابور فارغ حالياً. يمكنك إرسال ملف إكسل لإضافته.")
            else:
                lines = ["📋 **طابور فحص الشيتات الحالي:**\n"]
                for i, j in enumerate(jobs, 1):
                    lines.append(f"{i}. `{j['filename']}` ({j['sheet_name']}) - [{j['status']}]")
                self._reply(chat_id, "\n".join(lines))
        elif text.startswith("/pause") or text.startswith("/stop"):
            self.orchestrator.pause_session()
            self._reply(chat_id, "⏸️ تم إيقاف جلسة الفحص مؤقتاً بنجاح.")
        elif text.startswith("/resume"):
            self.orchestrator.resume_session()
            self._reply(chat_id, "▶️ تم استئناف جلسة الفحص بنجاح.")

    def _handle_document_upload(self, doc: dict[str, Any], chat_id: int) -> None:
        file_id = doc.get("file_id")
        file_name = doc.get("file_name", f"uploaded_{int(time.time())}.xlsx")

        res = self._api_call("getFile", {"file_id": file_id})
        if not res or not res.get("ok"):
            self._reply(chat_id, f"⚠️ تعذر تنزيل الملف `{file_name}` من سيرفرات تلقرام.")
            return

        file_path_tg = res.get("result", {}).get("file_path")
        download_url = f"https://api.telegram.org/file/bot{self.bot_token}/{file_path_tg}"

        try:
            target_path = self.project_root / file_name
            with urllib.request.urlopen(download_url, timeout=30) as resp:
                target_path.write_bytes(resp.read())

            # Automatically inspect sheet and queue it
            from domain.workbook import inspect_sheet_schema
            from openpyxl import load_workbook
            wb = load_workbook(target_path, read_only=True, data_only=True)
            try:
                ws = wb.worksheets[0]
                schema = inspect_sheet_schema(ws)
                est_rows = schema.get("estimated_rows", 0)
                col_map = {
                    "lookup_col": schema.get("indices", {}).get("lookup_col", 12),
                    "amount_col": schema.get("indices", {}).get("remaining_col", 16),
                    "amount_col_2": schema.get("indices", {}).get("contract_col", 49),
                    "service_col": schema.get("indices", {}).get("service_col", 44),
                }
            finally:
                wb.close()

            job = self.queue_service.add_job(
                file_path=target_path,
                sheet_index=0,
                sheet_name="ورقة 1",
                mode="smart_hybrid",
                amount_target="contract",
                column_mapping=col_map,
                total_records=est_rows,
            )

            self._reply(chat_id, (
                f"✅ **تم استلام ملف الإكسل وإضافته للطابور بنجاح!**\n\n"
                f"• **اسم الملف:** `{file_name}`\n"
                f"• **عدد السجلات المقدر:** `{est_rows:,}` سجل\n"
                f"• **مبلغ الفحص الأساسي:** `العمود AW (مبلغ العقد)`\n"
                f"• **معرف المهمة في الطابور:** `{job.id}`"
            ))
        except Exception as exc:
            self._reply(chat_id, f"⚠️ خطأ أثناء حفظ الملف وإضافته للطابور: {exc}")

    def _reply(self, chat_id: int, text: str) -> None:
        self._api_call("sendMessage", {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
        })
