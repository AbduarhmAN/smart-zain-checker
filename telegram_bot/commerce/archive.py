"""Archive dispatch runs independently from financial outbox and browser workers."""
from __future__ import annotations
import json
import threading
import time
from pathlib import Path
from uuid import UUID
from .archive_config import ArchiveConfig
from .archive_store import ArchiveStore
from .archive_transport import ArchiveAmbiguous,ArchiveRejected,document_receipt
from .bridge import file_sha256
from .client import CommerceUnavailable


def archive_caption(plan,kind,digest):
    label='الملف الأصلي' if kind=='original' else 'ملف النتيجة'
    def money(value):return f'{value//100}.{value%100:02d}'
    text=(f"أرشيف التشييك · {label}\nOrder ID: {plan['order_id']}\n"
        f"Telegram: {plan['telegram_user_id']}\nالتشييكات: {plan['requested_checks']}\n"
        f"نتائج صحيحة: {plan['valid_checks']} · أعطال: {plan['technical_failures']}\n"
        f"المحصّل: {money(plan['charged_minor'])} جنيه مصري\n"
        f"المسترد: {money(plan['refunded_minor'])} جنيه مصري\n"
        f"SHA-256: {digest}\nArchive kind: {kind}")
    if len(text)>1024:raise ValueError('Archive caption too long')
    return text


