"""Shared native Telegram button layout; routes and button fields stay intact."""
import json
import re

LAYOUT_VERSION = 1

# Exact labels are also navigation aliases, including when an input is waiting.
ADMIN_SHORTCUTS = (
    (('💎 فحص ملف جديد', '/upload', 'upload'), ('📊 حالة الفحص', '/status', 'status')),
    (('🔄 طابور الملفات', '/queue', 'queue'), ('الشركاء والإحالات', '/partners', None), ('💼 محفظتي', '/wallet', 'wallet')),
    (('💡 طريقة الاستخدام', '/help', 'help'), ('🏠 الرئيسية', '/start', 'home')),
)
CUSTOMER_SHORTCUTS = (
    (('💎 إضافة ملف للفحص', '/upload', 'upload'), ('📊 حالة فحوصاتي', '/status', 'status')),
    (('📋 ملفاتي للمراجعة', '/drafts', 'drafts'), ('💼 محفظتي', '/wallet', 'wallet'), ('🎫 كود الشحن', '/redeem', 'redeem')),
    (('💡 طريقة الاستخدام', '/help', 'help'), ('🏠 الرئيسية', '/start', 'home')),
)
REPLY_ACTIONS = {text: command for group in (ADMIN_SHORTCUTS, CUSTOMER_SHORTCUTS)
                for row in group for text, command, _ in row}


def reply_keyboard(is_admin=False, icons=None):
    icons = icons or {}
    rows = []
    for row in ADMIN_SHORTCUTS if is_admin else CUSTOMER_SHORTCUTS:
        buttons = []
        for text, command, icon in row:
            item = {'text': text}
            if command == '/upload':item['style'] = 'success'
            if icon and icons.get(icon):item['icon_custom_emoji_id'] = str(icons[icon])
            buttons.append(item)
        rows.append(buttons)
    return {'keyboard': rows, 'resize_keyboard': True, 'is_persistent': True,
            'input_field_placeholder': 'اختر إجراءً أو أرسل ملف XLSX'}


def _label(item):
    return re.sub(r'[^\w\s]', '', str(item.get('text', '')), flags=re.UNICODE).strip()


def _navigation(item):
    """Pack short navigation; financial decisions and record titles stay explicit."""
    if item.get('style') in ('danger', 'success'):return False
    if any(key in item for key in ('pay', 'callback_game', 'request_contact', 'request_location')):return False
    label = _label(item)
    if len(label) > 24:return False
    if re.search(r'أؤكد|تأكيد|اعتماد|أوافق|رفض|إيقاف|حذف|حفظ|إرسال|صرف|دفع|شحن', label):return False
    action = str(item.get('callback_data', '')).removeprefix('zc:')
    if action in ('home', 'wallet', 'status', 'help', 'archive', 'upload') or action.startswith('menu:'):return True
    return bool(re.match(r'^(?:رجوع|الرئيسية|السابق|التالي|الأحدث|الأقدم|تحديث|الطلبات|طلباتي|المحفظة|سجل|قائمة|الشركاء|التواصل مع الدعم)(?:\s|$)', label))


def inline_rows(rows):
    """Preserve designed rows; combine adjacent short navigation singletons."""
    result = []; pending = []
    def flush():
        nonlocal pending
        if pending:result.append(pending);pending = []
    pieces = (list(original)[index:index+3] for original in rows or []
              for index in range(0, len(original), 3))
    for row in pieces:
        if len(row) != 1 or not _navigation(row[0]):
            flush()
            result.append(row)
            continue
        item = row[0]
        candidate = pending + [item]
        capacity = 3 if all(len(_label(button)) <= 12 for button in candidate) else 2
        if len(candidate) > capacity:flush()
        pending.append(item)
    flush()
    return result


def normalize_markup(markup):
    """Handle dict and JSON markup used by text, media and notification paths."""
    encoded = isinstance(markup, str)
    if encoded:
        try:value = json.loads(markup)
        except (ValueError, TypeError):return markup
    else:value = markup
    if not isinstance(value, dict):return markup
    result = dict(value)
    if isinstance(value.get('inline_keyboard'), list):
        result['inline_keyboard'] = inline_rows(value['inline_keyboard'])
    if isinstance(value.get('keyboard'), list) and len(value['keyboard']) > 3:
        buttons = [item for row in value['keyboard'] for item in row]
        count = len(buttons); start = 0; rows = []
        for index in range(min(3, count)):
            size = (count - start + (3-index) - 1) // (3-index)
            rows.append(buttons[start:start+size]); start += size
        result['keyboard'] = rows
    return json.dumps(result, ensure_ascii=False) if encoded else result
