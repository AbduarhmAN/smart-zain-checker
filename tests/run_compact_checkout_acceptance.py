"""Retained local PostgreSQL tests. No production, build commands, or deletion."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import sys

sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'tests'))
from run_commerce_database_acceptance import DATA,sql


def main():
    if Path(sql('postgres',statement='show data_directory')[0]).resolve()!=DATA.resolve():
        raise RuntimeError('Refusing writes to an unknown PostgreSQL instance')
    database='commerce_compact_'+secrets.token_hex(6)
    sql('postgres',statement='create database '+database)
    for path in sorted((ROOT/'supabase/migrations').glob('*commerce*.sql')):
        sql(database,file=path)
    sql(database,file=ROOT/'supabase/tests/commerce_compact_checkout.sql')
    # Separate committed sessions exercise concurrent draft reuse and funding.
    source={'file_sha256':'8'*64,'storage_version':2,'sheet_index':0}
    rows=[{'row_number':2,'check_type':'account'},{'row_number':4,'check_type':'service'}]
    def statement_rpc(name,arguments):
        values=','.join("'"+json.dumps(v).replace("'","''")+"'::jsonb" if isinstance(v,(dict,list))
                        else str(v) if type(v) is int else "'"+v.replace("'","''")+"'" for v in arguments)
        return json.loads(sql(database,statement='select commerce_rpc.'+name+'('+values+')')[0])
    def create(index):
        return statement_rpc('create_order',[991047,'compact-concurrent-'+str(index),rows,source])['order_id']
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids=list(pool.map(create,range(12)))
    assert len(set(ids))==1,'Concurrent identical inputs created duplicate drafts'
    order=ids[0]
    assert sql(database,statement=f"select count(*) from commerce.checks where order_id='{order}'")[0]=='0'
    statement_rpc('confirm_receipt',[991047,'compact-concurrency-funds','fixture','compact-concurrency-funds',20,'isolated-proof'])
    state=statement_rpc('checkout_state',[order,991047])
    statement_rpc('checkout_consent',[order,991047,state['pricing_snapshot']['policy_version'],'compact-concurrency-consent'])
    def reserve(_):
        return statement_rpc('reserve_order_with_rows',[order,991047,'compact-concurrency-consent',rows])
    with ThreadPoolExecutor(max_workers=6) as pool:
        held=list(pool.map(reserve,range(10)))
    assert all(r['state']=='held' for r in held)
    assert sql(database,statement=f"select count(*) from commerce.checks where order_id='{order}'")[0]=='2'
    wallet=statement_rpc('wallet_state',[991047])['customer']
    assert wallet['reserved_minor']==2 and wallet['available_minor']==18
    assert sql(database,statement="select count(*) from commerce.journals j where (select sum(delta_minor) from commerce.ledger_entries where journal_id=j.id)<>0")[0]=='0'
    evidence={'at':datetime.now(timezone.utc).isoformat(),'production':False,'database':database,'passed':True,
              'fixtures':'Synthetic inputs on a retained isolated local PostgreSQL instance. No real payments or checks.',
              'checks':['all migrations through 47 applied','3000-row unpaid summary creates zero details',
                        '2500 accounts / 500 services counted','insufficient funds and invalid consent leave no details',
                        'manifest tamper and owner checks','atomic materialization and reservation','legacy quotes unchanged',
                        'free-offer shared snapshot, cancellation and one-run settlement',
                        'partner approval and exact sheet-price allocation','12 concurrent matching drafts reuse one order',
                        '10 concurrent reservations spend once','balanced journals']}
    destination=ROOT/'docs/telegram_commerce'/('acceptance_'+database+'.json')
    destination.write_text(json.dumps(evidence,indent=2),encoding='utf-8')
    print(json.dumps({'passed':True,'database':database,'evidence':str(destination)}))


if __name__=='__main__':main()
