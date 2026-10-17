"""Order-linked support captured locally before acknowledging Telegram updates.

Only a selected support conversation is captured. Normal bot traffic and secrets
from configuration are never journaled. No binary attachment download is needed.
"""
from __future__ import annotations
import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from uuid import UUID,uuid5,NAMESPACE_URL
from .client import CommerceUnavailable
from .outbox import Outbox,OutboxWorker


def canonical(data):return json.dumps(data,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False)


def support_record(message,bot_id,role,*,edited=False):
    actor=(message.get('from') or {}).get('id');chat=message.get('chat') or {}
    if type(actor) is not int or actor<=0 or chat.get('id')!=actor or chat.get('type')!='private' or (message.get('from') or {}).get('is_bot'):
        raise ValueError('Private authenticated support sender required')
    source={'bot_id':bot_id,'chat_id':actor,'message_id':message.get('message_id'),'date':message.get('date')}
    if any(type(value) is not int or value<=0 for value in source.values()):raise ValueError('Telegram support identity missing')
    if edited:
        if type(message.get('edit_date')) is not int or message['edit_date']<source['date']:raise ValueError('Telegram edit timestamp required')
        source['edit_date']=message['edit_date']
    body=message.get('text',message.get('caption',''))
    if not isinstance(body,str) or len(body)>4096:raise ValueError('Support text is too long')
    attachments=[]
    for kind in ('document','photo','voice','audio','video'):
        item=message.get(kind)
        if kind=='photo' and isinstance(item,list):item=item[-1] if item else None
        if not isinstance(item,dict):continue
        entry={'kind':kind}
        for field in ('file_id','file_unique_id'):
            if not isinstance(item.get(field),str) or not 1<=len(item[field])<=512:raise ValueError('Attachment identity required')
            entry[field]=item[field]
        for field in ('file_name','mime_type'):
            if field in item:
                if not isinstance(item[field],str) or len(item[field])>(256 if field=='file_name' else 128):raise ValueError('Attachment metadata invalid')
                entry[field]=item[field]
        if 'file_size' in item:
            if type(item['file_size']) is not int or item['file_size']<0:raise ValueError('Attachment size invalid')
            entry['file_size']=item['file_size']
        attachments.append(entry)
    if not body.strip() and not attachments:raise ValueError('Support message is empty')
    if role not in {'customer','admin'}:raise ValueError('Invalid support role')
    if role=='admin' and (attachments or len(body.encode('utf-16-le'))//2>3000):raise ValueError('Staff replies require text up to 3000 characters')
    kind=('customer_edit' if edited else 'customer_message') if role=='customer' else ('staff_edit' if edited else 'staff_reply')
    return {'kind':kind,'body':body,'attachments':attachments,'source':source}


def reply_text(order,body):return 'دعم الطلب: '+str(UUID(order))+'\n\n'+body


class SupportStore(Outbox):
    def __init__(self,path):
        super().__init__(path)
        db=self._connect()
        try:
            with db:
                db.execute('''create table if not exists support_contexts(actor integer primary key,
                    context text not null,active integer not null,expires_at real not null)''')
                db.execute('create table if not exists support_bots(fingerprint text primary key,bot_id integer not null)')
                db.execute('create table if not exists support_targets(event_key text primary key,payload text not null)')
                db.execute("create index if not exists support_source_identity on commands(json_extract(payload,'$.p_record.source.chat_id'),json_extract(payload,'$.p_record.source.message_id')) where method='record_support_message'")
        finally:db.close()

    def bind_bot(self,fingerprint,bot_id):
        if type(bot_id) is not int or bot_id<=0:raise ValueError('Verified support bot identity required')
        db=self._connect()
        try:
            with db:
                prior=db.execute('select bot_id from support_bots where fingerprint=?',(fingerprint,)).fetchone()
                if prior and prior['bot_id']!=bot_id:raise ValueError('Support bot namespace changed')
                db.execute('insert or ignore into support_bots(fingerprint,bot_id) values(?,?)',(fingerprint,bot_id))
        finally:db.close()

    def bot_id(self,fingerprint):
        db=self._connect()
        try:
            row=db.execute('select bot_id from support_bots where fingerprint=?',(fingerprint,)).fetchone()
            return row['bot_id'] if row else None
        finally:db.close()

    def set_context(self,actor,order,role,bot_id,fingerprint):
        data={'actor':actor,'order':str(UUID(order)),'role':role,'bot_id':bot_id,'bot_fingerprint':fingerprint}
        db=self._connect()
        try:
            with db:db.execute('''insert into support_contexts(actor,context,active,expires_at) values(?,?,1,?)
                on conflict(actor) do update set context=excluded.context,active=1,expires_at=excluded.expires_at''',(actor,canonical(data),time.time()+1800))
        finally:db.close()

    def context(self,actor,fingerprint):
        db=self._connect()
        try:row=db.execute('select context from support_contexts where actor=? and active=1 and expires_at>?',(actor,time.time())).fetchone()
        finally:db.close()
        value=json.loads(row['context']) if row else None
        return value if value and value['bot_fingerprint']==fingerprint else None

    def end_context(self,actor):
        db=self._connect()
        try:
            with db:db.execute('update support_contexts set active=0 where actor=?',(actor,))
        finally:db.close()

    def original(self,actor,message_id,bot_id):
        db=self._connect()
        try:
            row=db.execute("select payload from commands where method='record_support_message' and json_extract(payload,'$.p_record.source.bot_id')=? and json_extract(payload,'$.p_record.source.chat_id')=? and json_extract(payload,'$.p_record.source.message_id')=? and json_extract(payload,'$.p_record.kind') in ('customer_message','staff_reply') order by created_at limit 1",(bot_id,actor,message_id)).fetchone()
            return json.loads(row['payload']) if row else None
        finally:db.close()

    def event(self,key):
        db=self._connect()
        try:
            row=db.execute("select payload from commands where key=? and method='record_support_message'",('support-record:'+str(UUID(key)),)).fetchone()
            return json.loads(row['payload']) if row else None
        finally:db.close()

    def target(self,key,data=None):
        db=self._connect()
        try:
            with db:
                prior=db.execute('select payload from support_targets where event_key=?',(str(UUID(key)),)).fetchone()
                if data is not None:
                    value=canonical(data)
                    if prior and prior['payload']!=value:raise ValueError('Support recipient snapshot changed')
                    db.execute('insert or ignore into support_targets(event_key,payload) values(?,?)',(str(UUID(key)),value))
                    return data
                return json.loads(prior['payload']) if prior else None
        finally:db.close()

    def confirmed_receipts(self):
        db=self._connect()
        try:return [(r['order_id'],json.loads(r['receipt'])) for r in db.execute("select order_id,receipt from deliveries where state='confirmed' and receipt is not null")]
        finally:db.close()

    def supersede_unsent(self,key):
        db=self._connect()
        try:
            with db:db.execute("insert into deliveries(order_id,state,updated_at) values(?,'superseded',?) on conflict(order_id) do update set state='superseded',updated_at=excluded.updated_at where deliveries.state='rejected'",
                (str(UUID(key)),time.time()))
        finally:db.close()


class SupportService:
    def __init__(self,root,*,client,bot_fingerprint):
        self.root=Path(root);self.client=client;self.fingerprint=bot_fingerprint
        self.store=SupportStore(self.root/'.commerce'/'support.sqlite')
        self.bot_id=self.store.bot_id(self.fingerprint)
        self.worker=OutboxWorker(self.store,client)
        self._stop=threading.Event();self._wake=threading.Event();self._thread=None;self.last_error=None
        self._recovery_needed=threading.Event();self._recovery_needed.set()

    def begin(self,actor,order,role,bot_id):
        profile=self.client.rpc('support_order',{'p_order_id':str(UUID(order)),'p_actor_telegram_id':actor,'p_role':role})
        if not isinstance(profile,dict) or profile.get('order_id')!=str(UUID(order)) or type(profile.get('owner_telegram_id')) is not int:
            raise ValueError('Owned support order is unavailable')
        self.store.bind_bot(self.fingerprint,bot_id);self.bot_id=bot_id
        self.store.set_context(actor,order,role,bot_id,self.fingerprint)
        return profile

    def capture(self,update,admins):
        edited='edited_message' in update;message=update.get('edited_message' if edited else 'message')
        if not isinstance(message,dict):return None
        actor=(message.get('from') or {}).get('id');chat=message.get('chat') or {}
        if type(actor) is not int or actor<=0 or chat.get('id')!=actor or chat.get('type')!='private' or (message.get('from') or {}).get('is_bot'):return None
        previous=self.store.original(actor,message.get('message_id'),self.bot_id)
        context=self.store.context(actor,self.fingerprint)
        if previous:
            # A replay or edit must keep the exact original order and role.
            context={'actor':actor,'order':previous['p_order_id'],'role':previous['p_role'],
                'bot_id':previous['p_record']['source']['bot_id'],'bot_fingerprint':self.fingerprint}
        elif edited:return None
        if not context:return None
        navigation=str(message.get('text','')).strip()
        if not edited and (navigation.startswith('/') or navigation.lower() in {'cancel','back','إلغاء','الغاء','رجوع','‹ الرئيسية','خروج','🏠 الرئيسية'}):
            self.store.end_context(actor);return None
        if context['role']=='admin' and actor not in admins:
            self.store.end_context(actor);return None
        record=support_record(message,context['bot_id'],context['role'],edited=edited)
        key=str(uuid5(NAMESPACE_URL,'zain-support:'+context['order']+':'+canonical(record)))
        arguments={'p_order_id':context['order'],'p_actor_telegram_id':actor,'p_role':context['role'],
            'p_event_key':key,'p_record':record}
        dependency='support-record:'+previous['p_event_key'] if edited and previous else None
        self.store.enqueue('support-record:'+key,'record_support_message',arguments,depends_on=dependency)
        if edited and previous and context['role']=='admin':self.store.supersede_unsent(previous['p_event_key'])
        if not previous:self.store.end_context(actor)
        self._wake.set();return key

    def record_response(self,key):return self.store.response('support-record:'+str(UUID(key)))

    def prepare_delivery(self,key,actor,owner):
        event=self.store.event(key)
        if not event or event['p_role']!='admin' or event['p_actor_telegram_id']!=actor:raise ValueError('Owned staff reply required')
        if type(owner) is not int or owner<=0:raise ValueError('Verified support recipient required')
        return self.store.target(key,{'order_id':event['p_order_id'],'owner':owner,'actor':actor,
            'bot_id':event['p_record']['source']['bot_id'],'text':reply_text(event['p_order_id'],event['p_record']['body'])})

    def capture_delivery(self,key,actor,message):
        event=self.store.event(key);target=self.store.target(key)
        if not event or not target or target['actor']!=actor:raise ValueError('Prepared support recipient required')
        source=event['p_record']['source'];expected=reply_text(event['p_order_id'],event['p_record']['body'])
        if (not isinstance(message,dict) or (message.get('chat') or {}).get('id')!=target['owner']
                or (message.get('from') or {}).get('id')!=source['bot_id'] or (message.get('from') or {}).get('is_bot') is not True
                or type(message.get('message_id')) is not int or message['message_id']<=0
                or type(message.get('date')) is not int or message['date']<=0 or message.get('text')!=expected):
            raise ValueError('Actual Telegram support receipt required')
        receipt={'chat':{'id':target['owner']},'from':{'id':source['bot_id'],'is_bot':True},
            'message_id':message['message_id'],'date':message['date'],'text':expected}
        args={'p_event_key':key,'p_actor_telegram_id':actor,'p_receipt':receipt}
        self.store.delivery_confirmed(key,args)
        try:self._enqueue_receipt(args)
        except (OSError,sqlite3.Error):self._recovery_needed.set();self._wake.set();raise

    def _enqueue_receipt(self,args):
        key=args['p_event_key'];self.store.enqueue('support-delivery:'+key,'confirm_support_delivery',args,
            depends_on='support-record:'+key);self._wake.set()

    def recover(self):
        self._recovery_needed.clear()
        for key,args in self.store.confirmed_receipts():self._enqueue_receipt(args)

    def start(self):
        if self._thread and self._thread.is_alive():return
        self._stop.clear();self._thread=threading.Thread(target=self._loop,name='commerce-support',daemon=True);self._thread.start()

    def _loop(self):
        recovery_at=0
        while not self._stop.is_set():
            try:
                if self._recovery_needed.is_set() and time.monotonic()>=recovery_at:self.recover();recovery_at=time.monotonic()+60
                if self.worker.run_once():continue
            except Exception:self.last_error='support_storage_or_rpc_unavailable';self._recovery_needed.set()
            self._wake.wait(2);self._wake.clear()

    def stop(self):
        self._stop.set();self._wake.set()
        if self._thread and self._thread is not threading.current_thread():self._thread.join(timeout=1)

    def status(self):return {'commands':self.store.status(),'deliveries':self.store.delivery_counts(),
        'running':bool(self._thread and self._thread.is_alive() and not self._stop.is_set()),'last_error':self.last_error}
