import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
import psutil

from zain_checker.config import (
    CHROME_EXTENSION_DIRECTORY,
    ZAIN_CHECKER_PROFILE_DIRECTORY,
    ZAIN_CHECKER_PROXY_PROFILE_DIRECTORY,
    ZAIN_CHECKER_PROFILE_READY_PATH,
)
from zain_checker.console import format_console_message, log

from zain_checker.config import PROJECT_DIRECTORY

def get_worker_profile_dir(worker_id: str | bool) -> Path:
    if isinstance(worker_id, bool):
        return ZAIN_CHECKER_PROXY_PROFILE_DIRECTORY if worker_id else ZAIN_CHECKER_PROFILE_DIRECTORY
    w_str = str(worker_id).lower()
    if "2" in w_str or "proxy" in w_str:
        return ZAIN_CHECKER_PROXY_PROFILE_DIRECTORY
    elif "1" in w_str or "router" in w_str:
        return ZAIN_CHECKER_PROFILE_DIRECTORY
    else:
        clean_id = "".join(c if c.isalnum() else "_" for c in str(worker_id)).strip("_")
        return PROJECT_DIRECTORY / f".zain-checker-profile-{clean_id}"


def is_cmdline_for_worker_profile(cmdline: list[str] | None, profile_dir: Path) -> bool:
    """Strictly matches a process cmdline to a specific worker profile.
    Prevents false positive substring matches where .zain-checker-profile
    falsely matched .zain-checker-profile-proxy."""
    if not cmdline:
        return False
    try:
        target_norm = os.path.normcase(os.path.normpath(str(profile_dir.resolve())))
        target_name = profile_dir.name.lower()
        for i, arg in enumerate(cmdline):
            arg_lower = arg.lower()
            if arg_lower.startswith("--user-data-dir="):
                val = arg.split("=", 1)[1].strip('"').strip("'")
                val_norm = os.path.normcase(os.path.normpath(val))
                if val_norm == target_norm or os.path.basename(val_norm) == target_name:
                    return True
            elif arg_lower == "--user-data-dir" and i + 1 < len(cmdline):
                val = cmdline[i + 1].strip('"').strip("'")
                val_norm = os.path.normcase(os.path.normpath(val))
                if val_norm == target_norm or os.path.basename(val_norm) == target_name:
                    return True
    except Exception:
        pass
    return False


def is_worker_chrome_running(worker_id: str | bool) -> bool:
    """Checks if a Chrome process is currently running for this specific worker's profile."""
    profile_dir = get_worker_profile_dir(worker_id)
    try:
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                if proc.info['name'] and 'chrome' in proc.info['name'].lower():
                    if is_cmdline_for_worker_profile(proc.info.get('cmdline'), profile_dir):
                        return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except Exception:
        pass
    return False


def clean_extension_storage(profile_dir: Path | None = None) -> None:
    """Cleans up ephemeral extension storage to prevent LevelDB block checksum
    corruption and purges stale network cookies that trigger Zain F5 ASM WAF blocks."""
    target_dir = profile_dir or ZAIN_CHECKER_PROFILE_DIRECTORY
    settings_root = (
        target_dir
        / "Default"
        / "Local Extension Settings"
    )
    if settings_root.exists():
        for item in settings_root.iterdir():
            if item.is_dir() and item.name != "ghbmnnjooekpmoecnnnilnnbdlolhkhi":
                try:
                    shutil.rmtree(item, ignore_errors=True)
                except Exception:
                    pass

    # Purge stale network cookies to ensure Zain F5 ASM never blocks the profile
    net_dir = target_dir / "Default" / "Network"
    if net_dir.exists():
        for f in net_dir.glob("Cookies*"):
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass


def terminate_worker_chrome_process(worker_id: str | bool) -> None:
    """Terminates Chrome processes for a specific worker only using native psutil.
    Guarantees 100% independence: never terminates any other worker processes."""
    target_procs = []
    profile_dir = get_worker_profile_dir(worker_id)
    
    try:
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                if proc.info['name'] and 'chrome' in proc.info['name'].lower():
                    if is_cmdline_for_worker_profile(proc.info.get('cmdline'), profile_dir):
                        target_procs.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        for p in target_procs:
            try:
                p.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        if target_procs:
            psutil.wait_procs(target_procs, timeout=3.0)
    except Exception:
        pass

    lock_file = profile_dir / "LOCK"
    if lock_file.exists():
        try:
            lock_file.unlink()
        except Exception:
            pass
    clean_extension_storage(profile_dir)



