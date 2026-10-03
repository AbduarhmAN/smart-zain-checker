"""Native Telegram panels. Pure rendering, with no requests or state changes."""
from __future__ import annotations

from dataclasses import dataclass
from html import escape
import math


@dataclass(frozen=True)
class Panel:
    text: str
    rows: list[list[dict]]


def safe(value, limit=110):
    return escape(str(value or "—")[:limit])


def number(value):
    try:
        result = float(value or 0)
        return result if math.isfinite(result) else 0
    except (ValueError, TypeError):
        return 0


def count(value):
    return f"{max(0, int(number(value))):,}"


def button(text, action, style=None):
    result = {"text": text, "callback_data": "zc:" + action}
    if style:
        result["style"] = style
    return result


def nav():
    return [button("‹ الرئيسية", "home"), button("📊 الحالة", "status")]


def state_label(state):
    if state.get("verifications"):
        return "🟠 بانتظار تحقق زين"
    if state.get("running"):
        return "⏸ متوقف مؤقتًا" if state.get("paused") else "🟢 الفحص جارٍ"
    kpis = state.get("kpis", {})
    total = number(kpis.get("total"))
    if total:
        return "✓ انتهت المعالجة" if number(kpis.get("completed")) >= total else "⚪ الجلسة غير نشطة"
    return "⚪ جاهز لفحصك"


def controls(state):
    rows = []
    if state.get("running") and not state.get("verifications"):
        rows.append([button("▶ استئناف الفحص", "resume", "success") if state.get("paused")
                     else button("⏸ إيقاف مؤقت", "pause")])
    kpis = state.get("kpis", {})
    if (not state.get("running") and not state.get("round_pending") and number(kpis.get("total")) > 0
            and number(kpis.get("completed")) >= number(kpis.get("total"))):
        rows.append([button("📥 استلام نتائج الجولة", "results", "primary")])
    if number(state.get("kpis", {}).get("errors")) + number(state.get("kpis", {}).get("needs_review")):
        rows.append([button("🛠 إصلاح الأخطاء / إعادة الفحص", "repair")])
    return rows


def home(state, jobs):
    pending = sum(job.get("status") == "pending" for job in jobs)
    text = ("<b>تشيك.</b>\n<i>ملفاتك. تقدمك. نتائجك.</i>\n\n"
            f"{state_label(state)}\nالملف: <b>{safe(state.get('workbook') or 'لم تبدأ جلسة بعد')}</b>\n"
            f"ملفات في الانتظار: {count(pending)}\n\n"
            "اختر ما تريد متابعته من الأزرار.")
    return Panel(text, [[button("📊 متابعة الفحص", "status", "primary"), button("📁 ملفات الطابور", "queue:0")],
                        *controls(state), [button("📎 إضافة ملف", "upload"), button("💡 دليل الاستخدام", "help")]])


def status(state):
    k = state.get("kpis", {})
    total, done = number(k.get("total")), number(k.get("completed"))
    percent = min(100, max(0, int(done / total * 100))) if total else 0
    filled = percent // 10
    bar = "▰" * filled + "▱" * (10 - filled)
    text = (f"<b>📊 متابعة الفحص</b>\n{state_label(state)}\n\n"
            f"<b>{safe(state.get('workbook') or 'لا يوجد ملف حالي')}</b>\n"
            f"<code>{bar}  {percent}%</code>\n"
            f"تمت معالجتها: {count(done)} من {count(total)}\n"
            f"متبقية: {count(k.get('remaining'))}\n\n"
            f"أرصدة مؤكدة: {count(k.get('verified'))}\n"
            f"مطابقات تامة: {count(k.get('matches'))}\n"
            f"فروقات الرصيد: {count(k.get('mismatches'))}\n"
            f"إجمالي الفروقات: {number(k.get('mismatch_total')):,.2f} ر.س\n"
            f"تحتاج مراجعة / غير موجودة: {count(number(k.get('needs_review')) + number(k.get('not_found')))}\n"
            f"مهلات وأخطاء: {count(k.get('errors'))}\n"
            f"مؤجلة: {count(k.get('deferred'))}\n\n"
            "<i>نسبة المعالجة مستقلة عن عدد الأرصدة المؤكدة.</i>")
    if state.get("verifications"):
        text += "\n\nأكمل تحقق زين في Chrome أو لوحة التحكم، ثم حدّث الحالة."
    return Panel(text, [[button("↻ تحديث الحالة", "status", "primary")], *controls(state),
                        [button("📁 الطابور", "queue:0"), button("‹ الرئيسية", "home")]])


