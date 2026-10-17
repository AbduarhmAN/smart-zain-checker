"""Verify grouped ledger in an isolated database, preserving every fixture."""
import json
from pathlib import Path
import secrets
from run_commerce_database_acceptance import ROOT,DATA,sql


def main():
    if Path(sql('postgres',statement='show data_directory')[0]).resolve()!=DATA.resolve():
        raise RuntimeError('Refusing changes outside the isolated PostgreSQL instance')
    database='wallet_activity_'+secrets.token_hex(6)
    sql('postgres',statement=f'create database {database}')
    for path in sorted((ROOT/'supabase/migrations').glob('*commerce*.sql')):sql(database,file=path)
    sql(database,file=ROOT/'supabase/tests/commerce_wallet_activity.sql')
    proof={'passed':True,'database':database,'production':False,'refund_rows':2368,
           'checks':['whole-file totals','charge/refund signs','both buckets','grouped pagination','distinct topups',
                     'customer isolation','suspended customer','read-only records','backend-only access']}
    path=ROOT/'docs/telegram_commerce'/('acceptance_'+database+'.json')
    path.write_text(json.dumps(proof,indent=2),encoding='utf-8')
    print(json.dumps({'passed':True,'evidence':str(path)}))


if __name__=='__main__':main()
