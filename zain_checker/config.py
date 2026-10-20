import sys
from pathlib import Path


import os

# BUNDLE_DIRECTORY: Where internal bundled assets (static web files, embedded extension) reside
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    BUNDLE_DIRECTORY = Path(sys._MEIPASS)
else:
    # In Nuitka onefile or development, __file__ points inside the bundle/source tree
    BUNDLE_DIRECTORY = Path(__file__).resolve().parent.parent

# PROJECT_DIRECTORY: Where the user's workspace, Excel files, and outputs reside
def _resolve_project_directory() -> Path:
    # Check sys.argv[0] directory first
    try:
        if sys.argv and sys.argv[0]:
            cand = Path(sys.argv[0]).resolve().parent
            if cand.name.lower() == "zain_checker":
                cand = cand.parent
            if cand.exists() and "onefile_" not in cand.name.lower() and "temp" not in str(cand).lower():
                return cand
    except Exception:
        pass
    cwd = Path.cwd()
    if cwd.name.lower() == "zain_checker":
        cwd = cwd.parent
    if "onefile_" not in cwd.name.lower() and "temp" not in str(cwd).lower():
        return cwd
    return Path.home() / "Desktop"

PROJECT_DIRECTORY = _resolve_project_directory()

ZAIN_BROWSER_PROFILE_DIRECTORY = PROJECT_DIRECTORY / ".zain-browser-profile"
ZAIN_CHECKER_PROFILE_DIRECTORY = PROJECT_DIRECTORY / ".zain-checker-profile"
ZAIN_CHECKER_PROXY_PROFILE_DIRECTORY = PROJECT_DIRECTORY / ".zain-checker-profile-proxy"
ZAIN_CHECKER_PROFILE_READY_PATH = PROJECT_DIRECTORY / ".zain-checker-profile-ready.json"
# Support standalone extension folder next to the .exe as a manual backup/override.
EXTERNAL_EXTENSION = PROJECT_DIRECTORY / "chrome_extension"
if EXTERNAL_EXTENSION.exists() and (EXTERNAL_EXTENSION / "manifest.json").exists():
    CHROME_EXTENSION_DIRECTORY = EXTERNAL_EXTENSION
else:
    CHROME_EXTENSION_DIRECTORY = BUNDLE_DIRECTORY / "chrome_extension"
BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 8766
BRIDGE_TOKEN = (
    "614e3c6720854e8cb09fa5bc41c609a1"
    "84cc75123924446aa306f08f0efa085b"
)
CHECKPOINT_PATH = PROJECT_DIRECTORY / ".zain-checkpoint.json"
WALLET_WORKSHEET_INDEX = 0
ACCOUNTS_WORKSHEET_INDEX = 1
HEADER_ROW_NUMBER = 1
CUSTOMER_NAME_COLUMN_NUMBER = 7
CONTRACT_COLUMN_NUMBER = 12
EXPECTED_AMOUNT_COLUMN_NUMBER = 15
COLLECTOR_COLUMN_NUMBER = 21
MAIN_STATUS_COLUMN_NUMBER = 25
SUB_STATUS_COLUMN_NUMBER = 26
SERVICE_NUMBER_COLUMN_NUMBER = 43
MISMATCH_SHEET_NAME = "الفروقات"
ERROR_SHEET_NAME = "الأخطاء"
RESULT_WORKBOOK_PATH = PROJECT_DIRECTORY / "نتائج فحص زين.xlsx"
ZAIN_CONTRACT_PAYMENT_URL = "https://business.zain.sa/dashboard/quick-pay"
ZAIN_QUICKPAY_URL = "https://app.sa.zain.com/ar/quickpay"
ZAIN_HOME_URL = "https://app.sa.zain.com/ar/home"

import os

PAGE_TIMEOUT_MS = 10_000
AMOUNT_TIMEOUT_MS = 10_000
# Delay after every successful Zain amount collection: 0.5 seconds (kept unchanged).
BETWEEN_CUSTOMERS_DELAY_SECONDS = max(
    0.5, float(os.environ.get("ZAIN_DELAY_SECONDS", "0.5"))
)
# Waiting duration remains completely unchanged across workers as requested by user.
BETWEEN_CUSTOMERS_PROXY_DELAY_SECONDS = BETWEEN_CUSTOMERS_DELAY_SECONDS
MISMATCH_RECHECK_DELAY_SECONDS = 5
REJECTION_RETRY_DELAY_SECONDS = 0  # Rejections wait for terminal confirmation.
# Cooldown duration when Zain soft-blocks wallet/quickpay queries with redirects (default 10 minutes)
WALLET_COOLDOWN_SECONDS = max(
    60, int(os.environ.get("ZAIN_WALLET_COOLDOWN_SECONDS", "600"))
)
ZAIN_PROXY_SERVER = os.environ.get("ZAIN_PROXY_SERVER", "socks5://166.0.39.3:7511")

import json
WORKERS_CONFIG_PATH = PROJECT_DIRECTORY / "config_workers.json"
WORKERS_CONFIG = []
try:
    if WORKERS_CONFIG_PATH.exists():
        with open(WORKERS_CONFIG_PATH, "r", encoding="utf-8") as f:
            _config_data = json.load(f)
            WORKERS_CONFIG = _config_data.get("workers", [])
except Exception as e:
    pass

# Fallback to internal bundled config if external not found
if not WORKERS_CONFIG:
    bundled_cfg = BUNDLE_DIRECTORY / "config_workers.json"
    if bundled_cfg.exists():
        try:
            with open(bundled_cfg, "r", encoding="utf-8") as f:
                _config_data = json.load(f)
                WORKERS_CONFIG = _config_data.get("workers", [])
        except Exception:
            pass

# Fallback to old dual-worker logic if config is empty or missing
if not WORKERS_CONFIG:
    WORKERS_CONFIG = [
        {"worker_id": "worker_1", "proxy": None},
        {"worker_id": "worker_2", "proxy": ZAIN_PROXY_SERVER}
    ]


