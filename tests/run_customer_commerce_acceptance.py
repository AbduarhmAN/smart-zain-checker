"""Run the newly agreed customer rules in an identified local test database."""
import json
from pathlib import Path
import secrets
from run_commerce_database_acceptance import ROOT,DATA,sql

def main():
    if Path(sql('postgres',statement='show data_directory')[0]).resolve()!=DATA.resolve():
        raise RuntimeError('Refusing changes outside the isolated PostgreSQL instance')
    database='customer_commerce_'+secrets.token_hex(6)
    sql('postgres',statement=f'create database {database}')
    for path in sorted((ROOT/'supabase/migrations').glob('*commerce*.sql')):sql(database,file=path)
    results=[]
    for name in ['commerce_customer_pricing.sql','commerce_promotional_expiry.sql','commerce_customer_stop.sql','commerce_cent_discount.sql','commerce_legacy_transition.sql','commerce_customer_history.sql','commerce_bound_invoice_monitor.sql','commerce_binance_note_binding.sql']:
        sql(database,file=ROOT/'supabase/tests'/name);results.append({'test':name,'passed':True})
    proof={'database':database,'production':False,'passed':True,'checks':results}
    path=ROOT/'docs/telegram_commerce'/('acceptance_'+database+'.json')
    path.write_text(json.dumps(proof,indent=2),encoding='utf-8')
    print(json.dumps({'passed':True,'database':database,'evidence':str(path)}))

if __name__=='__main__':main()
