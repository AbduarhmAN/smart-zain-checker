"""Telegram referral syntax, shared by link and manual input adapters."""
from __future__ import annotations
import re

CODE = re.compile(r'[A-Z0-9_-]{3,48}', re.ASCII)
BOT = re.compile(r'[A-Za-z0-9_]{5,32}', re.ASCII)


def normalize_referral_code(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError('Referral code must be text')
    code = value.strip().upper()
    if not CODE.fullmatch(code):
        raise ValueError('Invalid referral code')
    return code


def referral_link(bot_username: str, code: str) -> str:
    bot = bot_username.removeprefix('@')
    if not BOT.fullmatch(bot):
        raise ValueError('Invalid Telegram bot username')
    return f'https://t.me/{bot}?start=ref_{normalize_referral_code(code)}'


def referral_arguments(telegram_user_id: int, value: str, *, source: str) -> dict:
    if type(telegram_user_id) is not int or not 0 < telegram_user_id <= 9223372036854775807:
        raise ValueError('Invalid Telegram user ID')
    if source == 'deep_link':
        if not isinstance(value, str) or not value.startswith('ref_'):
            raise ValueError('Invalid referral start payload')
        value = value[4:]
    elif source != 'manual':
        raise ValueError('Unknown referral source')
    return {'p_telegram_user_id': telegram_user_id,
            'p_code': normalize_referral_code(value), 'p_source': source}
