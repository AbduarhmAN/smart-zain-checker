"""Retained immutable wallet quotes, persisted before issuing any provider request."""
from domain import decimal_json as json
from pathlib import Path
import threading
import sqlite3
from contextlib import closing
from uuid import UUID
from .payment_quotes import PaymentQuote


class TopupStore:
    _guard=threading.Lock()
    _locks={}
    def __init__(self,root):
        self.path=Path(root).resolve()/'.commerce'/'topups.sqlite'
        with self._guard:self.lock=self._locks.setdefault(str(self.path),threading.RLock())
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with closing(self._connect()) as conn,conn:
            conn.execute('create table if not exists topups (id text primary key,user_id integer not null,payload text not null,created_at text not null)')
            conn.execute('create index if not exists topups_owner on topups(user_id,created_at)')
            conn.execute('create table if not exists wallet_owners(user_id integer primary key)')

    def _connect(self):
        conn=sqlite3.connect(self.path,timeout=10)
        conn.row_factory=sqlite3.Row
        conn.execute('pragma journal_mode=PERSIST');conn.execute('pragma synchronous=FULL')
        return conn

    def put(self,record):
        quote=PaymentQuote(**record['quote'])
        if quote.order_id is not None:raise ValueError('Wallet quote must not fund an order directly')
        if record['provider'] not in {'cryptopay','binance_pay','bsc'} or not isinstance(record['receiver'],str):
            raise ValueError('Supported wallet provider required')
        payload=json.dumps(record,sort_keys=True,ensure_ascii=False,allow_nan=False)
        if len(payload)>8192:raise ValueError('Wallet quote metadata exceeds supported bounds')
        with self.lock,closing(self._connect()) as conn,conn:
            conn.execute('begin immediate')
            previous=conn.execute('select payload from topups where id=?',(quote.id,)).fetchone()
            if previous:
                if json.loads(previous['payload'])!=record:raise ValueError('Conflicting wallet quote replay')
                return quote.id
            conn.execute('insert into topups(id,user_id,payload,created_at) values(?,?,?,?)',
                (quote.id,quote.user_id,payload,quote.observed_at))
            conn.execute('insert or ignore into wallet_owners(user_id) values(?)',(quote.user_id,))
        return quote.id

    def find(self,quote_id,user_id):
        quote_id=str(UUID(quote_id))
        with closing(self._connect()) as conn:
            row=conn.execute('select user_id,payload from topups where id=?',(quote_id,)).fetchone()
        if row is None:return None
        if row['user_id']!=user_id:raise ValueError('Unowned wallet request')
        record=json.loads(row['payload'])
        quote=PaymentQuote(**record['quote'])
        if quote.order_id is not None:raise ValueError('Request is not a wallet topup')
        return record

    def get(self,quote_id,user_id):
        record=self.find(quote_id,user_id)
        if record is None:raise ValueError('Unknown wallet request')
        return record

    def recent(self,user_id,limit=10):
        if type(limit) is not int or not 1<=limit<=50:raise ValueError('Invalid wallet request page')
        with closing(self._connect()) as conn:
            return [json.loads(row['payload']) for row in conn.execute(
                'select payload from topups where user_id=? order by created_at desc,id desc limit ?',(user_id,limit))]

    def mark_owner(self,user_id):
        from .finance import positive_minor
        positive_minor(user_id)
        with closing(self._connect()) as conn,conn:
            conn.execute('insert or ignore into wallet_owners(user_id) values(?)',(user_id,))

    def is_owner(self,user_id):
        with closing(self._connect()) as conn:
            return conn.execute('select 1 from wallet_owners where user_id=?',(user_id,)).fetchone() is not None
