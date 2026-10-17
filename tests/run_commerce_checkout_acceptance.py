"""Real local checkout -> queue -> outbox -> delivery -> settlement, no live APIs."""
import json
from pathlib import Path
import secrets
import sys
from datetime import datetime, timezone

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from test_commerce_checkout import CheckoutTests,TOKEN
from run_commerce_database_acceptance import DATA,sql
from run_commerce_bridge_acceptance import LocalClient,drain
from manager.queue import QueueService
from telegram_bot.commerce.bridge import CommerceBridge
from telegram_bot.commerce.client import CommerceUnavailable
from telegram_bot.commerce.finance import FinanceService,VerifiedReceipt


class LostAckClient(LocalClient):
    def __init__(self,database):
        super().__init__(database)
        self.lose_reserve=True

    def rpc(self,name,args):
        result=super().rpc(name,args)
        if name=='reserve_order' and self.lose_reserve:
            self.lose_reserve=False
            raise CommerceUnavailable('Synthetic lost reservation acknowledgment')
        return result


def main():
    if Path(sql('postgres',statement='show data_directory')[0]).resolve()!=DATA.resolve():
        raise RuntimeError('Refusing writes to an unknown local PostgreSQL instance')
    database='commerce_checkout_'+secrets.token_hex(6)
    sql('postgres',statement=f'create database {database}')
    for path in sorted((ROOT/'supabase/migrations').glob('*commerce*.sql')):
        sql(database,file=path)
    fixture=CheckoutTests()
    fixture.setUp()
    try:
        client=LostAckClient(database)
        bot=fixture.bot
        bot._commerce_client=client
        bot.commerce_bridge=CommerceBridge(fixture.root,client=client)
        FinanceService(client).confirm_payment(31,'checkout-fixture-topup',
            VerifiedReceipt('fixture','checkout-fixture-transfer',9000,'synthetic-proof'))
        fixture.prepare()
        order=bot._sheet_drafts[TOKEN]['commerce_order_id']
        fixture.callback('wallet')
        if bot.queue_service.get_jobs():
            raise AssertionError('Lost acknowledgment should recover before enqueue')
        before=client.rpc('wallet_state',{'p_telegram_user_id':31})['customer']
        if before['available_minor']!=3000 or before['reserved_minor']!=6000:
            raise AssertionError('Actual checkout did not reserve exactly two executable checks')
        # Lose in-memory drafts, restore the queue, and replace the uploaded
        # original. The immutable priced input must remain the executable one.
        fixture.original.write_bytes(b'synthetic-original-replacement')
        bot._sheet_drafts={}
        bot.queue_service=QueueService(fixture.root/'.sheet_queue.json')
        bot._restore_sheet_drafts()
        bot._recover_commerce_checkouts()
        bot._recover_commerce_checkouts()
        jobs=bot.queue_service.get_jobs()
        if len(jobs)!=1 or jobs[0]['total_records']!=2:
            raise AssertionError('Restart duplicated a paid queue job or changed its rows')
        job=jobs[0]
        drain(bot.commerce_bridge)
        state=client.rpc('checkout_state',{'p_order_id':order,'p_telegram_user_id':31})
        if state['checker_job_id']!=job['id'] or not state['refund_consent_reference']:
            raise AssertionError('Queue binding or explicit consent did not reach PostgreSQL')
        result=fixture.root/'synthetic-result.xlsx'
        result.write_bytes((fixture.root/job['filename']).read_bytes())
        completion=bot.commerce_bridge.capture_completion({'commerce_order_id':order,'job_id':job['id'],
            'telegram_chat_id':31,'result_file':str(result),'total':2,
            'commerce_records':[{'row':2,'status':'match','live_sar':0},{'row':4,'status':'error'}]})
        drain(bot.commerce_bridge)
        if client.rpc('checkout_state',{'p_order_id':order,'p_telegram_user_id':31})['payment_state']!='reserved':
            raise AssertionError('Checkout settled before the result delivery receipt')
        bot.commerce_bridge.capture_delivery(completion,
            {'chat':{'id':31},'message_id':10,'document':{'file_id':'synthetic-delivery-file'}})
        drain(bot.commerce_bridge)
        restarted=CommerceBridge(fixture.root,client=client)
        restarted.recover();drain(restarted)
        final=client.rpc('wallet_state',{'p_telegram_user_id':31})['customer']
        if final['available_minor']!=6000 or final['reserved_minor']!=0:
            raise AssertionError('Actual delivered checkout settled or refunded incorrectly')
        if sql(database,statement=f"select count(*) from commerce.refunds where order_id='{order}'")[0]!='1':
            raise AssertionError('Delivery replay duplicated the refund')
        evidence={'passed':True,'at':datetime.now(timezone.utc).isoformat(),'database':database,
            'production':False,'fixture':str(fixture.root),'telegram_and_checker':'synthetic, no external messages or checks',
            'reserved_minor':6000,'charged_minor':3000,'refunded_minor':3000,'ending_available_minor':6000,
            'quoted_checks':2,'raw_rows':3,'queued_jobs':1,'refunds':1,
            'lost_reservation_ack_recovered':True,'immutable_input_after_original_replaced':True,
            'explicit_policy_consent':True,'no_settlement_before_delivery':True}
        path=ROOT/'docs/telegram_commerce'/('acceptance_'+database+'.json')
        path.write_text(json.dumps(evidence,indent=2),encoding='utf-8')
        print(json.dumps({'passed':True,'evidence':str(path)}))
    finally:
        fixture.doCleanups()


if __name__=='__main__':main()
