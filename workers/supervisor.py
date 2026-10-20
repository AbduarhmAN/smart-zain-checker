"""Worker Supervisor for Smart Zain Checker.
Implements the Erlang/OTP-style One-for-One supervision strategy.
Guarantees absolute independence: failures in one worker NEVER affect the other!
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from workers.actor import WorkerActor

logger = logging.getLogger("WorkerSupervisor")


class WorkerSupervisor:
    """Oversees a pool of isolated WorkerActor instances using One-for-One supervision."""

    def __init__(
        self,
        extension_dir: Optional[Path] = None,
        base_profiles_dir: Optional[Path] = None,
        project_root: Optional[Path] = None,
        workers_config: Optional[List[dict[str, Any]]] = None,
        on_worker_status_change: Optional[Callable[[str, str], None]] = None,
        enable_proxy_workers: bool = True,
    ) -> None:
        root = project_root or Path.cwd()
        self.extension_dir = extension_dir or (root / "chrome_extension")
        self.base_profiles_dir = base_profiles_dir or root
        self.on_worker_status_change = on_worker_status_change
        self.lock = threading.Lock()
        self.workers: Dict[str, WorkerActor] = {}
        self._watchdog_thread: Optional[threading.Thread] = None
        self._stop_watchdog = threading.Event()

        # Load workers config from file or defaults
        cfgs = workers_config
        if not cfgs:
            cfg_file = root / "config_workers.json"
            if cfg_file.exists():
                try:
                    import json
                    with open(cfg_file, "r", encoding="utf-8") as f:
                        file_data = json.load(f)
                        cfgs = file_data.get("workers")
                except Exception as e:
                    logger.warning(f"Failed to read {cfg_file}: {e}")
            if not cfgs:
                cfgs = [
                    {
                        "worker_id": "worker_1",
                        "name": "Worker 1 (الراوتر)",
                        "use_proxy": False,
                        "proxy_url": None,
                    },
                    {
                        "worker_id": "worker_2",
                        "name": "Worker 2 (البروكسي)",
                        "use_proxy": True,
                        "proxy_url": "socks5://195.40.62.31:7252",
                    },
                ]

        for cfg in cfgs:
            uses_proxy = bool(cfg.get("use_proxy", bool(cfg.get("proxy_url") or cfg.get("proxy"))))
            if uses_proxy and not enable_proxy_workers:
                continue
            w_id = cfg["worker_id"]
            prof_dir = self.base_profiles_dir / f".zain-profile-{w_id}"
            actor = WorkerActor(
                worker_id=w_id,
                name=cfg.get("name", f"Worker {w_id}"),
                profile_dir=prof_dir,
                extension_dir=self.extension_dir,
                use_proxy=uses_proxy,
                proxy_url=cfg.get("proxy_url") or cfg.get("proxy"),
                task_scope=cfg.get("task_scope", "both"),
            )
            self.workers[w_id] = actor

    def register_worker(self, actor: WorkerActor) -> None:
        with self.lock:
            self.workers[actor.worker_id] = actor

    def get_worker(self, worker_id: str) -> Optional[WorkerActor]:
        with self.lock:
            return self.workers.get(worker_id)

    def get_all_workers(self) -> List[WorkerActor]:
        with self.lock:
            return list(self.workers.values())

    def get_available_worker(self) -> Optional[WorkerActor]:
        """Finds an idle or ready worker that is NOT in cooldown and NOT currently processing."""
        with self.lock:
            for actor in self.workers.values():
                if actor.status == "ready" and not actor.is_in_cooldown() and actor.current_task is None:
                    return actor
            return None

    def start_all(self, target_url: Optional[str] = None) -> None:
        """Launches all registered workers concurrently without cross-blocking."""
        self._stop_watchdog.clear()

        def _launch(actor: WorkerActor):
            if target_url:
                actor.target_url = target_url
            actor.launch()
            if self.on_worker_status_change:
                self.on_worker_status_change(actor.worker_id, actor.status)

        threads = []
        with self.lock:
            for actor in self.workers.values():
                t = threading.Thread(target=_launch, args=(actor,), daemon=True)
                threads.append(t)
                t.start()

        for t in threads:
            t.join(timeout=8.0)


        self._start_watchdog()

    def stop_all(self) -> None:
        """Terminates all workers cleanly."""
        self._stop_watchdog.set()
        with self.lock:
            for actor in self.workers.values():
                actor.terminate()
                if self.on_worker_status_change:
                    self.on_worker_status_change(actor.worker_id, "stopped")

    def restart_single_worker(self, worker_id: str) -> bool:
        """Restarts ONLY the specified worker. All other workers continue unaffected!"""
        actor = self.get_worker(worker_id)
        if not actor:
            return False
        logger.info(f"One-for-One Restarting {actor.name} independently...")
        success = actor.restart()
        if self.on_worker_status_change:
            self.on_worker_status_change(worker_id, actor.status)
        return success

    def pause_single_worker(self, worker_id: str, cooldown_seconds: float, reason: str = "") -> bool:
        """Puts ONLY the specified worker into cooldown. All other workers continue unaffected!"""
        actor = self.get_worker(worker_id)
        if not actor:
            return False
        logger.warning(f"One-for-One Pausing {actor.name} for {cooldown_seconds}s. Reason: {reason}")
        actor.set_cooldown(cooldown_seconds, reason)
        if self.on_worker_status_change:
            self.on_worker_status_change(worker_id, "cooldown")
        return True

    def _start_watchdog(self) -> None:
        """Starts the background supervisor watchdog loop."""
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            return
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self._watchdog_thread.start()

    def _watchdog_loop(self) -> None:
        """Supervises each worker independently (One-for-One failure recovery)."""
        while not self._stop_watchdog.is_set():
            time.sleep(3.0)
            with self.lock:
                workers_snapshot = list(self.workers.values())

            for actor in workers_snapshot:
                if self._stop_watchdog.is_set():
                    break

                # If actor should be running but its Chrome process died unexpectedly
                if actor.status in ("ready", "processing") and not actor.is_alive():
                    logger.warning(f"Detected crash for {actor.name}. Performing One-for-One recovery...")
                    actor.restart()
                    if self.on_worker_status_change:
                        self.on_worker_status_change(actor.worker_id, actor.status)

    def get_status_summary(self) -> List[dict[str, Any]]:
        with self.lock:
            return [actor.to_dict() for actor in self.workers.values()]
