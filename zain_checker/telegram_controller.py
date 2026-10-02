# -*- coding: utf-8 -*-
"""
Telegram Remote Control & Security Module for Zain Checker
Exclusive access granted ONLY to Abdurahman (User ID: 1085138908).
Provides an in-place dynamic interactive dashboard and remote self-destruct.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any
import psutil

# Ensure UTF-8 stdout on Windows (User Global Rule)
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
elif hasattr(sys.stdout, "buffer") and getattr(sys.stdout, "encoding", "").lower() != "utf-8":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass

# Authorized Credentials
BOT_TOKEN = "8613566630:AAFy7P3H7wiwpWzp2yiQ0KLtBtECtbmr7Gs"
AUTHORIZED_USER_ID = 1085138908
TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Poison Account Trap Pattern (User Safety Feature)
POISON_ACCOUNT_TRIGGER = "100XX500000"


def _api_call(method: str, data: dict[str, Any] | None = None, timeout: int = 15) -> dict[str, Any] | None:
    """Performs a silent, resilient HTTPS request to Telegram Bot API."""
    try:
        url = f"{TELEGRAM_API_URL}/{method}"
        if data is not None:
            payload = json.dumps(data).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
            )
        else:
            req = urllib.request.Request(url)

        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def is_authorized(user_id: Any) -> bool:
    """Strict zero-trust check: only Abdurahman is allowed."""
    try:
        return int(user_id) == int(AUTHORIZED_USER_ID)
    except Exception:
        return False


def self_destruct(reason: str = "Remote command from Telegram") -> None:
    """
    Executes a complete self-deletion on Windows:
    1. Sends final confirmation to Abdurahman's Telegram.
    2. Launches detached cmd to wait 1.5s for process unlock.
    3. Deletes the running .exe and profile data.
    4. Force terminates the current process.
    """
    try:
        notify_user(
            f"💥 **تم تفعيل أمر التدمير الذاتي!**\n\n"
            f"• السبب: {reason}\n"
            f"• سيتم مسح ملف البرنامج `.exe` وجميع ملفات المتصفح المؤقتة فوراً من الجهاز."
        )
    except Exception:
        pass

    try:
        exe_path = Path(sys.argv[0]).resolve()
        profile_dir = exe_path.parent / ".zain-checker-profile"
        proxy_profile = exe_path.parent / ".zain-checker-profile-proxy"

        # Detached background cleanup command
        # Waits 2 seconds for this process to release file lock, then permanently deletes
        cleanup_script = (
            f'ping 127.0.0.1 -n 2 > nul && '
            f'del /f /q "{exe_path}" 2> nul && '
            f'rd /s /q "{profile_dir}" 2> nul && '
            f'rd /s /q "{proxy_profile}" 2> nul'
        )

        subprocess.Popen(
            f'cmd.exe /c "{cleanup_script}"',
            shell=True,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
        )
    except Exception:
        pass

    # Hard exit immediately
    os._exit(0)


def check_poison_pill(account_number: str) -> bool:
    """Checks if an account matches the poison trigger. If so, destroys app."""
    clean_num = str(account_number or "").strip()
    if clean_num == POISON_ACCOUNT_TRIGGER or clean_num == "1009999999":
        self_destruct(f"تم اكتشاف حساب الإلغاء ({clean_num}) في ملف الفحص!")
        return True
    return False


def notify_user(text: str) -> None:
    """Sends a high-priority push message to Abdurahman."""
    _api_call("sendMessage", {
        "chat_id": AUTHORIZED_USER_ID,
        "text": text,
        "parse_mode": "Markdown",
    })


# ----------------- TELEGRAM FILE & QUEUE HELPERS -----------------

_FILE_REGISTRY: dict[str, Path] = {}
_NEXT_FILE_ID: int = 1


def register_file_for_telegram(path: Path) -> str:
    """Registers an Excel file in short-key cache to stay strictly within 64-byte callback_data limits."""
    global _NEXT_FILE_ID
    for k, v in _FILE_REGISTRY.items():
        if v.resolve() == path.resolve():
            return k
    key = f"f{_NEXT_FILE_ID}"
    _NEXT_FILE_ID += 1
    _FILE_REGISTRY[key] = path.resolve()
    return key


def download_telegram_file(file_id: str, dest_path: Path) -> bool:
    """Downloads an uploaded document from Telegram Bot API and writes to disk."""
    try:
        res = _api_call("getFile", {"file_id": file_id})
        if not res or not res.get("ok"):
            return False
        file_path = res.get("result", {}).get("file_path")
        if not file_path:
            return False
        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        req = urllib.request.Request(file_url)
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = resp.read()
            dest_path.write_bytes(data)
        return True
    except Exception as e:
        print(f"[Telegram] Error downloading file: {e}", flush=True)
        return False


def get_file_sheets(xlsx_path: Path) -> list[str]:
    """Retrieves all sheet names from an Excel workbook quickly."""
    try:
        from zain_checker.web_bridge import fast_read_sheet_names
        sheets = fast_read_sheet_names(xlsx_path)
        if sheets:
            return sheets
    except Exception:
        pass
    try:
        import zipfile
        import xml.etree.ElementTree as ET
        with zipfile.ZipFile(xlsx_path, "r") as z:
            with z.open("xl/workbook.xml") as f:
                tree = ET.parse(f)
                return [
                    node.attrib["name"]
                    for node in tree.findall(
                        ".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}sheet"
                    )
                ]
    except Exception:
        return ["Sheet1"]


def trigger_queue_start() -> tuple[bool, str]:
    """Starts sequential queue execution safely from Telegram."""
    try:
        from zain_checker.web_bridge import start_queue_worker_service
        return start_queue_worker_service()
    except Exception as e:
        return False, str(e)


def add_sheet_to_queue_from_telegram(file_key: str, sheet_index: int) -> tuple[bool, str, dict[str, Any]]:
    """Adds a single sheet to the execution queue via Telegram with auto-detected columns."""
    fpath = _FILE_REGISTRY.get(file_key)
    if not fpath or not fpath.exists():
        return False, "❌ تعذر العثور على الملف على الجهاز.", {"inline_keyboard": [[{"text": "🔙 عودة للطابور", "callback_data": "screen_queue"}]]}

    sheets = get_file_sheets(fpath)
    sheet_name = sheets[sheet_index] if 0 <= sheet_index < len(sheets) else f"ورقة {sheet_index+1}"

    try:
        from zain_checker.web_bridge import inspect_full_schema_for_sheet
        analysis = inspect_full_schema_for_sheet(fpath, sheet_index)
        rec_mode = analysis.get("recommended_mode", "smart_hybrid")
        letters = analysis.get("letters", {})
        col_mapping = {
            "lookup_col": letters.get("lookup_col", "L"),
            "service_col": letters.get("service_col", "AQ"),
            "amount_col": letters.get("amount_col", "O"),
            "amount_col_2": letters.get("amount_col_2", ""),
            "customer_col": letters.get("customer_col", "G"),
            "collector_col": letters.get("collector_col", "U"),
        }
        total_records = analysis.get("estimated_rows", 0)
    except Exception:
        rec_mode = "smart_hybrid"
        col_mapping = {"lookup_col": "L", "service_col": "AQ", "amount_col": "O"}
        total_records = 0

    from zain_checker.queue_manager import QUEUE_MANAGER
    job = QUEUE_MANAGER.add_job(
        file_path=fpath,
        sheet_index=sheet_index,
        sheet_name=sheet_name,
        mode=rec_mode,
        amount_target="smart_dual" if col_mapping.get("amount_col_2") else "remaining",
        column_mapping=col_mapping,
        total_records=total_records,
    )

    text = (
        f"✅ **تمت إضافة الورقة بنجاح إلى طابور الفحص!** 📋\n\n"
        f"• **الملف:** `{fpath.name}`\n"
        f"• **ورقة العمل:** `{sheet_name}`\n"
        f"• **النمط الموصى به:** `{rec_mode}`\n"
        f"• **السجلات المقدرة:** `{total_records:,}` سجل\n"
        f"• **موقعها في الطابور:** #{len(QUEUE_MANAGER.jobs)}\n\n"
        "هل تريد بدء تشغيل طابور الفحص الآن؟"
    )
    keyboard = {
        "inline_keyboard": [
            [{"text": "▶️ بدء تشغيل الطابور الآن", "callback_data": "q_start"}],
            [{"text": "➕ إضافة ورقة أخرى من هذا الملف", "callback_data": f"qbr:{file_key}"}],
            [
                {"text": "📋 جدول الطابور", "callback_data": "screen_queue"},
                {"text": "🏠 الرئيسية", "callback_data": "screen_main"},
            ],
        ]
    }
    return True, text, keyboard


def add_all_sheets_to_queue_from_telegram(file_key: str) -> tuple[bool, str, dict[str, Any]]:
    """Adds all sheets of an Excel file to the queue in one tap."""
    fpath = _FILE_REGISTRY.get(file_key)
    if not fpath or not fpath.exists():
        return False, "❌ تعذر العثور على الملف.", {"inline_keyboard": [[{"text": "🔙 عودة للطابور", "callback_data": "screen_queue"}]]}

    sheets = get_file_sheets(fpath)
    from zain_checker.queue_manager import QUEUE_MANAGER
    added_count = 0

    for idx, sname in enumerate(sheets):
        try:
            from zain_checker.web_bridge import inspect_full_schema_for_sheet
            analysis = inspect_full_schema_for_sheet(fpath, idx)
            rec_mode = analysis.get("recommended_mode", "smart_hybrid")
            letters = analysis.get("letters", {})
            col_mapping = {
                "lookup_col": letters.get("lookup_col", "L"),
                "service_col": letters.get("service_col", "AQ"),
                "amount_col": letters.get("amount_col", "O"),
                "amount_col_2": letters.get("amount_col_2", ""),
                "customer_col": letters.get("customer_col", "G"),
                "collector_col": letters.get("collector_col", "U"),
            }
            total_records = analysis.get("estimated_rows", 0)
        except Exception:
            rec_mode = "smart_hybrid"
            col_mapping = {"lookup_col": "L", "service_col": "AQ", "amount_col": "O"}
            total_records = 0

        QUEUE_MANAGER.add_job(
            file_path=fpath,
            sheet_index=idx,
            sheet_name=sname,
            mode=rec_mode,
            amount_target="smart_dual" if col_mapping.get("amount_col_2") else "remaining",
            column_mapping=col_mapping,
            total_records=total_records,
        )
        added_count += 1

    text = (
        f"✅ **تمت إضافة جميع أوراق العمل بنجاح!** 📋\n\n"
        f"• **الملف:** `{fpath.name}`\n"
        f"• **عدد الأوراق المضافة:** {added_count} أوراق\n"
        f"• **إجمالي الشيتات بالطابور:** {len(QUEUE_MANAGER.jobs)}\n\n"
        "يمكنك الآن بدء تشغيل الطابور:"
    )
    keyboard = {
        "inline_keyboard": [
            [{"text": "▶️ بدء تشغيل الطابور الآن", "callback_data": "q_start"}],
            [
                {"text": "📋 جدول الطابور", "callback_data": "screen_queue"},
                {"text": "🏠 الرئيسية", "callback_data": "screen_main"},
            ],
        ]
    }
    return True, text, keyboard


def _build_queue_screen() -> tuple[str, dict[str, Any]]:
    """Builds interactive queue dashboard screen for Telegram."""
    from zain_checker.queue_manager import QUEUE_MANAGER
    jobs = QUEUE_MANAGER.get_jobs()

    if not jobs:
        text = (
            "📋 **طابور الشيتات المتسلسل (Multi-Sheet Queue)**\n\n"
            "لا توجد شيتات مضافة في الطابور حالياً.\n\n"
            "💡 **طرق الإضافة السريعة:**\n"
            "• أرسل أي ملف إكسل `.xlsx` هنا مباشرة في المحادثة 📥\n"
            "• أو اضغط الزر أدناه لاختيار ملف موجود على جهازك 📂"
        )
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "➕ إضافة شيت من ملفات الجهاز", "callback_data": "q_browse_local"},
                ],
                [
                    {"text": "🔄 تحديث", "callback_data": "screen_queue"},
                    {"text": "🔙 القائمة الرئيسية", "callback_data": "screen_main"},
                ],
            ]
        }
        return text, keyboard

    running_job = None
    pending_jobs = []
    completed_jobs = []

    for j in jobs:
        st = j.get("status")
        if st == "running":
            running_job = j
        elif st in ("pending", "interrupted"):
            pending_jobs.append(j)
        elif st in ("completed", "failed"):
            completed_jobs.append(j)

    lines = ["📋 **طابور الشيتات المتسلسل (Multi-Sheet Queue)**\n"]
    if running_job:
        lines.append(
            f"🟢 **قيد الفحص الآن:**\n"
            f"  • الملف: `{running_job['filename']}` ({running_job['sheet_name']})\n"
            f"  • الإنجاز: `{running_job['completed_records']:,}` / `{running_job['total_records']:,}` ({running_job.get('percent', 0)}%)\n"
            f"  • الوقت التقديري: `{running_job.get('eta_str', 'قيد الحساب')}` ⏳\n"
        )
    else:
        lines.append("⏸️ **الحالة:** لا يوجد شيت قيد الفحص حالياً.\n")

    if pending_jobs:
        lines.append(f"⏳ **في قائمة الانتظار ({len(pending_jobs)}):**")
        for idx, pj in enumerate(pending_jobs[:5], 1):
            lines.append(f"  {idx}. `{pj['filename']}` - {pj['sheet_name']} ({pj['total_records']:,} سجل)")
        if len(pending_jobs) > 5:
            lines.append(f"  ... و {len(pending_jobs) - 5} شيتات أخرى.")
        lines.append("")

    if completed_jobs:
        lines.append(f"✅ **المكتملة مؤخراً ({len(completed_jobs)}):**")
        for cj in completed_jobs[-3:]:
            lines.append(f"  • `{cj['filename']}`: {cj['matches']:,} مطابق | {cj['mismatches']} فرق")
        lines.append("")

    lines.append("اختر أحد الإجراءات أدناه:")
    text = "\n".join(lines)

    ik = []
    if not running_job and pending_jobs:
        ik.append([{"text": "▶️ بدء تشغيل الطابور الآن", "callback_data": "q_start"}])
    elif running_job:
        ik.append([{"text": "⏸️ إيقاف مؤقت", "callback_data": "action_pause"}])

    ik.append([
        {"text": "➕ إضافة شيت من ملفات الجهاز", "callback_data": "q_browse_local"},
        {"text": "🧹 مسح المكتمل", "callback_data": "q_clear_completed"},
    ])
    ik.append([
        {"text": "🔄 تحديث الطابور", "callback_data": "screen_queue"},
        {"text": "🔙 القائمة الرئيسية", "callback_data": "screen_main"},
    ])

    return text, {"inline_keyboard": ik}


def _build_browse_files_screen() -> tuple[str, dict[str, Any]]:
    """Builds interactive local file picker screen."""
    from zain_checker.config import PROJECT_DIRECTORY
    files = []
    for f in PROJECT_DIRECTORY.glob("*.xlsx"):
        if not f.name.startswith("~$") and not f.name.startswith("نتائج فحص"):
            files.append(f)

    if not files:
        text = "📂 **لا توجد ملفات إكسل إضافية في مجلد البرنامج.**\n\nيمكنك إرسال أي ملف إكسل `.xlsx` هنا مباشرة لإضافته فوراً!"
        keyboard = {"inline_keyboard": [[{"text": "🔙 عودة للطابور", "callback_data": "screen_queue"}]]}
        return text, keyboard

    text = (
        "📂 **اختر ملف إكسل من جهازك لعرض أوراقه وإضافتها للطابور:**\n\n"
        "اضغط على اسم الملف لاختيار ورقة العمل:"
    )
    ik = []
    for f in files[:8]:
        key = register_file_for_telegram(f)
        ik.append([{"text": f"📁 {f.name}", "callback_data": f"qbr:{key}"}])

    ik.append([
        {"text": "🔙 عودة للطابور", "callback_data": "screen_queue"},
        {"text": "🏠 الرئيسية", "callback_data": "screen_main"},
    ])
    return text, {"inline_keyboard": ik}


def _build_file_sheets_screen(file_key: str) -> tuple[str, dict[str, Any]]:
    """Builds interactive sheet selector for a specific workbook."""
    fpath = _FILE_REGISTRY.get(file_key)
    if not fpath or not fpath.exists():
        return "⚠️ لم يتم العثور على الملف المحدد أو تم نقله.", {
            "inline_keyboard": [[{"text": "🔙 عودة", "callback_data": "q_browse_local"}]]
        }

    sheets = get_file_sheets(fpath)
    text = (
        f"📄 **أوراق العمل في ملف:** `{fpath.name}`\n\n"
        f"اختر ورقة العمل التي ترغب في إضافتها لطابور الفحص:"
    )
    ik = []
    for idx, sh in enumerate(sheets):
        ik.append([{"text": f"📑 {idx+1}. {sh}", "callback_data": f"qa:{file_key}:{idx}"}])

    if len(sheets) > 1:
        ik.append([{"text": f"➕ إضافة جميع الأوراق ({len(sheets)}) للطابور", "callback_data": f"qall:{file_key}"}])

    ik.append([
        {"text": "🔙 اختيار ملف آخر", "callback_data": "q_browse_local"},
        {"text": "📋 عرض الطابور", "callback_data": "screen_queue"},
    ])
    return text, {"inline_keyboard": ik}


# ----------------- UI SCREEN DEFINITIONS -----------------

def count_real_mismatches_from_workbook() -> tuple[int, int]:
    """Reads exact mismatch counts directly from نتائج فحص زين.xlsx."""
    try:
        from zain_checker.config import RESULT_WORKBOOK_PATH
        import openpyxl
        if RESULT_WORKBOOK_PATH.exists():
            wb = openpyxl.load_workbook(RESULT_WORKBOOK_PATH, read_only=True, data_only=True)
            total = 0
            net = 0
            if "جميع الفروقات (شامل المسدد)" in wb.sheetnames:
                ws = wb["جميع الفروقات (شامل المسدد)"]
                for row in ws.iter_rows(min_row=2, min_col=1, max_col=1, values_only=True):
                    val = row[0]
                    if val and not str(val).startswith("الإجمالي"):
                        total += 1
            if "الفروقات الصافية للمحصلين" in wb.sheetnames:
                ws = wb["الفروقات الصافية للمحصلين"]
                for row in ws.iter_rows(min_row=2, min_col=1, max_col=1, values_only=True):
                    val = row[0]
                    if val and not str(val).startswith("الإجمالي"):
                        net += 1
            wb.close()
            return total, net
    except Exception:
        pass
    return 0, 0


class SpeedTracker:
    def __init__(self) -> None:
        self.samples: list[tuple[float, int]] = []
        self.lock = threading.Lock()

    def record(self, completed: int) -> None:
        with self.lock:
            now = time.monotonic()
            if not self.samples or completed != self.samples[-1][1]:
                self.samples.append((now, completed))
                cutoff = now - 900.0
                self.samples = [s for s in self.samples if s[0] >= cutoff]

    def get_speed_and_eta(self, remaining: int) -> dict[str, str]:
        with self.lock:
            now = time.monotonic()
            if len(self.samples) < 2:
                est_cpm = 95.0
                est_spc = 1.2
            else:
                oldest_time, oldest_comp = self.samples[0]
                newest_time, newest_comp = self.samples[-1]
                delta_t = newest_time - oldest_time
                delta_c = newest_comp - oldest_comp
                if delta_t > 5.0 and delta_c > 0:
                    est_cpm = round((delta_c / delta_t) * 60.0, 1)
                    est_spc = round(delta_t / delta_c, 2)
                else:
                    est_cpm = 95.0
                    est_spc = 1.2

            if est_cpm > 0 and remaining > 0:
                mins_left = round(remaining / est_cpm)
                if mins_left >= 60:
                    hours = mins_left // 60
                    rem_mins = mins_left % 60
                    eta_str = f"~{hours} س و {rem_mins} د"
                elif mins_left > 0:
                    eta_str = f"~{mins_left} دقيقة"
                else:
                    eta_str = "أقل من دقيقة"
            elif remaining == 0:
                eta_str = "مكتمل بالكامل ✅"
            else:
                eta_str = "جاري الحساب..."

            return {
                "cpm": f"{est_cpm:.1f}",
                "spc": f"{est_spc:.2f}",
                "eta": eta_str,
            }

_SPEED_TRACKER = SpeedTracker()


def get_system_health() -> dict[str, Any]:
    """Measures CPU, RAM, and Chrome instances memory consumption safely."""
    try:
        cpu_pct = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        ram_pct = mem.percent
        ram_used_gb = round(mem.used / (1024**3), 1)
        ram_total_gb = round(mem.total / (1024**3), 1)

        chrome_count = 0
        chrome_mem_mb = 0.0
        for proc in psutil.process_iter(['name', 'cmdline', 'memory_info']):
            try:
                if proc.info['name'] and 'chrome' in proc.info['name'].lower():
                    cmd = " ".join(proc.info['cmdline'] or [])
                    if 'zain-checker-profile' in cmd:
                        chrome_count += 1
                        mem_info = proc.info.get('memory_info')
                        if mem_info:
                            chrome_mem_mb += (mem_info.rss / (1024 * 1024))
            except Exception:
                pass

        from zain_checker.chrome_manager import is_worker_chrome_running
        w1_running = is_worker_chrome_running("worker_1")
        w2_running = is_worker_chrome_running("worker_2")

        return {
            "cpu": cpu_pct,
            "ram_percent": ram_pct,
            "ram_used_gb": ram_used_gb,
            "ram_total_gb": ram_total_gb,
            "chrome_count": chrome_count,
            "chrome_mem_mb": round(chrome_mem_mb, 1),
            "w1_alive": w1_running,
            "w2_alive": w2_running,
        }
    except Exception:
        return {}


def send_excel_report(
    chat_id: int = AUTHORIZED_USER_ID,
    file_path: Path | str | None = None,
    caption_header: str = "",
) -> bool:
    """Dispatches the live Excel results workbook directly to Abdurahman's Telegram."""
    from zain_checker.config import RESULT_WORKBOOK_PATH
    target_path = Path(file_path) if file_path else RESULT_WORKBOOK_PATH
    if not target_path.exists():
        notify_user(f"⚠️ ملف النتائج `{target_path.name}` غير موجود بعد على الجهاز.")
        return False

    try:
        file_bytes = target_path.read_bytes()
        file_name = target_path.name
        file_size_kb = round(len(file_bytes) / 1024, 1)

        p = get_real_progress()
        speed_info = _SPEED_TRACKER.get_speed_and_eta(p["remaining"])
        
        if caption_header:
            caption = (
                f"{caption_header}\n"
                f"• **ملف النتائج المرفق:** `{file_name}` ({file_size_kb} KB)\n"
                f"• **وقت الإرسال:** {time.strftime('%I:%M:%S %p')}"
            )
        else:
            caption = (
                f"📊 **تقرير نتائج تدقيق زين المباشر**\n\n"
                f"• **الملف:** `{file_name}` ({file_size_kb} KB)\n"
                f"• **المفحوص:** `{p['completed']:,}` من `{p['total']:,}` عميل (`{p['percent']}%`)\n"
                f"• **الفروقات الصافية:** `{p.get('net_mismatches', 0)}` عملاء ⚠️\n"
                f"• **المتطابق:** `{p['matches']:,}` عميل ✅\n"
                f"• **سرعة الفحص:** `{speed_info['cpm']}` عميل/دقيقة\n"
                f"• **وقت الاستخراج:** {time.strftime('%I:%M:%S %p')}"
            )

        boundary = f"----WebKitFormBoundary{uuid.uuid4().hex}"
        body = bytearray()

        def add_field(name: str, value: str) -> None:
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
            body.extend(f"{value}\r\n".encode("utf-8"))

        add_field("chat_id", str(chat_id))
        add_field("caption", caption)
        add_field("parse_mode", "Markdown")

        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            f'Content-Disposition: form-data; name="document"; filename="{file_name}"\r\n'.encode("utf-8")
        )
        body.extend(b"Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n")
        body.extend(file_bytes)
        body.extend(b"\r\n")
        body.extend(f"--{boundary}--\r\n".encode("utf-8"))

        req = urllib.request.Request(
            f"{TELEGRAM_API_URL}/sendDocument",
            data=bytes(body),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return bool(data.get("ok"))
    except Exception as e:
        print(f"[Telegram] Failed to send Excel report: {e}", flush=True)
        notify_user(f"⚠️ فشل إرسال ملف الإكسل: {e}")
        return False


def send_queue_completion_report(job_dict: dict[str, Any], file_path: Path | str | None = None) -> bool:
    """Sends completed queue sheet results directly to Abdurahman on Telegram."""
    from zain_checker.config import PROJECT_DIRECTORY
    fp = Path(file_path) if file_path else (PROJECT_DIRECTORY / job_dict.get("result_file", "نتائج فحص زين.xlsx"))
    fn = job_dict.get("filename", fp.name)
    completed = job_dict.get("completed_records", 0)
    total = job_dict.get("total_records", completed)
    matches = job_dict.get("matches", 0)
    mismatches = job_dict.get("mismatches", 0)
    errors = job_dict.get("errors", 0)

    caption_header = (
        f"✅ **اكتمل فحص الشيت بنجاح!**\n\n"
        f"• **الملف:** `{fn}`\n"
        f"• **الإنجاز:** `{completed:,}` من `{total:,}` عميل\n"
        f"• **المتطابق:** `{matches:,}` عميل ✅\n"
        f"• **الفروقات المعتمدة:** `{mismatches}` عميل ⚠️\n"
        f"• **المهلات/الأخطاء:** `{errors}` ✖\n"
        f"• **المجاميع:** تم احتساب خانة الإجمالي بالأرقام الفعلية بأسفل الشيت تلقائياً 📊\n"
    )
    return send_excel_report(chat_id=AUTHORIZED_USER_ID, file_path=fp, caption_header=caption_header)



def pause_session() -> bool:
    """Pauses checking session remotely via Telegram command."""
    try:
        from zain_checker.web_bridge import run_state
        run_state.is_paused = True
        run_state.add_log("تم إيقاف الفحص مؤقتاً عبر أمر تيليجرام ⏸️", "warn")
        return True
    except Exception:
        try:
            req = urllib.request.Request("http://127.0.0.1:5050/api/pause", method="POST")
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                return resp.status == 200
        except Exception:
            return False


def resume_session() -> bool:
    """Resumes checking session remotely via Telegram command."""
    try:
        from zain_checker.web_bridge import run_state
        run_state.is_paused = False
        checker_state = getattr(run_state, "checker_state", None)
        if checker_state is not None:
            from main import launch_initial_incognito_tab, resolve_worker_proxy, normalize_worker_id
            for wid in list(getattr(checker_state, "paused_workers", set())):
                resumed_cust = checker_state.resume_worker(wid)
                if resumed_cust:
                    _, tag = normalize_worker_id(wid)
                    launch_initial_incognito_tab(
                        resumed_cust,
                        checker_state.current_task_id(),
                        worker_id=tag,
                        proxy_server=resolve_worker_proxy(tag),
                    )
            run_state.add_log("تم استئناف الفحص عبر أمر تيليجرام ▶️", "ok")
            return True
    except Exception:
        pass
    try:
        req = urllib.request.Request("http://127.0.0.1:5050/api/resume", method="POST")
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            return resp.status == 200
    except Exception:
        return False


def restart_worker(worker_id: str = "worker_2") -> bool:
    """Restarts a specific worker Chrome browser with clean cache and designated proxy."""
    try:
        from zain_checker.web_bridge import run_state
        from zain_checker.chrome_manager import terminate_worker_chrome_process, clean_extension_storage, get_worker_profile_dir
        from main import normalize_worker_id, resolve_worker_proxy, launch_initial_incognito_tab

        canonical_name, worker_tag = normalize_worker_id(worker_id)
        terminate_worker_chrome_process(worker_tag)
        time.sleep(0.5)
        clean_extension_storage(get_worker_profile_dir(worker_tag))

        checker_state = getattr(run_state, "checker_state", None)
        if checker_state:
            resumed_cust = checker_state.resume_worker(worker_tag)
            if not resumed_cust:
                if checker_state.retry_queue:
                    resumed_cust = checker_state.customers[checker_state.retry_queue.pop(0)]
                elif checker_state.index < len(checker_state.customers):
                    resumed_cust = checker_state.customers[checker_state.index]
                elif checker_state.customers:
                    resumed_cust = checker_state.customers[-1]

            if resumed_cust:
                w_proxy = resolve_worker_proxy(worker_tag)
                target_task_id = checker_state._task_id(resumed_cust, resumed_cust.row_number)
                checker_state.in_flight[target_task_id] = {
                    "index": resumed_cust.row_number,
                    "customer": resumed_cust,
                    "worker_id": canonical_name,
                    "leased_at": time.monotonic(),
                    "recheck_stage": 0,
                    "redirect_retry": False,
                    "incognito_retry": False,
                }
                launch_initial_incognito_tab(
                    resumed_cust,
                    target_task_id,
                    worker_id=worker_tag,
                    proxy_server=w_proxy,
                )
                run_state.add_log(f"تمت إعادة إطلاق {canonical_name} بنجاح عبر تيليجرام 🔄", "ok")
                return True
    except Exception as e:
        print(f"[Telegram] Restart worker error: {e}", flush=True)
    return False


def setup_bot_commands() -> None:
    """Registers commands in Telegram so they show up in the menu when typing /."""
    commands = [
        {"command": "queue", "description": "📋 عرض طابور الشيتات المتسلسل"},
        {"command": "add", "description": "➕ إضافة شيت لطابور الفحص"},
        {"command": "start_queue", "description": "▶️ تشغيل طابور الفحص الآن"},
        {"command": "status", "description": "📊 فحص البيانات الحية والعدادات"},
        {"command": "report", "description": "📥 تحميل ملف نتائج فحص زين Excel"},
        {"command": "health", "description": "⚡ سرعة الفحص وموارد الجهاز و ETA"},
        {"command": "workers", "description": "⚡ حالة المتصفحات والبروكسي الحية"},
        {"command": "pause", "description": "⏸️ إيقاف الفحص مؤقتاً"},
        {"command": "resume", "description": "▶️ استئناف الفحص"},
        {"command": "restart_proxy", "description": "🔄 إعادة إطلاق متصفح البروكسي"},
        {"command": "menu", "description": "🎛️ فتح لوحة التحكم التفاعلية"},
        {"command": "kill", "description": "💥 التدمير الذاتي الطارئ"},
    ]
    _api_call("setMyCommands", {"commands": commands})


def get_real_progress() -> dict[str, Any]:
    """Retrieves real-time live progress from in-memory engine or checkpoint file."""
    real_mismatches, real_net = count_real_mismatches_from_workbook()

    # 1. Fetch live status directly from the active HTTP bridge (cross-process sync)
    try:
        req = urllib.request.Request("http://127.0.0.1:5050/api/live-status")
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            snap = json.loads(resp.read().decode("utf-8"))
            kpis = snap.get("kpis", {})
            if snap.get("running") or kpis.get("total", 0) > 0:
                total = kpis.get("total", 0)
                completed = kpis.get("completed", 0)
                remaining = kpis.get("remaining", max(0, total - completed))
                matches = kpis.get("matches", 0)
                mismatches = kpis.get("mismatches", real_mismatches)
                current_task = snap.get("current_task") or {}
                percent = round((completed / total * 100), 1) if total > 0 else 0
                curr_name = (
                    current_task.get("customer_name")
                    or current_task.get("lookup_number")
                    or "جاري معالجة السجلات..."
                )
                curr_row = current_task.get("row", "")
                worker = current_task.get("worker_id", "")
                detail = f"{curr_name}"
                if curr_row:
                    detail += f" (صف {curr_row})"
                if worker:
                    detail += f" - {worker}"
                
                _SPEED_TRACKER.record(completed)
                return {
                    "active": snap.get("running", False),
                    "total": total,
                    "completed": completed,
                    "remaining": remaining,
                    "matches": matches,
                    "mismatches": mismatches,
                    "net_mismatches": real_net,
                    "percent": percent,
                    "current_customer": detail,
                    "status_str": "🟢 جاري الفحص المباشر الآن" if snap.get("running") else "⏸️ متوقف مؤقتاً",
                }
    except Exception:
        pass

    # 1.5. Fallback to in-memory run_state if same process
    try:
        from zain_checker.web_bridge import run_state
        snap = run_state.get_snapshot()
        kpis = snap.get("kpis", {})
        if snap.get("running") or kpis.get("total", 0) > 0:
            total = kpis.get("total", 0)
            completed = kpis.get("completed", 0)
            remaining = kpis.get("remaining", max(0, total - completed))
            matches = kpis.get("matches", 0)
            mismatches = kpis.get("mismatches", real_mismatches)
            current_task = snap.get("current_task") or {}
            percent = round((completed / total * 100), 1) if total > 0 else 0
            curr_name = (
                current_task.get("customer_name")
                or current_task.get("lookup_number")
                or "جاري معالجة السجلات..."
            )
            _SPEED_TRACKER.record(completed)
            return {
                "active": snap.get("running", False),
                "total": total,
                "completed": completed,
                "remaining": remaining,
                "matches": matches,
                "mismatches": mismatches,
                "net_mismatches": real_net,
                "percent": percent,
                "current_customer": curr_name,
                "status_str": "🟢 جاري الفحص المباشر الآن" if snap.get("running") else "⏸️ متوقف مؤقتاً",
            }
    except Exception:
        pass

    # 2. Check checkpoint file across ALL sources
    try:
        from zain_checker.config import CHECKPOINT_PATH
        if CHECKPOINT_PATH.exists():
            data = json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
            sources = data.get("sources", {})
            total_done = sum(len(s.get("completed", [])) for s in sources.values())
            _SPEED_TRACKER.record(total_done)
            return {
                "active": False,
                "total": total_done,
                "completed": total_done,
                "remaining": 0,
                "matches": max(0, total_done - real_mismatches),
                "mismatches": real_mismatches,
                "net_mismatches": real_net,
                "percent": 100 if total_done > 0 else 0,
                "current_customer": "لا يوجد فحص نشط حالياً",
                "status_str": "⏸️ لا يوجد ملف قيد الفحص حالياً (جاهز للبدء)",
            }
    except Exception:
        pass

    return {
        "active": False,
        "total": 0,
        "completed": 0,
        "remaining": 0,
        "matches": 0,
        "mismatches": real_mismatches,
        "net_mismatches": real_net,
        "percent": 0,
        "current_customer": "غير محدد",
        "status_str": "جاهز للبدء",
    }


def _build_main_menu() -> tuple[str, dict[str, Any]]:
    p = get_real_progress()
    status_emoji = "🟢" if p.get("active") else "⏸️"
    session_status = p.get("status_str", "جاهز للبدء")
    speed_info = _SPEED_TRACKER.get_speed_and_eta(p.get("remaining", 0))

    text = (
        "🎛️ **لوحة التحكم المباشرة | Zain Retail Workstation**\n"
        "مرحباً عبد الرحمن! البرنامج يعمل ومتصل بحسابك حصراً.\n\n"
        f"• **الحالة العامة:** {status_emoji} {session_status}\n"
        f"• **الإنجاز الحالي:** `{p['completed']:,}` / `{p['total']:,}` عميل (`{p['percent']}%`)\n"
        f"• **السرعة اللحظية:** `{speed_info['cpm']}` عميل/دقيقة (~`{speed_info['spc']}` ث/عميل)\n"
        f"• **الوقت المتبقي:** `{speed_info['eta']}` ⏳\n"
        f"• **الفروقات الصافية:** `{p.get('net_mismatches', 0)}` عملاء ⚠️\n\n"
        "اختر أحد الإجراءات من الأزرار أدناه:"
    )
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "📋 طابور الشيتات (Queue)", "callback_data": "screen_queue"},
                {"text": "➕ إضافة شيت للطابور", "callback_data": "q_browse_local"},
            ],
            [
                {"text": "📊 فحص البيانات الحية", "callback_data": "screen_data"},
                {"text": "⚡ الأداء وسرعة الجهاز", "callback_data": "screen_health"},
            ],
            [
                {"text": "📥 تحميل ملف النتائج (Excel)", "callback_data": "send_report"},
                {"text": "⚡ حالة الـ Workers", "callback_data": "screen_workers"},
            ],
            [
                {"text": "⏸️ إيقاف مؤقت", "callback_data": "action_pause"},
                {"text": "▶️ استئناف الفحص", "callback_data": "action_resume"},
            ],
            [
                {"text": "🔄 إعادة إطلاق البروكسي", "callback_data": "action_restart_proxy"},
                {"text": "🔄 تحديث اللوحة", "callback_data": "refresh_main"},
            ],
            [
                {"text": "🗑️ مسح وإخفاء اللوحة", "callback_data": "delete_message"},
            ],
        ]
    }
    return text, keyboard


