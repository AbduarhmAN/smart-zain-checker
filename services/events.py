"""Durable engine event stream; acknowledgement is per named consumer."""
import sqlite3
import time
from pathlib import Path
from contextlib import contextmanager
from .codec import dumps, loads


class EventStore:
    def __init__(self,root):
        from .persistence import database_path
        self.path=database_path(root)
        with self.connect() as db:
            db.execute('pragma journal_mode=PERSIST')
            db.execute('create table if not exists ipc_events (id integer primary key autoincrement,event text not null,payload text not null,created real not null)')
            db.execute('create table if not exists ipc_acknowledgements (consumer text not null,event_id integer not null,primary key(consumer,event_id))')

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=5); db.execute('pragma synchronous=FULL')
        try:
            with db: yield db
        finally: db.close()

    def publish(self,event,payload):
        with self.connect() as db: db.execute('insert into ipc_events(event,payload,created) values(?,?,?)',(event,dumps(payload),time.time()))

    def list(self,payload):
        if payload.get('consumer')!='telegram': raise ValueError('Unknown event consumer')
        limit=payload.get('limit',32)
        if type(limit) is not int or not 1<=limit<=100: raise ValueError('Invalid event batch')
        with self.connect() as db:
            rows=db.execute('select id,event,payload from ipc_events where not exists (select 1 from ipc_acknowledgements a where a.consumer=? and a.event_id=ipc_events.id) order by id limit ?',('telegram',limit)).fetchall()
        return [{'id':row[0],'event':row[1],'payload':loads(row[2])} for row in rows]

    def ack(self,payload):
        if payload.get('consumer')!='telegram' or type(payload.get('event_id')) is not int: raise ValueError('Invalid event acknowledgement')
        with self.connect() as db:
            if not db.execute('select 1 from ipc_events where id=?',(payload['event_id'],)).fetchone(): raise ValueError('Unknown event')
            db.execute('insert or ignore into ipc_acknowledgements values (?,?)',('telegram',payload['event_id']))
        return True
