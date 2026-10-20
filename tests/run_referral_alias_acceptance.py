"""Verify real SQL on a new retained isolated database; no production fixtures."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import secrets
import sys

sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tests'))
from run_commerce_database_acceptance import DATA,sql

if Path(sql('postgres',statement='show data_directory')[0]).resolve()!=DATA.resolve():
    raise RuntimeError('Unrecognized PostgreSQL instance')
database='commerce_refalias_'+secrets.token_hex(6)
sql('postgres',statement='create database '+database)
migrations=sorted((ROOT/'supabase/migrations').glob('*commerce*.sql'))
for path in migrations:sql(database,file=path)
sql(database,file=ROOT/'supabase/tests/commerce_referral_aliases.sql')
evidence={'verified_at':datetime.now(timezone.utc).isoformat(),'database':database,'production':False,
    'passed':True,'migration_count':len(migrations),'migration_sha256':hashlib.sha256(migrations[-1].read_bytes()).hexdigest(),
    'checks':['old and current names share canonical code identity','deep link and manual entry','replay and first source preserved',
    'self-referral, inactive code and partner blocked','canonical rename preserves alias','prior service and legacy history rules',
    'shared redemption limit','namespace collisions and owner mismatch blocked','restricted RPC and table privileges'],
    'builds_run':False,'files_deleted':False}
target=ROOT/'docs/telegram_commerce'/('acceptance_'+database+'.json')
target.write_text(json.dumps(evidence,indent=2),encoding='utf-8')
print(json.dumps(evidence))
