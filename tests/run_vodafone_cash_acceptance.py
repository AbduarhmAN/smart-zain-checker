"""Real isolated PostgreSQL and Telegram handlers; no real transfers or messages."""
import concurrent.futures
import hashlib
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import sys
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from run_commerce_database_acceptance import DATA,sql
from run_commerce_bridge_acceptance import LocalClient
from test_commerce_vodafone_cash import VodafoneTests
from telegram_bot.commerce.client import CommerceUnavailable


class VodafoneClient(LocalClient):
    def __init__(self,database):super().__init__(database);self.lose_approval=False

    def rpc(self,name,args):
        if name not in {'create_vodafone_request','submit_vodafone_request','vodafone_request',
                        'list_vodafone_requests','review_vodafone_request','vodafone_message'}:
            return super().rpc(name,args)
        parameters=[]
        for key,value in args.items():
            kind=('uuid' if key=='p_request_id' else 'jsonb' if key=='p_proof' else 'boolean' if key=='p_admin'
                  else 'integer' if key=='p_offset' else 'bigint' if key in {'p_actor_telegram_id','p_telegram_user_id','p_message_id','p_amount_minor'} else 'text')
            encoded=json.dumps(value,ensure_ascii=False) if kind=='jsonb' else str(value)
            literal='null' if value is None else "'"+encoded.replace("'","''")+"'"
            parameters.append(f'{key}=>{literal}::{kind}')
        result=json.loads(sql(self.database,statement='set role service_role; select commerce_rpc.'+name+'('+','.join(parameters)+')')[0].splitlines()[-1])
        self.calls.append(name)
        if self.lose_approval and name=='review_vodafone_request':
            self.lose_approval=False;raise CommerceUnavailable('Synthetic lost committed approval acknowledgement')
        return result


