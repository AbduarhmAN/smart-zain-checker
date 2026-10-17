"""Local Telegram configuration. Never include credentials in logs or responses."""
from __future__ import annotations
import json
import logging
import os
import re
import threading
from uuid import uuid4
from pathlib import Path

logger = logging.getLogger("TelegramConfig")

LOCAL_SETTINGS = Path(__file__).resolve().parents[1] / '.zain-telegram-local.json'
ADMIN_SETTINGS_LOCK=threading.RLock()


def settings() -> dict:
    try:
        data = json.loads(LOCAL_SETTINGS.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}

    def read(name, legacy, key, default=''):
        return os.environ.get(name, os.environ.get(legacy, data.get(key, default)))

    def chat_number(value):
        if type(value) is str:
            value=value.strip()
            if not re.fullmatch(r'-?(?:0|[1-9][0-9]{0,15})',value):return 0
            value=int(value)
        return value if type(value) is int and -2**52<value<2**52 else 0

    def parse_id_list(val) -> list[int]:
        if not val:
            return []
        if isinstance(val, (int, float)):
            n = chat_number(val)
            return [n] if n != 0 else []
        if isinstance(val, str):
            res = []
            for item in val.replace(';', ',').split(','):
                n = chat_number(item.strip())
                if n != 0:
                    res.append(n)
            return res
        if isinstance(val, (list, tuple, set)):
            res = []
            for item in val:
                n = chat_number(item)
                if n != 0:
                    res.append(n)
            return res
        return []

    auth_id = chat_number(read('ZAIN_TELEGRAM_AUTHORIZED_ID', 'TELEGRAM_AUTHORIZED_ID', 'authorized_id', data.get('chat_id', 0)))

    # Authorized user IDs (admins)
    auth_ids: list[int] = []
    for x in parse_id_list(data.get('authorized_ids', [])):
        if x > 0 and x not in auth_ids:
            auth_ids.append(x)
    for env_k in ('ZAIN_TELEGRAM_AUTHORIZED_IDS', 'TELEGRAM_AUTHORIZED_IDS'):
        for x in parse_id_list(os.environ.get(env_k, '')):
            if x > 0 and x not in auth_ids:
                auth_ids.append(x)
    if auth_id > 0 and auth_id not in auth_ids:
        auth_ids.append(auth_id)
    revoked={n for n in parse_id_list(data.get('revoked_admin_ids',[])) if n>0 and n!=auth_id}
    auth_ids=[n for n in auth_ids if n not in revoked]

    # Admin group IDs (channels/groups usually negative, but accept any configured)
    admin_groups: list[int] = []
    for gid in parse_id_list(data.get('admin_groups', [])):
        if gid not in admin_groups:
            admin_groups.append(gid)
    for gid in parse_id_list(data.get('admin_group', '')):
        if gid not in admin_groups:
            admin_groups.append(gid)
    for env_k in ('ZAIN_TELEGRAM_ADMIN_GROUPS', 'TELEGRAM_ADMIN_GROUPS', 'ZAIN_TELEGRAM_ADMIN_GROUP', 'TELEGRAM_ADMIN_GROUP'):
        for gid in parse_id_list(os.environ.get(env_k, '')):
            if gid not in admin_groups:
                admin_groups.append(gid)
    # Any negative IDs placed in authorized_ids list are also treated as admin groups
    for raw_id in parse_id_list(data.get('authorized_ids', [])):
        if raw_id < 0 and raw_id not in admin_groups:
            admin_groups.append(raw_id)

    chat_id = chat_number(read('ZAIN_TELEGRAM_CHAT_ID', 'TELEGRAM_CHAT_ID', 'chat_id', 0))
    payout_channels=parse_id_list(data.get('payout_channels', []))
    for target in parse_id_list(os.environ.get('ZAIN_TELEGRAM_PAYOUT_CHANNELS','')):
        if target not in payout_channels:payout_channels.append(target)

    # All unique admin targets to receive reports and broadcast notifications
    admin_targets: list[int] = []
    candidates = ([chat_id] if chat_id else []) + ([auth_id] if auth_id else []) + auth_ids + admin_groups
    for cid in candidates:
        if cid != 0 and cid not in revoked and cid not in admin_targets:
            admin_targets.append(cid)

    button_icons = data.get('button_icons', {})
    if not isinstance(button_icons, dict):
        button_icons = {}

    return {
        'bot_token': str(read('ZAIN_TELEGRAM_BOT_TOKEN', 'TELEGRAM_BOT_TOKEN', 'bot_token')).strip(),
        'chat_id': chat_id,
        'authorized_id': auth_id,
        'authorized_ids': auth_ids,
        'revoked_admin_ids':sorted(revoked),
        'admin_groups': admin_groups,
        'payout_channels': [target for target in payout_channels if target<0],
        'admin_targets': admin_targets,
        'button_icons': button_icons,
    }


