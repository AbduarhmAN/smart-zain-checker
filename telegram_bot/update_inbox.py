"""Crash-safe Telegram update intake, bounded dispatch and per-actor ordering.

The Telegram cursor acknowledges durable intake, not completion. Monetary
handlers retain their existing business idempotency references on replay.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path


class UpdateInbox:
    def __init__(self, path, fingerprint, *, capacity=2048):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fingerprint = fingerprint
        self.capacity = capacity
        with closing(self._connect()) as conn, conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS telegram_updates (
                bot TEXT NOT NULL, id INTEGER NOT NULL, actor TEXT NOT NULL,
                payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                received REAL NOT NULL, finished REAL, priority INTEGER NOT NULL DEFAULT 2,
                PRIMARY KEY(bot,id))''')
            if 'priority' not in {row[1] for row in conn.execute('PRAGMA table_info(telegram_updates)')}:
                conn.execute('ALTER TABLE telegram_updates ADD COLUMN priority INTEGER NOT NULL DEFAULT 2')
            conn.execute('CREATE INDEX IF NOT EXISTS telegram_updates_dispatch ON telegram_updates(bot,state,id)')

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=PERSIST')
        conn.execute('PRAGMA synchronous=FULL')
        return conn

    @staticmethod
    def actor(update):
        message = update.get('message') or update.get('edited_message') or {}
        callback = update.get('callback_query') or {}
        value = (callback.get('from') or message.get('from') or {}).get('id')
        return str(value) if type(value) is int else 'unknown'

    @staticmethod
    def priority(update):
        command = str((update.get('callback_query') or {}).get('data', ''))
        text = str((update.get('message') or {}).get('text', '')).strip().split(' ')[0].split('@')[0].lower()
        if command.startswith(('zc:oh:stopconfirm:', 'zc:oh:cstop:', 'zc:oh:stop:')):return 0
        if text in {'/start', '/help', '/status', '/cancel'} or command in {'zc:home', 'zc:help', 'zc:status'}:return 1
        return 2

    def recover(self):
        """Call only while holding the exclusive bot-token process lock."""
        with closing(self._connect()) as conn, conn:
            conn.execute("UPDATE telegram_updates SET state='pending' WHERE bot=? AND state='running'", (self.fingerprint,))

    def put(self, updates):
        encoded = [(self.fingerprint, item['update_id'], self.actor(item),
                    json.dumps(item, ensure_ascii=False, allow_nan=False), time.time(), self.priority(item)) for item in updates]
        with closing(self._connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            pending = conn.execute("SELECT count(*) FROM telegram_updates WHERE bot=? AND state IN ('pending','running')",
                                   (self.fingerprint,)).fetchone()[0]
            missing = sum(not conn.execute('SELECT 1 FROM telegram_updates WHERE bot=? AND id=?', row[:2]).fetchone()
                          for row in encoded)
            if pending + missing > self.capacity:
                raise BufferError('Telegram durable inbox capacity reached')
            conn.executemany('INSERT OR IGNORE INTO telegram_updates(bot,id,actor,payload,received,priority) VALUES (?,?,?,?,?,?)', encoded)

    def claim(self, *, priority=None):
        with closing(self._connect()) as conn, conn:
            lane = '' if priority is None else (' AND priority<2' if priority else ' AND priority=2')
            query = '''SELECT * FROM telegram_updates u WHERE bot=? AND state='pending'
                AND NOT EXISTS (SELECT 1 FROM telegram_updates r WHERE r.bot=u.bot AND r.actor=u.actor AND r.state='running')
                AND NOT EXISTS (SELECT 1 FROM telegram_updates e WHERE e.bot=u.bot AND e.actor=u.actor
                    AND e.state='pending' AND e.id<u.id)
                ''' + lane + ' ORDER BY priority,id LIMIT 1'
            # Idle dispatch must not contend with financial writes merely to
            # discover an empty inbox. Recheck under the write transaction
            # before claiming a candidate, so competing dispatchers stay safe.
            if conn.execute(query, (self.fingerprint,)).fetchone() is None:return None
            conn.execute('BEGIN IMMEDIATE')
            item = conn.execute(query, (self.fingerprint,)).fetchone()
            if item is None:
                return None
            conn.execute("UPDATE telegram_updates SET state='running' WHERE bot=? AND id=?", (self.fingerprint, item['id']))
            return json.loads(item['payload'])

    def finish(self, update_id, success):
        with closing(self._connect()) as conn, conn:
            conn.execute("UPDATE telegram_updates SET state=?,finished=? WHERE bot=? AND id=? AND state='running'",
                         ('done' if success else 'review', time.time(), self.fingerprint, update_id))

    def counts(self):
        with closing(self._connect()) as conn:
            return {row['state']: row['n'] for row in conn.execute(
                'SELECT state,count(*) n FROM telegram_updates WHERE bot=? GROUP BY state', (self.fingerprint,))}
