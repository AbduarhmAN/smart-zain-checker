"""Audited legacy wallet transition; preserve exact existing debit and job identities."""
from datetime import datetime,timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import secrets
from domain.workbook import load_fast_workbook as load_workbook
from .checkout import snapshot_input
from .client import CommerceClient
from .legacy_wallets import reconcile,minor
from .order_input import quote_rows
from .topup_store import TopupStore

ROOT=Path(__file__).resolve().parents[2]


def write_atomic(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    pending=path.with_name(path.name+'.'+secrets.token_hex(6)+'.pending')
    with pending.open('x',encoding='utf-8') as stream:
        json.dump(value,stream,ensure_ascii=False,indent=2,allow_nan=False);stream.flush();os.fsync(stream.fileno())
    os.replace(pending,path)


def read_sources(root):
    result={}
    for name in ('user_wallets.json','pending_payments.json','.zain-queue.json'):
        raw=(root/name).read_bytes()
        result[name]=(json.loads(raw.decode('utf-8-sig'),parse_float=Decimal),hashlib.sha256(raw).hexdigest())
    return result


def prepare(root=ROOT):
    root=Path(root).resolve();audit=reconcile(root)
    if audit['conflicts']:raise ValueError('Legacy wallet arithmetic requires review before transition')
    sources=read_sources(root);wallets=sources['user_wallets.json'][0];payments=sources['pending_payments.json'][0]
    jobs={job['id']:job for job in sources['.zain-queue.json'][0]}
    accounts=[];seen_deposits=set()
    for uid,wallet in sorted(wallets.items()):
        for entry in wallet.get('history',[]):
            if entry.get('type')!='credit':continue
            reason=str(entry.get('reason',''));kind,separator,reference=reason.partition(':')
            if kind not in {'binance_pay_auto','bsc_bep20_auto'} or not separator or not reference:
                raise ValueError('Legacy credit classification requires review; no unknown credit is imported as collected cash')
            fingerprint=hashlib.sha256((kind+':'+reference).encode()).hexdigest()
            if fingerprint in seen_deposits:raise ValueError('Repeated historical deposit needs review')
            seen_deposits.add(fingerprint)
        holds=[];history_review=False
        for token,payment in sorted(payments.items()):
            if str(payment.get('user_id'))!=uid:continue
            if payment.get('status')=='fulfilled' and payment.get('kind')!='wallet':history_review=True
        for receipt in wallet.get('history',[]):
            if receipt.get('type')!='debit':continue
            prefix=f'wallet:{uid}:';key=receipt.get('idempotency_key','')
            if not key.startswith(prefix):raise ValueError('Legacy debit ownership requires review')
            token=key[len(prefix):];payment=payments.get(token)
            if not payment or str(payment.get('user_id'))!=uid or payment.get('kind')!='wallet':
                raise ValueError('A deducted wallet request is missing its durable source')
            job=jobs.get(payment.get('job_id'))
            if not job or job.get('column_mapping',{}).get('payment_token')!=f'telegram:{uid}:{token}':
                raise ValueError('Deducted request has no matching persistent checker job')
            if job['status'] in {'completed','archived'}:
                history_review=True;continue
            if job['status'] not in {'pending','active','paused'}:raise ValueError('Legacy request is not resumable')
            draft=payment['draft_info'];path=Path(draft['path']).resolve()
            from telegram_bot.sheet_workflow import file_version
            if path.parent!=root or tuple(draft['version'])!=file_version(path):raise ValueError('Legacy paid input changed')
            amount=minor(receipt['amount'])
            if amount!=minor(draft['egp_amount']):raise ValueError('Stored legacy price differs from its original debit')
            frozen,digest=snapshot_input(root,path)
            workbook=load_workbook(frozen,read_only=True,data_only=True)
            try:
                sheet=workbook.worksheets[draft['sheet_index']]
                rows=quote_rows(sheet,draft['mapping'],draft['mode'],draft['amount_target'])
                if job.get('completed',0):
                    from domain.workbook import inspect_sheet_schema,extract_session_customers
                    from manager.checkpoint import CheckpointManager
                    from manager.recovery import input_signature,read_journal,restore_records
                    from .finance import result_rows
                    schema=inspect_sheet_schema(sheet,has_headers=job['column_mapping'].get('has_headers',True),count_rows=False)
                    full_mapping={**(schema.get('letters') or {}),**job['column_mapping']}
                    customers=extract_session_customers(sheet,full_mapping,job['mode'],auto_indices=schema.get('indices') or {})
                    checkpoint=root/job['checkpoint_file'];saved=CheckpointManager(checkpoint).load()
                    expected=input_signature(customers,full_mapping,job['mode'],job['amount_target'],job['sheet_index'])
                    if saved.get('input_signature')!=expected:raise ValueError('Legacy journal differs from the immutable paid input')
                    journal=root/('.wal_'+checkpoint.name[len('.checkpoint_'):-len('.json')]+'.jsonl')
                    restored=restore_records(customers,saved,read_journal(journal))
                    if len(restored)<job['completed']:raise ValueError('Legacy completed counter exceeds proven committed rows')
                    result_rows(list(restored.values()))
            finally:workbook.close()
            if tuple(draft['version'])!=file_version(path):raise ValueError('Legacy input changed while preparing')
            source={'file_sha256':digest,**{key:draft[key] for key in ('sheet_index','mapping','mode','amount_target')}}
            for name in ('original_telegram_file_id','original_telegram_file_unique_id','original_filename'):
                if draft.get(name):source[name]=draft[name]
            holds.append({'token':token,'amount_minor':amount,'job_id':job['id'],'receipt_verified':True,
                          'operation_id':draft.get('operation_id',''),'source':source,'rows':rows})
        accounts.append({'telegram_user_id':int(uid),'legacy_account_id':wallet['account_id'],
                         'available_minor':minor(wallet['balance']),'held_orders':holds,'service_history_requires_review':history_review})
    document={'version':1,'wallets_sha256':sources['user_wallets.json'][1],
              'payments_sha256':sources['pending_payments.json'][1],'queue_sha256':sources['.zain-queue.json'][1],'accounts':accounts}
    encoded=json.dumps(document,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
    digest=hashlib.sha256(encoded.encode('utf-8')).hexdigest()
    folder=root/'.commerce/legacy_transition';folder.mkdir(parents=True,exist_ok=True)
    path=folder/('manifest_'+digest+'.json')
    if path.exists():
        if path.read_text(encoding='utf-8')!=encoded:raise ValueError('Conflicting retained manifest')
    else:
        with path.open('x',encoding='utf-8') as stream:stream.write(encoded);stream.flush();os.fsync(stream.fileno())
    return {'manifest':str(path),'sha256':digest,'accounts':len(accounts),'held_orders':sum(len(a['held_orders']) for a in accounts),
            'available_minor':sum(a['available_minor'] for a in accounts),'held_minor':sum(h['amount_minor'] for a in accounts for h in a['held_orders'])}


def apply(manifest,actor,*,root=ROOT,client=None):
    root=Path(root).resolve();path=Path(manifest).resolve()
    if path.parent!=root/'.commerce/legacy_transition':raise ValueError('Transition manifest must be retained in its workspace')
    if root==ROOT:
        import socket
        with socket.socket() as probe:
            probe.settimeout(2)
            if probe.connect_ex(('127.0.0.1',5050))==0:
                raise ValueError('Stop the local application before committing the transition')
    encoded=path.read_text(encoding='utf-8');document=json.loads(encoded);digest=hashlib.sha256(encoded.encode('utf-8')).hexdigest()
    marker=root/'.commerce/legacy-transition-state.json'
    current=read_sources(root)
    if not marker.exists():
        if any(current[name][1]!=document[field] for name,field in [('user_wallets.json','wallets_sha256'),('pending_payments.json','payments_sha256'),('.zain-queue.json','queue_sha256')]):
            raise ValueError('Source changed; prepare a new manifest before any import')
        write_atomic(marker,{'state':'importing','manifest_sha256':digest,'manifest':str(path)})
    state=json.loads(marker.read_text(encoding='utf-8'))
    if state['manifest_sha256']!=digest:raise ValueError('A different transition already owns this workspace')
    client=CommerceClient() if client is None else client
    receipt=client.rpc('import_legacy_wallets',{'p_actor_telegram_id':actor,'p_manifest_text':encoded,'p_manifest_sha256':digest})
    if receipt.get('state')!='imported':raise ValueError('Transition was not confirmed')
    write_atomic(marker,{'state':'imported','manifest_sha256':digest,'manifest':str(path),'orders':receipt['orders']})
    store=TopupStore(root)
    for account in document['accounts']:store.mark_owner(account['telegram_user_id'])
    from manager.queue import QueueService
    queue=QueueService(root/'.zain-queue.json')
    for account in document['accounts']:
        for held in account['held_orders']:
            record=receipt['orders'][held['token']]
            queue.attach_legacy_financial_order(held['job_id'],account['telegram_user_id'],held['token'],record['order_id'],
                held['source'],held['rows'],held['amount_minor'])
    return {'state':'imported','accounts':len(document['accounts']),'held_orders':len(receipt['orders']),
            'manifest_sha256':digest,'new_deductions':0}


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['prepare','apply']);parser.add_argument('--manifest')
    args=parser.parse_args()
    if args.action=='prepare':print(json.dumps(prepare(),ensure_ascii=False))
    else:
        from telegram_bot.config import settings
        cfg=settings();actors=cfg['authorized_ids'];actor=cfg.get('authorized_id') or actors[0]
        print(json.dumps(apply(args.manifest,int(actor)),ensure_ascii=False))
