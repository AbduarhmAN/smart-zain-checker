"""Real local PostgreSQL, offer wizard, classified credit, free checkout and bridge."""
import json
from pathlib import Path
import secrets
import sys
from datetime import datetime,timezone

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from test_commerce_campaigns import CampaignTests
from test_commerce_checkout import TOKEN
from run_commerce_database_acceptance import DATA,sql
from run_commerce_bridge_acceptance import LocalClient,drain
from telegram_bot.commerce.bridge import CommerceBridge
from telegram_bot.commerce.client import CommerceUnavailable
from telegram_bot.commerce.finance import FinanceService,VerifiedReceipt


class LostActivationAck(LocalClient):
    def __init__(self,database):super().__init__(database);self.lost=set()
    def rpc(self,name,args):
        result=super().rpc(name,args)
        if name=='activate_campaign' and args['p_code'] not in self.lost:
            self.lost.add(args['p_code']);raise CommerceUnavailable('Synthetic lost activation acknowledgment after commit')
        return result


def finish(fixture,client,token,records):
    bot=fixture.bot;job=bot.queue_service.get_jobs()[0];drain(bot.commerce_bridge)
    order=bot._sheet_drafts[token]['commerce_order_id']
    result=fixture.fixture.root/'campaign-result.xlsx';result.write_bytes((fixture.fixture.root/job['filename']).read_bytes())
    completed=bot.commerce_bridge.capture_completion({'commerce_order_id':order,'job_id':job['id'],
        'telegram_chat_id':31,'result_file':str(result),'total':2,'commerce_records':records})
    drain(bot.commerce_bridge)
    bot.commerce_bridge.capture_delivery(completed,{'chat':{'id':31},'message_id':10,'document':{'file_id':'campaign-fixture-result'}})
    drain(bot.commerce_bridge);return client.rpc('settle_order',{'p_order_id':order})


def main():
    if Path(sql('postgres',statement='show data_directory')[0]).resolve()!=DATA.resolve():raise RuntimeError('Refusing writes to unknown PostgreSQL instance')
    database='commerce_campaign_'+secrets.token_hex(6);sql('postgres',statement=f'create database {database}')
    for path in sorted((ROOT/'supabase/migrations').glob('*commerce*.sql')):sql(database,file=path)
    fixture=CampaignTests();fixture.setUp();second=None
    try:
        client=LostActivationAck(database);bot=fixture.bot;bot._commerce_client=client
        bot.commerce_bridge=CommerceBridge(fixture.fixture.root,client=client)
        fixture.callback('oa:limits:0');review=next(iter(bot._campaign_reviews))
        for value in ['50','10','100','1000','3','30','10']:fixture.text(value)
        fixture.callback('oa:save:'+review)
        coupon=fixture.offer('percent','CAMPAIGN_PERCENT');fixture.callback('oa:save:'+coupon)
        grant=fixture.offer('wallet_credit','CAMPAIGN_GRANT');fixture.callback('oa:save:'+grant)
        partner=sql(database,statement="select commerce_rpc.create_partner(41,'CAMPAIGN_PARTNER')->>'partner_id'")[0]
        sql(database,statement=f"select commerce_rpc.configure_partner('{partner}',9,3000,'forever',null,true)")
        client.rpc('attribute_referral',{'p_telegram_user_id':31,'p_code':'CAMPAIGN_PARTNER','p_source':'manual'})
        FinanceService(client).confirm_payment(31,'campaign-cash-fund',VerifiedReceipt('fixture','campaign-cash-fund',9000,'fixture-proof'))
        for code in ['CAMPAIGN_PERCENT','CAMPAIGN_GRANT']:
            bot._activate_offer(31,code,4,'fixture-activate');bot._activate_offer(31,code,4,'fixture-repeat')
        if client.rpc('wallet_state',{'p_telegram_user_id':31})['customer']['promotional_available_minor']!=1000:
            raise AssertionError('Lost acknowledgment duplicated or lost classified grant')
        fixture.fixture.prepare();order=bot._sheet_drafts[TOKEN]['commerce_order_id']
        if client.rpc('checkout_state',{'p_order_id':order,'p_telegram_user_id':31})['quoted_minor']!=5400:raise AssertionError('Wizard offer did not reach checkout price')
        fixture.fixture.callback('wallet')
        settled=finish(fixture,client,TOKEN,[{'row':2,'status':'match','live_sar':0},{'row':4,'status':'error'}])
        if (settled['charged_minor'],settled['refunded_minor'],settled['collected_minor'],settled['commission_minor'])!=(2700,2700,1700,510):
            raise AssertionError('Actual checker settlement failed to exclude promotional funding from collected revenue')
        free=fixture.offer('free_checks','CAMPAIGN_FREE',eligibility='any');fixture.callback('oa:save:'+free)
        bot._activate_offer(31,'CAMPAIGN_FREE',4,'fixture-activate');bot._activate_offer(31,'CAMPAIGN_FREE',4,'fixture-repeat')
        second=CampaignTests();second.setUp();second.bot._commerce_client=client
        second.bot.commerce_bridge=CommerceBridge(second.fixture.root,client=client)
        new_token='123456abcdef';second.bot._sheet_drafts[new_token]=second.bot._sheet_drafts.pop(TOKEN)
        second.bot._commerce_start_checkout(new_token,31,4)
        order=second.bot._sheet_drafts[new_token]['commerce_order_id']
        if client.rpc('checkout_state',{'p_order_id':order,'p_telegram_user_id':31})['quoted_minor']!=0:raise AssertionError('Free offer requires a payment')
        second.callback('co:wallet:'+new_token,user=31)
        free_result=finish(second,client,new_token,[{'row':2,'status':'match','live_sar':0},{'row':4,'status':'not_found'}])
        if (free_result['charged_minor'],free_result['collected_minor'],free_result['commission_minor'])!=(0,0,0):raise AssertionError('Free check created paid revenue or commission')
        wallet=client.rpc('wallet_state',{'p_telegram_user_id':31})['customer'];partner_finance=client.rpc('partner_finance',{'p_telegram_user_id':41})
        if wallet['available_minor']!=7300 or wallet['promotional_available_minor']!=0 or partner_finance['available_minor']!=510 or partner_finance['paid_checks']!=1:
            raise AssertionError('Campaign checkout altered classified wallet or paid service counters')
        evidence={'passed':True,'at':datetime.now(timezone.utc).isoformat(),'database':database,'production':False,
            'external_messages_checker_invoices_and_transfers':'synthetic, no live operations','offer_created_via_admin_wizard':True,
            'lost_activation_ack_recovered':True,'grant_minor':1000,'discount_minor':600,'charged_minor':2700,'refunded_minor':2700,
            'collected_cash_minor':1700,'commission_minor':510,'ending_customer_minor':7300,'paid_checks':1,
            'free_checkout_no_invoice':True,'free_checkout_revenue_minor':0,'referral_preserved':True}
        path=ROOT/'docs/telegram_commerce'/('acceptance_'+database+'.json');path.write_text(json.dumps(evidence,indent=2),encoding='utf-8')
        print(json.dumps({'passed':True,'evidence':str(path)}))
    finally:
        if second:second.doCleanups()
        fixture.doCleanups()


if __name__=='__main__':main()
