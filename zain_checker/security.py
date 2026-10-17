# -*- coding: utf-8 -*-
"""
Hardware Lock & Anti-Tamper Security Module
Enforces hardware authorization using SHA-256 hashed multi-factor fingerprinting.
Clear-text hardware signatures are NEVER stored in source code.
"""

import os
import sys
import hashlib

try:
    import winreg
except ImportError:
    winreg = None

import ctypes

# Authorized SHA-256 signatures for THIS device (AM / Latitude E5570)
_AUTHORIZED_SIGNATURES = {
    # Full signature with BIOS
    "3a02da143124d6493ecb7082dd3741db9a94aa0fb066d81f7278ccbfb7e2ad6c",
    # Core signature
    "976b00e20d7b25f6b6cd68aee49518c4609252a85f4d7de0232cd0fbd4eca681"
}

def _check_anti_debug():
    """Detect if execution is running under a debugger or monitoring tool."""
    if sys.platform != "win32":
        return
    try:
        if hasattr(ctypes, "windll") and ctypes.windll.kernel32.IsDebuggerPresent():
            os._exit(1)
    except Exception:
        pass

def _get_reg_val(root, subkey, name):
    if not winreg:
        return ""
    try:
        with winreg.OpenKey(root, subkey) as k:
            val, _ = winreg.QueryValueEx(k, name)
            return str(val).strip().lower()
    except Exception:
        return ""

def _compute_hashes():
    comp = os.environ.get("COMPUTERNAME", "").strip().lower()
    mid = _get_reg_val(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\SQMClient", "MachineId")
    model = _get_reg_val(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\BIOS", "SystemProductName")
    bios = _get_reg_val(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\BIOS", "BIOSVersion")
    mfg = _get_reg_val(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\BIOS", "SystemManufacturer")
    cpu = _get_reg_val(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0", "ProcessorNameString")

    # Hash 1: Full
    raw_full = f"{comp}|{mid}|{model}|{bios}|{cpu}|{mfg}"
    h_full = hashlib.sha256(raw_full.encode("utf-8")).hexdigest()

    # Hash 2: Core
    raw_core = f"{comp}|{mid}|{model}|{cpu}|{mfg}"
    h_core = hashlib.sha256(raw_core.encode("utf-8")).hexdigest()

    return {h_full, h_core}

def enforce_hardware_lock(silent=False):
    """Enforces that the current runtime matches an authorized hardware signature."""
    if sys.platform != "win32":
        return True
    _check_anti_debug()

    current_hashes = _compute_hashes()
    
    # Check if any computed hash matches authorized signatures
    if not current_hashes.intersection(_AUTHORIZED_SIGNATURES):
        comp_name = os.environ.get("COMPUTERNAME", "UNKNOWN_PC")
        try:
            from zain_checker.telegram_controller import self_destruct
            if not silent:
                try:
                    ctypes.windll.user32.MessageBoxW(
                        0,
                        "The application was unable to start correctly (0xc000007b).\nClick OK to close the application.",
                        "Windows System Error",
                        0x10  # MB_ICONERROR
                    )
                except Exception:
                    pass
            self_destruct(f"محاولة تشغيل غير مصرح بها على جهاز غريب: {comp_name}")
        except Exception:
            pass
        os._exit(1)
