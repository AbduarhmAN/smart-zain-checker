"""Runtime controls and confirmed delivery for the local dashboard."""
from __future__ import annotations
import threading
import time
import sqlite3
from pathlib import Path
from .maintenance_access import active as maintenance_active

class TelegramIntegration:
    def __init__(self, orchestrator, queue_service, project_root: Path, *,
                 bot_token=None, chat_id=None, authorized_id=None, commerce_client=None):
        from telegram_bot.notifier import TelegramNotifier
        from telegram_bot.bot import TelegramBotRunner
        from telegram_bot.commerce.bridge import CommerceBridge
        from telegram_bot.commerce.archive import ArchiveDispatcher
        from telegram_bot.commerce.archive_transport import ArchiveTransport
        from manager.event_bus import EVENT_BUS
        self.orchestrator = orchestrator
        self.notifier = TelegramNotifier(bot_token=bot_token, chat_id=chat_id)
        self.commerce_bridge = CommerceBridge(project_root,event_bus=EVENT_BUS,queue_service=queue_service,
                                               client=commerce_client)
        options = {}
        if bot_token is not None:options['bot_token'] = bot_token
        if authorized_id is not None:options['authorized_id'] = authorized_id
        self.runner = TelegramBotRunner(orchestrator, queue_service, project_root, **options)
        if commerce_client is not None:self.runner._commerce_client = commerce_client
        self.archive_dispatcher = None
        self.archive_start_error = None
        if isinstance(self.notifier.bot_token,str) and self.notifier.bot_token:
            try:
                self.archive_dispatcher = ArchiveDispatcher(project_root,client=self.commerce_bridge.worker.client,
                    transport=ArchiveTransport(self.notifier.bot_token))
            except (OSError,sqlite3.Error,ValueError):self.archive_start_error='archive_initialization_unavailable'
        self.commerce_bridge.archive_dispatcher = self.archive_dispatcher
        self.runner.archive_dispatcher = self.archive_dispatcher
        self.notifier.commerce_bridge = self.commerce_bridge
        self.runner.commerce_bridge = self.commerce_bridge
        self.commerce_bridge.start()
        private_maintenance=maintenance_active(project_root)
        if self.archive_dispatcher is not None and not private_maintenance:self.archive_dispatcher.start()
        self.checkout_recovery = threading.Thread(target=self.runner._recover_commerce_checkouts,
            name='CommerceCheckoutRecovery',daemon=True)
        if not private_maintenance:self.checkout_recovery.start()
        self.lock = threading.RLock()
        self.connected = None
        self.username = ""
        self.checked_at = None

    def status(self):
        with self.lock:
            enabled = bool(self.orchestrator.telegram_enabled)
            runner_status=self.runner.status()
            if not isinstance(runner_status,dict):runner_status={}
            return {"status": "ok", "enabled": enabled,
                    "configured": bool(self.notifier.bot_token and self.notifier.chat_id and self.runner.authorized_id),
                    "connected": self.connected, "username": self.username,
                    "checked_at": self.checked_at,"polling":runner_status.get('polling',False),
                    "bot_health":runner_status,"delivery":self.notifier.delivery_status(),
                    "commerce":self.commerce_bridge.status(),
                    "archive":self.archive_dispatcher.status() if self.archive_dispatcher is not None else {'enabled':False,'last_error':self.archive_start_error}}

    def set_enabled(self, enabled: bool):
        with self.lock:
            if enabled == bool(self.orchestrator.telegram_enabled):
                health=self.runner.status() if enabled else None
                if not enabled or not isinstance(health,dict) or health.get('polling'):return
            if enabled:
                if not self.notifier.bot_token or not self.notifier.chat_id or not self.runner.authorized_id:
                    raise ValueError('Telegram local configuration is incomplete')
                try:
                    self.runner.start()
                    self.notifier.start_listening()
                    if not maintenance_active(self.runner.project_root):self.notifier.recover_commerce_deliveries()
                except Exception:
                    self.notifier.stop_listening();self.runner.stop();raise
            else:
                self.notifier.stop_listening()
                self.runner.stop()
            self.orchestrator.telegram_enabled = enabled

    def check_connection(self):
        result = self.notifier.check_connection()
        with self.lock:
            self.connected = result["connected"]
            self.username = result.get("username", "")
            self.checked_at = time.time()
            return result

    def stop(self):
        self.set_enabled(False)
        self.commerce_bridge.stop()
        if self.archive_dispatcher is not None:self.archive_dispatcher.stop()