def terminate_checker_chrome_processes() -> None:
    """Terminates orphaned Chrome processes using the dedicated checker profiles.
    Safely ignores the user's regular personal Chrome browser."""
    target_procs = []
    try:
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                if proc.info['name'] and 'chrome' in proc.info['name'].lower():
                    cmd = " ".join(proc.info['cmdline'] or [])
                    if 'zain-checker-profile' in cmd:
                        target_procs.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        for p in target_procs:
            try:
                p.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        if target_procs:
            psutil.wait_procs(target_procs, timeout=3.0)
    except Exception:
        pass

    for prof in (ZAIN_CHECKER_PROFILE_DIRECTORY, ZAIN_CHECKER_PROXY_PROFILE_DIRECTORY):
        lock_file = prof / "LOCK"
        if lock_file.exists():
            try:
                lock_file.unlink()
            except Exception:
                pass
        clean_extension_storage(prof)


def mark_checker_chrome_profile_ready() -> None:
    """Writes the profile readiness confirmation file."""
    ZAIN_CHECKER_PROFILE_READY_PATH.parent.mkdir(parents=True, exist_ok=True)
    ZAIN_CHECKER_PROFILE_READY_PATH.write_text(
        json.dumps(
            {
                "profile": str(ZAIN_CHECKER_PROFILE_DIRECTORY),
                "extension": str(CHROME_EXTENSION_DIRECTORY),
                "confirmed_at": datetime.now().isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def ensure_checker_chrome_profile_ready(interactive: bool = True) -> None:
    """Ensures the Chrome profile is ready. Does not block on input in GUI/web mode."""
    if ZAIN_CHECKER_PROFILE_READY_PATH.is_file():
        return

    # In non-interactive mode (Web UI / GUI / redirected stdin), auto-mark as ready
    if not interactive or not sys.stdin.isatty():
        mark_checker_chrome_profile_ready()
        return

    from main import checker_chrome_arguments, find_chrome_executable

    chrome_executable = find_chrome_executable()
    log(
        "One-time checker Chrome setup is required. Opening the dedicated "
        "persistent Chrome profile at chrome://extensions."
    )
    subprocess.Popen(
        [chrome_executable, *checker_chrome_arguments(), "chrome://extensions"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    input(
        format_console_message(
            "In the dedicated Chrome window, confirm Zain Account and Wallet "
            "Checker is loaded, open Details, enable Allow in Incognito, then "
            "press Enter here: "
        )
    )
    mark_checker_chrome_profile_ready()


def ensure_worker_profile_ready(worker_id: str) -> None:
    """Ensures dynamic worker profiles have extension incognito permissions cloned from the main profile."""
    target_profile = get_worker_profile_dir(worker_id)
    if target_profile == ZAIN_CHECKER_PROFILE_DIRECTORY:
        return
        
    if ZAIN_CHECKER_PROFILE_DIRECTORY.exists():
        target_profile.mkdir(parents=True, exist_ok=True)
        # Copy root Local State so encryption/HMAC keys match
        src_ls = ZAIN_CHECKER_PROFILE_DIRECTORY / "Local State"
        dst_ls = target_profile / "Local State"
        if src_ls.exists():
            try:
                shutil.copy2(src_ls, dst_ls)
            except Exception:
                pass

        proxy_default = target_profile / "Default"
        proxy_default.mkdir(parents=True, exist_ok=True)
        for fname in ["Secure Preferences", "Preferences"]:
            src = ZAIN_CHECKER_PROFILE_DIRECTORY / "Default" / fname
            dst = proxy_default / fname
            if src.exists():
                try:
                    shutil.copy2(src, dst)
                except Exception:
                    pass

# Compatibility alias for legacy calls
ensure_proxy_profile_ready = ensure_worker_profile_ready