class ArchiveDispatcher:
    def __init__(self,root,*,client,transport):
        self.root=Path(root).resolve();self.client=client;self.transport=transport
        self.config=ArchiveConfig(self.root);self.store=ArchiveStore(self.root/'.commerce'/'archive.sqlite')
        self._stop=threading.Event();self._wake=threading.Event();self._thread=None
        self.last_error=None;self._recovery_needed=False

    def enqueue(self,order_id):
        try:self.store.enqueue(order_id);self._wake.set()
        except Exception:self._recovery_needed=True;self.last_error='archive_enqueue_requires_recovery'

    def recover(self):
        # Startup/lost-enqueue recovery only, never an XLSX scan per row or per second.
        self._recovery_needed=False
        for path in (self.root/'.commerce').glob('completion_*.json'):
            try:self.store.enqueue(str(UUID(path.stem[len('completion_'):])))
            except Exception:self._recovery_needed=True;self.last_error='archive_recovery_requires_review'

    def _payload(self,job,config):
        if job['payload']:
            value=json.loads(job['payload'])
            if value['config']!=config:raise ValueError('Archive destination/policy changed after preparation')
            return value
        plan=self.client.rpc('archive_plan',{'p_order_id':job['order_id']})
        if not isinstance(plan,dict) or plan.get('order_id')!=job['order_id']:raise ValueError('Archive plan differs from job')
        if plan.get('state')=='awaiting_settlement':return None
        if plan.get('state')!='ready':raise ValueError('Archive is not ready')
        kind=job['kind'];digest=plan[kind+'_sha256']
        if not isinstance(digest,str) or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('Archive digest invalid')
        path=self.root/'.commerce'/('inputs/'+digest+'.xlsx' if kind=='original' else 'result_'+job['order_id']+'.xlsx')
        if not path.resolve().is_relative_to(self.root/'.commerce') or file_sha256(path)!=digest:
            raise ValueError('Archive immutable backup differs')
        value={'config':config,'plan':plan,'path':str(path),'sha256':digest,'size':path.stat().st_size,
            'file_id':plan.get(kind+'_file_id'),'caption':archive_caption(plan,kind,digest),
            'filename':job['order_id']+'_'+kind+'.xlsx'}
        self.store.freeze(job,value)
        return value

    def run_once(self):
        full=self.config.load()
        if not full or not full.get('enabled'):return False
        config={'chat_id':full['chat_id'],'bot_id':full['bot_id'],'policy':full['policy']}
        job=self.store.claim()
        if job is None:return False
        sent=False
        try:
            payload=self._payload(job,config)
            if payload is None:
                self.store.postpone(job,'awaiting_settlement',30);return True
            if not job['receipt']:
                # All read-only preparation succeeds before the send is durably claimed.
                self.transport.verify(config)
                if file_sha256(payload['path'])!=payload['sha256']:raise ValueError('Archive backup changed')
                self.store.sending(job);sent=True
                receipt=self.transport.send(config,file_id=payload['file_id'],path=payload['path'],
                    sha256=payload['sha256'],caption=payload['caption'],filename=payload['filename'])
                self.store.receipt(job,receipt)
            else:receipt=json.loads(job['receipt'])
            confirmation=self.client.rpc('confirm_archive_file',{'p_order_id':job['order_id'],'p_kind':job['kind'],
                'p_chat_id':receipt['chat_id'],'p_message_id':receipt['message_id'],'p_file_id':receipt['file_id'],
                'p_unique_id':receipt['file_unique_id'],'p_file_size':receipt['file_size'],
                'p_sha256':payload['sha256'],'p_policy':config['policy']})
            if not isinstance(confirmation,dict) or confirmation.get('state')!='confirmed':
                raise CommerceUnavailable('Archive confirmation unavailable')
            self.store.complete(job);self.last_error=None
        except ArchiveRejected as exc:
            if exc.code==429 or exc.code>=500:self.store.postpone(job,'telegram_rejected',exc.retry_after)
            else:self.store.attention(job,'telegram_rejected')
            self.last_error='telegram_rejected'
        except ArchiveAmbiguous:
            if sent and not job.get('receipt'):self.store.attention(job,'send_acknowledgement_unknown',ambiguous=True)
            else:self.store.postpone(job,'telegram_read_unavailable',60)
            self.last_error='telegram_response_unavailable'
        except CommerceUnavailable as exc:
            if str(exc)=='Commerce RPC HTTP 400':self.store.attention(job,'archive_index_requires_review')
            else:self.store.postpone(job,'commerce_unavailable',60)
            self.last_error='commerce_unavailable'
        except Exception:
            # A persistence error immediately after the send may lose its acknowledgement.
            self.store.attention(job,'archive_requires_review',ambiguous=sent and not job.get('receipt'))
            self.last_error='archive_requires_review'
        return True

    def reconcile(self,order_id,kind,message):
        row=self.store.lookup(order_id,kind)
        if not row or row['state']!='ambiguous':raise ValueError('No unknown archive send')
        payload=json.loads(row['payload'])
        receipt=document_receipt(message,payload['config']['chat_id'],expected_size=payload['size'])
        self.transport.verify_download(receipt['file_id'],payload['sha256'])
        # After SHA verification it is safe to accept the new bot-scoped file_id.
        receipt['verified_sha256']=payload['sha256']
        self.store.reconcile(order_id,kind,receipt);self._wake.set()

    def start(self):
        if self._thread and self._thread.is_alive():return
        self._stop.clear()
        self._thread=threading.Thread(target=self._loop,name='commerce-archive',daemon=True);self._thread.start()

    def _loop(self):
        try:self.recover()
        except Exception:self._recovery_needed=True
        next_recovery=0
        while not self._stop.is_set():
            try:
                if self._recovery_needed and time.monotonic()>=next_recovery:
                    self.recover();next_recovery=time.monotonic()+60
                if self.run_once():continue
            except Exception:self.last_error='archive_storage_or_configuration_unavailable'
            self._wake.wait(5);self._wake.clear()

    def stop(self):
        self._stop.set();self._wake.set()
        if self._thread and self._thread is not threading.current_thread():self._thread.join(timeout=1)

    def status(self):
        try:config=self.config.load();states=self.store.counts()
        except Exception:
            return {'enabled':False,'states':{},'running':False,'automatic_deletion':False,
                'last_error':'archive_storage_or_configuration_unavailable'}
        return {'enabled':bool(config and config.get('enabled')),'states':states,
            'running':bool(self._thread and self._thread.is_alive() and not self._stop.is_set()),
            'automatic_deletion':False,'last_error':self.last_error}
