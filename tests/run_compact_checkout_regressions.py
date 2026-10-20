"""Run relevant retained SQL regressions on the isolated local test cluster."""
from datetime import datetime,timezone
import json
from pathlib import Path
import sys
sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tests'))
from run_commerce_database_acceptance import DATA,sql

if Path(sql('postgres',statement='show data_directory')[0]).resolve()!=DATA.resolve():
    raise RuntimeError('Unexpected local PostgreSQL instance')
database=sql('postgres',statement="select datname from pg_database where datname like 'commerce_compact_%' order by oid desc limit 1")[0]
if not database.startswith('commerce_compact_') or not database.replace('_','').isalnum():
    raise RuntimeError('Retained compact acceptance database required')
checks=[]
for name in ['commerce_compact_checkout.sql','commerce_customer_pricing.sql','commerce_customer_stop.sql',
             'commerce_promotional_expiry.sql','commerce_partner_cost_sheets.sql','commerce_partner_sheet_access.sql',
             'commerce_partner_campaign_approvals.sql']:
    file=ROOT/'supabase/tests'/name
    sql(database,file=file)
    checks.append({'test':name,'passed':True})
    print(json.dumps(checks[-1]),flush=True)
report={'at':datetime.now(timezone.utc).isoformat(),'production':False,'database':database,'passed':True,'checks':checks}
destination=ROOT/'docs/telegram_commerce'/'compact_checkout_regressions_20261019.json'
destination.write_text(json.dumps(report,indent=2),encoding='utf-8')
print(json.dumps({'passed':True,'evidence':str(destination)}))
