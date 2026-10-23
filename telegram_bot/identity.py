"""Telegram profile names for presentation, independent of financial identities."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import threading
import time

from .authorization import user_id

_BIDI = re.compile('[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]')
_STORES = {}
_STORES_LOCK = threading.Lock()


def retired_telegram_identity(root, target):
    """Check the local retirement mirror without forwarding authorization."""
    target = user_id(target)
    if root is None or target is None:
        return False
    path = Path(root)/'.commerce/retired-telegram-identities.json'
    if not path.is_file():
        return False
    try:
        saved = json.loads(path.read_text(encoding='utf-8'))
        if saved.get('version') != 1 or not isinstance(saved.get('retired'), dict):
            raise ValueError('Invalid retired identity registry')
        record = saved['retired'].get(str(target))
        if record is None:
            return False
        canonical = user_id(record.get('canonical_telegram_user_id')) if isinstance(record, dict) else None
        if canonical is None or canonical == target:
            raise ValueError('Invalid canonical identity')
        return True
    except (OSError, ValueError, TypeError, AttributeError):
        raise RuntimeError('Retired identity registry is unavailable') from None


def profile_name(first_name, last_name=''):
    parts = [part for part in (first_name, last_name) if isinstance(part, str)]
    text = re.sub(r'[\x00-\x1f\x7f]', ' ', ' '.join(parts))
    return re.sub(r'\s+', ' ', _BIDI.sub('', text)).strip()[:120]


class ProfileNames:
    def __init__(self, root=None):
        self.root = root
        self.path = Path(root)/'.commerce/telegram-profiles.json' if root is not None else None
        self.lock = threading.RLock()
        self.records = {}
        self.failed_until = {}
        if self.path and self.path.is_file():
            try:
                saved = json.loads(self.path.read_text(encoding='utf-8'))
                if saved.get('version') == 1:
                    self.records = {key: value for key, value in saved.get('profiles', {}).items()
                                    if user_id(key) and isinstance(value, dict)
                                    and profile_name(value.get('first_name'), value.get('last_name'))}
            except (OSError, ValueError, TypeError, AttributeError):
                pass  # Names are a cache; the existing file is retained on failure.

    def remember(self, profile):
        target = user_id(profile.get('id')) if isinstance(profile, dict) else None
        if target is None or profile.get('is_bot') is True:
            return ''
        if retired_telegram_identity(self.root, target):
            return ''
        name = profile_name(profile.get('first_name'), profile.get('last_name'))
        if not name:
            return ''
        record = {key: profile_name(profile.get(key)) for key in ('first_name', 'last_name')}
        with self.lock:
            old = self.records.get(str(target), {})
            changed = any(old.get(key) != record[key] for key in record)
            self.records[str(target)] = {**record, 'updated_at': time.time()}
            self.failed_until.pop(target, None)
            if self.path and changed:
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    pending = self.path.with_name(self.path.name+'.'+secrets.token_hex(6)+'.pending')
                    with pending.open('x', encoding='utf-8') as stream:
                        json.dump({'version': 1, 'profiles': self.records}, stream, ensure_ascii=False)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(pending, self.path)
                except OSError:
                    pass
        return name

    def resolve(self, target, fetch=None):
        target = user_id(target)
        if target is None:
            return 'الاسم غير متاح'
        if retired_telegram_identity(self.root, target):
            return 'حساب مدمج'
        with self.lock:
            record = dict(self.records.get(str(target), {}))
            name = profile_name(record.get('first_name'), record.get('last_name'))
            try:
                age = time.time()-float(record.get('updated_at', 0))
            except (ValueError, TypeError):
                age = 6*3600
            fresh = name and 0 <= age < 6*3600
            retry_later = self.failed_until.get(target, 0) > time.monotonic()
        if fresh or retry_later or fetch is None:
            return name or 'الاسم غير متاح'
        try:
            response = fetch('getChat', {'chat_id': target}, timeout=3)
            profile = response.get('result', {}) if isinstance(response, dict) and response.get('ok') is True else {}
            if isinstance(profile, dict) and profile.get('id') == target and profile.get('type') == 'private':
                found = self.remember(profile)
                if found:
                    return found
        except (OSError, ValueError, TypeError, RuntimeError):
            pass
        with self.lock:
            self.failed_until[target] = time.monotonic()+60
        return name or 'الاسم غير متاح'


class TelegramIdentityMixin:
    def _retired_identity_gate(self, update):
        callback = update.get('callback_query') or {}
        message = callback.get('message') or update.get('message') or update.get('edited_message') or {}
        actor = callback.get('from') or message.get('from') or {}
        return retired_telegram_identity(getattr(self, 'project_root', None), actor.get('id'))

    def _profile_names(self):
        root = getattr(self, 'project_root', None)
        key = str(Path(root).resolve()) if root is not None else None
        with _STORES_LOCK:
            if key not in _STORES:
                _STORES[key] = ProfileNames(root)
            return _STORES[key]

    def _remember_update_identity(self, update):
        callback = update.get('callback_query') or {}
        message = callback.get('message') or update.get('message') or {}
        actor = callback.get('from') or message.get('from') or {}
        chat = message.get('chat') or {}
        if (chat.get('type', 'private') == 'private' and user_id(actor.get('id')) is not None
                and actor.get('id') == chat.get('id')):
            self._profile_names().remember(actor)

    def _telegram_person_name(self, target):
        return self._profile_names().resolve(target, getattr(self, '_api_call', None))
