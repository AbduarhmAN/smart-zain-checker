"""Competing note confirmations against a retained isolated PostgreSQL database."""
import concurrent.futures
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import secrets
from uuid import uuid4
from run_commerce_database_acceptance import ROOT,DATA,sql


def literal(value):return "'"+json.dumps(value,separators=(',',':')).replace("'","''")+"'::jsonb"


def main():
    if Path(sql('postgres',statement='show data_directory')[0]).resolve()!=DATA.resolve():raise RuntimeError('Not the owned test instance')
    database='binance_note_'+secrets.token_hex(6)
    sql('postgres',statement=f'create database {database}')
    for file in sorted((ROOT/'supabase/migrations').glob('*commerce*.sql')):sql(database,file=file)
    now=datetime.now(timezone.utc);q1,q2=str(uuid4()),str(uuid4())
    for q,owner in [(q1,888301),(q2,888302)]:
        quote={'id':q,'user_id':owner,'order_id':None,'amount_minor':3000,'asset':'USDT','asset_amount':'0.600000',
               'precision':6,'egp_per_asset':'50','rate_source':'isolated rate','observed_at':(now-timedelta(seconds=1)).isoformat(),
               'expires_at':(now+timedelta(minutes=14)).isoformat()}
        sql(database,statement=f"select commerce_rpc.create_payment_request({literal(quote)},'binance_pay','uid:41'); select commerce_rpc.issue_binance_note('{q}',{owner},'999')")
    note=json.loads(sql(database,statement=f"select commerce_rpc.binance_note('{q1}',888301)")[0])
    proof={'transaction_id':'fixture-race-note-tx','order_type':'C2C','payer_uid':'999','payment_note':note['challenge'],
           'receiver':'uid:41','asset':'USDT','amount':'0.600000'}
    evidence={'provider':'binance_pay','reference':'fixture-race-note-tx','asset':'USDT','amount':'0.600000','receiver':'uid:41',
              'received_at':note['issued_at'],'proof_json':json.dumps(proof,separators=(',',':'))}
    def attempt(index):
        target=q1 if index%2==0 else q2
        try:return json.loads(sql(database,statement=f"select commerce_rpc.confirm_binance_note('{target}',{literal(evidence)})")[0])
        except RuntimeError as error:
            if target!=q2 or 'private pre-payment challenge' not in str(error):raise
            return {'denied':True}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:results=list(pool.map(attempt,range(32)))
    payments={row['payment_id'] for row in results if 'payment_id' in row}
    if len(payments)!=1 or sum(row.get('denied',False) for row in results)!=16:raise AssertionError('Race changed invoice ownership')
    counts=sql(database,statement="select count(*) from commerce.payments; select commerce_rpc.wallet_state(888301)->'customer'->>'available_minor'; select commerce_rpc.wallet_state(888302)->'customer'->>'available_minor'")[0].splitlines()
    if counts!=['1','3000','0']:raise AssertionError('Concurrent receipt replay duplicated or reassigned cash')
    report={'passed':True,'production':False,'database':database,'concurrent_attempts':32,'owned_attempts':16,
            'unauthorized_copy_attempts_denied':16,'payments':1,'owner_available_minor':3000,'other_available_minor':0,
            'private_note_reused_for_another_request':False,'real_money_transferred':False}
    path=ROOT/'docs/telegram_commerce'/('acceptance_'+database+'.json')
    path.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({'passed':True,'evidence':str(path)}))


if __name__=='__main__':main()
