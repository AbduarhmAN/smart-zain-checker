"""Advance accounting and concurrent confirmations in a retained local database."""
import concurrent.futures
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import secrets
from run_commerce_database_acceptance import sql,ROOT,DATA


def main():
    if Path(sql('postgres',statement='show data_directory')[0]).resolve()!=DATA.resolve():
        raise RuntimeError('Wrong PostgreSQL instance; refusing test writes')
    database='advance_acceptance_'+secrets.token_hex(6)
    sql('postgres',statement='create database '+database)
    for file in sorted((ROOT/'supabase/migrations').glob('*commerce*.sql')):sql(database,file=file)
    fixture=ROOT/'supabase/tests/commerce_partner_advances.sql'
    sql(database,file=fixture)
    sql(database,statement="""
        insert into commerce.support_actors(telegram_user_id,active,selection_reference) values(889090,true,'isolated concurrency administrator');
        select commerce_rpc.create_partner(889091,'ADVANCE_CONCURRENT');
        select commerce.post_pair('concurrency-initial-earnings','commission',null,'platform:revenue','wallet:'||w.id||':available',700,null,w.id)
            from commerce.wallets w join commerce.users u on u.id=w.user_id where u.telegram_user_id=889091 and w.kind='partner';
        update commerce.wallets set available_minor=700 where user_id=(select id from commerce.users where telegram_user_id=889091) and kind='partner';
    """)

    def payout(index):
        try:
            result=sql(database,statement=f"""select commerce_rpc.record_partner_advance_payout(889090,889091,1000,
                'isolated bank','bank','RACE-{index}','native-race','advance-race-{index}',
                'reviewed advance against future earnings',700)""")[0]
            return {'index':index,'result':json.loads(result)}
        except RuntimeError as error:
            if 'Partner balance changed; review advance again' not in str(error):raise
            return {'index':index,'review_changed':True}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:competing=list(pool.map(payout,range(2)))
    paid=[item for item in competing if 'result' in item]
    if len(paid)!=1 or sum(item.get('review_changed',False) for item in competing)!=1:
        raise AssertionError('Competing reviewed advances were not serialized')
    winner=paid[0]['index']
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:replays=list(pool.map(payout,[winner]*8))
    if len({item['result']['id'] for item in replays})!=1:raise AssertionError('Concurrent replay duplicated advance')
    balance=json.loads(sql(database,statement='select commerce_rpc.partner_finance(889091)')[0])
    if balance['account_balance_minor']!=-300 or balance['withdrawn_minor']!=1000:
        raise AssertionError('Concurrent confirmation changed the paid amount')

    def credit(index):
        return sql(database,statement=f"""begin;
            select commerce.post_pair('concurrent-future-earnings-{index}','commission',null,'platform:revenue','wallet:'||w.id||':available',200,null,w.id)
                from commerce.wallets w join commerce.users u on u.id=w.user_id where u.telegram_user_id=889091 and w.kind='partner';
            update commerce.wallets set available_minor=available_minor+200 where user_id=(select id from commerce.users where telegram_user_id=889091) and kind='partner';
            commit;""")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(credit,range(2)))
    balance=json.loads(sql(database,statement='select commerce_rpc.partner_finance(889091)')[0])
    if balance['account_balance_minor']!=100 or balance['advance_outstanding_minor']!=0 or balance['withdrawable_minor']!=100:
        raise AssertionError('Concurrent earnings lost money or over-repaid advance')
    if sql(database,statement='select count(*) from (select journal_id from commerce.ledger_entries group by journal_id having sum(delta_minor)<>0) imbalance')[0]!='0':
        raise AssertionError('Advance journal is not balanced')
    evidence={'at':datetime.now(timezone.utc).isoformat(),'database':database,'production':False,
        'checks':['seven earned, ten paid, minus three balance','future commission recovery','ordinary withdrawal limits',
            'customer funds and existing holds preserved','receipt and actor authentication','two competing advances',
            'eight concurrent confirmation replays','two concurrent future credits','balanced journals'],
        'fixture_sha256':hashlib.sha256(fixture.read_bytes()).hexdigest(),
        'migration_sha256':hashlib.sha256((ROOT/'supabase/migrations/202610160038_commerce_partner_advances.sql').read_bytes()).hexdigest()}
    path=ROOT/'docs/telegram_commerce'/('acceptance_'+database+'.json')
    path.write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':True,'database':database,'evidence':str(path)}))


if __name__=='__main__':main()