def queue(jobs, page=0):
    size = 5
    pages = max(1, math.ceil(len(jobs) / size))
    page = min(max(0, page), pages - 1)
    labels = {"pending":"◷ في الانتظار", "active":"🟢 قيد المعالجة", "completed":"✓ انتهت المعالجة",
              "failed":"⚠ يحتاج متابعة", "interrupted":"⏸ متوقف"}
    text = f"<b>📁 طابور الملفات</b>\n{count(len(jobs))} ملف · صفحة {page+1} من {pages}\n"
    if not jobs:
        text += "\nالطابور فارغ. أرسل ملف XLSX لإضافته."
    for index, job in enumerate(jobs[page*size:(page+1)*size], page*size+1):
        text += (f"\n<b>{index}. {safe(job.get('filename'))}</b>\n"
                 f"{safe(job.get('sheet_name'), 65)} · {labels.get(job.get('status'), 'حالة غير معروفة')}\n"
                 f"معالجة: {count(job.get('completed'))} / {count(job.get('total_records'))}\n")
    rows = []
    for index, job in enumerate(jobs[page*size:(page+1)*size], page*size+1):
        if job.get("id"):
            rows.append([button(f"إدارة الشيت {index}", f"job:{job['id']}")])
    pagination = []
    if page > 0:
        pagination.append(button("‹ السابق", f"queue:{page-1}"))
    if page+1 < pages:
        pagination.append(button("التالي ›", f"queue:{page+1}"))
    if pagination:
        rows.append(pagination)
    if any(job.get("status") == "pending" for job in jobs):
        rows.append([button("▶ بدء الجولة", "start_queue", "success")])
    rows += [[button("↻ تحديث الطابور", f"queue:{page}"), button("📎 إضافة ملف", "upload")], nav()]
    return Panel(text, rows)


def upload():
    return Panel("<b>📎 أضف ملف العملاء</b>\n\n"
                 "<b>① أرسل الملف</b>\nاضغط المرفق واختر ملف الإكسل بصيغة XLSX.\n\n"
                 "<b>② نقرأ الأعمدة</b>\nنتحقق من رقم الحساب أو الخدمة والمبلغ المطلوب مراجعته. اسم العميل اختياري.\n\n"
                 "<b>③ تحقق ثم ابدأ</b>\nإذا لم نعرف البيانات، اختر عمود الرقم وعمود المبلغ. أضف الشيت إلى الطابور، وكرر ذلك للملفات الأخرى، ثم اضغط بدء الجولة. النتائج بعد اكتمال الفحص.",
                 [[button("ملفات تحتاج مراجعة", "drafts:0"), button("📁 عرض الطابور", "queue:0")], nav()])


def help_panel():
    return Panel("<b>💡 استخدام تشيك</b>\n\n"
                 "<b>متابعة الفحص</b>\nالتقدم، الأرصدة المؤكدة، الفروقات، وحالات المراجعة في بطاقة واحدة. اضغط تحديث لقراءة الحالة الحالية.\n\n"
                 "<b>التحكم في الجلسة</b>\nأزرار الإيقاف والاستئناف تظهر عند وجود جلسة. إذا طلب زين تحققًا، أكمله في المتصفح أو اللوحة.\n\n"
                 "<b>النتائج</b>\nاضغط استلام النتائج، ثم أكد إرسال ملف الجلسة إلى هذه المحادثة.\n\n"
                 "<b>اختصارات</b>\n/start · /status · /queue · /results · /help",
                 [[button("📎 إضافة ملف", "upload")], nav()])


def notice(title, text):
    return Panel(f"<b>{safe(title)}</b>\n\n{safe(text, 1400)}", [nav()])


def confirmation(action, workbook, token):
    title, body, accept = {
        "pause": ("⏸ إيقاف مؤقت", "سنوقف استلام مهام جديدة؛ قد يكمل العامل الفحص الجاري.", "إيقاف مؤقت"),
        "resume": ("▶ استئناف الفحص", "سنكمل المهام المتبقية مع الاحتفاظ بالتقدم السابق.", "استئناف"),
        "results": ("📥 استلام نتائج الجولة", "سنرسل نتائج الجولة المكتملة إلى هذه المحادثة الخاصة. تتضمن الأرقام والأرصدة التي فُحصت.", "إرسال النتائج"),
        "repair": ("🛠 إعادة فحص الأخطاء", "سنُعيد فحص الأخطاء والحالات التي تحتاج مراجعة في هذه الجلسة. قد يستأنف ذلك الفحص المتوقف ويتصل بزين؛ لا يغيّر الخلايا الناقصة في ملفك.", "إعادة فحص الأخطاء"),
        "remove": ("إخراج الشيت من الطابور", "سيُزال إدخال الشيت من الطابور. ملف الإكسل ونتائجه يبقيان محفوظين.", "إخراج من الطابور"),
        "start_queue": ("▶ بدء الجولة", "سنفحص الملفات المنتظرة بالتتابع. يمكنك إيقاف الفحص مؤقتًا واستئنافه من الحالة؛ النتائج بعد اكتمال الجولة.", "بدء الفحص"),
    }[action]
    return Panel(f"<b>{title}</b>\n\nالملف: <b>{safe(workbook)}</b>\n\n{body}",
                 [[button(accept, f"confirm:{token}", "danger" if action == "remove" else "primary"),
                   button("رجوع", "queue:0" if action == "remove" else "status")]])
