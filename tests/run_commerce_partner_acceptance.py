"""Local real database, paid checker bridge, partner withdrawals and lost replies."""
import json
from pathlib import Path
import secrets
import sys
from datetime import datetime,timezone

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from test_commerce_checkout import CheckoutTests,TOKEN
from run_commerce_database_acceptance import DATA,sql
from run_commerce_bridge_acceptance import LocalClient,drain
from telegram_bot.commerce.bridge import CommerceBridge
from telegram_bot.commerce.client import CommerceUnavailable
from telegram_bot.commerce.finance import FinanceService,VerifiedReceipt


class LostAck(LocalClient):
    def __init__(self,database):super().__init__(database);self.lost=set()
    def rpc(self,name,args):
        result=super().rpc(name,args)
        if name in {'request_withdrawal','complete_withdrawal'} and name not in self.lost:
            self.lost.add(name);raise CommerceUnavailable('Synthetic lost response after commit')
        return result


def main():
    if Path(sql('postgres',statement='show data_directory')[0]).resolve()!=DATA.resolve():
        raise RuntimeError('Refusing writes to an unknown local PostgreSQL instance')
    database='commerce_partner_'+secrets.token_hex(6);sql('postgres',statement=f'create database {database}')
    for path in sorted((ROOT/'supabase/migrations').glob('*commerce*.sql')):sql(database,file=path)
    fixture=CheckoutTests();fixture.setUp()
    try:
        client=LostAck(database);bot=fixture.bot;bot._commerce_client=client
        bot.commerce_bridge=CommerceBridge(fixture.root,client=client)
        partner=sql(database,statement="select commerce_rpc.create_partner(41,'PARTNER_FIXTURE')->>'partner_id'")[0]
        sql(database,statement=f"select commerce_rpc.configure_partner('{partner}',9,3000,'checks',1,true)")
        client.rpc('attribute_referral',{'p_telegram_user_id':31,'p_code':'PARTNER_FIXTURE','p_source':'manual'})
        FinanceService(client).confirm_payment(31,'partner-funding',VerifiedReceipt('fixture','partner-funding',9000,'fixture-proof'))
        fixture.prepare();order=bot._sheet_drafts[TOKEN]['commerce_order_id'];fixture.callback('wallet')
        job=bot.queue_service.get_jobs()[0];drain(bot.commerce_bridge)
        result=fixture.root/'partner-result.xlsx';result.write_bytes((fixture.root/job['filename']).read_bytes())
        completed=bot.commerce_bridge.capture_completion({'commerce_order_id':order,'job_id':job['id'],
            'telegram_chat_id':31,'result_file':str(result),'total':2,
            'commerce_records':[{'row':2,'status':'match','live_sar':0},{'row':4,'status':'error'}]})
        drain(bot.commerce_bridge)
        if client.rpc('partner_finance',{'p_telegram_user_id':41})['available_minor']!=0:
            raise AssertionError('Commission became available before delivery')
        bot.commerce_bridge.capture_delivery(completed,{'chat':{'id':31},'message_id':10,'document':{'file_id':'partner-fixture-result'}})
        drain(bot.commerce_bridge);client.rpc('settle_order',{'p_order_id':order})
        finance=client.rpc('partner_finance',{'p_telegram_user_id':41})
        if finance['available_minor']!=900 or finance['paid_checks']!=1 or finance['collected_minor']!=3000:
            raise AssertionError('Paid checkout commission or referral metrics differ')
        bot._partner_finance_callback('pf:withdraw:0',41,4,{})
        bot._commerce_text_input(41,'5.00',bot._waiting_user_inputs.pop(41))
        bot._commerce_text_input(41,'bank fixture destination',bot._waiting_user_inputs.pop(41))
        token=next(iter(bot._withdrawal_reviews))
        bot._partner_finance_callback('pf:confirm:'+token,41,4,{})
        bot._partner_finance_callback('pf:confirm:'+token,41,4,{})
        history=client.rpc('partner_withdrawals',{'p_telegram_user_id':41});withdrawal=history[0]['id']
        if len(history)!=1:raise AssertionError('Lost request acknowledgment duplicated hold')
        bot._withdrawal_admin_callback('wa:approve:'+withdrawal,9,4,{'id':'fixture-approval'})
        bot._withdrawal_admin_callback('wa:receipt:'+withdrawal,9,4,{})
        waiting=bot._waiting_user_inputs.pop(9);waiting['_telegram_input_message_id']=11
        bot._commerce_text_input(9,'bank TX001',waiting);payout=next(iter(bot._payout_reviews))
        bot._withdrawal_admin_callback('wa:paid:'+payout,9,4,{})
        bot._withdrawal_admin_callback('wa:paid:'+payout,9,4,{})
        rejected=client.rpc('request_withdrawal',{'p_telegram_user_id':41,'p_key':'reject-fixture','p_amount_minor':300,'p_destination':'bank fixture destination'})['withdrawal_id']
        bot._withdrawal_admin_callback('wa:reject:'+rejected,9,4,{'id':'fixture-reject'})
        bot._withdrawal_admin_callback('wa:reject:'+rejected,9,4,{'id':'fixture-reject-replay'})
        final=client.rpc('partner_finance',{'p_telegram_user_id':41})
        customer=client.rpc('wallet_state',{'p_telegram_user_id':31})['customer']
        if (final['available_minor'],final['reserved_minor'],final['withdrawn_minor'])!=(400,0,500) or customer['available_minor']!=6000:
            raise AssertionError('Withdrawal or customer balance changed incorrectly')
        if sql(database,statement='select count(*) from commerce.commissions')[0]!='1':raise AssertionError('Commission duplicated')
        evidence={'passed':True,'at':datetime.now(timezone.utc).isoformat(),'database':database,'production':False,
            'external_telegram_checker_and_payouts':'synthetic, no messages, checks, transfers or invoices',
            'charged_minor':3000,'refund_minor':3000,'commission_minor':900,'paid_checks':1,
            'withdrawn_minor':500,'available_partner_minor':400,'reserved_partner_minor':0,
            'request_lost_ack_recovered':True,'payout_lost_ack_recovered':True,'rejection_released_once':True,
            'customer_wallet_separate':True,'no_commission_before_delivery':True}
        path=ROOT/'docs/telegram_commerce'/('acceptance_'+database+'.json')
        path.write_text(json.dumps(evidence,indent=2),encoding='utf-8');print(json.dumps({'passed':True,'evidence':str(path)}))
    finally:fixture.doCleanups()


if __name__=='__main__':main()