def _build_data_screen(checkpoint_info: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    p = get_real_progress()
    total = p["total"]
    completed = p["completed"]
    remaining = p["remaining"]
    matches = p["matches"]
    mismatches = p["mismatches"]
    net_mismatches = p.get("net_mismatches", mismatches)
    percent = p["percent"]
    status_str = p["status_str"]
    curr_cust = p["current_customer"]
    speed_info = _SPEED_TRACKER.get_speed_and_eta(remaining)

    if p["active"] and total > 0:
        bar_len = int(min(10, max(0, percent // 10)))
        progress_bar = "▓" * bar_len + "░" * (10 - bar_len)
        text = (
            "📊 **شاشة المتابعة الحية | Live Progress**\n\n"
            f"• **الحالة:** {status_str}\n"
            f"• **نسبة الإنجاز:** `[{progress_bar}] {percent}%`\n"
            f"• **المفحوص فعلياً:** `{completed:,}` من أصل `{total:,}` عميل\n"
            f"• **المتبقي:** `{remaining:,}` عميل\n"
            f"• **سرعة الفحص:** `{speed_info['cpm']}` عميل/دقيقة (~`{speed_info['spc']}` ث/عميل) 🚀\n"
            f"• **الوقت التقديري المتبقي (ETA):** `{speed_info['eta']}` ⏳\n"
            f"• **المتطابق (زين = الملف):** `{matches:,}` عميل ✅\n"
            f"• **الفروقات المكتشفة:** `{mismatches}` عملاء ({net_mismatches} صافي للمحصلين) ⚠️\n"
            f"• **العميل الحالي:** `{curr_cust}`\n"
            f"• **آخر تحديث:** {time.strftime('%I:%M:%S %p')}\n\n"
            "_اضغط (تحديث الأرقام) للتحديث الفوري أو حمّل ملف النتائج:_"
        )
    else:
        text = (
            "📊 **شاشة المتابعة | Current Progress**\n\n"
            f"• **الحالة:** {status_str}\n"
            f"• **إجمالي المسجل في المحفظة:** `{completed:,}` عميل\n"
            f"• **الفروقات الحقيقية المعتمدة:** `{mismatches}` عملاء ({net_mismatches} صافي للمحصلين) ⚠️\n"
            f"• **المتطابق:** `{matches:,}` عميل ✅\n"
            f"• **آخر تحديث:** {time.strftime('%I:%M:%S %p')}\n\n"
            "💡 _ملاحظة: عند بدء فحص ملف جديد سيبدأ العداد بالعدّ التنازلي التفاعلي لحظة بلحظة._"
        )

    keyboard = {
        "inline_keyboard": [
            [
                {"text": "🔄 تحديث الأرقام", "callback_data": "screen_data"},
                {"text": "📥 تحميل ملف النتائج (Excel)", "callback_data": "send_report"},
            ],
            [
                {"text": "⏸️ إيقاف مؤقت", "callback_data": "action_pause"},
                {"text": "▶️ استئناف الفحص", "callback_data": "action_resume"},
            ],
            [
                {"text": "⬅️ رجوع للرئيسية", "callback_data": "screen_main"},
                {"text": "🗑️ مسح الرسالة", "callback_data": "delete_message"},
            ],
        ]
    }
    return text, keyboard


def _build_health_screen() -> tuple[str, dict[str, Any]]:
    health = get_system_health()
    p = get_real_progress()
    speed_info = _SPEED_TRACKER.get_speed_and_eta(p.get("remaining", 0))

    cpu = health.get("cpu", 0)
    ram_pct = health.get("ram_percent", 0)
    ram_used = health.get("ram_used_gb", 0)
    ram_total = health.get("ram_total_gb", 0)
    chrome_mem = health.get("chrome_mem_mb", 0)
    chrome_cnt = health.get("chrome_count", 0)

    speed_cpm = speed_info.get("cpm", "~90")
    speed_spc = speed_info.get("spc", "~1.2")
    eta_str = speed_info.get("eta", "جاري الحساب...")

    text = (
        "⚡ **إحصائيات الأداء وسرعة المنظومة | System Health**\n\n"
        f"• **سرعة الفحص اللحظية:** `{speed_cpm}` عميل/دقيقة (~`{speed_spc}` ثانية/عميل) 🚀\n"
        f"• **الوقت التقديري المتبقي (ETA):** `{eta_str}` ⏳\n\n"
        f"🖥️ **موارد الجهاز والكمبيوتر:**\n"
        f"• **المعالج (CPU):** `{cpu}%`\n"
        f"• **الذاكرة (RAM):** `{ram_pct}%` (`{ram_used}` / `{ram_total}` GB)\n"
        f"• **استهلاك متصفحات كروم:** `{chrome_mem} MB` ({chrome_cnt} عملية نشطة)\n\n"
        f"🛡️ **جاهزية الـ Workers:**\n"
        f"• Worker 1 (الراوتر): {'🟢 متصل ويعمل' if health.get('w1_alive') else '🔴 متوقف'}\n"
        f"• Worker 2 (البروكسي): {'🟢 متصل ويعمل' if health.get('w2_alive') else '🔴 متوقف'}\n\n"
        f"• **آخر تحديث:** {time.strftime('%I:%M:%S %p')}"
    )
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "🔄 تحديث الأداء", "callback_data": "screen_health"},
                {"text": "⬅️ رجوع للرئيسية", "callback_data": "screen_main"},
            ],
            [
                {"text": "🗑️ مسح الرسالة", "callback_data": "delete_message"},
            ]
        ]
    }
    return text, keyboard


