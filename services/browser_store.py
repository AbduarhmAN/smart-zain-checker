"""Browser task durability in the application's existing local outbox database."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time


class BrowserTaskStore:
    def __init__(self, root):
        from services.persistence import database_path
        self.path = database_path(root)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=PERSIST')
            db.execute('CREATE TABLE IF NOT EXISTS service_browser_tasks (id TEXT PRIMARY KEY, fingerprint TEXT, payload TEXT, state TEXT NOT NULL, result TEXT, updated REAL NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS ix_service_browser_tasks_state ON service_browser_tasks(state,updated)')
            # Browser operations are reads. Re-execution never commits a job,
            # workbook or charge; the engine retains lease/commit fencing.
            db.execute("UPDATE service_browser_tasks SET state='pending' WHERE state='running'")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        db.execute('PRAGMA busy_timeout=5000')
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, task_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM service_browser_tasks WHERE id=?', (task_id,)).fetchone()
        return dict(row) if row else None

    def submit(self, task_id, fingerprint, payload, limit):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT fingerprint FROM service_browser_tasks WHERE id=?', (task_id,)).fetchone()
            if old:
                if old['fingerprint'] is not None and old['fingerprint'] != fingerprint:
                    raise ValueError('Browser task identifier reused with different input')
                return
            count = db.execute("SELECT COUNT(*) FROM service_browser_tasks WHERE state IN ('pending','running')").fetchone()[0]
            if count >= limit:
                from services.ipc import RetryLater
                raise RetryLater(delay=3.0)
            db.execute('INSERT INTO service_browser_tasks VALUES (?,?,?,?,?,?)',
                       (task_id, fingerprint, json.dumps(payload), 'pending', None, time.time()))

    def claim(self):
        with self.connect() as db:
            candidate = db.execute("SELECT id FROM service_browser_tasks WHERE state='pending' ORDER BY updated,id LIMIT 1").fetchone()
            if candidate is None:
                return None
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM service_browser_tasks WHERE id=? AND state='pending'", (candidate['id'],)).fetchone()
            if row:
                db.execute("UPDATE service_browser_tasks SET state='running',updated=? WHERE id=?", (time.time(), row['id']))
                return dict(row)

    def finish(self, task_id, result):
        with self.connect() as db:
            # Cancellation is a durable tombstone; a late result cannot revive it.
            db.execute("UPDATE service_browser_tasks SET state='succeeded',result=?,updated=? WHERE id=? AND state!='cancelled'",
                       (json.dumps(list(result)), time.time(), task_id))

    def defer(self, task_id):
        with self.connect() as db:
            db.execute("UPDATE service_browser_tasks SET state='pending',updated=? WHERE id=? AND state='running'", (time.time(), task_id))

    def cancel(self, task_id):
        result = json.dumps(['network_error', None, 'أُلغيت مهمة المتصفح'])
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("INSERT INTO service_browser_tasks VALUES (?,NULL,NULL,'cancelled',?,?) ON CONFLICT(id) DO UPDATE SET state='cancelled',result=excluded.result,updated=excluded.updated WHERE service_browser_tasks.state IN ('pending','running')",
                       (task_id, result, time.time()))

    def counts(self):
        with self.connect() as db:
            return dict(db.execute('SELECT state,COUNT(*) FROM service_browser_tasks GROUP BY state').fetchall())
