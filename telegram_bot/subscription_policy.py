"""Shared, standalone channel membership policy for the bot and maintenance worker."""
try:
    from support_policy import support_button
except ModuleNotFoundError:
    from telegram_bot.official_support import support_button

CHANNEL = '@zainchecker'
CHANNEL_URL = 'https://t.me/zainchecker'
CHECK_CALLBACK = 'zc:subscription:check'


def membership(api_call, actor):
    """No positive cache: leaving the channel blocks the next customer action."""
    if type(actor) is not int or actor <= 0:
        return 'unavailable'
    try:
        response = api_call('getChatMember', {'chat_id': CHANNEL, 'user_id': actor}, timeout=5)
    except Exception:
        return 'unavailable'
    if not isinstance(response, dict) or response.get('ok') is not True:
        return 'unavailable'
    result = response.get('result')
    if not isinstance(result, dict):
        return 'unavailable'
    user = result.get('user')
    if not isinstance(user, dict) or type(user.get('id')) is not int or user['id'] != actor:
        return 'unavailable'
    status = result.get('status')
    if status in ('creator', 'administrator', 'member'):
        return 'member'
    if status == 'restricted' and type(result.get('is_member')) is bool:
        return 'member' if result['is_member'] else 'missing'
    if status in ('left', 'kicked'):
        return 'missing'
    return 'unavailable'


def prompt(status):
    if status == 'unavailable':
        return ('⚠️ تعذّر التحقق من اشتراكك الآن.\n'
                'أعد المحاولة بزر «تحقق من الاشتراك»، أو تواصل مع الدعم الرسمي.')
    return ('📢 <b>الاشتراك في القناة مطلوب لاستخدام البوت</b>\n\n'
            'اشترك في @zainchecker، ثم اضغط «تحقق من الاشتراك».\n'
            'يجب استمرار الاشتراك لاستخدام خدمات البوت.')


def rows():
    return [[{'text': '📢 الاشتراك في القناة', 'url': CHANNEL_URL}],
            [{'text': '✅ تحقق من الاشتراك', 'callback_data': CHECK_CALLBACK}],
            [support_button()]]


def support_route(value):
    """The sole official support destination remains reachable before joining."""
    command = str(value).strip().split(maxsplit=1)[0] if str(value).strip() else ''
    return (command.split('@')[0].lower() in ('/support', '/support_reply')
            or value == 'zc:support'
            or str(value).startswith(('zc:sp:compose:', 'zc:vc:complaint:', 'zc:vc:reply:')))