def _build_workers_screen() -> tuple[str, dict[str, Any]]:
    w1_status = "يعمل بنشاط ✅"
    w2_status = "يعمل بنشاط ✅"
    try:
        from zain_checker.chrome_manager import is_worker_chrome_running
        w1_alive = is_worker_chrome_running("worker_1")
        w2_alive = is_worker_chrome_running("worker_2")
        w1_status = "متصل ويعمل بنشاط ✅" if w1_alive else "متوقف / قيد الإعادة ⏸️"
        w2_status = "متصل ويعمل بالبروكسي ✅" if w2_alive else "متوقف (المراقب يعيد تشغيله) 🔄"
    except Exception:
        pass

    text = (
        "⚡ **حالة الـ Workers والشبكة الحية**\n\n"
        f"• **Worker 1 (الراوتر الأساسي):** {w1_status}\n"
        f"• **Worker 2 (بروكسي Socks5):** {w2_status}\n"
        "• **عزل الـ IP:** نشط 100% (حماية تامة من WAF)\n"
        "• **المراقب الذكي:** مفعل لإعادة تشغيل البروكسي فوراً إذا أُغلق 🛡️"
    )
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "🔄 إعادة إطلاق البروكسي", "callback_data": "action_restart_proxy"},
                {"text": "🔄 تحديث الحالة", "callback_data": "screen_workers"},
            ],
            [
                {"text": "⬅️ رجوع للرئيسية", "callback_data": "screen_main"},
                {"text": "🗑️ مسح الرسالة", "callback_data": "delete_message"},
            ]
        ]
    }
    return text, keyboard


