"""Runtime controls and confirmed delivery for the local dashboard."""
from __future__ import annotations
import threading
import time
from pathlib import Path

class TelegramIntegration:
    def __init__(self, orchestrator, queue_service, project_root: Path):
        from telegram_bot.notifier import TelegramNotifier
        from telegram_bot.bot import TelegramBotRunner
        self.orchestrator = orchestrator
        self.notifier = TelegramNotifier()
        self.runner = TelegramBotRunner(orchestrator, queue_service, project_root)
        self.lock = threading.RLock()
        self.connected = None
        self.username = ""
        self.checked_at = None

    def status(self):
        with self.lock:
            enabled = bool(self.orchestrator.telegram_enabled)
            return {"status": "ok", "enabled": enabled,
                    "configured": bool(self.notifier.bot_token and self.notifier.chat_id),
                    "connected": self.connected, "username": self.username,
                    "checked_at": self.checked_at}

    def set_enabled(self, enabled: bool):
        with self.lock:
            if enabled == bool(self.orchestrator.telegram_enabled):
                return
            if enabled:
                self.runner.start()
                self.notifier.start_listening()
            else:
                self.notifier.stop_listening()
                self.runner.stop()
            self.orchestrator.telegram_enabled = enabled

    def check_connection(self):
        with self.lock:
            result = self.notifier.check_connection()
            self.connected = result["connected"]
            self.username = result.get("username", "")
            self.checked_at = time.time()
            return result

    def stop(self):
        self.set_enabled(False)
