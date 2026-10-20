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
import threading
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
    proxy_options = parser.add_mutually_exclusive_group()
    proxy_options.add_argument("--no-proxy-workers", action="store_true", help="Disable configured proxy workers and use only direct workers")
    proxy_options.add_argument("--with-proxy-workers", action="store_true", help="Explicitly enable configured proxy workers (default: enabled)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    from server.runtime_guard import AppRuntimeGuard
    runtime_guard = AppRuntimeGuard(PROJECT_ROOT).acquire(args.host, (args.port, args.bridge_port))

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
    enable_proxy = not args.no_proxy_workers
    supervisor = WorkerSupervisor(project_root=PROJECT_ROOT, enable_proxy_workers=enable_proxy)
    logger.info("Worker Supervisor initialized (%s workers, proxy workers %s).",
                len(supervisor.get_all_workers()), "enabled" if enable_proxy else "disabled")

    # 2. Initialize Queue Service
    from manager.queue import QueueService
    queue_service = QueueService(storage_path=QUEUE_FILE)
    logger.info(f"Queue Service loaded ({len(queue_service.get_jobs())} jobs on disk).")

    # 3. Initialize Orchestrator Module
    from manager.scheduler import create_orchestrator as Orchestrator
    orchestrator = Orchestrator(
        supervisor=supervisor,
        queue_service=queue_service,
        project_root=PROJECT_ROOT,
    )
    logger.info("State Orchestrator initialized.")

    # 4. Initialize Telegram Bot Module (Decoupled EventBus Listener)
    tg_notifier = None
    tg_runner = None
    from telegram_bot.commerce.secret_store import load_service_key,CredentialUnavailable
    try:
        load_service_key()
        logger.info('Commerce local credential load verified.')
    except CredentialUnavailable as exc:
        logger.warning('Commerce local credential load failed: reason=%s windows=%s',exc.reason,exc.windows_code)
    except (RuntimeError,OSError,ValueError):
        logger.warning('Commerce local credential load failed: reason=credential_unavailable')
    from telegram_bot.integration import TelegramIntegration
    orchestrator.telegram_integration = TelegramIntegration(orchestrator, queue_service, PROJECT_ROOT)
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

    if not args.no_telegram:
        try:
            orchestrator.telegram_integration.set_enabled(True)
            tg_notifier = orchestrator.telegram_integration.notifier
            tg_runner = orchestrator.telegram_integration.runner
            logger.info("Telegram Bot & Event Subscriber active.")
        except Exception as exc:
            logger.warning(f"Telegram Bot failed to start: {exc}")
    else:
        logger.info("Telegram integration disabled via --no-telegram.")

    from manager.continuous_service import ContinuousQueueService
    continuous_service=ContinuousQueueService(orchestrator,queue_service,PROJECT_ROOT,
        orchestrator.telegram_integration.commerce_bridge.worker.client)
    orchestrator.continuous_service=continuous_service
    continuous_service.start()

    shutdown_requested = threading.Event()
    # Graceful Shutdown Handler
    def shutdown_handler(signum: int, frame: Any) -> None:
        if shutdown_requested.is_set():return
        shutdown_requested.set()
        print("\n🛑 Shutdown signal received. Performing graceful cleanup...")
        continuous_service.stop()
        orchestrator.cancel_session()
        gateway.stop()
        if tg_runner:
            tg_runner.stop()
        orchestrator.telegram_integration.stop()
        supervisor.stop_all()
        from workers.stealth_service_engine import stop_service_browsers
        stop_service_browsers()
        from workers.account_transport import close_account_sessions
        close_account_sessions()
        print("✓ All workers, servers, and processes safely terminated.")

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    try:
        while not shutdown_requested.wait(1):
            pass
    except KeyboardInterrupt:
        shutdown_handler(0, None)


if __name__ == "__main__":
    main()
