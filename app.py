"""Smart Zain Checker - Modular Monolith Unified Entrypoint.
Orchestrates Actor Workers, EventBus, QueueService, ServerGateway, and TelegramBot.
"""
from __future__ import annotations

import argparse
import io
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

# Enforce UTF-8 on Windows Console and I/O to prevent UnicodeEncodeError crashes
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# Setup Logging with UTF-8
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("ZainChecker")

PROJECT_ROOT = Path(__file__).resolve().parent
WEB_UI_DIR = PROJECT_ROOT / "web_ui"
QUEUE_FILE = PROJECT_ROOT / ".zain-queue.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Smart Zain Checker - High Performance Modular Monolith System"
    )
    parser.add_argument("--host", default="127.0.0.1", help="Binding host for servers (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=5050, help="Web Dashboard port (default: 5050)")
    parser.add_argument("--bridge-port", type=int, default=8766, help="Chrome Extension Bridge port (default: 8766)")
    parser.add_argument("--no-telegram", action="store_true", help="Disable Telegram bot and event notifier")
    parser.add_argument("--no-workers", action="store_true", help="Start gateway only without auto-spawning browser workers")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    print("=" * 65)
    print("🛡️  SMART ZAIN CHECKER - MODULAR MONOLITH ARCHITECTURE v3.0")
    print("=" * 65)
    print(f"📁 Project Root : {PROJECT_ROOT}")
    print(f"🌐 Web Dashboard: http://{args.host}:{args.port}")
    print(f"🔌 Chrome Bridge: http://{args.host}:{args.bridge_port}")
    print("💎 Core Rules   : Exact Halalas Financial Math (مبلغ العقد AW Priority)")
    print("⚡ Architecture : Actor Model (One-for-One Isolation) + EventBus")
    print("=" * 65)

    # 1. Initialize Workers Module (Actor Model & One-for-One Supervisor)
    from workers.supervisor import WorkerSupervisor
    supervisor = WorkerSupervisor(project_root=PROJECT_ROOT)
    logger.info("Worker Supervisor initialized (Worker 1: Direct, Worker 2: Proxy).")

    # 2. Initialize Queue Service
    from manager.queue import QueueService
    queue_service = QueueService(storage_path=QUEUE_FILE)
    logger.info(f"Queue Service loaded ({len(queue_service.get_jobs())} jobs on disk).")

    # 3. Initialize Orchestrator Module
    from manager.orchestrator import Orchestrator
    orchestrator = Orchestrator(
        supervisor=supervisor,
        queue_service=queue_service,
        project_root=PROJECT_ROOT,
    )
    logger.info("State Orchestrator initialized.")

    # 4. Initialize Telegram Bot Module (Decoupled EventBus Listener)
    tg_notifier = None
    tg_runner = None
    if not args.no_telegram:
        try:
            from telegram_bot.notifier import TelegramNotifier
            from telegram_bot.bot import TelegramBotRunner

            tg_notifier = TelegramNotifier()
            tg_notifier.start_listening()

            tg_runner = TelegramBotRunner(
                orchestrator=orchestrator,
                queue_service=queue_service,
                project_root=PROJECT_ROOT,
            )
            tg_runner.start()
            logger.info("Telegram Bot & Event Subscriber active.")
        except Exception as exc:
            logger.warning(f"Telegram Bot failed to start: {exc}")
    else:
        logger.info("Telegram integration disabled via --no-telegram.")

    # 5. Initialize Server Gateway (Ports 5050 & 8766)
    from server.gateway import ServerGateway
    gateway = ServerGateway(
        orchestrator=orchestrator,
        queue_service=queue_service,
        web_ui_dir=WEB_UI_DIR,
        project_root=PROJECT_ROOT,
        web_port=args.port,
        bridge_port=args.bridge_port,
        bind_host=args.host,
    )
    gateway.start()
    logger.info("Server Gateway online. Ready to accept connections.")

    # Graceful Shutdown Handler
    def shutdown_handler(signum: int, frame: Any) -> None:
        print("\n🛑 Shutdown signal received. Performing graceful cleanup...")
        orchestrator.cancel_session()
        gateway.stop()
        if tg_runner:
            tg_runner.stop()
        supervisor.stop_all()
        print("✓ All workers, servers, and processes safely terminated.")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        shutdown_handler(0, None)


if __name__ == "__main__":
    main()
