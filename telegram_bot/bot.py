"""Telegram Interactive Bot Controller.
Handles remote commands, status requests, and automatic sheet uploads into Queue.
"""
from __future__ import annotations

import json
import hashlib
import math
import os
import re
import uuid
import logging
import threading
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

import requests
from requests.adapters import HTTPAdapter

if TYPE_CHECKING:
    from manager.orchestrator import Orchestrator
    from manager.queue import QueueService
from telegram_bot.ui import TelegramUiMixin
from telegram_bot.subscription import SubscriptionMixin
from telegram_bot.subscription_policy import support_route
from telegram_bot.maintenance_access import MaintenanceAccessMixin,active as maintenance_active
from telegram_bot.sheet_workflow import SheetWorkflowMixin
from telegram_bot import views
from telegram_bot.commerce.bot_workflow import CommerceBotMixin
from telegram_bot.commerce.checkout import CommerceCheckoutMixin
from telegram_bot.commerce.cash_codes import CashCreditMixin
from telegram_bot.commerce.archive_ui import ArchiveUiMixin
from telegram_bot.commerce.support_ui import SupportUiMixin
from telegram_bot.commerce.pricing_ui import PricingUiMixin
from telegram_bot.commerce.partner_sheets import PartnerSheetMixin
from telegram_bot.commerce.partner_lifecycle import PartnerLifecycleMixin
from telegram_bot.commerce.binance_note_ui import BinanceNoteUiMixin
from telegram_bot.authorization import user_id as authorized_user_id
from telegram_bot.commerce.manual_payments import ManualPaymentsMixin
from telegram_bot.commerce.wallets import WalletPaymentsMixin
from telegram_bot.commerce.vodafone_cash import VodafoneCashMixin
from telegram_bot.commerce.partner_finance import PartnerFinanceMixin
from telegram_bot.commerce.campaigns import CampaignsMixin
from telegram_bot.commerce.client import CommerceUnavailable
from telegram_bot.commerce.admin_roles import AdminRolesMixin
from telegram_bot.commerce.partner_campaign_approvals import PartnerCampaignApprovalsMixin
from telegram_bot.logging_safety import install_telegram_log_filter

install_telegram_log_filter()
logger = logging.getLogger("TelegramBot")

from telegram_bot.config import settings as telegram_settings
_CONFIG = telegram_settings()
DEFAULT_BOT_TOKEN = _CONFIG['bot_token']
AUTHORIZED_USER_ID = _CONFIG['authorized_id']