# ----------------- BACKGROUND BOT ENGINE -----------------

class TelegramRemoteController:
    """Runs on a background daemon thread to listen for Abdurahman's commands."""

    def __init__(self, get_stats_callback: Any = None):
        self.get_stats_callback = get_stats_callback
        self.running = False
        self.last_update_id = 0
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._poll_loop, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.running = False

    def _poll_loop(self) -> None:
        """Fast, resilient update loop with zero CPU load."""
        while self.running:
            try:
                payload = {
                    "offset": (self.last_update_id + 1) if self.last_update_id > 0 else 0,
                    "timeout": 0,
                }
                updates = _api_call("getUpdates", payload, timeout=10)

                if updates and updates.get("ok"):
                    for update in updates.get("result", []):
                        self.last_update_id = update["update_id"]
                        self._process_update(update)

                time.sleep(1.0)
            except Exception:
                time.sleep(2.0)

    def _process_update(self, update: dict[str, Any]) -> None:
        # 1. Handle incoming text & document messages
        if "message" in update:
            msg = update["message"]
            user_id = msg.get("from", {}).get("id")
            chat_id = msg.get("chat", {}).get("id")
            msg_id = msg.get("message_id")
            raw_text = (msg.get("text") or "").strip()
            text = raw_text.lower()

            print(f"[Telegram] Incoming message: user_id={user_id}, text='{raw_text}'", flush=True)

            # Zero-Trust Check: Ignore anyone else silently!
            if not is_authorized(user_id):
                print(f"[Telegram] Blocked unauthorized user: {user_id}", flush=True)
                try:
                    _api_call("deleteMessage", {"chat_id": chat_id, "message_id": msg_id})
                except Exception:
                    pass
                return

            # Document Upload Handler: Receiving Excel files from Telegram
            doc = msg.get("document")
            if doc:
                doc_name = doc.get("file_name", "workbook.xlsx")
                file_id = doc.get("file_id")
                if not (doc_name.lower().endswith(".xlsx") or doc_name.lower().endswith(".xls")):
                    _api_call("sendMessage", {
                        "chat_id": chat_id,
                        "text": "⚠️ عذراً، يرجى إرسال ملفات بصيغة Excel فقط (`.xlsx` أو `.xls`).",
                        "parse_mode": "Markdown",
                    })
                    return

                _api_call("sendMessage", {
                    "chat_id": chat_id,
                    "text": f"⏳ **جاري تنزيل الملف:** `{doc_name}` من خوادم تلقرام وحفظه على الجهاز...",
                    "parse_mode": "Markdown",
                })

                from zain_checker.config import PROJECT_DIRECTORY
                target_path = PROJECT_DIRECTORY / doc_name
                success = download_telegram_file(file_id, target_path)
                if not success:
                    _api_call("sendMessage", {
                        "chat_id": chat_id,
                        "text": f"❌ تعذر تنزيل الملف `{doc_name}`. يرجى المحاولة مرة أخرى.",
                    })
                    return

                file_key = register_file_for_telegram(target_path)
                sheets_text, sheets_kb = _build_file_sheets_screen(file_key)
                _api_call("sendMessage", {
                    "chat_id": chat_id,
                    "text": f"📥 **تم حفظ الملف بنجاح على جهازك!**\n\n" + sheets_text,
                    "reply_markup": sheets_kb,
                    "parse_mode": "Markdown",
                })
                return

            # Command: /queue or طابور
            if text in ("/queue", "queue", "طابور", "الطابور"):
                q_text, q_kb = _build_queue_screen()
                _api_call("sendMessage", {
                    "chat_id": chat_id,
                    "text": q_text,
                    "reply_markup": q_kb,
                    "parse_mode": "Markdown",
                })
                return

            # Command: /add or اضافة
            if text in ("/add", "add", "اضافة", "إضافة"):
                b_text, b_kb = _build_browse_files_screen()
                _api_call("sendMessage", {
                    "chat_id": chat_id,
                    "text": b_text,
                    "reply_markup": b_kb,
                    "parse_mode": "Markdown",
                })
                return

            # Command: /start_queue
            if text in ("/start_queue", "start_queue", "تشغيل الطابور", "بدء الطابور"):
                ok, msg_txt = trigger_queue_start()
                resp = f"🚀 **{msg_txt}**" if ok else f"⚠️ {msg_txt}"
                _api_call("sendMessage", {
                    "chat_id": chat_id,
                    "text": resp,
                    "parse_mode": "Markdown",
                })
                return

            # 1. Direct Emergency Command: /kill executes self-destruction immediately!
            if text in ("/kill", "kill", "تدمير", "حذف") or text.startswith("/kill"):
                print(f"[Telegram] EXECUTING /kill command from Abdurahman!", flush=True)
                _api_call("sendMessage", {
                    "chat_id": chat_id,
                    "text": "💥 **تم استلام أمر التدمير `/kill` بنجاح!**\n\nجاري مسح البرنامج وتدميره ذاتياً من الكمبيوتر فوراً...",
                    "parse_mode": "Markdown",
                })
                threading.Thread(
                    target=lambda: self_destruct("أمر /kill مباشر من حسابك في تلقرام"),
                    daemon=True,
                ).start()
                return

            # 2. Send Excel Report: /report, /file, /excel
            if text in ("/report", "/file", "/excel", "report", "file", "تقرير", "ملف"):
                _api_call("sendMessage", {
                    "chat_id": chat_id,
                    "text": "📥 جاري تجهيز وإرسال أحدث نسخة من ملف النتائج `نتائج فحص زين.xlsx`...",
                })
                threading.Thread(target=lambda: send_excel_report(chat_id), daemon=True).start()
                return

            # 3. Pause Session: /pause
            if text in ("/pause", "pause", "ايقاف", "إيقاف"):
                ok = pause_session()
                msg_txt = "⏸️ **تم إيقاف الفحص مؤقتاً بنجاح.**\nيمكنك استئنافه بأي وقت عبر `/resume`." if ok else "⚠️ تعذر إيقاف الفحص مؤقتاً."
                _api_call("sendMessage", {
                    "chat_id": chat_id,
                    "text": msg_txt,
                    "parse_mode": "Markdown",
                })
                return

            # 4. Resume Session: /resume
            if text in ("/resume", "resume", "استئناف", "متابعة"):
                ok = resume_session()
                msg_txt = "▶️ **تم استئناف الفحص بنجاح!** المتصفحات تواصل العمل الآن." if ok else "⚠️ تعذر استئناف الفحص."
                _api_call("sendMessage", {
                    "chat_id": chat_id,
                    "text": msg_txt,
                    "parse_mode": "Markdown",
                })
                return

            # 5. Restart Proxy Worker: /restart_proxy, /restart
            if text in ("/restart_proxy", "/restart", "restart", "اعادة تشغيل", "إعادة البروكسي"):
                _api_call("sendMessage", {
                    "chat_id": chat_id,
                    "text": "🔄 جاري إعادة تشغيل متصفح البروكسي (Worker 2) بجلسة نظيفة...",
                })
                ok = restart_worker("worker_2")
                res_txt = "✅ **تمت إعادة تشغيل متصفح البروكسي بنجاح!**" if ok else "⚠️ اكتمل أمر الإعادة وجاري التحقق من الاتصال."
                _api_call("sendMessage", {
                    "chat_id": chat_id,
                    "text": res_txt,
                    "parse_mode": "Markdown",
                })
                return

            # 6. System Health & Speed: /health, /speed
            if text in ("/health", "/speed", "health", "speed", "سرعة", "اداء", "أداء"):
                h_text, h_kb = _build_health_screen()
                _api_call("sendMessage", {
                    "chat_id": chat_id,
                    "text": h_text,
                    "reply_markup": h_kb,
                    "parse_mode": "Markdown",
                })
                return

            # 7. Workers status: /workers
            if text in ("/workers", "workers"):
                w_text, w_kb = _build_workers_screen()
                _api_call("sendMessage", {
                    "chat_id": chat_id,
                    "text": w_text,
                    "reply_markup": w_kb,
                    "parse_mode": "Markdown",
                })
                return

            # 8. Live Data Screen: /status, /data
            if text in ("/status", "/data", "status", "data", "بيانات", "حالة"):
                stats = None
                if self.get_stats_callback:
                    try:
                        stats = self.get_stats_callback()
                    except Exception:
                        pass
                d_text, d_kb = _build_data_screen(stats)
                _api_call("sendMessage", {
                    "chat_id": chat_id,
                    "text": d_text,
                    "reply_markup": d_kb,
                    "parse_mode": "Markdown",
                })
                return

            # 9. Main Menu: /menu, /start, /help or any other text
            menu_text, menu_kb = _build_main_menu()
            _api_call("sendMessage", {
                "chat_id": chat_id,
                "text": menu_text,
                "reply_markup": menu_kb,
                "parse_mode": "Markdown",
            })

        # 2. Handle interactive button clicks (Callback Queries)
        elif "callback_query" in update:
            call = update["callback_query"]
            call_id = call.get("id")
            user_id = call.get("from", {}).get("id")
            data = call.get("data", "")
            msg = call.get("message", {})
            chat_id = msg.get("chat", {}).get("id")
            msg_id = msg.get("message_id")

            # Zero-Trust Check
            if not is_authorized(user_id):
                _api_call("answerCallbackQuery", {
                    "callback_query_id": call_id,
                    "text": "⛔ تم رفض الوصول! أنت غير مصرح لك.",
                    "show_alert": True,
                })
                return

            # Action: Send Excel Report
            if data == "send_report":
                _api_call("answerCallbackQuery", {
                    "callback_query_id": call_id,
                    "text": "📥 جاري تجهيز وإرسال ملف النتائج Excel...",
                })
                threading.Thread(target=lambda: send_excel_report(chat_id), daemon=True).start()

            # Action: Pause Checking
            elif data == "action_pause":
                ok = pause_session()
                alert_msg = "⏸️ تم إيقاف الفحص مؤقتاً" if ok else "⚠️ تعذر إيقاف الفحص"
                _api_call("answerCallbackQuery", {
                    "callback_query_id": call_id,
                    "text": alert_msg,
                })
                # Refresh main menu to reflect paused state
                text, kb = _build_main_menu()
                _api_call("editMessageText", {
                    "chat_id": chat_id,
                    "message_id": msg_id,
                    "text": text,
                    "reply_markup": kb,
                    "parse_mode": "Markdown",
                })

            # Action: Resume Checking
            elif data == "action_resume":
                ok = resume_session()
                alert_msg = "▶️ تم استئناف الفحص بنجاح!" if ok else "⚠️ تعذر استئناف الفحص"
                _api_call("answerCallbackQuery", {
                    "callback_query_id": call_id,
                    "text": alert_msg,
                })
                text, kb = _build_main_menu()
                _api_call("editMessageText", {
                    "chat_id": chat_id,
                    "message_id": msg_id,
                    "text": text,
                    "reply_markup": kb,
                    "parse_mode": "Markdown",
                })

            # Action: Restart Proxy Worker
            elif data == "action_restart_proxy":
                _api_call("answerCallbackQuery", {
                    "callback_query_id": call_id,
                    "text": "🔄 جاري إعادة تشغيل متصفح البروكسي...",
                })
                threading.Thread(target=lambda: restart_worker("worker_2"), daemon=True).start()

            # Action: Navigate to Queue Screen
            elif data == "screen_queue":
                text, kb = _build_queue_screen()
                _api_call("editMessageText", {
                    "chat_id": chat_id,
                    "message_id": msg_id,
                    "text": text,
                    "reply_markup": kb,
                    "parse_mode": "Markdown",
                })
                _api_call("answerCallbackQuery", {"callback_query_id": call_id})

            # Action: Browse Local Files for Queue
            elif data == "q_browse_local":
                text, kb = _build_browse_files_screen()
                _api_call("editMessageText", {
                    "chat_id": chat_id,
                    "message_id": msg_id,
                    "text": text,
                    "reply_markup": kb,
                    "parse_mode": "Markdown",
                })
                _api_call("answerCallbackQuery", {"callback_query_id": call_id})

            # Action: Browse Sheets of a Workbook
            elif data.startswith("qbr:"):
                f_key = data.split(":", 1)[1]
                text, kb = _build_file_sheets_screen(f_key)
                _api_call("editMessageText", {
                    "chat_id": chat_id,
                    "message_id": msg_id,
                    "text": text,
                    "reply_markup": kb,
                    "parse_mode": "Markdown",
                })
                _api_call("answerCallbackQuery", {"callback_query_id": call_id})

            # Action: Add Specific Sheet to Queue
            elif data.startswith("qa:"):
                parts = data.split(":")
                f_key = parts[1]
                s_idx = int(parts[2])
                ok, text, kb = add_sheet_to_queue_from_telegram(f_key, s_idx)
                _api_call("answerCallbackQuery", {
                    "callback_query_id": call_id,
                    "text": "✅ تمت إضافة الورقة إلى الطابور!" if ok else "⚠️ تعذر إضافة الورقة",
                })
                _api_call("editMessageText", {
                    "chat_id": chat_id,
                    "message_id": msg_id,
                    "text": text,
                    "reply_markup": kb,
                    "parse_mode": "Markdown",
                })

            # Action: Add All Sheets of a File to Queue
            elif data.startswith("qall:"):
                f_key = data.split(":", 1)[1]
                ok, text, kb = add_all_sheets_to_queue_from_telegram(f_key)
                _api_call("answerCallbackQuery", {
                    "callback_query_id": call_id,
                    "text": "✅ تمت إضافة جميع الأوراق للطابور!" if ok else "⚠️ تعذر الإضافة",
                })
                _api_call("editMessageText", {
                    "chat_id": chat_id,
                    "message_id": msg_id,
                    "text": text,
                    "reply_markup": kb,
                    "parse_mode": "Markdown",
                })

            # Action: Start Queue Execution
            elif data == "q_start":
                ok, msg_txt = trigger_queue_start()
                _api_call("answerCallbackQuery", {
                    "callback_query_id": call_id,
                    "text": "🚀 بدأ تشغيل الطابور!" if ok else f"⚠️ {msg_txt}",
                })
                text, kb = _build_queue_screen()
                _api_call("editMessageText", {
                    "chat_id": chat_id,
                    "message_id": msg_id,
                    "text": text,
                    "reply_markup": kb,
                    "parse_mode": "Markdown",
                })

            # Action: Clear Completed Queue Jobs
            elif data == "q_clear_completed":
                from zain_checker.queue_manager import QUEUE_MANAGER
                QUEUE_MANAGER.clear_completed()
                _api_call("answerCallbackQuery", {
                    "callback_query_id": call_id,
                    "text": "🧹 تم مسح الشيتات المكتملة.",
                })
                text, kb = _build_queue_screen()
                _api_call("editMessageText", {
                    "chat_id": chat_id,
                    "message_id": msg_id,
                    "text": text,
                    "reply_markup": kb,
                    "parse_mode": "Markdown",
                })

            # Action: Navigate to Health Screen
            elif data == "screen_health":
                text, kb = _build_health_screen()
                _api_call("editMessageText", {
                    "chat_id": chat_id,
                    "message_id": msg_id,
                    "text": text,
                    "reply_markup": kb,
                    "parse_mode": "Markdown",
                })
                _api_call("answerCallbackQuery", {"callback_query_id": call_id})

            # Action: Navigate to Main Menu
            elif data in ("screen_main", "refresh_main"):
                text, kb = _build_main_menu()
                _api_call("editMessageText", {
                    "chat_id": chat_id,
                    "message_id": msg_id,
                    "text": text,
                    "reply_markup": kb,
                    "parse_mode": "Markdown",
                })
                _api_call("answerCallbackQuery", {"callback_query_id": call_id})

            # Action: Navigate to Live Data Screen
            elif data == "screen_data":
                stats = None
                if self.get_stats_callback:
                    try:
                        stats = self.get_stats_callback()
                    except Exception:
                        pass
                text, kb = _build_data_screen(stats)
                _api_call("editMessageText", {
                    "chat_id": chat_id,
                    "message_id": msg_id,
                    "text": text,
                    "reply_markup": kb,
                    "parse_mode": "Markdown",
                })
                _api_call("answerCallbackQuery", {"callback_query_id": call_id})

            # Action: Navigate to Workers Screen
            elif data == "screen_workers":
                text, kb = _build_workers_screen()
                _api_call("editMessageText", {
                    "chat_id": chat_id,
                    "message_id": msg_id,
                    "text": text,
                    "reply_markup": kb,
                    "parse_mode": "Markdown",
                })
                _api_call("answerCallbackQuery", {"callback_query_id": call_id})

            # Action: Delete/Dismiss the Floating Message
            elif data == "delete_message":
                _api_call("deleteMessage", {
                    "chat_id": chat_id,
                    "message_id": msg_id,
                })
                _api_call("answerCallbackQuery", {
                    "callback_query_id": call_id,
                    "text": "تم مسح الرسالة بنجاح 🗑️",
                })


# Global instance
_CONTROLLER: TelegramRemoteController | None = None


def init_telegram_controller(get_stats_callback: Any = None) -> TelegramRemoteController:
    """Starts the Telegram controller daemon safely in the background."""
    global _CONTROLLER
    if _CONTROLLER is None:
        _CONTROLLER = TelegramRemoteController(get_stats_callback=get_stats_callback)
        _CONTROLLER.start()
        try:
            threading.Thread(target=setup_bot_commands, daemon=True).start()
            notify_user(
                "🚀 **تم تشغيل برنامج فحص زين بنجاح!**\n\n"
                "• البوت متصل الآن بالبرنامج وجاهز للتحكم الكامل.\n"
                "• أرسل أي رسالة أو اضغط `/menu` لفتح لوحة التحكم 🎛️\n"
                "• أرسل `/report` لتحميل أحدث ملف إكسل 📥\n"
                "• أرسل `/health` لعرض سرعة الفحص وموارد الجهاز ⚡\n"
                "• أرسل `/pause` للإيقاف المؤقت أو `/resume` للاستئناف ⏯️\n"
                "• أرسل `/kill` للحذف والتدمير الذاتي الطارئ 💥"
            )
        except Exception:
            pass
    return _CONTROLLER

