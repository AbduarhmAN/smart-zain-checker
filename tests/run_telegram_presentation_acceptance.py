"""Exercise presentation metadata and notice integrity in a fresh retained local DB."""
import hashlib
import json
from pathlib import Path
import secrets
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from retained_runtime import install
install()
import run_commerce_database_acceptance as database_runtime
from run_commerce_database_acceptance import DATA,sql


def query(database,statement):
    return json.loads(sql(database,statement='select '+statement)[0])


def main():
    global DATA
    if len(sys.argv)==2:
        DATA=Path(sys.argv[1]).resolve()
        assert DATA.is_relative_to(ROOT/'outputs/telegram_ui_20261021'), 'Use the owned UI test instance only'
        database_runtime.DATA=DATA
    assert Path(sql('postgres',statement='show data_directory')[0]).resolve()==DATA.resolve(), 'Unknown PostgreSQL instance'
    for role in ('anon','authenticated','service_role'):
        sql('postgres',statement=f"do $$ begin if not exists(select 1 from pg_roles where rolname='{role}') then create role {role} nologin; end if; end $$")
    database='commerce_ui_'+secrets.token_hex(6)
    sql('postgres',statement='create database '+database)
    migrations=sorted((ROOT/'supabase/migrations').glob('*commerce*.sql'))
    migration=ROOT/'supabase/migrations/202610210051_commerce_telegram_presentation.sql'
    for path in migrations:
        if path!=migration:sql(database,file=path)
    sql(database,statement="insert into commerce.support_actors(telegram_user_id,active,selection_reference) values(889700,true,'ui-fixture-administrator')")
    added=query(database,"commerce_rpc.add_partner_with_notice(889700,889701,'UI_FORMER')")
    partner=added['partner_id']
    terms=query(database,f"commerce_rpc.configure_partner('{partner}',889700,3000,'forever',null,true)")
    ended=query(database,f"commerce_rpc.end_partner(889700,'{partner}','{terms['version']}','ui-end-fixture')")
    before=query(database,'commerce_rpc.admin_earnings_summary(889700,0,8)')
    snapshots="jsonb_build_object('events',(select jsonb_agg(to_jsonb(e) order by id) from commerce.partner_lifecycle_events e),'wallets',(select jsonb_agg(to_jsonb(w) order by id) from commerce.wallets w),'ledger',(select jsonb_agg(to_jsonb(l) order by to_jsonb(l)::text) from commerce.ledger_entries l))"
    frozen=query(database,snapshots)
    fixtures={}
    for name in ['commerce_partner_lifecycle.sql','commerce_partner_advances.sql','commerce_admin_earnings.sql','commerce_partner_sheet_access.sql']:
        try:sql(database,file=ROOT/'supabase/tests'/name);fixtures[name]=None
        except RuntimeError as exc:fixtures[name]=str(exc)
    sql(database,file=migration)
    assert query(database,snapshots)==frozen, 'Migration rewrote existing money or audit records'
    after=query(database,'commerce_rpc.admin_earnings_summary(889700,0,8)')
    assert after['partners'][0]['ended_at'], 'Financial list must expose actual ended metadata'
    normalized=dict(after,partners=[{k:v for k,v in item.items() if k!='ended_at'} for item in after['partners']])
    assert normalized==before, 'Financial summary changed beyond end metadata'
    access=query(database,f"commerce_rpc.partner_sheet_access(889700,'{partner}')")
    assert access['ended_at'] and not access['effective']
    assert not query(database,'commerce_rpc.list_partners(0)')
    assert query(database,'commerce_rpc.ended_partners(889700,0)')[0]['id']==partner
    for name,baseline_error in fixtures.items():
        try:sql(database,file=ROOT/'supabase/tests'/name);error=None
        except RuntimeError as exc:error=str(exc)
        assert error==baseline_error, 'SQL regression changed: '+name
    result=query(database,"commerce_rpc.add_partner_with_notice(889700,889702,'UI_CURRENT')")
    event=query(database,f"commerce_rpc.partner_lifecycle_event('{result['notice_event_id']}',889700)")
    assert event['body'].startswith('\u200f') and '\u2066889702\u2069' in event['body']
    assert event['body_sha256']==hashlib.sha256(event['body'].encode()).hexdigest()
    acknowledged=query(database,f"commerce_rpc.acknowledge_partner_notice('{event['id']}',889702,'ui-ack-fixture')")
    assert acknowledged['acknowledged_at']
    assert query(database,f"commerce_rpc.partner_lifecycle_event('{ended['id']}',889700)")['body_sha256']==ended['body_sha256']
    report={'passed':True,'database':database,'production':False,'migration':migration.name,
        'checks':['all migrations accepted by PostgreSQL','suspended ended record exposes its end date',
        'former partner absent from current RPC and preserved in former RPC','existing financial results unchanged',
        'existing wallets ledger events and hashes unchanged','new native notice has Arabic direction and exact stored hash',
        'owned acknowledgement and unchanged results on four legacy SQL regression suites'],
        'legacy_sql_results':fixtures,
        'builds_run':False,'files_deleted':False,'customer_messages_sent':False}
    path=ROOT/'outputs/telegram_ui_20261021/database_acceptance.json'
    path.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report))


if __name__=='__main__':main()
