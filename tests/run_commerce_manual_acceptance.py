"""Real local manual assignment with lost acknowledgments, no provider transactions."""
import json
from pathlib import Path
import secrets
import sys
from datetime import datetime,timezone
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from test_commerce_manual_payments import ManualPaymentTests
from test_commerce_checkout import TOKEN
from run_commerce_database_acceptance import DATA,sql
from run_commerce_bridge_acceptance import LocalClient,drain
from telegram_bot.commerce.bridge import CommerceBridge
from telegram_bot.commerce.client import CommerceUnavailable


class LostApprovalClient(LocalClient):
    def __init__(self,database):super().__init__(database);self.lose_approval=True
    def rpc(self,name,args):
        result=super().rpc(name,args)
        if name=='approve_payment_claim' and self.lose_approval:
            self.lose_approval=False;raise CommerceUnavailable('Synthetic lost committed approval acknowledgment')
        return result


def main():
    if Path(sql('postgres',statement='show data_directory')[0]).resolve()!=DATA.resolve():
        raise RuntimeError('Refusing writes to an unknown local PostgreSQL instance')
    database='commerce_manual_'+secrets.token_hex(6)
    sql('postgres',statement=f'create database {database}')
    for path in sorted((ROOT/'supabase/migrations').glob('*commerce*.sql')):sql(database,file=path)
    fixture=ManualPaymentTests();fixture.setUp()
    try:
        client=LostApprovalClient(database);bot=fixture.bot
        bot._commerce_client=client
        bot.commerce_bridge=CommerceBridge(fixture.root,client=client)
        bot._sheet_drafts[TOKEN].pop('commerce_order_id')
        bot._commerce_start_checkout(TOKEN,31,4)
        bot._commerce_checkout_callback('co:binance:'+TOKEN,31,4,{'id':'user-consent:1'})
        bot._commerce_checkout_callback('co:sendref:'+TOKEN,31,4,{'id':'user-reference:1'})
        bot._commerce_text_input(31,'ORDER1',bot._waiting_user_inputs.pop(31))
        claim=bot._sheet_drafts[TOKEN]['commerce_claim_id'];order=bot._sheet_drafts[TOKEN]['commerce_order_id']
        if sql(database,statement='select count(*) from commerce.payments')[0]!='0':
            raise AssertionError('A raw reference was treated as paid money')
        with patch('telegram_bot.crypto_pay.BINANCE_PAY.get_recent_transactions',return_value=[fixture.exchange_record()]):
            bot._payment_review_callback('cm:review:'+claim,9,4,{'id':'admin-review:1'})
            if sql(database,statement='select count(*) from commerce.payments')[0]!='0':
                raise AssertionError('Preview alone paid the request')
            bot._payment_review_callback('cm:approve:'+claim,9,4,{'id':'admin-approval:1'})
            if bot.queue_service.get_jobs():raise AssertionError('Lost confirmation should recover its receipt before enqueue')
            if sql(database,statement='select count(*) from commerce.payments')[0]!='1':
                raise AssertionError('Atomic claim approval was not committed')
            bot._recover_commerce_checkouts()
            bot._payment_review_callback('cm:approve:'+claim,9,4,{'id':'admin-approval:2'})
        jobs=bot.queue_service.get_jobs()
        if len(jobs)!=1:raise AssertionError('Manual payment retry duplicated its checking job')
        drain(bot.commerce_bridge)
        result=fixture.root/'manual-result.xlsx';result.write_bytes((fixture.root/jobs[0]['filename']).read_bytes())
        completion=bot.commerce_bridge.capture_completion({'commerce_order_id':order,'job_id':jobs[0]['id'],
            'telegram_chat_id':31,'result_file':str(result),'total':2,
            'commerce_records':[{'row':2,'status':'not_found'},{'row':4,'status':'error'}]})
        drain(bot.commerce_bridge)
        bot.commerce_bridge.capture_delivery(completion,{'chat':{'id':31},'message_id':10,
            'document':{'file_id':'synthetic-manual-result'}})
        drain(bot.commerce_bridge)
        wallet=client.rpc('wallet_state',{'p_telegram_user_id':31})['customer']
        if wallet['available_minor']!=3000 or wallet['reserved_minor']!=0:
            raise AssertionError('Direct payment settlement did not refund the failed check')
        record=client.rpc('payment_claim',{'p_claim_id':claim})
        if record['state']!='approved' or record['actor_telegram_id']!=9:
            raise AssertionError('The original human assignment audit was not retained')
        if sql(database,statement='select count(*) from commerce.payments')[0]!='1':
            raise AssertionError('Repeated assignment created another ledger payment')
        evidence={'passed':True,'at':datetime.now(timezone.utc).isoformat(),'database':database,
            'production':False,'provider_and_telegram':'synthetic, no live transfers or messages',
            'raw_reference_credited':False,'preview_credited':False,'approved_payments':1,'queued_jobs':1,
            'lost_approval_ack_recovered':True,'charged_minor':3000,'refunded_minor':3000,'assignment_actor':9}
        path=ROOT/'docs/telegram_commerce'/('acceptance_'+database+'.json')
        path.write_text(json.dumps(evidence,indent=2),encoding='utf-8')
        print(json.dumps({'passed':True,'evidence':str(path)}))
    finally:fixture.doCleanups()


if __name__=='__main__':main()
