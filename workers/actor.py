"""Worker Actor Implementation for Smart Zain Checker.
Each worker is an independent Actor with its own Chrome process, proxy configuration,
error cooldown state machine, and statistics.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from workers.launcher import launch_worker_chrome, terminate_worker_process
from workers.proxy import verify_proxy_connectivity


def normalize_task_scope(val: Any) -> str:
    s = str(val or "").strip().lower()
    if s in ("account", "accounts", "1", "contract", "contracts"):
        return "account"
    if s in ("service", "services", "2", "wallet", "wallets"):
        return "service"
    return "both"


@dataclass
class WorkerActor:
    worker_id: str
    name: str
    profile_dir: Path
    extension_dir: Path
    use_proxy: bool = False
    proxy_url: Optional[str] = None
    target_url: str = "https://business.zain.sa/dashboard/quick-pay"
    use_direct_api: bool = True  # Headless Direct REST API mode (No Chrome browser)
    task_scope: str = "both"  # 'both', 'account' (starts with 1), 'service' (starts with 2)

    status: str = "idle"  # 'idle', 'launching', 'ready', 'processing', 'cooldown', 'stopped', 'error'
    status_reason: str = ""
    pid: Optional[int] = None
    cooldown_until: float = 0.0
    last_active_time: float = field(default_factory=time.time)
    current_task: Optional[dict[str, Any]] = None

    completed_count: int = 0
    match_count: int = 0
    mismatch_count: int = 0
    error_count: int = 0

    def __post_init__(self) -> None:
        self.task_scope = normalize_task_scope(self.task_scope)

    @property
    def can_check_accounts(self) -> bool:
        return self.task_scope in ("both", "account")

    @property
    def can_check_services(self) -> bool:
        return self.task_scope in ("both", "service")

    @property
    def ip_group(self) -> str:
        """Returns a string identifier for the worker's outbound IP network."""
        if self.use_proxy:
            from workers.service_transport import connection_group
            try:
                if not self.proxy_url or not str(self.proxy_url).strip():
                    raise ValueError("Missing proxy")
                return connection_group(self.proxy_url)
            except ValueError:
                return f"invalid_proxy_{self.worker_id}"
        return "local_direct"

    def launch(self) -> bool:
        """Initializes the worker (Direct API or dedicated Chrome browser)."""
        if self.is_alive():
            return True

        if self.use_direct_api:
            try:
                if self.use_proxy and self.proxy_url:
                    is_healthy, msg = verify_proxy_connectivity(self.proxy_url)
                    if not is_healthy:
                        self.status = "error"
                        self.status_reason = f"Proxy failed health check: {msg}"
                        return False

                self.status = "ready"
                self.status_reason = f"Direct Zain REST API Worker Ready ({'Proxy' if self.use_proxy else 'Direct'})."
                self.last_active_time = time.time()
                return True
            except Exception as exc:
                self.status = "error"
                self.status_reason = f"API Worker Init Failed: {exc}"
                return False

        self.status = "launching"
        self.status_reason = "Launching dedicated Chrome browser..."

        try:
            # If worker is configured to use proxy, check proxy health first
            effective_proxy = self.proxy_url if self.use_proxy else None
            if self.use_proxy and self.proxy_url:
                is_healthy, msg = verify_proxy_connectivity(self.proxy_url)
                if not is_healthy:
                    self.status = "error"
                    self.status_reason = f"Proxy failed health check: {msg}"
                    return False

            pid = launch_worker_chrome(
                worker_id=self.worker_id,
                profile_dir=self.profile_dir,
                extension_dir=self.extension_dir,
                target_url=self.target_url,
                proxy_server=effective_proxy,
            )
            self.pid = pid
            self.status = "ready"
            self.status_reason = "Chrome ready and connected to extension."
            self.last_active_time = time.time()
            return True
        except Exception as exc:
            self.status = "error"
            self.status_reason = f"Launch failed: {exc}"
            return False

    def terminate(self) -> bool:
        """Terminates ONLY this worker's process without affecting any other worker."""
        self.status = "stopped"
        self.status_reason = "Terminated by supervisor."
        if self.use_direct_api:
            self.current_task = None
            return True
        success = terminate_worker_process(self.pid, self.profile_dir)
        self.pid = None
        self.current_task = None
        return success

    def execute_task_api(self, contract_number: str) -> tuple[str, Optional[float], str]:
        """Queries the Zain Business REST API directly."""
        from workers.zain_api import query_contract_due_amount
        clean_num = str(contract_number).strip()
        # Service numbers (starting with 2) use stealth browser with proxy support.
        # Contracts (starting with 1) always use direct Business API for fast, zero-CAPTCHA inquiries.
        is_service = clean_num.startswith("2")
        if not self.can_check_accounts and not is_service:
            raise PermissionError(
                f"STRICT SCOPE GUARD: Worker '{self.worker_id}' is restricted to services only and is strictly forbidden from checking account numbers ({contract_number})!"
            )
        if not self.can_check_services and is_service:
            raise PermissionError(
                f"STRICT SCOPE GUARD: Worker '{self.worker_id}' is restricted to accounts only and is strictly forbidden from checking service numbers ({contract_number})!"
            )
        if self.use_proxy and is_service and (not self.proxy_url or not str(self.proxy_url).strip()):
            return "error", None, "Proxy worker requires a valid proxy URL"
        if self.use_proxy and is_service:
            from workers.service_transport import connection_group
            try:
                connection_group(self.proxy_url)
            except ValueError:
                return "error", None, "Proxy worker requires a valid proxy URL"
        effective_proxy = self.proxy_url if self.use_proxy else None
        res = query_contract_due_amount(contract_number, proxy_url=effective_proxy,
                                         on_verification=getattr(self, "on_verification", None))
        status, amount, msg = res
        if "TargetClosed" in str(msg) or "TargetClosed" in str(status):
            return "network_error", None, "أُغلقت جلسة المتصفح؛ يجري استعادتها (TargetClosedError)"
        return res

    def restart(self) -> bool:
        """Safely restarts this worker independently."""
        self.terminate()
        time.sleep(0.5)
        return self.launch()

    def set_cooldown(self, seconds: float, reason: str = "") -> None:
        """Puts ONLY this worker into cooldown (e.g. router IP block or rate-limit)."""
        if self.use_proxy:
            return
        self.cooldown_until = time.monotonic() + seconds
        self.status = "cooldown"
        self.status_reason = reason or f"Cooldown for {int(seconds)}s"

    def is_in_cooldown(self) -> bool:
        """Checks if this worker is currently in cooldown."""
        if self.use_proxy:
            return False
        if self.cooldown_until > time.monotonic():
            return True
        if self.status == "cooldown":
            self.status = "ready"
            self.status_reason = "Cooldown expired, ready for tasks."
        return False

    def is_ready_for_work(self) -> bool:
        """Checks if this worker is ready to receive verification tasks."""
        return not self.is_in_cooldown() and self.status != "stopped" and self.status != "error"

    def is_alive(self) -> bool:
        """Checks if this worker is currently active."""
        if self.use_direct_api:
            return self.status in ("ready", "processing")
        if not self.pid:
            return False
        import subprocess
        try:
            res = subprocess.run(
                ["tasklist", "/FI", f"PID eq {self.pid}", "/NH"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
            )
            return str(self.pid) in res.stdout
        except Exception:
            return False

    def assign_task(self, task: dict[str, Any]) -> None:
        """Assigns an account verification task to this worker."""
        search_num = str(task.get("search_number", "")).strip()
        is_service = search_num.startswith("2")
        if not self.can_check_accounts and not is_service:
            raise PermissionError(
                f"STRICT SCOPE GUARD: Worker '{self.worker_id}' cannot be assigned account {search_num}!"
            )
        if not self.can_check_services and is_service:
            raise PermissionError(
                f"STRICT SCOPE GUARD: Worker '{self.worker_id}' cannot be assigned service {search_num}!"
            )
        self.current_task = task
        self.status = "processing"
        self.status_reason = f"Checking row {task.get('row')}: {task.get('search_number')}"
        self.last_active_time = time.time()

    def record_outcome(self, outcome: str) -> None:
        """Records result metrics for this worker."""
        self.completed_count += 1
        self.last_active_time = time.time()
        self.current_task = None
        self.status = "ready"
        if outcome == "match":
            self.match_count += 1
            self.status = "ready"
        elif outcome == "mismatch":
            self.mismatch_count += 1
            self.status = "ready"
        elif outcome == "error":
            self.error_count += 1
            self.status = "ready"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.worker_id,
            "worker_id": self.worker_id,
            "name": self.name,
            "status": self.status,
            "status_reason": self.status_reason,
            "use_proxy": self.use_proxy,
            "proxy_configured": bool(self.proxy_url),
            "ip_group": self.ip_group,
            "pid": self.pid,
            "in_cooldown": self.is_in_cooldown(),
            "cooldown_remaining_seconds": max(0, int(self.cooldown_until - time.monotonic())) if not self.use_proxy else 0,
            "current_task": self.current_task,
            "completed": self.completed_count,
            "matches": self.match_count,
            "mismatches": self.mismatch_count,
            "errors": self.error_count,
            "task_scope": self.task_scope,
            "can_check_accounts": self.can_check_accounts,
            "can_check_services": self.can_check_services,
            "last_active": time.strftime("%H:%M:%S", time.localtime(self.last_active_time)),
        }
