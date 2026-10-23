"""Private, request-scoped waiting messages; never retries a business action."""
from contextvars import ContextVar
from functools import wraps
import threading

_CURRENT = ContextVar('telegram_operation_progress', default=None)
WAIT_DELAY = 0.75


def _confirmed(response):
    return isinstance(response, dict) and (response.get('ok') is True or
        'message is not modified' in str(response.get('description', '')).lower())


def current_progress(bot, chat):
    progress = _CURRENT.get()
    return progress if progress is not None and progress.bot is bot and progress.chat == chat else None


class OperationProgress:
    def __init__(self, bot, chat):
        self.bot, self.chat = bot, chat
        self.message_id = None
        self.text = '⏳ جارٍ تجهيز الرد…'
        self.lock = threading.RLock()
        self.timer = threading.Timer(WAIT_DELAY, self.show)
        self.timer.daemon = True
        self.rendering = False
        self.failed_panel = False
        self.closed = False
        self.attempted = False
        self.armed = False

    def arm(self):
        with self.lock:
            if not self.closed and not self.rendering and not self.armed:
                self.armed = True
                self.timer.start()

    def request(self, method, payload):
        # Cosmetic feedback must not introduce flood waits or delivery retries.
        try:
            return self.bot._api_call(method, payload, timeout=2, max_attempts=1)
        except Exception:
            return None

    def show(self):
        with self.lock:
            if self.closed or self.rendering or self.attempted:
                return
            self.attempted = True  # A lost send acknowledgement must not create duplicates.
            response = self.request('sendMessage', {'chat_id': self.chat, 'text': self.text})
            result = response.get('result') if isinstance(response, dict) else None
            if _confirmed(response) and isinstance(result, dict):
                mid = result.get('message_id')
                chat = result.get('chat', {}).get('id', self.chat)
                if type(mid) is int and mid > 0 and chat == self.chat:
                    self.message_id = mid

    def stage(self, text):
        with self.lock:
            if self.closed or self.rendering:
                return
            previous, self.text = self.text, text
            self.timer.cancel()
            if self.message_id is None:
                self.show()
            elif text != previous:
                self.request('editMessageText', {'chat_id': self.chat,
                    'message_id': self.message_id, 'text': text})

    def prepare(self):
        # Serializes a timer already inside its network call with final rendering.
        with self.lock:
            self.timer.cancel()
            self.rendering = True
            return self.message_id

    def remove(self, *, final_confirmed=False):
        with self.lock:
            if self.message_id is None:
                return
            mid = self.message_id
            response = self.request('deleteMessage', {'chat_id': self.chat, 'message_id': mid})
            if not _confirmed(response):
                # A failed deletion must not leave an obsolete waiting message.
                text = ('اكتمل عرض التفاصيل في الرسالة الأخيرة.' if final_confirmed else
                        'انتهت هذه الخطوة. يمكنك متابعة البوت.')
                self.request('editMessageText', {'chat_id': self.chat, 'message_id': mid, 'text': text})
            self.message_id = None

    def delivered(self, response, *, target=None, previous=None):
        with self.lock:
            if not _confirmed(response):
                self.failed_panel = True
                return
            result = response.get('result')
            result_mid = result.get('message_id') if isinstance(result, dict) else None
            if self.message_id is not None and target == self.message_id and result_mid in (None, target):
                self.message_id = None  # Waiting text was replaced with the final panel.
            else:
                self.remove(final_confirmed=True)
            self.failed_panel = False
            # Keep the earlier quote/receipt text, retiring only its old buttons.
            if target is not None and previous is not None and previous != target:
                self.request('editMessageReplyMarkup', {'chat_id': self.chat,
                    'message_id': previous, 'reply_markup': {'inline_keyboard': []}})

    def finish(self, *, failed=False):
        with self.lock:
            self.timer.cancel()
            self.closed = True
            if self.message_id is not None and (failed or self.failed_panel):
                self.request('editMessageText', {'chat_id': self.chat, 'message_id': self.message_id,
                    'text': 'تعذر تأكيد عرض التفاصيل. راجع حالة طلبك قبل إعادة المحاولة.',
                    'reply_markup': {'inline_keyboard': [[{'text': 'الرئيسية', 'callback_data': 'zc:home'}]]}})
                self.message_id = None
            else:
                # Some routes deliver a document or a provider reply directly.
                self.remove()


def update_progress(method):
    @wraps(method)
    def wrapped(bot, update, *args, **kwargs):
        callback = update.get('callback_query') or {}
        message = callback.get('message') or update.get('message') or {}
        sender = callback.get('from') or message.get('from') or {}
        chat = message.get('chat') or {}
        actor = sender.get('id')
        if (type(actor) is not int or actor <= 0 or sender.get('is_bot') or
                chat.get('id') != actor or chat.get('type', 'private') != 'private'):
            return method(bot, update, *args, **kwargs)
        progress = OperationProgress(bot, actor)
        action = str(callback.get('data', ''))
        if action.startswith(('zc:oh:', 'zc:ar:')):
            progress.text = '⏳ جارٍ تحميل بيانات الطلب وتجهيز النتيجة…'
        elif action.startswith(('zc:wallet', 'zc:cw:', 'zc:vc:')):
            progress.text = '⏳ جارٍ تحميل بيانات المحفظة والدفع…'
        elif action.startswith('zc:map:'):
            progress.text = '⏳ جارٍ مراجعة اختيارات الملف…'
        token = _CURRENT.set(progress)
        failed = True
        try:
            if not callback:
                progress.arm()
            result = method(bot, update, *args, **kwargs)
            failed = result is False
            return result
        finally:
            progress.finish(failed=failed)
            _CURRENT.reset(token)
    return wrapped


def progress_panel(method):
    @wraps(method)
    def wrapped(bot, chat, panel, message_id=None):
        progress = current_progress(bot, chat)
        if progress is None:
            return method(bot, chat, panel, message_id)
        pending = progress.prepare()
        # Native image panels keep their banner and caption rendering path.
        target = pending if pending is not None and not getattr(panel, 'photo', None) else message_id
        response = method(bot, chat, panel, target)
        progress.delivered(response, target=target, previous=message_id)
        return response
    return wrapped


def progress_reply(method):
    @wraps(method)
    def wrapped(bot, chat, text):
        progress = current_progress(bot, chat)
        if progress is not None:
            progress.prepare()
        response = method(bot, chat, text)
        if progress is not None:
            progress.delivered(response)
        return response
    return wrapped


def progress_stage(bot, chat, text):
    progress = current_progress(bot, chat)
    if progress is not None:
        progress.stage(text)


def progress_arm(bot, chat):
    progress = current_progress(bot, chat)
    if progress is not None:
        progress.arm()
