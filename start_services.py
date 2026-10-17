"""Smart Zain Checker - Multi-Process Service Orchestrator.
Spawns and supervises 4 isolated OS processes (files, browser, engine, telegram).
"""
from __future__ import annotations

import argparse
import io
import logging
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import threading
import time
from typing import Any

# Enforce UTF-8 on Windows Console and I/O to prevent UnicodeEncodeError crashes (User Global Rule)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")
elif hasattr(sys.stdout, "buffer") and getattr(sys.stdout, "encoding", "").lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("ServiceOrchestrator")

PROJECT_ROOT = Path(__file__).resolve().parent
SERVICES_DIR = PROJECT_ROOT / ".services"
LOGS_DIR = SERVICES_DIR / "logs"

PORTS = {
    "engine": 18701,
    "files": 18702,
    "browser": 18703,
    "telegram": 18704,
}


def get_or_create_ipc_token() -> str:
    """Retrieves or creates a durable 48-byte IPC token for cross-service authentication."""
    token = os.environ.get("ZAIN_IPC_TOKEN", "").strip()
    if len(token) >= 32:
        return token
    token_file = SERVICES_DIR / "ipc_token"
    if token_file.exists():
        try:
            token = token_file.read_text(encoding="utf-8").strip()
            if len(token) >= 32:
                return token
        except OSError:
            pass
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(48)
    token_file.write_text(token, encoding="utf-8")
    return token


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Smart Zain Checker - Multi-Process Orchestrator"
    )
    parser.add_argument("--host", default="127.0.0.1", help="Binding host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=5050, help="Web Dashboard port (default: 5050)")
    parser.add_argument("--bridge-port", type=int, default=8766, help="Chrome Extension Bridge port (default: 8766)")
    parser.add_argument("--no-telegram", action="store_true", help="Do not launch the Telegram service")
    parser.add_argument("--no-workers", action="store_true", help="Start without spawning browser worker actors")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # Ensure required directories exist
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    (SERVICES_DIR / "locks").mkdir(parents=True, exist_ok=True)

    ipc_token = get_or_create_ipc_token()
    os.environ["ZAIN_IPC_TOKEN"] = ipc_token

    environment = os.environ.copy()
    environment.update({
        "ZAIN_IPC_TOKEN": ipc_token,
        "ZAIN_PROJECT_ROOT": str(PROJECT_ROOT),
        "ZAIN_DASHBOARD_PORT": str(args.port),
        "ZAIN_BRIDGE_PORT": str(args.bridge_port),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    })
    if args.no_workers:
        environment["ZAIN_DISABLE_WORKERS"] = "1"

    roles = ["files", "browser", "engine"]
    if not args.no_telegram:
        roles.append("telegram")

    print("=" * 70)
    print("🛡️  SMART ZAIN CHECKER - 4-SERVICE MULTI-PROCESS ORCHESTRATOR")
    print("=" * 70)
    print(f"📁 Project Root : {PROJECT_ROOT}")
    print(f"🌐 Web Dashboard: http://{args.host}:{args.port}")
    print(f"🔌 Chrome Bridge: http://{args.host}:{args.bridge_port}")
    print("⚡ Architecture : 4 Independent OS Processes with Authenticated IPC")
    print("=" * 70)
    print("⏳ Launching and verifying services...")

    from services.ipc import RpcClient, ServiceUnavailable

    processes: dict[str, subprocess.Popen] = {}
    log_streams: dict[str, Any] = {}
    shutdown_requested = threading.Event()

    def shutdown_handler(signum: int = 0, frame: Any = None) -> None:
        if shutdown_requested.is_set():
            return
        shutdown_requested.set()
        print("\n🛑 Shutdown signal received. Performing graceful cleanup of all services...")
        # Graceful shutdown order: telegram first, then engine, then browser, then files
        for role in ("telegram", "engine", "browser", "files"):
            proc = processes.get(role)
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
        for role, proc in processes.items():
            if proc.poll() is None:
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        proc.kill()
                        proc.wait(timeout=3)
                    except Exception:
                        pass
        for stream in log_streams.values():
            try:
                stream.close()
            except Exception:
                pass
        print("✓ All isolated service processes safely terminated.")

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    try:
        for role in roles:
            log_path = LOGS_DIR / f"{role}.log"
            stream = log_path.open("ab")
            log_streams[role] = stream
            cmd = [sys.executable, "-B", "-m", "services", role, "--root", str(PROJECT_ROOT)]
            proc = subprocess.Popen(
                cmd,
                cwd=str(PROJECT_ROOT),
                env=environment,
                stdout=stream,
                stderr=subprocess.STDOUT,
            )
            processes[role] = proc

            client = RpcClient(role, root=PROJECT_ROOT)
            deadline = time.monotonic() + 35
            ready = False
            last_err = ""
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    log_tail = ""
                    try:
                        lines = log_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-10:]
                        log_tail = "\n".join(lines)
                    except Exception:
                        pass
                    raise RuntimeError(
                        f"Service '{role}' (PID: {proc.pid}) terminated with exit code {proc.returncode}:\n{log_tail}"
                    )
                try:
                    state = client.health(timeout=2)
                    if role != "telegram" or state.get("ready"):
                        ready = True
                        break
                except ServiceUnavailable as exc:
                    last_err = str(exc)
                except Exception as exc:
                    last_err = str(exc)
                time.sleep(0.2)

            if not ready:
                raise RuntimeError(
                    f"Service '{role}' (PID: {proc.pid}) did not become ready within 35s. Last error: {last_err}"
                )

            port = PORTS.get(role, "unknown")
            print(f"  • {role.upper():<10} -> PID: {proc.pid:<6} | IPC Port: {port:<5} [HEALTHY ✓]")

        print("-" * 70)
        print("✓ All 4 isolated services are actively running in separate OS processes!")
        print(f"🌐 Dashboard is ready: http://{args.host}:{args.port}")
        print("💡 Press Ctrl+C at any time to safely terminate all 4 processes.")
        print("=" * 70)

        # Supervision Watchdog loop
        while not shutdown_requested.wait(1.0):
            for role, proc in list(processes.items()):
                code = proc.poll()
                if code is not None:
                    logger.error(f"Service '{role}' (PID: {proc.pid}) exited unexpectedly with code {code}!")
                    log_path = LOGS_DIR / f"{role}.log"
                    if log_path.exists():
                        try:
                            lines = log_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-5:]
                            for line in lines:
                                logger.error(f"  [{role}] {line}")
                        except Exception:
                            pass
                    shutdown_handler(0, None)
                    break
    except KeyboardInterrupt:
        shutdown_handler(0, None)
    except Exception as exc:
        print(f"\n❌ Error starting services: {exc}")
        shutdown_handler(0, None)
        sys.exit(1)


if __name__ == "__main__":
    main()
