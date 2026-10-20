"""Durable archive acknowledgements, with no blind resend after a lost response."""
from __future__ import annotations
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID,uuid4


def encoded(data):return json.dumps(data,sort_keys=True,ensure_ascii=False,allow_nan=False)


class ArchiveStore:
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.execute('''create table if not exists archive_jobs(order_id text not null,kind text not null,
                state text not null default 'pending',payload text,receipt text,owner text,lease_until real,
                next_at real not null default 0,attempts integer not null default 0,last_error text,
                primary key(order_id,kind))''')
            db.execute('create index if not exists archive_due on archive_jobs(state,next_at)')

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=15);db.row_factory=sqlite3.Row
        db.execute('pragma journal_mode=PERSIST');db.execute('pragma synchronous=FULL')
        try:
            with db:yield db
        finally:db.close()

    def enqueue(self,order_id):
        order=str(UUID(order_id))
        with self.connect() as db:
            db.executemany('insert or ignore into archive_jobs(order_id,kind) values(?,?)',[(order,k) for k in ('original','result')])

    def claim(self,*,now=None,lease_seconds=600):
        now=time.time() if now is None else now
        with self.connect() as db:
            db.execute('begin immediate')
            # A send that outlived its worker cannot be known to have failed.
            db.execute("update archive_jobs set state='ambiguous',owner=null,last_error='send_interrupted' where state='sending' and lease_until<=?",(now,))
            db.execute("update archive_jobs set state='indexing',owner=null where state='leased' and lease_until<=? and receipt is not null",(now,))
            db.execute("update archive_jobs set state='pending',owner=null where state='leased' and lease_until<=? and receipt is null",(now,))
            row=db.execute("select * from archive_jobs where state in ('pending','indexing') and next_at<=? order by next_at,order_id,kind limit 1",(now,)).fetchone()
            if row is None:return None
            owner=str(uuid4())
            db.execute("update archive_jobs set state='leased',owner=?,lease_until=?,attempts=attempts+1 where order_id=? and kind=?",(owner,now+lease_seconds,row['order_id'],row['kind']))
            return {**dict(row),'owner':owner}

    def _update(self,job,statement,args=()):
        with self.connect() as db:
            changed=db.execute('update archive_jobs set '+statement+' where order_id=? and kind=? and owner=?',
                (*args,job['order_id'],job['kind'],job['owner'])).rowcount
            if changed!=1:raise ValueError('Archive lease no longer owned')

    def freeze(self,job,payload):
        value=encoded(payload)
        with self.connect() as db:
            db.execute('begin immediate')
            row=db.execute('select payload,owner from archive_jobs where order_id=? and kind=?',(job['order_id'],job['kind'])).fetchone()
            if row is None or row['owner']!=job['owner']:raise ValueError('Archive lease no longer owned')
            if row['payload'] is not None and row['payload']!=value:raise ValueError('Archive plan changed')
            db.execute('update archive_jobs set payload=? where order_id=? and kind=?',(value,job['order_id'],job['kind']))
        job['payload']=value

    def sending(self,job):
        with self.connect() as db:
            changed=db.execute("update archive_jobs set state='sending' where order_id=? and kind=? and owner=? and state='leased' and lease_until>?",
                (job['order_id'],job['kind'],job['owner'],time.time())).rowcount
            if changed!=1:raise ValueError('Archive send lease expired or no longer owned')
    def receipt(self,job,receipt):
        value=encoded(receipt)
        self._update(job,"state='leased',receipt=?",(value,));job['receipt']=value
    def complete(self,job):self._update(job,"state='confirmed',owner=null,last_error=null")
    def postpone(self,job,error,delay=60):
        self._update(job,"state=case when receipt is null then 'pending' else 'indexing' end,owner=null,next_at=?,last_error=?",(time.time()+delay,error))
    def attention(self,job,error,*,ambiguous=False):
        state='ambiguous' if ambiguous else 'blocked'
        self._update(job,"state=case when receipt is not null and ?='ambiguous' then 'indexing' else ? end,owner=null,last_error=?",(state,state,error))
    def counts(self):
        with self.connect() as db:return dict(db.execute('select state,count(*) from archive_jobs group by state').fetchall())
    def lookup(self,order_id,kind):
        with self.connect() as db:
            row=db.execute('select * from archive_jobs where order_id=? and kind=?',(str(UUID(order_id)),kind)).fetchone()
            return dict(row) if row else None

    def retry_blocked(self,order_id,kind):
        if kind not in {'original','result'}:raise ValueError('Invalid archive kind')
        with self.connect() as db:
            changed=db.execute("update archive_jobs set state=case when receipt is null then 'pending' else 'indexing' end,next_at=0,last_error=null,owner=null where order_id=? and kind=? and state='blocked'",
                (str(UUID(order_id)),kind)).rowcount
            if changed!=1:raise ValueError('Only a known blocked archive can be retried')

    def reconcile(self,order_id,kind,receipt):
        # Called only with the actual authenticated channel document update.
        with self.connect() as db:
            db.execute('begin immediate')
            row=db.execute('select * from archive_jobs where order_id=? and kind=?',(str(UUID(order_id)),kind)).fetchone()
            if row is None or row['state']!='ambiguous' or row['payload'] is None:
                raise ValueError('No ambiguous archive to reconcile')
            payload=json.loads(row['payload'])
            if receipt['chat_id']!=payload['config']['chat_id'] or receipt['file_size']!=payload['size']:
                raise ValueError('Reconciliation destination/size differs')
            if receipt.get('verified_sha256')!=payload['sha256']:
                # file IDs may change between sends; identity then requires manual hash verification.
                raise ValueError('Reconciliation requires a verified SHA-256 identity')
            db.execute("update archive_jobs set state='indexing',receipt=?,owner=null,next_at=0,last_error=null where order_id=? and kind=?",(encoded(receipt),order_id,kind))
