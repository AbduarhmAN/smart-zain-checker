"""Real isolated SQL plus cash payout Telegram wizard; no external messages."""
from pathlib import Path
import sys
import secrets
from decimal import Decimal
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from domain import decimal_json as json
from run_commerce_database_acceptance import DATA,sql
from run_commerce_bridge_acceptance import LocalClient
from test_admin_partner_payouts import AdminPartnerPayoutTests
from telegram_bot.commerce.client import CommerceUnavailable

class LostAckClient(LocalClient):
    def __init__(self,database):super().__init__(database);self.lost=False
    def rpc(self,name,args):
        result=super().rpc(name,args)
        if name=='record_partner_payout' and not self.lost:
            self.lost=True;raise CommerceUnavailable('Synthetic lost committed acknowledgment')
        return result

def main():
    if Path(sql('postgres',statement='show data_directory')[0]).resolve()!=DATA.resolve():
        raise RuntimeError('Refusing unknown database instance')
    database='commerce_cash_payout_'+secrets.token_hex(6)
    sql('postgres',statement='create database '+database)
    for path in sorted((ROOT/'supabase/migrations').glob('*commerce*.sql')):sql(database,file=path)
    sql(database,statement="""
        insert into commerce.support_actors(telegram_user_id,active,selection_reference) values(9,true,'isolated cash payout fixture');
        select commerce_rpc.create_partner(31,'CASH_PAYOUT_TEST');
        select commerce_rpc.configure_partner((select id from commerce.partners limit 1),9,3000,'forever',null,true);
        update commerce.wallets set available_minor=1000.125 where kind='partner';
        select commerce_rpc.request_withdrawal(31,'unrelated-hold',0.125,'unrelated existing hold');
        select commerce_rpc.configure_payout_notice_channels(9,'[-100600]');
    """)
    fixture=AdminPartnerPayoutTests();fixture.setUp();bot=fixture.bot
    client=LostAckClient(database);bot._commerce.return_value=client
    fixture.callback('wa:paypartner:31')
    waiting=bot._waiting_user_inputs.pop(9);bot._commerce_text_input(9,'0.00001',waiting)
    token=bot._waiting_user_inputs[9]['payout_token'];fixture.callback('wa:paycash:'+token)
    assert sql(database,statement="select count(*) from commerce.withdrawals where state='paid'")[0]=='0'
    fixture.callback('wa:payconfirm:'+token)
    fixture.callback('wa:payconfirm:'+token)
    balances=json.loads(sql(database,statement="select jsonb_build_object('available',available_minor,'reserved',reserved_minor,'withdrawn',withdrawn_minor) from commerce.wallets where kind='partner'")[0],parse_float=Decimal)
    assert balances=={'available':Decimal('999.999'),'reserved':Decimal('0.125'),'withdrawn':Decimal('0.001')},balances
    assert sql(database,statement="select count(*) from commerce.withdrawals where state='paid' and payout_reference like 'cash:CASH-%'")[0]=='1'
    assert sql(database,statement='select count(*) from commerce.withdrawal_receipts')[0]=='1'
    assert sql(database,statement='select count(*) from commerce.payout_notifications')[0]=='2'
    assert sql(database,statement="select count(*) from commerce.journals j where (select sum(delta_minor) from commerce.ledger_entries where journal_id=j.id)<>0")[0]=='0'
    proof={'at':datetime.now(timezone.utc).isoformat(),'database':database,'production':False,
        'cash_without_external_receipt':True,'no_write_before_final_confirmation':True,
        'lost_ack_replay_single_paid_withdrawal':True,'exact_egp_amount':'0.00001',
        'unrelated_reserved_funds_preserved':True,'one_receipt_two_durable_notices':True,
        'journals_balanced':True,'real_telegram_messages_sent':False}
    output=ROOT/'docs/telegram_commerce'/('cash_payout_acceptance_'+database.rsplit('_',1)[-1]+'.json')
    output.write_text(json.dumps(proof,indent=2),encoding='utf-8');print(json.dumps(proof))

if __name__=='__main__':main()