def main():
    if Path(sql('postgres',statement='show data_directory')[0]).resolve()!=DATA.resolve():
        raise RuntimeError('Refusing writes to an unknown database')
    database='commerce_vodafone_'+secrets.token_hex(6)
    sql('postgres',statement=f'create database {database}')
    for path in sorted((ROOT/'supabase/migrations').glob('*commerce*.sql')):sql(database,file=path)
    sql(database,statement="select commerce_rpc.configure_support_actors('[9,18]'::jsonb,'vodafone-acceptance-admins')")
    client=VodafoneClient(database)
    def request(owner=31,amount=6050):
        key=str(uuid4())
        client.rpc('create_vodafone_request',{'p_request_id':key,'p_telegram_user_id':owner,'p_amount_minor':amount})
        return key
    def submit(key,owner=31):
        return client.rpc('submit_vodafone_request',{'p_request_id':key,'p_telegram_user_id':owner,
            'p_sender_phone':'01000000000','p_proof':{'kind':'photo','file_id':'synthetic-photo','file_unique_id':'synthetic-unique:'+key,
                'chat_id':owner,'sender_id':owner,'message_id':50}})
    def approve(key,ref='TEST-TXN-1',actor=9,action='approve'):
        return client.rpc('review_vodafone_request',{'p_request_id':key,'p_actor_telegram_id':actor,
            'p_action':action,'p_value':ref,'p_review_reference':'synthetic:review:'+ref})
    def denied(operation):
        try:operation()
        except RuntimeError:return
        raise AssertionError('Invalid or unauthorized operation succeeded')
    key=request();submit(key)
    assert sql(database,statement='select count(*) from commerce.payments')[0]=='0'
    denied(lambda:approve(key,actor=31))
    denied(lambda:client.rpc('vodafone_request',{'p_request_id':key,'p_actor_telegram_id':44,'p_admin':False}))
    denied(lambda:client.rpc('vodafone_request',{'p_request_id':key,'p_actor_telegram_id':31,'p_admin':True}))
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        approvals=list(pool.map(lambda _:approve(key),range(8)))
    assert len({r['payment_id'] for r in approvals})==1
    assert client.rpc('wallet_state',{'p_telegram_user_id':31})['customer']['available_minor']==6050
    duplicate=request(44);submit(duplicate,44)
    denied(lambda:approve(duplicate))
    assert client.rpc('vodafone_request',{'p_request_id':duplicate,'p_actor_telegram_id':44,'p_admin':False})['state']=='pending'
    copied=request(44)
    client.rpc('submit_vodafone_request',{'p_request_id':copied,'p_telegram_user_id':44,'p_sender_phone':'01000000000',
        'p_proof':{'kind':'photo','file_id':'synthetic-copy','file_unique_id':'synthetic-unique:'+key,'chat_id':44,'sender_id':44,'message_id':51}})
    denied(lambda:approve(copied,'DIFFERENT-REF-SAME-PROOF'))
    assert client.rpc('wallet_state',{'p_telegram_user_id':44})['customer']['available_minor']==0
    rejected=request();submit(rejected);approve(rejected,'لم يصل التحويل',action='reject')
    denied(lambda:approve(rejected,'DIFFERENT-TXN'))
    assert client.rpc('wallet_state',{'p_telegram_user_id':31})['customer']['available_minor']==6050
    missing=request();denied(lambda:approve(missing,'NO-PROOF-TXN'))
    denied(lambda:client.rpc('create_vodafone_request',{'p_request_id':key,'p_telegram_user_id':31,'p_amount_minor':6000}))
    denied(lambda:client.rpc('create_vodafone_request',{'p_request_id':str(uuid4()),'p_telegram_user_id':31,'p_amount_minor':0}))
    denied(lambda:client.rpc('submit_vodafone_request',{'p_request_id':missing,'p_telegram_user_id':31,'p_sender_phone':'01000000000',
        'p_proof':{'kind':'text','body':'synthetic','message_id':55,'chat_id':44,'sender_id':44}}))
    denied(lambda:client.rpc('submit_vodafone_request',{'p_request_id':missing,'p_telegram_user_id':31,'p_sender_phone':'01000000000',
        'p_proof':{'kind':'photo','message_id':55,'chat_id':31,'sender_id':31}}))
    client.rpc('vodafone_message',{'p_request_id':key,'p_actor_telegram_id':31,'p_admin':False,'p_message_id':70,'p_body':'Synthetic complaint'})
    assert key in {r['id'] for r in client.rpc('list_vodafone_requests',{'p_actor_telegram_id':9,'p_admin':True,'p_offset':0})}
    client.rpc('vodafone_message',{'p_request_id':key,'p_actor_telegram_id':31,'p_admin':False,'p_message_id':70,'p_body':'Synthetic complaint'})
    client.rpc('vodafone_message',{'p_request_id':key,'p_actor_telegram_id':9,'p_admin':True,'p_message_id':71,'p_body':'Synthetic reply'})
    assert key not in {r['id'] for r in client.rpc('list_vodafone_requests',{'p_actor_telegram_id':9,'p_admin':True,'p_offset':0})}
    assert sql(database,statement=f"select count(*) from commerce.vodafone_messages where request_id='{key}'")[0]=='2'
    denied(lambda:client.rpc('vodafone_message',{'p_request_id':key,'p_actor_telegram_id':44,'p_admin':False,'p_message_id':72,'p_body':'Unowned complaint'}))
    denied(lambda:sql(database,statement='set role anon; select commerce_rpc.list_vodafone_requests(9,true,0)'))
    denied(lambda:sql(database,statement='set role authenticated; select * from commerce.vodafone_requests'))
    # Exercise the actual UI with actual database commits and a lost response.
    test=VodafoneTests();test.setUp()
    try:
        test.bot._commerce_client=client
        test.callback('start');test.text('60.50',message_id=90)
        incoming=client.rpc('list_vodafone_requests',{'p_actor_telegram_id':31,'p_admin':False,'p_offset':0})
        ui_key=incoming[0]['id']
        test.callback('phone',ui_key);test.text('01000000000',message_id=91)
        test.text('Synthetic transfer notification',message_id=92)
        test.callback('review',ui_key,9);test.callback('transaction',ui_key,9);test.text('UI-TXN-2',9,93)
        client.lose_approval=True
        test.callback('approve',ui_key,9);test.callback('approve',ui_key,9)
        assert client.rpc('wallet_state',{'p_telegram_user_id':31})['customer']['available_minor']==12100
        assert test.bot.queue_service.get_jobs()==[]
        test.callback('complaint',ui_key);test.text('Synthetic payment complaint',message_id=94)
        test.callback('reply',ui_key,9);test.text('Synthetic reviewed reply',9,95);test.callback('send',ui_key,9)
        assert len(client.rpc('vodafone_request',{'p_request_id':ui_key,'p_actor_telegram_id':31,'p_admin':False})['messages'])==2
    finally:test.doCleanups()
    assert sql(database,statement="select count(*) from commerce.payments where provider='vodafone_cash'")[0]=='2'
    assert sql(database,statement="select count(*) from commerce.journals j where (select sum(delta_minor) from commerce.ledger_entries where journal_id=j.id)<>0")[0]=='0'
    evidence={'at':datetime.now(timezone.utc).isoformat(),'production':False,'database':database,'passed':True,
        'migration_sha256':hashlib.sha256((ROOT/'supabase/migrations/202610200050_commerce_vodafone_cash.sql').read_bytes()).hexdigest(),
        'checks':['all migrations applied','8 concurrent approvals credit once','transaction cannot fund another request','approved attachment cannot be reused even with another reference',
          'receipt submission never credits','unowned access and forged admin denied','missing and forged proof denied',
          'rejection does not credit','complaint replay stored once','anonymous/table access denied',
          'Telegram UI with real database and lost commit acknowledgment','reviewed complaint reply','balanced ledger']}
    path=ROOT/'docs/telegram_commerce'/('acceptance_'+database+'.json')
    path.write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':True,'evidence':str(path)}))


if __name__=='__main__':main()
