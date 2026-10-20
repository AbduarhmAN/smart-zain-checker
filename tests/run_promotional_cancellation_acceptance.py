"""Real isolated PostgreSQL, Telegram callbacks and competing reclaim requests."""
import concurrent.futures
from datetime import datetime,timezone
import json
from pathlib import Path
import secrets
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from run_commerce_database_acceptance import DATA,sql
from run_commerce_bridge_acceptance import LocalClient
from test_promotional_campaign_cancellation import PromotionalCancellationTests
from telegram_bot.commerce.client import CommerceUnavailable


def main():
    if Path(sql('postgres',statement='show data_directory')[0]).resolve()!=DATA.resolve():
        raise RuntimeError('Refusing writes to an unknown PostgreSQL instance')
    database='commerce_promo_cancel_'+secrets.token_hex(6)
    sql('postgres',statement=f'create database {database}')
    for path in sorted((ROOT/'supabase/migrations').glob('*commerce*.sql')):sql(database,file=path)
    for name in ['commerce_promotional_campaign_cancellation.sql','commerce_promotional_expiry.sql',
                 'commerce_cash_code_cancellation.sql','commerce_partner_campaign_approvals.sql']:
        sql(database,file=ROOT/'supabase/tests'/name)
    sql(database,statement="select commerce_rpc.configure_support_actors('[9,21]'::jsonb,'acceptance-promotion-admins')")
    client=LocalClient(database)
    rules={'benefit_kind':'wallet_credit','benefit_value':100,'eligibility':'any','check_types':['account','service'],
        'max_checks':10,'max_discount_minor':100,'max_total_discount_minor':10000,'duration_seconds':3600,'max_redemptions':50}
    client.rpc('configure_campaign_policy',{'p_actor_telegram_id':9,'p_reference':'acceptance-policy','p_rules':{
        'max_percent_basis_points':10000,'max_checks_per_user':4000,'max_discount_minor_per_user':100000,
        'max_total_discount_minor':1000000,'max_duration_seconds':86400,'max_wallet_credit_minor':100000,'max_redemptions':100}})
    def create(key):
        return client.rpc('create_platform_campaign',{'p_actor_telegram_id':9,'p_key':key,'p_code':key,
            'p_rules':rules,'p_reference':'acceptance-create'})['campaign_id']
    campaign=create('TELEGRAM_RECLAIM')
    client.rpc('activate_campaign',{'p_telegram_user_id':899500,'p_code':'TELEGRAM_RECLAIM','p_reference':'grant'})
    fixture=PromotionalCancellationTests();fixture.setUp()
    try:
        fixture.bot._commerce_client=client
        fixture.bot._campaign_admin_callback('oa:cancelpromo:'+campaign,9,4,{})
        assert '1.00' in fixture.panel().text
        assert not client.rpc('promotion_cancellation_record',{'p_actor_telegram_id':9,'p_campaign_id':campaign})['cancelled']
        def lost_ack(name,args):
            result=client.rpc(name,args)
            if name=='cancel_promotional_campaign':raise CommerceUnavailable('Synthetic lost cancellation acknowledgement')
            return result
        fixture.bot._commerce_client=type('LostAck',(),{'rpc':staticmethod(lost_ack)})()
        fixture.bot._campaign_admin_callback('oa:cancelpromook:'+campaign,9,4,{'id':'acceptance-lost'})
        # Durable cancellation is sufficient for background reclamation without user activity.
        client.rpc('expire_promotional_credit',{})
        fixture.bot._commerce_client=client
        fixture.bot._campaign_admin_callback('oa:cancelpromook:'+campaign,9,4,{'id':'acceptance-retry'})
        assert '1.00' in fixture.panel().text
        record=client.rpc('promotion_cancellation_record',{'p_actor_telegram_id':9,'p_campaign_id':campaign})
        assert record['reclaimed_minor']==100 and record['available_minor']==0
    finally:fixture.doCleanups()

    campaign=create('CONCURRENT_RECLAIM')
    client.rpc('activate_campaign',{'p_telegram_user_id':899598,'p_code':'CONCURRENT_RECLAIM','p_reference':'seed-grant-before-race'})
    def competing(index):
        if index%2:
            return client.rpc('cancel_promotional_campaign',{'p_actor_telegram_id':9,'p_campaign_id':campaign,'p_reference':'cancel-'+str(index)})
        return client.rpc('activate_campaign',{'p_telegram_user_id':899600+index,'p_code':'CONCURRENT_RECLAIM','p_reference':'activate-'+str(index)})
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:outcomes=list(pool.map(competing,range(16)))
    assert sum(result.get('replayed') is False for result in outcomes)==1
    assert client.rpc('activate_campaign',{'p_telegram_user_id':899700,'p_code':'CONCURRENT_RECLAIM','p_reference':'after-cancel'})['state']=='inactive_offer'
    def reclaim(index):
        if index%2:return client.rpc('wallet_state',{'p_telegram_user_id':899598 if index==1 else 899600+(index//2)*2})
        return client.rpc('reclaim_cancelled_promotion',{'p_actor_telegram_id':9,'p_campaign_id':campaign,'p_limit':1})
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(reclaim,range(16)))
    client.rpc('expire_promotional_credit',{})
    record=client.rpc('promotion_cancellation_record',{'p_actor_telegram_id':9,'p_campaign_id':campaign})
    grants=1+sum(result.get('state')=='activated' for result in outcomes)
    assert record['available_minor']==0 and record['reclaimed_minor']==grants*100
    assert sql(database,statement="select count(*) from commerce.promotional_credit_lots where available_minor+reserved_minor+consumed_minor+expired_minor+reclaimed_minor<>granted_minor")[0]=='0'
    assert sql(database,statement="select count(*) from commerce.journals j where (select sum(delta_minor) from commerce.ledger_entries where journal_id=j.id)<>0")[0]=='0'
    evidence={'at':datetime.now(timezone.utc).isoformat(),'production':False,'database':database,'passed':True,
        'checks':['all migrations applied','new cancellation SQL regression','existing expiry and cash-code regression',
            'partner approval snapshots preserved','Telegram review does not cancel','lost acknowledgement followed by background reclaim',
            '16 competing activation and cancellation calls','16 competing wallet reads and reclaim calls',
            'protected held funds; later promotional refunds reclaimed','cash preserved; no cash payout','balanced journals and conserved grants'],
        'concurrent_grants':grants,'concurrent_reclaimed_minor':int(record['reclaimed_minor'])}
    path=ROOT/'docs/telegram_commerce'/('acceptance_'+database+'.json')
    path.write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':True,'evidence':str(path)}))


if __name__=='__main__':main()