class TelegramBotRunner(MaintenanceAccessMixin, SubscriptionMixin, VodafoneCashMixin, AdminRolesMixin, PartnerCampaignApprovalsMixin, PartnerLifecycleMixin, PartnerSheetMixin, BinanceNoteUiMixin, PricingUiMixin, SupportUiMixin, ArchiveUiMixin, CashCreditMixin, ManualPaymentsMixin, WalletPaymentsMixin, CampaignsMixin, PartnerFinanceMixin, CommerceCheckoutMixin, CommerceBotMixin, SheetWorkflowMixin, TelegramUiMixin):
    def __init__(
        self,
        orchestrator: Orchestrator,
        queue_service: QueueService,
        project_root: Path,
        bot_token: str = DEFAULT_BOT_TOKEN,
        authorized_id: int = AUTHORIZED_USER_ID,
        authorized_ids: Optional[list[int] | set[int]] = None,
    ) -> None:
        self.orchestrator = orchestrator
        self.queue_service = queue_service
        self.project_root = project_root
        self.bot_token = bot_token
        self.authorized_id = authorized_user_id(authorized_id) or 0
        self._authorized_override = authorized_ids is not None or authorized_id != AUTHORIZED_USER_ID
        if authorized_ids is not None:
            self.authorized_ids = {uid for x in authorized_ids if (uid:=authorized_user_id(x)) is not None}
        else:
            cfg_ids = [authorized_id] if self._authorized_override else _CONFIG.get('authorized_ids', [authorized_id])
            self.authorized_ids = {uid for x in cfg_ids if (uid:=authorized_user_id(x)) is not None}
        if self.authorized_id:
            self.authorized_ids.add(self.authorized_id)
        self.api_url = f"https://api.telegram.org/bot{self.bot_token}"

        self._stop_event = threading.Event()
        self._lifecycle_lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._ui_lock = threading.RLock()
        self._confirmations = {}
        self._sheet_drafts = {}
        self._waiting_user_inputs = {}
        self._last_update_id = 0
        self.last_poll_success = None
        self.last_poll_error = None
        self.last_update_at = None
        self.last_error_at = None
        self._cursor_saved_at = time.time()
        self._offset_path = self.project_root / '.telegram-update-offset.json'
        self._bot_fingerprint = hashlib.sha256(self.bot_token.encode('utf-8')).hexdigest()
        try:
            saved = json.loads(self._offset_path.read_text(encoding='utf-8'))
            saved_at = float(saved.get('saved_at', 0))
            # Telegram may randomize IDs after a week of inactivity. Old updates
            # expire after 24 hours, so resetting a six-day-old cursor is safe.
            if (saved.get('bot') == self._bot_fingerprint and math.isfinite(saved_at)
                    and 0 <= time.time() - saved_at < 6 * 86400):
                self._last_update_id = max(0, int(saved.get('update_id', 0)))
                self._cursor_saved_at = saved_at
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        self._restore_sheet_drafts()
        from .update_inbox import UpdateInbox
        inbox_path = self.project_root / '.commerce' / 'outbox.sqlite'
        fixture = self.project_root / '.services' / 'TEST_FIXTURE'
        if os.environ.get('ZAIN_SERVICE_MODE') == '1' and not inbox_path.is_file() and not (
                os.environ.get('ZAIN_SERVICE_TESTING') == '1' and fixture.is_file()
                and fixture.read_text(encoding='utf-8').strip() == 'smart-zain-isolated-tests'):
            raise ValueError('Existing commerce outbox database is required')
        self._update_inbox = UpdateInbox(inbox_path, self._bot_fingerprint)
        self._handler_workers = max(4, min(64, int(os.environ.get('ZAIN_TELEGRAM_WORKERS', '16'))))
        self._executor = ThreadPoolExecutor(max_workers=self._handler_workers-2, thread_name_prefix="TgWorker")
        self._priority_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='TgControl')
        self._handler_slots = threading.BoundedSemaphore(self._handler_workers-2)
        self._priority_slots = threading.BoundedSemaphore(2)
        self._inbox_thread = None
        self._consumer_lock = None
        self._session = requests.Session()
        # Retry policy belongs to _api_call; hidden adapter retries can repeat
        # sends and expose credential-bearing URLs in urllib3 warnings.
        adapter = HTTPAdapter(pool_connections=64, pool_maxsize=128, max_retries=0)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)
        from .payment_watcher import PaymentWatcher
        self.payment_watcher = PaymentWatcher(self, poll_interval=25)

    @property
    def current_authorized_ids(self) -> set[int]:
        if getattr(self,'_authorized_override',False):
            values=list(self.authorized_ids)+[self.authorized_id]
        else:
            # Removal from owner settings revokes authority immediately. Do not
            # union the current list with IDs cached during application startup.
            cfg=telegram_settings()
            values=list(cfg.get('authorized_ids',[]))+[cfg.get('authorized_id')]
        return {uid for value in values if (uid:=authorized_user_id(value)) is not None}

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._thread and self._thread.is_alive():
                if self._stop_event.is_set():
                    raise RuntimeError('Telegram polling is still stopping; try enabling it again shortly.')
                return
            if not self.bot_token:
                raise ValueError('Telegram bot token is not configured.')
            if self._consumer_lock is not None:
                raise RuntimeError('Telegram consumer is still shutting down')
            from services.locking import TokenLease
            consumer_lock = TokenLease(self._bot_fingerprint)
            consumer_lock.acquire()
            self._consumer_lock = consumer_lock
            self._update_inbox.recover()
            if getattr(self._executor, '_shutdown', False):
                self._executor = ThreadPoolExecutor(max_workers=self._handler_workers-2, thread_name_prefix='TgWorker')
                self._priority_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='TgControl')
            self._stop_event.clear()
            self._inbox_thread = threading.Thread(target=self._dispatch_inbox, name='telegram-inbox', daemon=True)
            self._inbox_thread.start()
            if hasattr(self,'_support_service'):self._support_service.start()
            self._thread = threading.Thread(target=self._poll_loop, name='telegram-poll', daemon=True)
            self._thread.start()
            try:
                if not maintenance_active(self.project_root):self.payment_watcher.start()
            except Exception as e:
                logger.warning("Could not start PaymentWatcher: %s", e)
        logger.info("TelegramBotRunner polling started with %s bounded handlers.", self._handler_workers)

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._stop_event.set()
            if hasattr(self,'_support_service'):self._support_service.stop()
            try:
                self.payment_watcher.stop()
            except Exception:
                pass
            if hasattr(self, '_executor'):
                try:
                    self._executor.shutdown(wait=False, cancel_futures=True)
                    self._priority_executor.shutdown(wait=False, cancel_futures=True)
                except Exception:
                    pass
            try:
                self._session.close()
            except Exception:
                pass
            if self._thread and self._thread is not threading.current_thread():
                self._thread.join(timeout=22)
                if self._thread.is_alive():
                    logger.warning('Telegram polling shutdown is waiting for an in-flight operation.')
            if self._inbox_thread and self._inbox_thread is not threading.current_thread():
                self._inbox_thread.join(timeout=2)
            # Retain the lock while a poll or handler can still act for this bot.
            if self._consumer_lock is not None:
                threading.Thread(target=self._release_consumer_after_shutdown,
                                 name='telegram-consumer-shutdown', daemon=True).start()

    def _release_consumer_after_shutdown(self):
        thread = self._thread
        if thread and thread is not threading.current_thread():thread.join()
        self._executor.shutdown(wait=True, cancel_futures=True)
        self._priority_executor.shutdown(wait=True, cancel_futures=True)
        with self._lifecycle_lock:
            if self._consumer_lock is not None:
                self._consumer_lock.close()
                self._consumer_lock = None

    def _dispatch_inbox(self):
        while not self._stop_event.is_set():
            dispatched = False
            for priority, slots, executor in ((True, self._priority_slots, self._priority_executor),
                                             (None, self._handler_slots, self._executor)):
                reserved=0
                while slots.acquire(False):reserved+=1
                if not reserved:continue
                try:
                    updates = self._update_inbox.claim_batch(limit=reserved,priority=priority)
                    for update in updates:
                        future=executor.submit(self._run_inbox_update,update,slots)
                        reserved-=1
                        future.add_done_callback(lambda completed,capacity=slots:capacity.release() if completed.cancelled() else None)
                        dispatched=True
                except Exception as exc:
                    logger.warning('Telegram inbox dispatch unavailable (%s).', type(exc).__name__)
                finally:
                    for _ in range(reserved):slots.release()
            if not dispatched:self._stop_event.wait(.05)

    def _run_inbox_update(self, update, slots=None):
        try:
            success = self._safe_handle_update(update, update['update_id'])
            self._update_inbox.finish(update['update_id'], success is not False)
        finally:
            (slots or self._handler_slots).release()

    def status(self) -> dict[str, Any]:
        """Read-only runtime health. Exception text/credentials are never exposed."""
        thread_alive = bool(self._thread and self._thread.is_alive())
        return {'polling': thread_alive and not self._stop_event.is_set(),
                'maintenance_active':maintenance_active(self.project_root),
                'thread_alive': thread_alive, 'stopping': thread_alive and self._stop_event.is_set(),
                'last_error': self.last_poll_error, 'last_error_at': self.last_error_at,
                'last_update_at': self.last_update_at, 'last_poll_at': self.last_poll_success,
                'handler_limit': self._handler_workers, 'reserved_control_handlers': 2,
                'inbox': self._update_inbox.counts()}

    def _api_call(self, method: str, data: Optional[dict[str, Any]] = None, timeout: int = 25) -> Optional[dict[str, Any]]:
        if data and data.get('parse_mode') == 'HTML' and isinstance(data.get('text'), str):
            from .presentation import rtl_html
            data = {**data, 'text': rtl_html(data['text'])}
        if data and 'reply_markup' in data:
            from .button_layout import normalize_markup
            data = {**data, 'reply_markup': normalize_markup(data['reply_markup'])}
        safe_retry = method in {'getUpdates', 'getFile', 'getMe', 'setMyCommands','deleteMyCommands',
                                'editMessageText', 'editMessageReplyMarkup', 'answerCallbackQuery'}
        url = f"{self.api_url}/{method}"
        for attempt in range(3):
            if self._stop_event.is_set():
                return None
            result = None
            try:
                resp = self._session.post(url, json=data, timeout=timeout)
                try:
                    result = resp.json()
                    if isinstance(result,dict) and not result.get('ok') and resp.status_code>=400:
                        result.setdefault('error_code',resp.status_code)
                except Exception:
                    result = {'ok': False, 'error_code': resp.status_code}
            except Exception as exc:
                logger.warning('Telegram %s transport failed (%s).', method, type(exc).__name__)
                if not safe_retry or attempt == 2 or self._stop_event.wait(2 ** attempt):
                    return None
                continue
            if not isinstance(result, dict):
                logger.warning('Telegram %s returned an invalid response.', method)
                return None
            if result.get('ok'):
                return result
            code = result.get('error_code')
            desc = str(result.get('description', ''))
            if 'message is not modified' in desc.lower():
                return result
            if 'query is too old' in desc.lower():
                logger.debug('Telegram callback query expired: %s', desc)
                return result
            if method in {'getChat','setMyCommands'} and code==400 and 'chat not found' in desc.lower():
                logger.debug('Telegram partner menu deferred until the user starts the bot.')
                return result
            chat_target = (data or {}).get('chat_id')
            if chat_target is not None:
                logger.warning('Telegram %s to chat %s was rejected (code %s): %s', method, chat_target, code, desc)
            else:
                logger.warning('Telegram %s was rejected (code %s): %s', method, code, desc)
            parameters = result.get('parameters')
            retry_after = parameters.get('retry_after') if isinstance(parameters, dict) else None
            try:
                delay = float(retry_after) if code == 429 else float(2 ** attempt)
                retryable = (code == 429 or (safe_retry and isinstance(code, int) and code >= 500))
                if not math.isfinite(delay) or delay < 0:
                    retryable = False
            except (ValueError, TypeError):
                retryable = False
                delay = 0
            if not retryable or attempt == 2 or self._stop_event.wait(delay):
                return result
        return None

    def _checkpoint_update(self, update_id: int) -> None:
        self._last_update_id = max(self._last_update_id, update_id)
        self._cursor_saved_at = time.time()
        checkpoint = self._offset_path.with_name(self._offset_path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with checkpoint.open('x', encoding='utf-8') as stream:
                json.dump({'bot': self._bot_fingerprint, 'update_id': self._last_update_id,
                           'saved_at': self._cursor_saved_at}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(checkpoint, self._offset_path)
        except OSError:
            # An unsuccessful temporary file is retained for diagnosis.
            logger.warning('Telegram update checkpoint could not be saved.')

    def _poll_loop(self) -> None:
        private_maintenance=maintenance_active(self.project_root)
        if not private_maintenance:
            self._recover_admin_role_action()
            self._start_partner_sheet_notifications()
            self._start_payout_notifications()
            self._start_partner_lifecycle_notifications()
        for aid in (() if private_maintenance else self.current_authorized_ids):
            self._api_call("setMyCommands", {"scope":{"type":"chat", "chat_id":aid},
                "commands":[{"command":"start", "description":"القائمة الرئيسية"},
                            {"command":"status", "description":"متابعة تقدم الفحص"},
                            {"command":"control", "description":"إدارة تشغيل الفحص"},
                            {"command":"wallet", "description":"عرض رصيد المحفظة"},
                            {"command":"queue", "description":"قائمة الفحوص"},
                            {"command":"results", "description":"استلام نتائج الجلسة"},
                            {"command":"upload", "description":"إضافة ملف العملاء"},
                            {"command":"partners", "description":"الشركاء"},
                            {"command":"partner_price", "description":"سعر ملف الشريك وإشعار الشركاء"},
                            {"command":"partner_checks", "description":"ملفات الشركاء بانتظار موافقتي"},
                            {"command":"earnings", "description":"أرباحي وأرباح الشركاء"},
                            {"command":"partner_add", "description":"إضافة شريك بكود إحالة"},
                            {"command":"partner_reactivate", "description":"إعادة تفعيل شراكة سابقة"},
                            {"command":"archive", "description":"استرجاع نتيجة برقم الطلب"},
                            {"command":"archive_admin", "description":"فهرس الأصل والنتيجة للإدارة"},
                            {"command":"help", "description":"طريقة الاستخدام"}]})
        if not private_maintenance:self._api_call('setMyCommands',{'scope':{'type':'all_private_chats'},'commands':[
            {'command':'start','description':'الرئيسية'},
            {'command':'upload','description':'إرسال ملف XLSX'},
            {'command':'drafts','description':'ملفاتي للمراجعة'},
            {'command':'status','description':'حالة فحوصاتي وطلباتي'},
            {'command':'wallet','description':'رصيد محفظتي'},
            {'command':'redeem','description':'شحن المحفظة بكود'},
            {'command':'archive','description':'استرجاع نتيجة سابقة'},
            {'command':'help','description':'خطوات الاستخدام'}]})
        try:
            if not private_maintenance:self._sync_partner_menus()
        except (CommerceUnavailable,OSError,ValueError,KeyError,TypeError):
            logger.warning('Partner menu initialization unavailable; /earnings remains accessible.')
        failures = 0
        while not self._stop_event.is_set():
            try:
                if time.time() - self._cursor_saved_at >= 6 * 86400:
                    self._last_update_id = 0
                    self._cursor_saved_at = time.time()
                res = self._api_call("getUpdates", {"offset": self._last_update_id + 1, "timeout": 15,
                                      "allowed_updates": ['message', 'callback_query','edited_message']}, timeout=35)
                if self._stop_event.is_set():
                    break
                if not res or not res.get("ok"):
                    failures += 1
                    code = (res or {}).get('error_code')
                    self.last_poll_error = code if type(code) is int else 'transport'
                    self.last_error_at = time.time()
                    self._stop_event.wait(min(30.0, 2.0 ** min(failures, 5)))
                    continue
                failures = 0
                self.last_poll_error = None
                self.last_poll_success = time.time()
                updates = res.get('result', [])
                if not isinstance(updates, list):
                    raise ValueError('Malformed update batch')
                if not updates:
                    continue

                max_batch_id = self._last_update_id
                valid_updates = []
                seen_batch_ids = set()
                for update in updates:
                    if not isinstance(update, dict) or type(update.get('update_id')) is not int:
                        logger.warning('Telegram ignored a malformed update.')
                        continue
                    update_id = update['update_id']
                    if update_id <= self._last_update_id or update_id in seen_batch_ids:
                        continue
                    seen_batch_ids.add(update_id)
                    valid_updates.append(update)
                    if update_id > max_batch_id:
                        max_batch_id = update_id

                if max_batch_id > self._last_update_id:
                    for update in valid_updates:self._capture_support_arrival(update)
                    self._update_inbox.put(valid_updates)
                    self._checkpoint_update(max_batch_id)
                    self.last_update_at = time.time()
            except Exception as exc:
                logger.error('Telegram polling cycle failed (%s).', type(exc).__name__)
                self.last_poll_error = type(exc).__name__
                self.last_error_at = time.time()
                self._stop_event.wait(3.0)

    def _safe_handle_update(self, update: dict[str, Any], update_id: int) -> None:
        try:
            self._handle_update(update)
            return True
        except Exception as exc:
            logger.error('Telegram update %s failed (%s): %s', update_id, type(exc).__name__, exc, exc_info=True)
            self.last_poll_error = 'update_' + type(exc).__name__
            self.last_error_at = time.time()
            cb_id = (update.get('callback_query') or {}).get('id')
            if cb_id:
                try:
                    self._api_call('answerCallbackQuery', {'callback_query_id': cb_id, 'text': '⚠️ تعذر إكمال الإجراء.', 'show_alert': True})
                except Exception:
                    pass
            message = update.get('message') or (update.get('callback_query') or {}).get('message') or {}
            chat_id = message.get('chat', {}).get('id')
            if chat_id:
                try:
                    self._reply(chat_id, '⚠️ تعذر إكمال هذا الإجراء. راجع الحالة قبل إعادة المحاولة.')
                except Exception:
                    pass
            return False

    def _handle_update(self, update: dict[str, Any]) -> None:
        self._remember_update_identity(update)
        if self._maintenance_gate(update):return
        if self._support_handle_message(update):return
        if update.get('edited_message'):return
        if update.get("callback_query"):
            self._handle_callback(update["callback_query"])
            return
        msg = update.get("message")
        if not msg:
            return

        from_user = msg.get("from", {})
        sender_id = from_user.get("id")
        chat = msg.get("chat", {})
        chat_id = chat.get("id", sender_id)
        chat_type = chat.get("type", "private")

        # Basic validations
        if type(sender_id) is not int or type(chat_id) is not int or from_user.get("is_bot"):
            return

        text = str(msg.get("text", "")).strip()

        # Handle groups, supergroups, and channels:
        if chat_type in {"group", "supergroup", "channel"}:
            cmd = text.split()[0].split("@")[0].lower() if text else ""
            if cmd in {"/id", "/group_id", "/chat_id"}:
                self._reply(chat_id, f"🆔 <b>معرّف هذه المجموعة:</b> <code>{chat_id}</code>")
                return

            is_sender_admin = (sender_id in self.current_authorized_ids)
            if not is_sender_admin:
                return

            if cmd in {"/set_admin_group", "/setgroup", "/add_admin_group"}:
                from telegram_bot.config import add_admin_group
                if add_admin_group(chat_id):
                    self._reply(
                        chat_id,
                        f"✅ <b>تم ربط هذه المجموعة كمجموعة مشرفين بنجاح!</b>\n\n"
                        f"🆔 <b>معرّف المجموعة:</b> <code>{chat_id}</code>\n"
                        f"📢 ستصل هنا تلقائياً جميع إشعارات اكتمال فحص الملفات وملفات النتائج (Excel)."
                    )
                else:
                    self._reply(chat_id, "⚠️ تعذر حفظ المجموعة في الإعدادات.")
                return
            elif cmd in {"/unset_admin_group", "/remove_admin_group", "/delgroup"}:
                from telegram_bot.config import remove_admin_group
                if remove_admin_group(chat_id):
                    self._reply(
                        chat_id,
                        f"❌ <b>تم إلغاء ربط هذه المجموعة من مجموعات المشرفين.</b>\n"
                        f"🆔 <code>{chat_id}</code>"
                    )
                else:
                    self._reply(chat_id, "⚠️ تعذر حذف المجموعة من الإعدادات.")
                return
            return

        # Wallets and upload drafts are identified by the private user's chat ID.
        if chat_id != sender_id or chat_type != "private":
            return
        if support_route(text):
            self._official_support(chat_id)
            return
        if not self._subscription_gate(chat_id):
            return
        self._refresh_partner_menu_for_chat(chat_id)
        username = from_user.get("username", "")

        is_admin = (sender_id in self.current_authorized_ids and chat_id in self.current_authorized_ids)

        if self._vodafone_handle_media(msg, chat_id):
            return
        doc = msg.get('document')
        if doc:
            if self._archive_forward(msg,chat_id):return
            with self._ui_lock:
                self._waiting_user_inputs.pop(chat_id, None)
            if str(doc.get('file_name', '')).lower().endswith('.xlsx'):
                self._handle_document_upload(doc, chat_id)
            else:
                self._reply(chat_id, '📎 أرسل ملف عملاء بصيغة XLSX. الملفات الأخرى غير مدعومة.')
                self._dispatch_ui('upload', chat_id)
            return
        if not text:
            # Stickers/photos must not consume an outstanding code/TxID prompt.
            return

        text_map = {
            "💎 فحص ملف جديد": "/upload",
            "💎 إضافة ملف فحص": "/upload",
            "💎 فحص ملف جديد (رفع شيت)": "/upload",
            "💎 إضافة ملف للفحص (رفع شيت)": "/upload",
            "💎 إضافة ملف للفحص": "/upload",
            "💎 المنتجات / فحص زين": "/upload",
            "📊 حالة الفحص": "/status", "📊 متابعة الفحص": "/status",
            "📊 حالة فحوصاتي": "/status",
            "📁 طابور الملفات": "/queue", "🔄 طابور الملفات": "/queue",
            "⏸ إيقاف مؤقت": "/pause", "▶ استئناف الفحص": "/resume",
            "📎 إضافة ملف": "/upload", "💡 طريقة الاستخدام": "/help",
            "💡 دليل الاستخدام": "/help", "💡 خطوات الاستخدام": "/help",
            "🏠 الرئيسية": "/start", "📥 نتائج الجلسة": "/results",
            "📥 نتائج الجولة": "/results", "💼 محفظتي": "/wallet",
            "💼 المحفظة والرصيد": "/wallet",
            "📋 ملفاتي للمراجعة": "/drafts", "📋 طلباتي وملفاتي": "/drafts",
            "🎫 كود الشحن": "/redeem", "🎁 شحن بكود": "/redeem",
            "‹ الرئيسية": "/start", "‹ إلغاء ورجوع للمحفظة": "/wallet",
            "👑 وضع المدير": "/admin", "👤 وضع الزبون": "/customer",
            "💲 شحن المحفظة": "/wallet",
            "كود الإحالة": "/ref",
            "الشركاء والإحالات": "/partners",
        }
        text_clean = text.strip()
        from .button_layout import REPLY_ACTIONS
        text_map.update(REPLY_ACTIONS)
        text_mapped = text_map.get(text_clean, text_clean)

        is_cmd_or_nav = (
            text_mapped.startswith("/")
            or text_clean in text_map
            or text_clean.lower() in {"cancel", "إلغاء", "الغاء", "خروج", "رجوع", "‹ رجوع", "‹ الرئيسية", "‹ إلغاء ورجوع للمحفظة", "back", "/cancel"}
        )

        if is_cmd_or_nav:
            # User sent a command or menu navigation — immediately clear waiting state to prevent getting trapped
            with self._ui_lock:
                self._waiting_user_inputs.pop(chat_id, None)
            if text_clean.lower() in {"cancel", "إلغاء", "الغاء", "خروج", "رجوع", "‹ رجوع", "back", "/cancel"}:
                self._reply(chat_id, "❌ تم إلغاء العملية.")
                self._dispatch_ui("home", chat_id)
                return
            text = text_mapped
        else:
            # Check if user is in an active input-waiting state (promo code, tx_id, or redeem)
            with self._ui_lock:
                waiting = self._waiting_user_inputs.pop(chat_id, None)
            if waiting and text:
                waiting['_telegram_input_message_id']=msg.get('message_id')
                if waiting.get('action','').startswith('commerce_vodafone_'):
                    waiting['_telegram_message']=msg
                self._handle_user_text_input(chat_id, username, text, waiting)
                return

        command = text.split()[0].split("@")[0].lower() if text else ""
        if self._commerce_command(text, chat_id, is_admin, username, message_id=msg.get('message_id')):
            return
        route = {
            "/start": "home",
            "/help": "help",
            "/upload": "upload",
            "/wallet": "wallet",
            "/drafts": "drafts:0",
            "/status": "status",
            "/control": "menu:operations",
            "/queue": "queue:0",
            "/pause": "pause",
            "/stop": "pause",
            "/resume": "resume",
            "/results": "results"
        }.get(command)

        if is_admin and command in {"/admin", "/customer"}:
            target_mode = "admin" if command == "/admin" else "customer"
            self._set_user_mode(chat_id, target_mode)
            mode_name_ar = "المدير (Admin)" if target_mode == "admin" else "العميل (Customer)"
            self._reply(chat_id, f"🔄 <b>تم التبديل بنجاح إلى وضع {mode_name_ar}.</b>\nتم تحديث لوحة التحكم وقائمة الأزرار السريعة.")
            self._dispatch_ui("home", chat_id)
            return

        if is_admin and command in {"/admin_groups", "/groups"}:
            cfg = telegram_settings()
            groups = cfg.get("admin_groups", [])
            lines = ["👥 <b>مجموعات المشرفين المسجلة:</b>\n"]
            if groups:
                for gid in groups:
                    lines.append(f"• <code>{gid}</code>")
            else:
                lines.append("<i>لا توجد مجموعات مسجلة حالياً.</i>")
            lines.append("\n💡 <b>طريقة الإضافة:</b>")
            lines.append("1. أضف هذا البوت إلى المجموعة في تليجرام.")
            lines.append("2. اكتب داخل المجموعة: <code>/set_admin_group</code>")
            lines.append("أو اكتب هنا: <code>/add_admin_group &lt;معرف المجموعة&gt;</code>")
            self._reply(chat_id, "\n".join(lines))
            return

        if is_admin and command in {"/add_admin_group", "/addgroup"}:
            from telegram_bot.config import add_admin_group
            parts = text.split()
            if len(parts) > 1:
                try:
                    gid = int(parts[1])
                    if add_admin_group(gid):
                        self._reply(chat_id, f"✅ تم حفظ مجموعة المشرفين: <code>{gid}</code>")
                    else:
                        self._reply(chat_id, "⚠️ تعذر حفظ معرّف المجموعة.")
                except ValueError:
                    self._reply(chat_id, "⚠️ يرجى إدخال معرّف رقمي صحيح (مثال: <code>-1001234567890</code>).")
            else:
                self._reply(chat_id, "ℹ️ اكتب الأمر متبوعاً بمعرّف المجموعة:\n<code>/add_admin_group -1001234567890</code>")
            return

        if is_admin and command in {"/remove_admin_group", "/delgroup"}:
            from telegram_bot.config import remove_admin_group
            parts = text.split()
            if len(parts) > 1:
                try:
                    gid = int(parts[1])
                    if remove_admin_group(gid):
                        self._reply(chat_id, f"❌ تم حذف مجموعة المشرفين: <code>{gid}</code>")
                    else:
                        self._reply(chat_id, "⚠️ تعذر حذف معرّف المجموعة.")
                except ValueError:
                    self._reply(chat_id, "⚠️ يرجى إدخال معرّف رقمي صحيح.")
            else:
                self._reply(chat_id, "ℹ️ اكتب الأمر متبوعاً بمعرّف المجموعة لحذفها.")
            return

        if is_admin and command in {"/set_icon", "/seticon"}:
            from telegram_bot.config import set_button_icon
            entities = msg.get("entities", [])
            custom_emojis = [ent.get("custom_emoji_id") for ent in entities if ent.get("type") == "custom_emoji" and ent.get("custom_emoji_id")]
            parts = text.split()
            if len(parts) > 1:
                btn_name = parts[1].lower()
                emoji_id = custom_emojis[0] if custom_emojis else (parts[2] if len(parts) > 2 else None)
                if emoji_id:
                    if set_button_icon(btn_name, emoji_id):
                        self._reply(chat_id, f"✅ تم تعيين الأيقونة المتحركة (ID: <code>{emoji_id}</code>) للزر <b>{btn_name}</b> بنجاح!\nتم تحديث واجهة البوت.")
                    else:
                        self._reply(chat_id, "⚠️ فشل في حفظ معرّف الأيقونة.")
                else:
                    self._reply(chat_id, "ℹ️ اكتب الأمر متبوعاً باسم الزر والإيموجي المتحرك:\n<code>/set_icon upload 💎</code>\nأو <code>/set_icon wallet &lt;ID&gt;</code>")
            else:
                self._reply(chat_id, "ℹ️ <b>طريقة تعيين أيقونة متحركة:</b>\n<code>/set_icon upload 💎</code>\nالأزرار المتاحة: <code>upload, wallet, status, queue, help, home, drafts, redeem</code>")
            return

        if is_admin and command in {"/get_icon", "/geticon", "/icon_id"}:
            entities = msg.get("entities", [])
            custom_emojis = [ent.get("custom_emoji_id") for ent in entities if ent.get("type") == "custom_emoji" and ent.get("custom_emoji_id")]
            if custom_emojis:
                self._reply(chat_id, f"🆔 <b>معرّفات الإيموجي المتحرك (Custom Emoji IDs):</b>\n" + "\n".join(f"• <code>{eid}</code>" for eid in custom_emojis))
            else:
                self._reply(chat_id, "ℹ️ أرسل إيموجي مميزاً (Telegram Premium Custom Emoji) في نفس الرسالة مع <code>/get_icon</code> لمعرفة معرّفه الرقمي.")
            return

        if not is_admin and route in {"queue:0", "pause", "resume", "results"}:
            self._reply(chat_id, "⚠️ هذا الإجراء متاح للمشرف فقط. يمكنك رفع ملف لفحصه أو عرض محفظتك.")
            return

        if command in {"/redeem", "/charge", "/code"}:
            from .payment_manager import PAYMENT_MGR
            from .crypto_pay import get_live_usd_egp_rate
            parts = text.split()
            if len(parts) > 1:
                code_to_redeem = parts[1]
                ok, msg, amount = PAYMENT_MGR.redeem_code(code_to_redeem, chat_id)
                acc_id = PAYMENT_MGR.get_user_account_id(chat_id)
                if ok:
                    op_id = PAYMENT_MGR.generate_operation_id()
                    lr = 0.0  # Report the committed EGP amount even if exchange services are unavailable.
                    usdt_equiv = round(amount / lr, 2) if lr > 0 else 0.0
                    PAYMENT_MGR.log_operation(
                        op_id,
                        op_type="redeem_code",
                        user_id=chat_id,
                        details={
                            "method": "Redeem Code",
                            "code": code_to_redeem.upper(),
                            "amount_egp": amount,
                            "amount_usdt": usdt_equiv,
                            "status": "completed",
                        }
                    )
                    new_bal = PAYMENT_MGR.get_wallet_balance(chat_id)
                    panel = views.redeem_success_panel(amount, new_bal, account_id=acc_id, live_rate=lr)
                    self._show_panel(chat_id, panel)
                else:
                    self._reply(chat_id, msg)
            else:
                with self._ui_lock:
                    self._waiting_user_inputs[chat_id] = {"action": "redeem"}
                panel = views.redeem_input_panel()
                self._show_panel(chat_id, panel)
            return

        if is_admin and command == "/gencode":
            from .payment_manager import PAYMENT_MGR
            from .crypto_pay import get_live_usd_egp_rate
            parts = text.split()
            try:
                amount = self._positive_amount(parts[1] if len(parts) > 1 else '30')
                max_uses = int(parts[2]) if len(parts) > 2 else 1
                if max_uses <= 0:
                    raise ValueError('positive uses required')
            except (ValueError, OverflowError):
                self._reply(chat_id, 'أدخل مبلغًا موجبًا وعدد استخدامات صحيحًا: <code>/gencode 30 1</code>')
                return
            code = PAYMENT_MGR.generate_redeem_code(amount=amount, max_uses=max_uses)
            lr = 0.0  # Report the committed EGP amount even if exchange services are unavailable.
            usdt_str = f" (~{amount / lr:.2f} USDT)" if lr > 0 else ""
            self._reply(
                chat_id,
                f"🎁 <b>تم توليد كود شحن جديد!</b>\n\n"
                f"الكود: <code>{code}</code>\n"
                f"القيمة: <b>{amount:.2f} جنيه مصري</b>{usdt_str}\n"
                f"مرات الاستخدام: <b>{max_uses}</b>\n\n"
                f"<i>أرسل الكود للعميل ليتم شحن محفظته بالجنيه المصري فوراً عند إدخاله.</i>"
            )
            return

        if is_admin and command == "/addcode":
            from .payment_manager import PAYMENT_MGR
            parts = text.split()
            if len(parts) >= 3:
                code_name = parts[1].upper()
                try:
                    amount = self._positive_amount(parts[2])
                    max_uses = int(parts[3]) if len(parts) > 3 else 1
                    if max_uses <= 0 or not re.fullmatch(r'[A-Z0-9_-]{3,64}', code_name):
                        raise ValueError('invalid code')
                except (ValueError, OverflowError):
                    self._reply(chat_id, 'استخدم كودًا من حروف إنجليزية وأرقام، ومبلغًا موجبًا: <code>/addcode CODE 30 1</code>')
                    return
                PAYMENT_MGR.add_redeem_code(code_name, amount, max_uses)
                self._reply(chat_id, f"✅ تم حفظ كود الشحن <code>{code_name}</code> بقيمة {amount:.2f} جنيه مصري.")
            else:
                self._reply(chat_id, "الاستخدام: <code>/addcode CODE 30 1</code>")
            return

        if is_admin and command == "/credit_user":
            from .payment_manager import PAYMENT_MGR
            parts = text.split()
            if len(parts) >= 3:
                target_id = parts[1]
                try:
                    amount = self._positive_amount(parts[2])
                except (ValueError, OverflowError):
                    self._reply(chat_id, 'أدخل مبلغ شحن موجبًا: <code>/credit_user USER_ID 30</code>')
                    return
                found = PAYMENT_MGR.find_user_by_account_id(target_id)
                if found:
                    uid, _ = found
                else:
                    uid = target_id
                try:
                    uid = int(uid)
                    if uid <= 0:
                        raise ValueError('invalid user')
                except (ValueError, TypeError):
                    self._reply(chat_id, 'لم أعثر على المستخدم. أدخل رقم حساب مسجلًا أو Telegram User ID صحيحًا.')
                    return
                credit_options={'reason':'شحن يدوي بواسطة المشرف'}
                if isinstance(msg.get('message_id'),int):credit_options['idempotency_key']=f"telegram-credit:{chat_id}:{msg['message_id']}"
                new_bal = PAYMENT_MGR.credit_wallet(uid, amount, **credit_options)
                acc_id = PAYMENT_MGR.get_user_account_id(uid)
                self._reply(chat_id, f"✅ تم شحن {amount:.2f} جنيه مصري للمستخدم (ID: <code>{acc_id}</code>). رصيده الجديد: {new_bal:.2f} جنيه مصري.")
                try:
                    self._api_call("sendMessage", {
                        "chat_id": int(uid),
                        "text": f"🎉 <b>تم شحن محفظتك بواسطة المشرف!</b>\n➕ المبلغ المضاف: <b>+{amount:.2f} جنيه مصري</b>\n💼 رصيدك الحالي: <b>{new_bal:.2f} جنيه مصري</b>",
                        "parse_mode": "HTML"
                    })
                except Exception:
                    pass
            else:
                self._reply(chat_id, "الاستخدام: <code>/credit_user [Account_ID أو User_ID] [المبلغ بالجنيه]</code>")
            return

        if is_admin and command in {"/lookup", "/checkop", "/op"}:
            from .payment_manager import PAYMENT_MGR
            parts = text.split()
            if len(parts) > 1:
                target_op = parts[1].strip().lstrip("#")
                op_data = PAYMENT_MGR.get_operation(target_op)
                if op_data:
                    op_id = op_data.get("operation_id", target_op)
                    acc_id = op_data.get("account_id", "غير محدد")
                    uid = op_data.get("user_id", "غير محدد")
                    ts = op_data.get("timestamp", "غير مسجل")
                    status_val = op_data.get("status", "غير معروف")
                    status_map = {
                        "completed": "✅ مكتملة / مدفوعة",
                        "approved": "✅ تمت الموافقة عليها",
                        "pending": "⏳ بانتظار المراجعة والتحقق",
                        "rejected": "❌ مرفوضة"
                    }
                    status_text = status_map.get(status_val, status_val)
                    details_lines = []
                    labels = {
                        "sheet_name": "📁 الملف",
                        "method": "💳 الطريقة",
                        "amount_egp": "💵 الرسوم الأساسية",
                        "amount_usdt": "🪙 المعادل (كريبتو)",
                        "tx_id": "🔗 TxID",
                        "invoice_id": "🧾 Invoice ID",
                        "code": "🎁 الكود"
                    }
                    for k in ("sheet_name", "method", "amount_egp", "amount_usdt", "tx_id", "invoice_id", "code"):
                        if k in op_data and op_data[k]:
                            v = op_data[k]
                            if k == "amount_egp":
                                v = f"{float(v):.2f} جنيه مصري"
                            elif k == "amount_usdt":
                                v = f"{float(v):.2f} USDT"
                            details_lines.append(f"{labels.get(k, k)}: <b>{views.safe(str(v))}</b>")
                    extra = "\n".join(details_lines)
                    msg_text = (
                        f"🔍 <b>تفاصيل العملية #{views.safe(op_id)}</b>\n\n"
                        f"📌 الحالة: <b>{views.safe(status_text)}</b>\n"
                        f"🕒 التوقيت: <code>{views.safe(ts)}</code>\n"
                        f"🆔 رقم الحساب: <code>{views.safe(acc_id)}</code> (Telegram: <code>{views.safe(uid)}</code>)\n"
                        f"{extra}"
                    )
                    self._reply(chat_id, msg_text)
                else:
                    self._reply(chat_id, f"⚠️ لم يتم العثور على أي عملية برقم <code>#{views.safe(target_op)}</code>.")
            else:
                self._reply(chat_id, "الاستخدام: <code>/lookup [رقم العملية مثل 261007-0001]</code>")
            return

        if route:
            self._dispatch_ui(route, chat_id)

            return

        self._dispatch_ui("help", chat_id)

    def _handle_user_text_input(self, chat_id: int, username: str, text: str, waiting: dict) -> None:
        if self._commerce_text_input(chat_id, text, waiting):
            return
        from .payment_manager import PAYMENT_MGR
        action = waiting.get("action")
        token = waiting.get("token")
        message_id = waiting.get("message_id")
        if token and self._sheet_drafts.get(token,{}).get('commerce_request_key'):
            self._commerce_start_checkout(token,chat_id,message_id)
            return

        if action == "redeem":
            clean_code = text.strip()
            for prefix in ("/redeem", "/code", "/charge"):
                if clean_code.lower().startswith(prefix):
                    clean_code = clean_code[len(prefix):].strip()
                    break
            ok, msg, amount = PAYMENT_MGR.redeem_code(clean_code, chat_id)
            acc_id = PAYMENT_MGR.get_user_account_id(chat_id)
            if ok:
                from .crypto_pay import get_live_usd_egp_rate
                op_id = PAYMENT_MGR.generate_operation_id()
                lr = 0.0  # Report the committed EGP amount even if exchange services are unavailable.
                usdt_equiv = round(amount / lr, 2) if lr > 0 else 0.0
                PAYMENT_MGR.log_operation(
                    op_id,
                    op_type="redeem_code",
                    user_id=chat_id,
                    details={
                        "method": "Redeem Code",
                        "code": clean_code.upper(),
                        "amount_egp": amount,
                        "amount_usdt": usdt_equiv,
                        "status": "completed",
                    }
                )
                new_bal = PAYMENT_MGR.get_wallet_balance(chat_id)
                panel = views.redeem_success_panel(amount, new_bal, account_id=acc_id, live_rate=lr)
                self._show_panel(chat_id, panel, message_id)
            else:
                self._api_call("sendMessage", {
                    "chat_id": chat_id,
                    "text": f"{msg}\n\nيرجى كتابة كود صالح أو إرسال <b>إلغاء</b> للخروج.",
                    "parse_mode": "HTML",
                })
                self._waiting_user_inputs[chat_id] = waiting
            return

        if action in {"topup_tx", "tx_id"}:
            # Public transfer references are receipt locators, not ownership
            # credentials. No legacy auto-credit/auto-fulfilment is permitted.
            self._show_panel(chat_id, views.Panel(
                '<b>رقم التحويل وحده لا يكفي لاعتماد الدفع</b>\n\n'
                'الدفع التلقائي يتم بفاتورة أنشأها البرنامج لطلبك. '
                'التحويل المباشر يحتاج مراجعة صاحبه؛ لن نضيف رصيدًا أو نبدأ الفحص بمجرد إرسال TXID. '
                'الطلبات القديمة محفوظة للمراجعة.',
                [[views.button('المحفظة وطرق الشحن', 'wallet')],
                 [views.button('طلباتي ومدفوعاتي', 'oh:list:0')]]), message_id)
            return

        if action == "topup_tx":
            from .crypto_pay import get_live_usd_egp_rate
            method_key = waiting.get("method_key", "binance")
            method_name = waiting.get("method_name", "Crypto")
            acc_id = PAYMENT_MGR.get_user_account_id(chat_id)
            live_rate = get_live_usd_egp_rate() or 50.0

            # 1. Binance Pay Top-up
            if method_key == "binance":
                from .crypto_pay import BINANCE_PAY
                auto_ok, auto_msg, tx_info = BINANCE_PAY.verify_payment(text, min_amount=0.1)
                if auto_ok:
                    actual_amt = float((tx_info or {}).get("amount", 1.0))
                    amount_egp = round(actual_amt * live_rate, 2)
                    new_bal = PAYMENT_MGR.credit_wallet(chat_id, amount_egp, reason=f"binance_pay_auto:{text.strip()}")
                    op_id = PAYMENT_MGR.generate_operation_id()
                    PAYMENT_MGR.log_operation(
                        op_id,
                        op_type="wallet_topup_binance_auto",
                        user_id=chat_id,
                        details={
                            "method": "Binance Pay",
                            "tx_id": text.strip(),
                            "amount_usdt": actual_amt,
                            "amount_egp": amount_egp,
                            "live_rate": live_rate,
                            "status": "completed",
                        }
                    )
                    panel = views.wallet_topup_success_panel(
                        amount_usdt=actual_amt,
                        amount_egp=amount_egp,
                        new_balance=new_bal,
                        tx_id=text.strip(),
                        method="Binance Pay",
                        live_rate=live_rate,
                        account_id=acc_id,
                    )
                    self._show_panel(chat_id, panel, message_id)
                    for aid in self.current_authorized_ids:
                        if aid != chat_id:
                            try:
                                self._api_call("sendMessage", {
                                    "chat_id": aid,
                                    "text": (
                                        f"⚡ <b>تم شحن محفظة عميل تلقائياً عبر Binance Pay!</b>\n\n"
                                        f"العميل: <b>{views.safe(self._telegram_person_name(chat_id))}</b> (ID: <code>{acc_id}</code>)\n"
                                        f"🪙 المبلغ: <b>{actual_amt:.2f} USDT</b> (+{amount_egp:.2f} ج.م)\n"
                                        f"💼 الرصيد الجديد: <b>{new_bal:.2f} ج.م</b>\n"
                                        f"🔗 رقم العملية: <code>{views.safe(text.strip())}</code>"
                                    ),
                                    "parse_mode": "HTML",
                                })
                            except Exception:
                                pass
                    return
                else:
                    self._show_panel(
                        chat_id,
                        views.notice(
                            "❌ لم يتم العثور على العملية في Binance Pay",
                            f"<b>{views.safe(auto_msg)}</b>\n\n"
                            f"🔍 الرقم المدخل: <code>{views.safe(text.strip())}</code>\n\n"
                            f"يرجى التأكد من إتمام التحويل من تطبيق Binance بالكامل وإرسال رقم العملية (Order ID أو TxID) الصحيح.",
                            html=True
                        ),
                        message_id
                    )
                    self._waiting_user_inputs[chat_id] = waiting
                    return

            # 2. BSC (BEP20) Top-up
            elif method_key in ("bsc", "bsc_usdt", "bsc_usdc"):
                from .crypto_pay import BSC_CHAIN
                auto_ok, auto_msg, actual_amt, contract = BSC_CHAIN.verify_transaction(text, min_amount=0.1)
                if auto_ok:
                    actual_amt = float(actual_amt or 1.0)
                    amount_egp = round(actual_amt * live_rate, 2)
                    new_bal = PAYMENT_MGR.credit_wallet(chat_id, amount_egp, reason=f"bsc_bep20_auto:{text.strip()}")
                    op_id = PAYMENT_MGR.generate_operation_id()
                    PAYMENT_MGR.log_operation(
                        op_id,
                        op_type="wallet_topup_bsc_auto",
                        user_id=chat_id,
                        details={
                            "method": "BSC (BEP20)",
                            "tx_id": text.strip(),
                            "amount_usdt": actual_amt,
                            "amount_egp": amount_egp,
                            "contract": contract,
                            "live_rate": live_rate,
                            "status": "completed",
                        }
                    )
                    panel = views.wallet_topup_success_panel(
                        amount_usdt=actual_amt,
                        amount_egp=amount_egp,
                        new_balance=new_bal,
                        tx_id=text.strip(),
                        method="BSC (BEP20)",
                        live_rate=live_rate,
                        account_id=acc_id,
                    )
                    self._show_panel(chat_id, panel, message_id)
                    for aid in self.current_authorized_ids:
                        if aid != chat_id:
                            try:
                                self._api_call("sendMessage", {
                                    "chat_id": aid,
                                    "text": (
                                        f"⚡ <b>تم شحن محفظة عميل تلقائياً عبر البلوكشين (BEP20)!</b>\n\n"
                                        f"العميل: <b>{views.safe(self._telegram_person_name(chat_id))}</b> (ID: <code>{acc_id}</code>)\n"
                                        f"🪙 المبلغ: <b>{actual_amt:.2f} USDT/USDC</b> (+{amount_egp:.2f} ج.م)\n"
                                        f"💼 الرصيد الجديد: <b>{new_bal:.2f} ج.م</b>\n"
                                        f"🔗 هاش المعاملة: <code>{views.safe(text.strip())}</code>"
                                    ),
                                    "parse_mode": "HTML",
                                })
                            except Exception:
                                pass
                    return
                else:
                    self._show_panel(
                        chat_id,
                        views.notice(
                            "❌ فشل التحقق على شبكة BSC البلوكشين",
                            f"<b>{views.safe(auto_msg)}</b>\n\n"
                            f"🔗 الهاش المدخل: <code>{views.safe(text.strip())}</code>\n\n"
                            f"تأكد من أن المعاملة مكتملة بنجاح (Status: Success) على شبكة BSC وأنها محولة إلى عنوان محفظتنا.",
                            html=True
                        ),
                        message_id
                    )
                    self._waiting_user_inputs[chat_id] = waiting
                    return

            # 3. Manual Networks (TON, SOL, ERC20) Top-up
            else:
                clean_val = text.strip()
                if len(clean_val) < 15:
                    self._show_panel(
                        chat_id,
                        views.notice(
                            "⚠️ صيغة المعاملة غير صحيحة",
                            f"النص المدخل: <code>{views.safe(clean_val)}</code> قصير جداً ولا يطابق صيغة معاملات البلوكشين.\n\n"
                            f"يرجى نسخ كامل هاش المعاملة أو التوقيع من محفظتك والمحاولة مجدداً.",
                            html=True
                        ),
                        message_id
                    )
                    self._waiting_user_inputs[chat_id] = waiting
                    return

                topup_id = PAYMENT_MGR.generate_operation_id()
                token = f"topup_{topup_id}"
                est_usdt = 10.0
                est_egp = round(est_usdt * live_rate, 2)
                PAYMENT_MGR.record_pending_payment(
                    token=token,
                    method=method_name,
                    tx_id=clean_val,
                    user_id=chat_id,
                    username=username,
                    sheet_name=f"شحن محفظة ({method_name})",
                    amount_usdt=est_usdt,
                    draft_info={
                        "kind": "topup",
                        "topup_id": topup_id,
                        "method": method_name,
                        "method_key": method_key,
                        "tx_id": clean_val,
                        "user_id": chat_id,
                        "amount_usdt": est_usdt,
                        "amount_egp": est_egp,
                    },
                    operation_id=topup_id,
                    amount_egp=est_egp,
                )
                self._show_panel(
                    chat_id,
                    views.notice(
                        "⏳ تم إرسال إثبات الشحن للمراجعة",
                        f"تم استلام إثبات العملية بنجاح!\n\n"
                        f"🆔 رقم الحساب: <code>{acc_id}</code>\n"
                        f"🧾 رقم الطلب: <code>#{topup_id}</code>\n"
                        f"🌐 الشبكة: <b>{views.safe(method_name)}</b>\n"
                        f"🔗 المعاملة: <code>{views.safe(clean_val)}</code>\n\n"
                        f"سيقوم المشرف بالتحقق من وصول المبلغ وإيداع الرصيد في محفظتك فوراً 🚀",
                        html=True
                    ),
                    message_id
                )
                for aid in self.current_authorized_ids:
                    try:
                        admin_panel = views.admin_topup_approval_panel(
                            display_name=self._telegram_person_name(chat_id),
                            user_id=str(chat_id),
                            username=username or "",
                            method=method_name,
                            tx_id=clean_val,
                            amount_usdt=est_usdt,
                            amount_egp=est_egp,
                            topup_id=token,
                            account_id=acc_id,
                        )
                        self._show_panel(aid, admin_panel)
                    except Exception:
                        pass
                return

        with self._ui_lock:
            draft = self._payment_draft(token, chat_id, recover=True)
        if not draft:
            self._api_call("sendMessage", {"chat_id": chat_id,
                "text": "⚠️ طلب الدفع غير متاح أو اكتمل بالفعل. راجع ملفاتك أو أرسل الملف للمراجعة."})
            return

        if action == "promo":
            from .payment_manager import money
            from decimal import Decimal
            code = text.strip().upper()
            with self._ui_lock:
                if draft.get("promo_code") and draft["promo_code"] != code:
                    self._show_panel(chat_id, views.notice("الكود مرتبط بهذا الطلب", "أعد إدخال نفس الكود لاستكمال الطلب دون احتساب خصم إضافي."), message_id)
                    return
                # Persist the base price and claim owner before marking the coupon
                # consumed. Queue or exchange-rate failures can replay this receipt.
                draft["promo_code"] = code
                draft.setdefault("promo_original_egp", draft.get("egp_amount", PAYMENT_MGR.get_config().get("sheet_price_egp", 30.0)))
                PAYMENT_MGR.record_payment_draft(token, chat_id, draft, kind="promo")
                ok, msg, promo = PAYMENT_MGR.apply_promo_code(code, chat_id, claim_id=token)
                if not ok:
                    draft.pop("promo_code", None)
                    PAYMENT_MGR.record_payment_draft(token, chat_id, draft, kind="promo")
                    self._waiting_user_inputs[chat_id] = waiting
                    self._show_panel(chat_id, views.notice("لم يتم تطبيق الكود", msg), message_id)
                    return
                try:
                    disc = Decimal(str(promo.get("discount", 100 if promo.get("type") == "free" else 0)))
                    if not disc.is_finite() or not 0 <= disc <= 100:
                        raise ValueError("Invalid configured discount")
                    free = promo.get("type") == "free" or disc == 100
                    if promo.get("type", "free") not in {"free", "percent"}:
                        raise ValueError("Unsupported coupon type")
                    # Preserve older receipts if a user pays an already issued
                    # invoice after obtaining a discounted replacement.
                    if not draft.get("promo_applied"):
                        old = draft.get("invoice")
                        if old and draft.get("invoice_id"):
                            draft.setdefault("previous_invoices", []).append({
                                "invoice_id": draft["invoice_id"], "usdt_amount": draft["usdt_amount"], "egp_amount": draft["egp_amount"]})
                        draft["invoice_id"] = 0
                        draft.pop("invoice", None)
                        draft["egp_amount"] = 0.0 if free else float(money(
                            money(draft["promo_original_egp"]) * (Decimal(100)-disc) / Decimal(100)))
                        draft["promo_applied"] = True
                        draft["promo_discount"] = float(disc)
                        draft.pop("usdt_amount", None)
                        draft.pop("live_rate", None)
                    PAYMENT_MGR.record_payment_draft(token, chat_id, draft, kind="promo")
                    self._sheet_drafts[token] = draft
                    if free:
                        job_id = self._enqueue_paid_draft(token, draft, chat_id)
                        PAYMENT_MGR.complete_payment_draft(token, job_id)
                        self._sheet_drafts.pop(token, None)
                        if hasattr(self, "_persist_sheet_drafts"):
                            self._persist_sheet_drafts()
                        self._show_panel(chat_id, views.Panel(
                            f"<b>🎉 تم تطبيق الكود {views.safe(code)}.</b>\nالفحص مجاني، وأُضيف الملف إلى قائمة الفحوص. ستصلك النتيجة بعد اكتمال الفحص.",
                            self._payment_success_rows(chat_id)), message_id)
                        return
                    from .config import show_crypto_payments
                    show_crypto = show_crypto_payments()
                    if show_crypto:
                        from .crypto_pay import calculate_egp_to_usdt
                        if "usdt_amount" not in draft:
                            usdt, rate = calculate_egp_to_usdt(draft["egp_amount"])
                            draft.update(usdt_amount=usdt, live_rate=rate)
                            PAYMENT_MGR.record_payment_draft(token, chat_id, draft, kind="promo")
                    if hasattr(self, "_persist_sheet_drafts"):
                        self._persist_sheet_drafts()
                    panel = views.payment_invoice_panel(
                        sheet_name=draft["sheet_name"], rows_count=draft["validation"]["rows"],
                        amount_usdt=draft.get("usdt_amount", 0.0), pay_url="", invoice_id=0,
                        token=token, live_rate=draft.get("live_rate", 0.0),
                        wallet_balance=PAYMENT_MGR.get_wallet_balance(chat_id),
                        account_id=PAYMENT_MGR.get_user_account_id(chat_id),
                        operation_id=draft.get("operation_id", ""), amount_egp=draft["egp_amount"],
                        show_crypto=show_crypto)
                    if show_crypto:
                        panel.rows.insert(0, [views.button("فاتورة CryptoBot بالمبلغ بعد الخصم", f"map:{token}:pay", "primary")])
                    self._show_panel(chat_id, panel, message_id)

                except Exception:
                    self._waiting_user_inputs[chat_id] = waiting
                    self._show_panel(chat_id, views.notice("الكود محفوظ لهذا الطلب", "لم يكتمل الطلب. أعد إدخال نفس الكود للمحاولة دون استهلاكه مرة أخرى أو تكرار الخصم."), message_id)

        elif action == "tx_id":
            method_name = waiting.get("method", "Transfer")
            target_key = waiting.get("target_key", "")
            amount_egp = draft.get("egp_amount", 30.0)
            amount_usdt = draft.get("usdt_amount", 0.57)
            op_id = draft.get("operation_id", "")

            # --- Automated Binance Pay Verification ---
            if "binance" in method_name.lower() or target_key == "binance":
                from .crypto_pay import BINANCE_PAY
                auto_ok, auto_msg, tx_info = BINANCE_PAY.verify_payment(text, min_amount=amount_usdt)
                if auto_ok:
                    acc_id = PAYMENT_MGR.get_user_account_id(chat_id)
                    op_line = f"🧾 رقم العملية: <code>#{op_id}</code>\n" if op_id else ""
                    actual_amt = (tx_info or {}).get("amount", amount_usdt)
                    if op_id:
                        PAYMENT_MGR.log_operation(
                            op_id,
                            op_type="binance_pay_auto",
                            user_id=chat_id,
                            details={
                                "method": "Binance Pay (Auto-verified)",
                                "tx_id": text.strip(),
                                "sheet_name": draft["sheet_name"],
                                "amount_egp": amount_egp,
                                "amount_usdt": float(actual_amt),
                                "status": "completed",
                            }
                        )
                    job_id = self._enqueue_paid_draft(token, draft, chat_id)
                    PAYMENT_MGR.mark_payment_fulfilled(token, job_id)
                    with self._ui_lock:
                        self._sheet_drafts.pop(token, None)
                    if hasattr(self, "_persist_sheet_drafts"):
                        self._persist_sheet_drafts()

                    success_panel = views.Panel(
                        f"<b>⚡ تم التحقق التلقائي من دفعتك عبر Binance Pay بنجاح!</b>\n\n"
                        f"🆔 رقم الحساب: <code>{acc_id}</code>\n"
                        f"{op_line}"
                        f"📁 الملف: <b>{views.safe(draft['sheet_name'])}</b>\n"
                        f"💵 الرسوم الأساسية: <b>{amount_egp:.2f} جنيه مصري</b>\n"
                        f"🪙 المبلغ المستلم في Binance: <b>{actual_amt} USDT</b>\n"
                        f"🔗 رقم العملية: <code>{views.safe(text.strip())}</code>\n\n"
                        f"✅ تمت إضافة الملف إلى قائمة الفحوص وجارٍ فحصه بالـ 9 عمال فوراً 🚀",
                        self._payment_success_rows(chat_id)
                    )
                    self._show_panel(chat_id, success_panel, message_id)

                    for aid in self.current_authorized_ids:
                        if aid != chat_id:
                            try:
                                self._api_call("sendMessage", {
                                    "chat_id": aid,
                                    "text": (
                                        f"⚡ <b>تم التحقق التلقائي من دفعة Binance Pay!</b>\n\n"
                                        f"العميل: <b>{views.safe(self._telegram_person_name(chat_id))}</b> (ID: <code>{acc_id}</code>)\n"
                                        f"📁 الملف: <b>{views.safe(draft['sheet_name'])}</b>\n"
                                        f"🪙 المبلغ: <b>{actual_amt} USDT</b> ({amount_egp:.2f} ج.م)\n"
                                        f"🔗 رقم العملية: <code>{views.safe(text.strip())}</code>\n"
                                        f"🚀 تمت إضافة الملف إلى قائمة الفحوص تلقائياً بدون تدخل بشري."
                                    ),
                                    "parse_mode": "HTML"
                                })
                            except Exception:
                                pass
                    return
                else:
                    self._show_panel(
                        chat_id,
                        views.notice(
                            "❌ لم يتم العثور على العملية في Binance Pay",
                            f"<b>{views.safe(auto_msg)}</b>\n\n"
                            f"🔍 الرقم المدخل: <code>{views.safe(text.strip())}</code>\n\n"
                            f"يرجى التأكد من إتمام التحويل من تطبيق Binance بالكامل وإرسال رقم العملية (Order ID أو TxID) الصحيح."
                        ),
                        message_id
                    )
                    self._waiting_user_inputs[chat_id] = waiting
                    return

            # --- Automated BEP20 Blockchain Verification ---
            elif target_key in ("bsc_usdt", "bsc_usdc", "bep20") or "bep20" in method_name.lower() or "bsc" in method_name.lower():
                from .crypto_pay import BSC_CHAIN
                auto_ok, auto_msg, actual_amt, contract = BSC_CHAIN.verify_transaction(text, min_amount=amount_usdt)
                if auto_ok:
                    acc_id = PAYMENT_MGR.get_user_account_id(chat_id)
                    op_line = f"🧾 رقم العملية: <code>#{op_id}</code>\n" if op_id else ""
                    if op_id:
                        PAYMENT_MGR.log_operation(
                            op_id,
                            op_type="bep20_pay_auto",
                            user_id=chat_id,
                            details={
                                "method": "USDC/USDT BEP20 (Auto-verified)",
                                "tx_id": text.strip(),
                                "sheet_name": draft["sheet_name"],
                                "amount_egp": amount_egp,
                                "amount_usdt": float(actual_amt or amount_usdt),
                                "contract": contract,
                                "status": "completed",
                            }
                        )
                    job_id = self._enqueue_paid_draft(token, draft, chat_id)
                    PAYMENT_MGR.mark_payment_fulfilled(token, job_id)
                    with self._ui_lock:
                        self._sheet_drafts.pop(token, None)
                    if hasattr(self, "_persist_sheet_drafts"):
                        self._persist_sheet_drafts()

                    success_panel = views.Panel(
                        f"<b>⚡ تم التحقق التلقائي من دفعتك عبر البلوكشين (BEP20) بنجاح!</b>\n\n"
                        f"🆔 رقم الحساب: <code>{acc_id}</code>\n"
                        f"{op_line}"
                        f"📁 الملف: <b>{views.safe(draft['sheet_name'])}</b>\n"
                        f"💵 الرسوم الأساسية: <b>{amount_egp:.2f} جنيه مصري</b>\n"
                        f"🪙 المبلغ المؤكد على BSC: <b>{actual_amt:.2f} USDC/USDT</b>\n"
                        f"🔗 هاش المعاملة: <code>{views.safe(text.strip())}</code>\n\n"
                        f"✅ تمت إضافة الملف إلى قائمة الفحوص وجارٍ فحصه بالـ 9 عمال فوراً 🚀",
                        self._payment_success_rows(chat_id)
                    )
                    self._show_panel(chat_id, success_panel, message_id)

                    for aid in self.current_authorized_ids:
                        if aid != chat_id:
                            try:
                                self._api_call("sendMessage", {
                                    "chat_id": aid,
                                    "text": (
                                        f"⚡ <b>تم التحقق التلقائي من دفعة BEP20 على البلوكشين!</b>\n\n"
                                        f"العميل: <b>{views.safe(self._telegram_person_name(chat_id))}</b> (ID: <code>{acc_id}</code>)\n"
                                        f"📁 الملف: <b>{views.safe(draft['sheet_name'])}</b>\n"
                                        f"🪙 المبلغ: <b>{actual_amt:.2f} USDC/USDT</b> ({amount_egp:.2f} ج.م)\n"
                                        f"🔗 TxHash: <code>{views.safe(text.strip())}</code>\n"
                                        f"🚀 تمت إضافة الملف إلى قائمة الفحوص تلقائياً بدون تدخل بشري."
                                    ),
                                    "parse_mode": "HTML"
                                })
                            except Exception:
                                pass
                    return
                else:
                    self._show_panel(
                        chat_id,
                        views.notice(
                            "❌ فشل التحقق على شبكة BSC البلوكشين",
                            f"<b>{views.safe(auto_msg)}</b>\n\n"
                            f"🔗 الهاش المدخل: <code>{views.safe(text.strip())}</code>\n\n"
                            f"تأكد من أن المعاملة مكتملة بنجاح (Status: Success) على شبكة BSC وأنها محولة إلى عنوان محفظتنا."
                        ),
                        message_id
                    )
                    self._waiting_user_inputs[chat_id] = waiting
                    return

            # --- Validation for manual networks (TON, SOL, ERC20) ---
            clean_val = text.strip()
            if len(clean_val) < 15:
                self._show_panel(
                    chat_id,
                    views.notice(
                        "⚠️ صيغة المعاملة غير صحيحة",
                        f"النص المدخل: <code>{views.safe(clean_val)}</code> قصير جداً ولا يطابق صيغة معاملات البلوكشين.\n\n"
                        f"يرجى نسخ كامل هاش المعاملة أو التوقيع من محفظتك والمحاولة مجدداً."
                    ),
                    message_id
                )
                self._waiting_user_inputs[chat_id] = waiting
                return

            PAYMENT_MGR.record_pending_payment(
                token=token,
                method=method_name,
                tx_id=text,
                user_id=chat_id,
                username=username,
                sheet_name=draft["sheet_name"],
                amount_usdt=amount_usdt,
                draft_info=dict(draft),
                operation_id=op_id,
                amount_egp=amount_egp,
            )
            op_line = f"🧾 رقم العملية: <code>#{op_id}</code>\n" if op_id else ""
            if "binance" in method_name.lower():
                auto_note = "\n<i>(لم يتم تأكيد الرقم آلياً في بايننس بعد؛ تم تحويل الطلب للمشرف للاعتماد الفوري)</i>"
            elif "bep20" in method_name.lower() or "bsc" in method_name.lower():
                auto_note = "\n<i>(لم يتم تأكيد الهاش آلياً على شبكة BSC بعد؛ تم تحويل الطلب للمشرف للاعتماد الفوري)</i>"
            else:
                auto_note = "\n<i>(تم إرسال إثبات التحويل للمشرف للاعتماد الفوري وتأكيد الملف)</i>"

            confirm_panel = views.Panel(
                f"<b>⏳ تم استلام إثبات التحويل بنجاح!</b>\n\n"
                f"{op_line}"
                f"📁 الملف: <b>{views.safe(draft['sheet_name'])}</b>\n"
                f"💳 طريقة الدفع: <b>{views.safe(method_name)}</b>\n"
                f"💵 الرسوم الأساسية: <b>{amount_egp:.2f} جنيه مصري</b>\n"
                f"🪙 المعادل المحوّل: <b>{amount_usdt:.2f} USDT/USDC</b>\n"
                f"🔗 رقم العملية (TxID): <code>{views.safe(text)}</code>\n"
                f"{auto_note}\n\n"
                f"يجري التحقق الآن وسيبدأ فحص الملف فوراً بمجرد التأكيد 🚀",
                [views.nav()]
            )
            self._show_panel(chat_id, confirm_panel, message_id)

            acc_id = PAYMENT_MGR.get_user_account_id(chat_id)
            admin_panel = views.admin_approval_panel(
                display_name=self._telegram_person_name(chat_id),
                user_id=str(chat_id),
                username=username,
                sheet_name=draft["sheet_name"],
                rows_count=draft["validation"]["rows"],
                method=method_name,
                tx_id=text,
                amount_usdt=amount_usdt,
                token=token,
                account_id=acc_id,
                operation_id=op_id,
                amount_egp=amount_egp,
            )
            for aid in self.current_authorized_ids:
                try:
                    self._api_call("sendMessage", {
                        "chat_id": aid,
                        "text": admin_panel.text,
                        "parse_mode": "HTML",
                        "reply_markup": {"inline_keyboard": admin_panel.rows}
                    })
                except Exception:
                    pass

    def _handle_document_upload(self, doc: dict[str, Any], chat_id: int) -> None:
        file_id = doc.get("file_id")
        original_name = str(doc.get("file_name") or "ملف_عملاء.xlsx")
        file_name = re.sub(r'[\\/<>:"|?*\x00-\x1f]', '_', original_name).strip(' .')[:160]
        if not file_name.lower().endswith('.xlsx'):
            file_name = 'ملف_عملاء.xlsx'
        if re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', file_name, re.IGNORECASE):
            file_name = 'upload_' + file_name
        max_bytes = 20 * 1024 * 1024
        try:
            if not file_id or int(doc.get('file_size', 0)) > max_bytes:
                self._reply(chat_id, '⚠️ حد التنزيل من Telegram هو 20 ميغابايت. أرسل ملف XLSX أصغر أو استخدم لوحة التحكم.')
                return
        except (TypeError, ValueError):
            self._reply(chat_id, '⚠️ بيانات الملف غير صالحة. أعد إرسال ملف XLSX.')
            return

        res = self._api_call("getFile", {"file_id": file_id})
        if not res or not res.get("ok"):
            self._reply(chat_id, f"⚠️ تعذر الحصول على ملف <b>{views.safe(file_name)}</b>. أعد إرساله أو ارفعه من لوحة التحكم.")
            return

        result = res.get('result')
        file_path_tg = result.get('file_path') if isinstance(result, dict) else None
        if (not isinstance(file_path_tg, str) or not file_path_tg or file_path_tg.startswith('/')
                or '\\' in file_path_tg or '..' in file_path_tg.split('/')
                or not re.fullmatch(r'[A-Za-z0-9_./-]+', file_path_tg)):
            self._reply(chat_id, '⚠️ لم يوفر Telegram رابط تنزيل صالحًا. أعد إرسال الملف.')
            return
        download_url = f"https://api.telegram.org/file/bot{self.bot_token}/{file_path_tg}"

        try:
            target_path = self.project_root / file_name
            if target_path.exists():
                target_path = target_path.with_name(f"{target_path.stem}_{uuid.uuid4().hex[:8]}.xlsx")
            file_bytes = self._download_file(download_url, max_bytes)
            if file_bytes is None:
                return
            if len(file_bytes) > max_bytes:
                self._reply(chat_id, '⚠️ الملف يتجاوز حد التنزيل البالغ 20 ميغابايت.')
                return
            if not file_bytes.startswith(b'PK\x03\x04'):
                self._reply(chat_id, '⚠️ محتوى الملف ليس XLSX صالحًا. افتحه واحفظ نسخة XLSX جديدة ثم أرسلها.')
                return
            with target_path.open('xb') as target_file:
                target_file.write(file_bytes)

            self._open_sheet_review(target_path, chat_id, telegram_document=doc)
        except Exception as exc:
            logger.warning('Telegram document processing failed (%s).', type(exc).__name__)
            self._reply(chat_id, "⚠ تعذر قراءة الملف أو إضافته إلى قائمة الفحوص. تأكد من صيغة XLSX وراجع لوحة التحكم.")

    def _download_file(self, url: str, max_bytes: int) -> Optional[bytes]:
        for attempt in range(3):
            if self._stop_event.is_set():
                return None
            try:
                resp = self._session.get(url, timeout=30, stream=True)
                if resp.status_code >= 500:
                    if attempt == 2:
                        return None
                    continue
                if resp.status_code != 200:
                    return None
                data = bytearray()
                for chunk in resp.iter_content(chunk_size=65536):
                    data.extend(chunk)
                    if len(data) > max_bytes + 1:
                        break
                return bytes(data)
            except Exception:
                if attempt == 2:
                    raise
            if self._stop_event.wait(2 ** attempt):
                return None
        return None

    @staticmethod
    def _positive_amount(value: str) -> float:
        amount = float(value)
        if not math.isfinite(amount) or amount <= 0 or round(amount, 2) <= 0:
            raise ValueError('A finite positive amount is required')
        return amount

    def _reply(self, chat_id: int, text: str) -> None:
        payload = {
            "chat_id": chat_id,
            "text": text.replace("**", "").replace("`", ""),
            "parse_mode": "HTML",
        }
        if chat_id > 0:
            is_admin_mode = (chat_id in self.current_authorized_ids and self._get_user_mode(chat_id) == "admin")
            from .button_layout import reply_keyboard
            payload["reply_markup"] = reply_keyboard(is_admin_mode, telegram_settings().get("button_icons", {}))
        self._api_call("sendMessage", payload)
