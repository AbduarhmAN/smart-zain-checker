"""Isolated SQL and Telegram cancellation acceptance, retaining all fixtures."""
import concurrent.futures
import json
from pathlib import Path
import secrets
import sys
from datetime import datetime,timezone

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from run_commerce_database_acceptance import DATA,sql
from run_commerce_bridge_acceptance import LocalClient
from test_commerce_cash_code_cancellation import CashCodeCancellationTests
from telegram_bot.commerce.client import CommerceUnavailable


def main():
    if Path(sql('postgres',statement='show data_directory')[0]).resolve()!=DATA.resolve():
        raise RuntimeError('Refusing writes to an unknown PostgreSQL instance')
    database='commerce_cash_cancel_'+secrets.token_hex(6)
    sql('postgres',statement=f'create database {database}')
    for path in sorted((ROOT/'supabase/migrations').glob('*commerce*.sql')):sql(database,file=path)
    sql(database,file=ROOT/'supabase/tests/commerce_cash_credit_codes.sql')
    sql(database,file=ROOT/'supabase/tests/commerce_cash_code_cancellation.sql')
    sql(database,statement="select commerce_rpc.configure_support_actors('[9,21]'::jsonb,'cancellation-acceptance-admins')")
    client=LocalClient(database)
    record=client.rpc('issue_cash_credit_code',{'p_code':'TELEGRAM_CANCEL','p_credit_minor':3000,
        'p_max_redemptions':6,'p_funded_minor':18000,'p_receipt_reference':'TELEGRAM-CANCEL',
        'p_actor_telegram_id':9,'p_approval_reference':'issue'})
    client.rpc('apply_customer_code',{'p_telegram_user_id':899001,'p_code':'TELEGRAM_CANCEL','p_reference':'first'})
    fixture=CashCodeCancellationTests();fixture.setUp()
    try:
        fixture.record['code_id']=record['code_id']
        fixture.bot._commerce_client=client
        fixture.callback('cancel')
        assert '150.00' in fixture.panel().text
        assert client.rpc('cash_credit_code_record',{'p_actor_telegram_id':9,'p_code_id':record['code_id']})['active']
        def lost_ack(name,args):
            client.rpc(name,args)
            raise CommerceUnavailable('Synthetic lost committed cancellation acknowledgement')
        fixture.bot._commerce_client=type('LostAckClient',(),{'rpc':staticmethod(lost_ack)})()
        fixture.callback('cancelok')
        assert 'لم نتأكد من إلغاء' in fixture.panel().text
        fixture.bot._commerce_client=client
        fixture.callback('cancelok')
        assert 'ملغى بالفعل' in fixture.panel().text
        assert client.rpc('apply_customer_code',{'p_telegram_user_id':899002,'p_code':'TELEGRAM_CANCEL','p_reference':'blocked'})['state']=='inactive_code'
        assert client.rpc('wallet_state',{'p_telegram_user_id':899001})['customer']['available_minor']==3000
    finally:fixture.doCleanups()

    race=client.rpc('issue_cash_credit_code',{'p_code':'CANCEL_RACE','p_credit_minor':3000,
        'p_max_redemptions':6,'p_funded_minor':18000,'p_receipt_reference':'CANCEL-RACE',
        'p_actor_telegram_id':9,'p_approval_reference':'race-issue'})
    def competing(index):
        if index%2:
            return client.rpc('cancel_cash_credit_code',{'p_actor_telegram_id':9,'p_code_id':race['code_id'],'p_reference':'race-'+str(index)})
        return client.rpc('apply_customer_code',{'p_telegram_user_id':899100+index,'p_code':'CANCEL_RACE','p_reference':'redeem-'+str(index)})
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:outcomes=list(pool.map(competing,range(16)))
    assert sum(result['state']=='cancelled' for result in outcomes)==1
    assert sum(result['state']=='already_cancelled' for result in outcomes)==7
    assert client.rpc('apply_customer_code',{'p_telegram_user_id':899200,'p_code':'CANCEL_RACE','p_reference':'after-all'})['state']=='inactive_code'
    state=client.rpc('cash_credit_code_record',{'p_actor_telegram_id':9,'p_code_id':race['code_id']})
    assert not state['active'] and state['cash_redeemed_minor']==state['redemptions']*3000
    held=sql(database,statement=f"select sum(delta_minor) from commerce.ledger_entries where account_key='cash-code:{race['code_id']}:unredeemed'")[0]
    assert int(held)+state['cash_redeemed_minor']==18000
    assert sql(database,statement="select count(*) from commerce.journals j where (select sum(delta_minor) from commerce.ledger_entries where journal_id=j.id)<>0")[0]=='0'
    evidence={'at':datetime.now(timezone.utc).isoformat(),'production':False,'database':database,'passed':True,
        'checks':['all migrations applied','existing cash code SQL regression','cancellation SQL permission and accounting checks',
            'Telegram review does not mutate','committed lost acknowledgement replay preserves cancellation',
            '16 competing cancellation and redemption calls serialize on the code row',
            'post-cancellation redemption blocked','funded liability and customer wallets conserved','all journals balanced'],
        'concurrent_redemptions':state['redemptions'],'remaining_minor':int(held)}
    path=ROOT/'docs/telegram_commerce'/f'acceptance_{database}.json'
    path.write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':True,'evidence':str(path)}))


if __name__=='__main__':main()
