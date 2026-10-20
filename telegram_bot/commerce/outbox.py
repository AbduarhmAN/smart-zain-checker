"""Durable, ordered RPC commands, with leases and no deletion/cleanup operations.

Only replay-safe checker/settlement commands enter this queue. Wallet credits
and provider verification are deliberately executed by the payment adapter.
"""
from __future__ import annotations
import hashlib
from domain import decimal_json as json
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

ALLOWED = {'bind_checker_job','record_terminal_results','confirm_result_delivery','settle_order',
           'record_support_message','confirm_support_delivery'}


class Outbox:
    def __init__(self,path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.execute('''create table if not exists commands (
                key text primary key, method text not null, payload text not null,
                fingerprint text not null, depends_on text references commands(key),
                state text not null default 'pending', attempts integer not null default 0,
                next_attempt real not null default 0, lease_token text, lease_until real,
                response text, last_error text, created_at real not null)''')
            conn.execute('create index if not exists commands_ready on commands(state,next_attempt,created_at)')
            conn.execute('''create table if not exists deliveries (
                order_id text primary key, state text not null, token text,
                receipt text, updated_at real not null)''')

    def _connect(self):
        conn = sqlite3.connect(self.path,timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute('pragma journal_mode=PERSIST')
        conn.execute('pragma synchronous=FULL')
        conn.execute('pragma foreign_keys=ON')
        return conn

    def enqueue(self,key,method,arguments,*,depends_on=None):
        return self.enqueue_many([(key,method,arguments,depends_on)])

    def enqueue_many(self,commands):
        encoded = []
        for key,method,arguments,dependency in commands:
            if not isinstance(key,str) or not key or len(key)>160 or method not in ALLOWED or not isinstance(arguments,dict):
                raise ValueError('Invalid commerce outbox command')
            if dependency == key:
                raise ValueError('A command cannot depend on itself')
            payload = json.dumps(arguments,sort_keys=True,separators=(',',':'),allow_nan=False)
            fingerprint = hashlib.sha256(payload.encode('utf-8')).hexdigest()
            encoded.append((key,method,payload,fingerprint,dependency))
        conn = self._connect()
        try:
            conn.execute('begin immediate')
            for key,method,payload,fingerprint,dependency in encoded:
                old = conn.execute('select * from commands where key=?',(key,)).fetchone()
                if old:
                    if old['method'] != method or old['payload'] != payload or old['depends_on'] != dependency:
                        raise ValueError('Conflicting durable command replay')
                else:
                    conn.execute('insert into commands(key,method,payload,fingerprint,depends_on,created_at) values (?,?,?,?,?,?)',
                                 (key,method,payload,fingerprint,dependency,time.time()))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def lease(self,*,now=None,lease_seconds=60):
        now = time.time() if now is None else now
        conn = self._connect()
        try:
            conn.execute('begin immediate')
            row = conn.execute('''select c.* from commands c
                where ((c.state='pending' and c.next_attempt<=?) or (c.state='leased' and c.lease_until<=?))
                and (c.depends_on is null or exists(select 1 from commands d where d.key=c.depends_on and d.state='done'))
                order by c.created_at,c.key limit 1''',(now,now)).fetchone()
            if row is None:
                conn.commit()
                return None
            if hashlib.sha256(row['payload'].encode('utf-8')).hexdigest() != row['fingerprint']:
                conn.execute("update commands set state='blocked',last_error='payload_integrity' where key=?",(row['key'],))
                conn.commit()
                return None
            token = uuid.uuid4().hex
            conn.execute("update commands set state='leased',lease_token=?,lease_until=?,attempts=attempts+1 where key=?",
                         (token,now+lease_seconds,row['key']))
            conn.commit()
            return {'key':row['key'],'method':row['method'],'arguments':json.loads(row['payload']),
                    'token':token,'attempts':row['attempts']+1}
        finally:
            conn.close()

    def complete(self,command,response):
        encoded = json.dumps(response,sort_keys=True,allow_nan=False)
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute("update commands set state='done',response=?,last_error=null,lease_until=null where key=? and state='leased' and lease_token=?",
                                  (encoded,command['key'],command['token']))
            return cursor.rowcount == 1

    def failed(self,command,error_code,*,permanent=False,now=None):
        now = time.time() if now is None else now
        # Store classifications only. Provider exception bodies may contain secrets.
        safe = str(error_code)[:80]
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute("update commands set state=?,last_error=?,next_attempt=?,lease_until=null where key=? and state='leased' and lease_token=?",
                ('blocked' if permanent else 'pending',safe,now+min(300,2**min(command['attempts'],8)),command['key'],command['token']))
            return cursor.rowcount == 1

    def retry_blocked(self,key):
        with closing(self._connect()) as conn, conn:
            return conn.execute("update commands set state='pending',next_attempt=0,last_error=null where key=? and state='blocked'",(key,)).rowcount == 1

    def status(self):
        with closing(self._connect()) as conn, conn:
            return {row['state']:row['n'] for row in conn.execute('select state,count(*) n from commands group by state')}

    def response(self,key):
        with closing(self._connect()) as conn, conn:
            row = conn.execute('select state,response from commands where key=?',(key,)).fetchone()
            return json.loads(row['response']) if row and row['state']=='done' else None

    def claim_delivery(self,order_id):
        """An uncertain Telegram send is never reclaimed automatically."""
        with closing(self._connect()) as conn, conn:
            conn.execute('begin immediate')
            row = conn.execute('select state from deliveries where order_id=?',(order_id,)).fetchone()
            if row and row['state'] != 'rejected':
                return None
            token = uuid.uuid4().hex
            conn.execute('''insert into deliveries(order_id,state,token,updated_at)
                values (?,'started',?,?) on conflict(order_id) do update
                set state='started',token=excluded.token,updated_at=excluded.updated_at''',
                (order_id,token,time.time()))
            return token

    def delivery_failed(self,order_id,token,*,rejected=False):
        with closing(self._connect()) as conn, conn:
            return conn.execute('''update deliveries set state=?,updated_at=?
                where order_id=? and state='started' and token=?''',
                ('rejected' if rejected else 'ambiguous',time.time(),order_id,token)).rowcount == 1

    def delivery_confirmed(self,order_id,receipt):
        encoded = json.dumps(receipt,sort_keys=True,allow_nan=False)
        with closing(self._connect()) as conn, conn:
            conn.execute('begin immediate')
            row = conn.execute('select receipt from deliveries where order_id=?',(order_id,)).fetchone()
            if row and row['receipt'] is not None and row['receipt'] != encoded:
                raise ValueError('Conflicting Telegram delivery receipt')
            conn.execute('''insert into deliveries(order_id,state,receipt,updated_at)
                values (?,'confirmed',?,?) on conflict(order_id) do update
                set state='confirmed',receipt=excluded.receipt,updated_at=excluded.updated_at''',
                (order_id,encoded,time.time()))

    def delivery_state(self,order_id):
        with closing(self._connect()) as conn, conn:
            row = conn.execute('select state from deliveries where order_id=?',(order_id,)).fetchone()
            return row['state'] if row else None

    def delivery_counts(self):
        with closing(self._connect()) as conn, conn:
            return {row['state']:row['n'] for row in conn.execute('select state,count(*) n from deliveries group by state')}


class OutboxWorker:
    def __init__(self,outbox,client):
        self.outbox,self.client = outbox,client

    def run_once(self):
        command = self.outbox.lease()
        if command is None:
            return False
        try:
            result = self.client.rpc(command['method'],command['arguments'])
            self.outbox.complete(command,result)
        except Exception as exc:
            # 400 indicates a rejected invariant, requiring review. 401/403 may
            # recover after credentials/configuration are repaired; preserve them.
            permanent = str(exc) == 'Commerce RPC HTTP 400'
            self.outbox.failed(command,'rpc_rejected' if permanent else type(exc).__name__,permanent=permanent)
        return True