def save_admin_membership(expected,candidate,owner):
    """Atomic local role update; deny overrides old environment allowlists."""
    from .authorization import user_id
    expected=set(expected);candidate=set(candidate)
    if not candidate or len(candidate)>30 or owner not in candidate or any(user_id(v)!=v for v in candidate):
        raise ValueError('Primary administrator and valid bounded list required')
    with ADMIN_SETTINGS_LOCK:
        cfg=settings()
        if cfg['authorized_id']!=owner or set(cfg['authorized_ids']) not in (expected,candidate):
            raise ValueError('Administrative settings changed; review again')
        raw=LOCAL_SETTINGS.read_text(encoding='utf-8');data=json.loads(raw)
        if not isinstance(data,dict):raise ValueError('Valid local configuration required')
        denied={v for item in data.get('revoked_admin_ids',[]) if (v:=user_id(item)) is not None}
        data['authorized_ids']=sorted(candidate)
        data['revoked_admin_ids']=sorted((denied|(expected-candidate))-candidate)
        if LOCAL_SETTINGS.read_text(encoding='utf-8')!=raw:raise ValueError('Configuration changed concurrently')
        temporary=LOCAL_SETTINGS.with_name(LOCAL_SETTINGS.name+'.'+uuid4().hex+'.tmp')
        with temporary.open('x',encoding='utf-8') as stream:
            json.dump(data,stream,ensure_ascii=False,indent=2);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,LOCAL_SETTINGS)
        if set(settings()['authorized_ids'])!=candidate:raise ValueError('Administrative settings verification failed')


def add_admin_group(group_id: int) -> bool:
    """Save an admin group chat ID to .zain-telegram-local.json."""
    if not isinstance(group_id, int) or group_id == 0:
        return False
    try:
        data = {}
        if LOCAL_SETTINGS.exists():
            try:
                data = json.loads(LOCAL_SETTINGS.read_text(encoding='utf-8'))
                if not isinstance(data, dict):
                    data = {}
            except Exception:
                data = {}
        raw = data.get('admin_groups', [])
        groups = [x for x in (raw if isinstance(raw, list) else [raw]) if isinstance(x, int)]
        if group_id not in groups:
            groups.append(group_id)
            data['admin_groups'] = groups
            LOCAL_SETTINGS.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
        return True
    except Exception as e:
        logger.error("Failed to add admin group %s: %s", group_id, e)
        return False


def remove_admin_group(group_id: int) -> bool:
    """Remove an admin group chat ID from .zain-telegram-local.json."""
    try:
        data = {}
        if LOCAL_SETTINGS.exists():
            try:
                data = json.loads(LOCAL_SETTINGS.read_text(encoding='utf-8'))
                if not isinstance(data, dict):
                    data = {}
            except Exception:
                data = {}
        raw = data.get('admin_groups', [])
        groups = [x for x in (raw if isinstance(raw, list) else [raw]) if isinstance(x, int)]
        if group_id in groups:
            groups.remove(group_id)
            data['admin_groups'] = groups
            LOCAL_SETTINGS.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
        return True
    except Exception as e:
        logger.error("Failed to remove admin group %s: %s", group_id, e)
        return False


def set_button_icon(button_key: str, custom_emoji_id: str | int) -> bool:
    """Save an animated custom emoji ID for a button in .zain-telegram-local.json."""
    if not button_key or not custom_emoji_id:
        return False
    try:
        data = {}
        if LOCAL_SETTINGS.exists():
            try:
                data = json.loads(LOCAL_SETTINGS.read_text(encoding='utf-8'))
                if not isinstance(data, dict):
                    data = {}
            except Exception:
                data = {}
        icons = data.get('button_icons', {})
        if not isinstance(icons, dict):
            icons = {}
        icons[str(button_key).strip().lower()] = str(custom_emoji_id).strip()
        data['button_icons'] = icons
        LOCAL_SETTINGS.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
        return True
    except Exception as e:
        logger.error("Failed to set button icon %s: %s", button_key, e)
        return False
