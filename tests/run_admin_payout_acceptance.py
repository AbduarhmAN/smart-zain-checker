"""Fresh isolated PostgreSQL only; no live financial records or Telegram calls."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
import json
from pathlib import Path
import secrets
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from run_commerce_database_acceptance import DATA,sql


def main():
    if Path(sql('postgres',statement='show data_directory')[0]).resolve()!=DATA.resolve():
        raise RuntimeError('Refusing unknown database instance')
    database='commerce_admin_payout_'+secrets.token_hex(6)
    sql('postgres',statement='create database '+database)
    for migration in sorted((ROOT/'supabase/migrations').glob('*commerce*.sql')):
        sql(database,file=migration)
    sql(database,file=ROOT/'supabase/tests/commerce_admin_partner_payouts.sql')
    sql(database,file=ROOT/'supabase/tests/commerce_payout_notifications.sql')
    # Keep evidence in this fresh database for concurrent lost-response replays.
    sql(database,statement="""
        insert into commerce.support_actors(telegram_user_id,active,selection_reference) values(888063,true,'synthetic concurrency admin');
        select commerce_rpc.create_partner(888064,'ADMIN_PAYOUT_RACE');
        update commerce.wallets set available_minor=10000 where kind='partner' and user_id=(select id from commerce.users where telegram_user_id=888064);
        select commerce_rpc.configure_payout_notice_channels(888063,'[-100600]');
    """)
    def payout(index):
        return json.loads(sql(database,statement="set role service_role; select commerce_rpc.record_partner_payout(888063,888064,1000.125,'synthetic race bank','bank','RACE-ONE','synthetic-proof-%s','admin-race-%s')"%(index,index))[0].splitlines()[-1])
    with ThreadPoolExecutor(max_workers=8) as pool: results=list(pool.map(payout,range(8)))
    assert len({r['id'] for r in results})==1
    balances=json.loads(sql(database,statement="select jsonb_build_object('available',available_minor,'reserved',reserved_minor,'withdrawn',withdrawn_minor) from commerce.wallets where kind='partner' and user_id=(select id from commerce.users where telegram_user_id=888064)")[0])
    assert balances=={'available':8999.875,'reserved':0,'withdrawn':1000.125}
    assert sql(database,statement='select count(*) from commerce.withdrawals')[0]=='1'
    assert sql(database,statement='select count(*) from commerce.withdrawal_receipts')[0]=='1'
    assert sql(database,statement='select count(*) from commerce.payout_notifications')[0]=='2'
    def claim(_):
        return json.loads(sql(database,statement="set role service_role; select coalesce(commerce_rpc.claim_payout_notification(),'null'::jsonb)")[0].splitlines()[-1])
    with ThreadPoolExecutor(max_workers=8) as pool: claimed=[n for n in pool.map(claim,range(8)) if n]
    assert len(claimed)==2 and len({n['id'] for n in claimed})==2
    assert {n['recipient_kind'] for n in claimed}=={'partner','channel'}
    evidence={'at':datetime.now(timezone.utc).isoformat(),'database':database,'isolated':True,
        'transactional_sql_cases_passed':True,'concurrent_requests':8,'unique_paid_withdrawals':1,
        'exact_amount_preserved':True,'held_funds_unchanged':True,'partner_ack_no_financial_write':True,
        'durable_partner_channel_notifications':True,'replay_does_not_duplicate_notices':True,
        'known_rejections_bounded':True,'uncertain_sends_not_retried_blindly':True,
        'concurrent_notification_claims_are_exclusive':True,
        'production_financial_records_modified':False,'partner_messages_sent':False}
    out=ROOT/'docs/telegram_commerce'/('admin_payout_acceptance_'+database.rsplit('_',1)[-1]+'.json')
    out.write_text(json.dumps(evidence,indent=2),encoding='utf-8');print(json.dumps(evidence))


if __name__=='__main__':main()
