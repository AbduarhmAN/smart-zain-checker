"""Actual native bot + isolated SQL support acceptance; synthetic Telegram only."""
import concurrent.futures
import json
import secrets
import sys
import threading
import time
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import Mock,patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from run_commerce_database_acceptance import DATA,sql
from run_commerce_bridge_acceptance import LocalClient
from telegram_bot.bot import TelegramBotRunner
from telegram_bot.commerce.support import SupportService


def main():
    if Path(sql('postgres',statement='show data_directory')[0]).resolve()!=DATA.resolve():raise RuntimeError('Refusing another PG instance')
    database='commerce_support_'+secrets.token_hex(6);sql('postgres',statement=f'create database {database}')
    for file in sorted((ROOT/'supabase/migrations').glob('*commerce*.sql')):sql(database,file=file)
    root=ROOT/'scratch'/database;root.mkdir(parents=True);client=LocalClient(database)
    owner,admin,bot_id=889910,889919,77
    client.rpc('configure_support_actors',{'p_actor_ids':[admin],'p_reference':'synthetic-only-authority'})
    client.rpc('register_telegram_user',{'p_telegram_user_id':owner,'p_username':'synthetic-support'})
    order=client.rpc('create_order',{'p_telegram_user_id':owner,'p_request_key':'synthetic-support-order',
        'p_rows':[{'row_number':2,'check_type':'service'}],'p_source':{'file_sha256':'a'*64}})['order_id']
    service=SupportService(root,client=client,bot_fingerprint='synthetic-binding');service.start=Mock()
    bot=TelegramBotRunner.__new__(TelegramBotRunner);bot.project_root=root;bot._bot_fingerprint='synthetic-binding'
    bot._ui_lock=threading.RLock();bot.authorized_ids={admin};bot.authorized_id=admin;bot._authorized_override=True
    bot._support_service=service;bot._commerce_client=client;bot._waiting_user_inputs={};bot._confirmations={}
    bot._show_panel=Mock();bot._reply=Mock();sent=[]
    def api(method,data):
        if method=='getMe':return {'ok':True,'result':{'id':bot_id,'is_bot':True}}
        if method=='sendMessage':
            sent.append(data)
            return {'ok':True,'result':{'from':{'id':bot_id,'is_bot':True},'chat':{'id':data['chat_id']},
                'message_id':500,'date':1700002000,'text':data['text']}}
        return {'ok':True}
    bot._api_call=Mock(side_effect=api)
    def message(actor,mid,body,edit=False):
        data={'from':{'id':actor},'chat':{'id':actor,'type':'private'},'message_id':mid,'date':1700000000,'text':body}
        if edit:data['edit_date']=1700000010
        return {'edited_message' if edit else 'message':data,'update_id':mid+(1000 if edit else 0)}
    def drain(current=service):
        for _ in range(20):
            if not current.worker.run_once():return
        raise AssertionError('Support outbox did not drain')
    bot._support_begin(owner,order,'customer')
    update=message(owner,100,'النتيجة تحتاج مراجعة');bot._capture_support_arrival(update)
    key=update['_support_event']
    abandoned=service.store.lease(now=time.time()-120,lease_seconds=1)
    client.rpc(abandoned['method'],abandoned['arguments']) # Remote commit, lost acknowledgement.
    resumed=SupportService(root,client=client,bot_fingerprint='synthetic-binding');resumed.start=Mock();bot._support_service=resumed
    drain(resumed);bot._handle_update(update)
    edit=message(owner,100,'تصحيح: السطر الثاني فقط',True);bot._capture_support_arrival(edit);bot._handle_update(edit);drain(resumed)
    original=resumed.store.event(key)
    if original['p_record']['body']!='النتيجة تحتاج مراجعة':raise AssertionError('Edit overwrote source')
    # Sixteen replay attempts against the actual RPC still leave one original.
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        replies=list(executor.map(lambda _:client.rpc('record_support_message',original),range(16)))
    if not all(r['replayed'] for r in replies):raise AssertionError('Replay was not recognized')
    bot._support_begin(admin,order,'admin')
    proposed=message(admin,200,'نراجع السطر الثاني ونوافيك بالنتيجة');bot._capture_support_arrival(proposed);bot._handle_update(proposed);drain(resumed)
    before=client.rpc('support_history',{'p_order_id':order,'p_actor_telegram_id':owner,'p_role':'customer'})
    if any(m['kind']=='staff_reply' for m in before['messages']):raise AssertionError('Proposed staff reply leaked')
    with patch.object(resumed,'_enqueue_receipt',side_effect=OSError('synthetic-enqueue-interruption')):
        try:bot._support_send(proposed['_support_event'],admin)
        except OSError:pass
        else:raise AssertionError('Receipt interruption not injected')
    restarted=SupportService(root,client=client,bot_fingerprint='synthetic-binding');restarted.start=Mock();restarted.recover();drain(restarted)
    bot._support_service=restarted;bot._support_send(proposed['_support_event'],admin)
    if len(sent)!=1:raise AssertionError('Staff reply duplicated')
    history=client.rpc('support_history',{'p_order_id':order,'p_actor_telegram_id':owner,'p_role':'customer'})
    if len(history['messages'])!=3 or not any(m['kind']=='staff_reply' and m['delivery_state']=='confirmed' for m in history['messages']):raise AssertionError('Confirmed history incomplete')
    try:client.rpc('support_history',{'p_order_id':order,'p_actor_telegram_id':owner+1,'p_role':'customer'})
    except RuntimeError:pass
    else:raise AssertionError('Cross-owner support access allowed')
    if sql(database,statement=f"select count(*) from commerce.journals where order_id='{order}'")[0]!='0':raise AssertionError('Support modified money')
    evidence={'at':datetime.now(timezone.utc).isoformat(),'database':database,'production':False,
        'telegram':'synthetic only; no outbound network','order_id':order,'native_bot':True,
        'original_preserved':True,'edited_record_preserved':True,'lost_record_ack_recovered':True,
        'concurrent_original_replays':16,'source_records':3,'synthetic_sends':len(sent),
        'receipt_recovered_after_enqueue_failure':True,'cross_owner_denied':True,'financial_side_effects':False,
        'commands':restarted.store.status(),'deliveries':restarted.store.delivery_counts(),'fixture':str(root)}
    path=ROOT/'docs/telegram_commerce'/('acceptance_'+database+'.json');path.write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':True,'evidence':str(path)}))


if __name__=='__main__':main()
