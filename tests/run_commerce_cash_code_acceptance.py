"""Real local SQL + Telegram handlers, lost acknowledgements and competing cash.

Retains all fixtures/databases; no production messages, payments or cleanup.
"""
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
from test_commerce_cash_codes import CashCodeTests
from telegram_bot.commerce.client import CommerceUnavailable


class LostAckClient(LocalClient):
    def __init__(self,database):super().__init__(database);self.lost=set()
    def rpc(self,name,args):
        result=super().rpc(name,args)
        if name in {'issue_cash_credit_code','apply_customer_code'} and name not in self.lost:
            self.lost.add(name);raise CommerceUnavailable('Synthetic lost committed cash acknowledgment')
        return result


def main():
    if Path(sql('postgres',statement='show data_directory')[0]).resolve()!=DATA.resolve():
        raise RuntimeError('Refusing writes to an unknown PostgreSQL instance')
    database='commerce_cash_code_'+secrets.token_hex(6)
    sql('postgres',statement=f'create database {database}')
    for path in sorted((ROOT/'supabase/migrations').glob('*commerce*.sql')):sql(database,file=path)
    fixture=CashCodeTests();fixture.setUp()
    try:
        client=LostAckClient(database);fixture.bot._commerce_client=client
        token=fixture.prepare();fixture.callback('cg:save:'+token);fixture.callback('cg:save:'+token)
        records=client.rpc('list_cash_credit_codes',{'p_offset':0})
        if len(records)!=1 or records[0]['cash_funded_minor']!=6002:raise AssertionError('Lost issuance duplicated funded code')
        for user in [31,31,32]:
            fixture.callback('cc:redeem:0',user=user);fixture.text('PAID_CASH',user=user)
        for user in [31,32]:
            wallet=client.rpc('wallet_state',{'p_telegram_user_id':user})
            if wallet['customer']['available_minor']!=3001 or wallet['customer']['promotional_available_minor']!=0 or wallet['partner']['available_minor']!=0:
                raise AssertionError('Cash redemption duplicated or misclassified wallet funds')
        if sql(database,statement="select count(*) from commerce.payments where provider='cash_credit_code'")[0]!='2':
            raise AssertionError('Cash redemption receipt duplicated')
        # Different actors concurrently spend one cash receipt through competing paths.
        def claim(index):
            call=("perform commerce_rpc.issue_cash_credit_code('RACE_CASH',3000,1,3000,'RACE-RECEIPT-001',9,'race')" if index%2 else
                  "perform commerce_rpc.admin_cash_topup(897100,3000,'RACE-RECEIPT-001',9,'race')")
            return sql(database,statement='do $$ begin '+call+'; exception when invalid_parameter_value then null; end $$;')[0]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(claim,range(16)))
        kind=sql(database,statement="select allocation_kind from commerce.cash_receipts where reference='RACE-RECEIPT-001'")[0]
        direct=int(sql(database,statement="select coalesce(sum(available_minor),0) from commerce.wallets w join commerce.users u on u.id=w.user_id where u.telegram_user_id=897100 and w.kind='customer'")[0])
        held=int(sql(database,statement="select coalesce(sum(cash_funded_minor-cash_redeemed_minor),0) from commerce.codes where code='RACE_CASH'")[0])
        if direct+held!=3000 or kind not in {'direct_topup','cash_credit_code'}:raise AssertionError('Receipt funded both competing paths')
        sql(database,statement="select commerce_rpc.issue_cash_credit_code('TWO_CASH',3000,2,6000,'TWO-CASH-001',9,'review')")
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            outcomes=list(pool.map(lambda i:json.loads(sql(database,statement=f"select commerce_rpc.redeem_cash_credit_code({897200+i},'TWO_CASH','race-redemption')")[0]),range(16)))
        if sum(item['state']=='redeemed' for item in outcomes)!=2:raise AssertionError('Concurrent redemption exceeded funded count')
        if sql(database,statement="select count(*) from commerce.journals j where (select sum(delta_minor::numeric) from commerce.ledger_entries where journal_id=j.id)<>0")[0]!='0':
            raise AssertionError('Cash journals are not balanced')
        if sql(database,statement='select count(*) from commerce.commissions')[0]!='0':raise AssertionError('Cash code created premature commission')
        evidence={'run_at':datetime.now(timezone.utc).isoformat(),'database':database,'production':False,
            'checks':['Telegram wizard review before approval','lost issuance and redemption acknowledgements replayed once',
                'ordinary customer balance; no promotional classification or commission','16 competing receipt allocations preserve one funded path',
                '16 competing redemptions preserve funded limit of two','all journals balanced'],
            'per_customer_minor':3001,'cash_receipt_winning_path':kind,'shared_receipt_total_minor':direct+held,'concurrent_redemptions':2}
        path=ROOT/'docs/telegram_commerce'/f'acceptance_{database}.json'
        path.write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'passed':True,'evidence':str(path)}))
    finally:fixture.doCleanups()


if __name__=='__main__':main()
