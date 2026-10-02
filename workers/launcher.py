"""Chrome Process Launcher for Smart Zain Checker Workers.
Provides safe, isolated process management for individual Chrome instances.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Standard Chrome executable locations on Windows
CHROME_CANDIDATE_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe"),
]


def find_chrome_executable() -> Optional[Path]:
    """Finds the installed Google Chrome executable on the system."""
    which_chrome = shutil.which("chrome") or shutil.which("google-chrome")
    if which_chrome:
        return Path(which_chrome)

    for p in CHROME_CANDIDATE_PATHS:
        path = Path(p)
        if path.is_file():
            return path
    return None


def clean_worker_extension_storage(profile_dir: Path) -> None:
    """Cleans extension local storage to ensure fresh session state."""
    storage_dir = profile_dir / "Default" / "Local Extension Settings"
    if storage_dir.exists():
        try:
            shutil.rmtree(storage_dir, ignore_errors=True)
        except Exception:
            pass


def launch_worker_chrome(
    worker_id: str,
    profile_dir: Path,
    extension_dir: Path,
    target_url: str = "https://business.zain.sa/dashboard/quick-pay",
    proxy_server: Optional[str] = None,
) -> Optional[int]:
    """Launches an isolated Chrome incognito instance for a specific worker.
    Returns the PID of the launched Chrome process.
    """
    chrome_exe = find_chrome_executable()
    if not chrome_exe:
        raise RuntimeError("Google Chrome executable not found on this system.")

    profile_dir.mkdir(parents=True, exist_ok=True)
    clean_worker_extension_storage(profile_dir)

    args = [
        str(chrome_exe),
        f"--user-data-dir={profile_dir.resolve()}",
        "--incognito",
        f"--load-extension={extension_dir.resolve()}",
        "--disable-first-run-ui",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-default-apps",
        "--disable-sync",
        "--disable-features=Translate,OptimizationHints,MediaRouter",
        "--disable-background-networking",
        "--disable-client-side-phishing-detection",
        "--disable-component-update",
    ]

    if proxy_server:
        args.append(f"--proxy-server={proxy_server.strip()}")

    args.append(target_url)

    # Launch isolated process without blocking
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

    proc = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        creationflags=creationflags,
    )

    pid = proc.pid
    pid_file = profile_dir / ".pid"
    try:
        pid_file.write_text(str(pid), encoding="utf-8")
    except Exception:
        pass

    return pid


def terminate_worker_process(pid: Optional[int], profile_dir: Optional[Path] = None) -> bool:
    """Terminates ONLY the specific worker's Chrome process using its PID.
    Does NOT affect any other worker or regular user Chrome window!
    """
    target_pid = pid
    if not target_pid and profile_dir:
        pid_file = profile_dir / ".pid"
        if pid_file.exists():
            try:
                target_pid = int(pid_file.read_text(encoding="utf-8").strip())
            except Exception:
                pass

    if not target_pid:
        return False

    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(target_pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            os.kill(target_pid, 9)
        time.sleep(0.3)
        return True
    except Exception:
        return False
