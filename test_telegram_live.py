# -*- coding: utf-8 -*-
import sys
import io
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from zain_checker.telegram_controller import init_telegram_controller
from zain_checker.web_bridge import read_checkpoint_info

print("=" * 60)
print("🤖 Telegram Bot Listener is RUNNING for Abdurahman (@A_maghrbi)")
print("ID: 1085138908 | Bot: @ZainCheckerbot")
print("=" * 60)

ctrl = init_telegram_controller(get_stats_callback=read_checkpoint_info)

try:
    while True:
        time.sleep(2)
except KeyboardInterrupt:
    print("Stopped.")
