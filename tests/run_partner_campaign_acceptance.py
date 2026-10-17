"""Isolated real PostgreSQL: partner donations from the current order, no past balance."""
import json
import secrets
import sys
from pathlib import Path
from datetime import datetime,timezone
from decimal import Decimal

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from domain import decimal_json as json
from run_commerce_database_acceptance import sql,DATA
from run_commerce_bridge_acceptance import LocalClient
from telegram_bot.commerce.finance import FinanceService,VerifiedReceipt

def main():
    assert Path(sql('postgres',statement='show data_directory')[0]).resolve()==DATA.resolve()
    database='commerce_partner_campaign_'+secrets.token_hex(6)
    sql('postgres',statement='create database '+database)
    for path in sorted((ROOT/'supabase/migrations').glob('*commerce*.sql')):sql(database,file=path)
    client=LocalClient(database);finance=FinanceService(client)
    def scalar(statement):return sql(database,statement=statement)[0].splitlines()[-1]
    partner=scalar("select commerce_rpc.create_partner(41,'PARTNER30')->>'partner_id'")
    scalar(f"select commerce_rpc.configure_partner('{partner}',9,3000,'forever',null,true)")
    policy={'max_percent_basis_points':5000,'max_checks_per_user':4000,'max_discount_minor_per_user':100000,
        'max_total_discount_minor':1000000,'max_duration_seconds':259200,'max_wallet_credit_minor':100000,'max_redemptions':1000}
    client.rpc('configure_campaign_policy',{'p_actor_telegram_id':9,'p_rules':policy,'p_reference':'synthetic-approved-limits'})
    rules={'benefit_kind':'percent','benefit_value':2000,'eligibility':'any','check_types':['account','service'],
        'max_checks':4000,'max_discount_minor':100000,'max_total_discount_minor':1000000,'duration_seconds':259200,'max_redemptions':1000}
    args={'p_telegram_user_id':41,'p_key':'synthetic-offer','p_code':'SAVE20','p_rules':rules,'p_reference':'synthetic-native-confirmation'}
    offer=client.rpc('create_partner_campaign',args)
    assert client.rpc('create_partner_campaign',args)['replayed']
    assert client.rpc('partner_finance',{'p_telegram_user_id':41})['available_minor']==0
    blocked=0
    for uid,value in [(42,2000),(41,3100)]:
        try:client.rpc('create_partner_campaign',{**args,'p_telegram_user_id':uid,'p_key':'blocked'+str(uid),'p_rules':{**rules,'benefit_value':value}})
        except RuntimeError:blocked+=1
    assert blocked==2
    assert client.rpc('partner_campaign_context',{'p_telegram_user_id':42})=={'is_partner':False}
    assert len(client.rpc('list_partner_campaigns',{'p_telegram_user_id':41,'p_offset':0}))==1
    try:client.rpc('pause_partner_campaign',{'p_telegram_user_id':42,'p_campaign_id':offer['campaign_id'],'p_reference':'unowned'})
    except RuntimeError:pass
    else:raise AssertionError('Non-owner paused another partner campaign')
    def fund(uid,amount=20000):
        finance.confirm_payment(uid,'fund-'+str(uid),VerifiedReceipt('fixture','fund-'+str(uid),amount,'synthetic-proof'))
        assert client.rpc('activate_campaign',{'p_telegram_user_id':uid,'p_code':'SAVE20','p_reference':'synthetic-customer-code'})['state']=='activated'
    def create(uid,count,price=None):
        if price is not None:scalar(f"update commerce.customer_price_settings set base_minor={price},service_excess_minor={price} where target_telegram_id=0")
        return client.rpc('create_order',{'p_telegram_user_id':uid,'p_request_key':'order-'+str(uid),
            'p_rows':[{'row_number':i+2,'check_type':'account'} for i in range(count)],'p_source':{'file_sha256':'c'*64}})['order_id']
    def settle(uid,order,count,success):
        state=client.rpc('checkout_state',{'p_order_id':order,'p_telegram_user_id':uid})
        client.rpc('checkout_consent',{'p_order_id':order,'p_telegram_user_id':uid,
            'p_policy_version':state['pricing_snapshot']['policy_version'],'p_reference':'synthetic-consent-'+str(uid)})
        assert client.rpc('reserve_order',{'p_order_id':order,'p_telegram_user_id':uid,
            'p_consent_reference':'synthetic-consent-'+str(uid),'p_payment_id':None})['state']=='held'
        job='synthetic-job-'+str(uid)
        client.rpc('bind_checker_job',{'p_order_id':order,'p_job_id':job})
        if len(success)<count:
            client.rpc('request_customer_stop',{'p_order_id':order,'p_telegram_user_id':uid,'p_reference':'synthetic-stop-'+str(uid)})
        client.rpc('record_terminal_results',{'p_order_id':order,'p_job_id':job,
            'p_rows':[{'row_number':i+2,'state':'succeeded' if i in success else 'cancelled',
                'verified_result':i in success} for i in range(count)],'p_result_sha256':'d'*64})
        client.rpc('confirm_result_delivery',{'p_order_id':order,'p_telegram_user_id':uid,'p_message_id':101,
            'p_file_id':'synthetic-result','p_result_sha256':'d'*64})
        result=client.rpc('settle_order',{'p_order_id':order})
        replay=client.rpc('settle_order',{'p_order_id':order})
        assert replay['replayed'] and replay['charged_minor']==result['charged_minor']
        assert Decimal(scalar(f"select coalesce(sum(amount_minor),0) from commerce.refunds where order_id='{order}'"))==result['refunded_minor']
        return result
    fund(31);order=create(31,2,5000)
    state=client.rpc('checkout_state',{'p_order_id':order,'p_telegram_user_id':31})
    assert state['quoted_minor']==10000 and state['pricing_snapshot']['estimated_final_minor']==8000
    # An administrative rate change cannot rewrite an already frozen order.
    scalar(f"select commerce_rpc.configure_partner('{partner}',9,1000,'forever',null,true)")
    full=settle(31,order,2,{0,1})
    assert (full['charged_minor'],full['commission_minor'],full['platform_minor'],full['refunded_minor'])==(8000,1000,7000,2000),full
    scalar(f"select commerce_rpc.configure_partner('{partner}',9,3000,'forever',null,true)")
    fund(32);partial_order=create(32,2,5000);partial=settle(32,partial_order,2,{0})
    assert (partial['charged_minor'],partial['commission_minor'],partial['platform_minor'],partial['refunded_minor'])==(4000,500,3500,6000),partial
    fund(33);stopped_order=create(33,2,5000);stopped=settle(33,stopped_order,2,set())
    assert (stopped['charged_minor'],stopped['commission_minor'],stopped['refunded_minor'])==(0,0,10000)
    fund(34);cent_order=create(34,100,1);cent=settle(34,cent_order,100,set(range(100)))
    assert (cent['charged_minor'],cent['commission_minor'],cent['platform_minor'])==(80,10,70),cent
    fund(35);cent_partial_order=create(35,100,1);cent_partial=settle(35,cent_partial_order,100,{i for i in range(100) if i%2})
    assert (cent_partial['charged_minor'],cent_partial['commission_minor'],cent_partial['platform_minor'],cent_partial['refunded_minor'])==(40,5,35,60),cent_partial
    other=scalar("select commerce_rpc.create_partner(51,'OTHER30')->>'partner_id'")
    scalar(f"select commerce_rpc.configure_partner('{other}',9,3000,'forever',null,true)")
    client.rpc('register_telegram_user',{'p_telegram_user_id':36,'p_username':None})
    client.rpc('attribute_referral',{'p_telegram_user_id':36,'p_code':'OTHER30','p_source':'manual'})
    assert client.rpc('activate_campaign',{'p_telegram_user_id':36,'p_code':'SAVE20','p_reference':'synthetic-other-source'})['state']=='ineligible_customer'
    assert scalar("select partner_id from commerce.referrals r join commerce.users u on u.id=r.user_id where u.telegram_user_id=36")==other
    # A partner cannot spend a promotional grant that earns no commission.
    grant={**rules,'benefit_kind':'wallet_credit','benefit_value':10000,'max_checks':1}
    client.rpc('create_platform_campaign',{'p_actor_telegram_id':9,'p_key':'grant','p_code':'GRANT100','p_rules':grant,'p_reference':'synthetic-grant'})
    client.rpc('activate_campaign',{'p_telegram_user_id':37,'p_code':'GRANT100','p_reference':'synthetic-grant-use'})
    client.rpc('activate_campaign',{'p_telegram_user_id':37,'p_code':'SAVE20','p_reference':'synthetic-offer-use'})
    grant_order=create(37,10,1);state=client.rpc('checkout_state',{'p_order_id':grant_order,'p_telegram_user_id':37})
    client.rpc('checkout_consent',{'p_order_id':grant_order,'p_telegram_user_id':37,'p_policy_version':state['pricing_snapshot']['policy_version'],'p_reference':'synthetic-grant-consent'})
    reserve=client.rpc('reserve_order',{'p_order_id':grant_order,'p_telegram_user_id':37,'p_consent_reference':'synthetic-grant-consent','p_payment_id':None})
    assert reserve['state']=='insufficient_balance' and reserve['available_minor']==0 and reserve['cash_only']
    assert client.rpc('wallet_state',{'p_telegram_user_id':37})['customer']['promotional_available_minor']==10000
    fund(39);tiny_order=create(39,1,1);tiny=settle(39,tiny_order,1,{0})
    assert (tiny['charged_minor'],tiny['commission_minor'],tiny['platform_minor'],tiny['refunded_minor'])==(Decimal('0.8'),Decimal('0.1'),Decimal('0.7'),Decimal('0.2')),tiny
    # After an offer ends, the partner again earns the full approved percentage.
    fund(38);client.rpc('pause_partner_campaign',{'p_telegram_user_id':41,'p_campaign_id':offer['campaign_id'],'p_reference':'synthetic-pause'})
    plain_order=create(38,100,1);plain=settle(38,plain_order,100,set(range(100)))
    assert (plain['charged_minor'],plain['commission_minor'])==(100,30),plain
    assert client.rpc('checkout_state',{'p_order_id':plain_order,'p_telegram_user_id':38})['pricing_snapshot']['financial_precision']=='exact_decimal'
    scalar(f"select commerce_rpc.configure_partner('{partner}',9,10,'forever',null,true)")
    finance.confirm_payment(40,'fund-micro',VerifiedReceipt('fixture','fund-micro',100,'synthetic-proof'))
    client.rpc('attribute_referral',{'p_telegram_user_id':40,'p_code':'PARTNER30','p_source':'manual'})
    micro=settle(40,create(40,1,1),1,{0})
    assert micro['commission_minor']==Decimal('0.001'),micro
    from domain.decimal_money import egp_from_minor
    assert egp_from_minor(micro['commission_minor'])=='0.00001'
    scalar(f"select commerce_rpc.configure_partner('{partner}',9,3000,'forever',null,true)")
    finance.confirm_payment(45,'fund-pending-old',VerifiedReceipt('fixture','fund-pending-old',100,'synthetic-proof'))
    client.rpc('attribute_referral',{'p_telegram_user_id':45,'p_code':'PARTNER30','p_source':'manual'})
    old_pending=create(45,1,1)
    # Model the unchanged pricing snapshot of a request issued before decimal storage.
    scalar(f"update commerce.orders set pricing_snapshot=pricing_snapshot-'financial_precision' where id='{old_pending}'")
    old_pending_result=settle(45,old_pending,1,{0})
    assert (old_pending_result['charged_minor'],old_pending_result['commission_minor'])==(1,Decimal('0.3'))
    # All source journals remain balanced, including discount rebates and income.
    assert int(scalar("select count(*) from (select journal_id,sum(delta_minor) as delta from commerce.ledger_entries group by journal_id having sum(delta_minor)<>0) x"))==0
    evidence={'passed':True,'at':datetime.now(timezone.utc).isoformat(),'database':database,'production':False,
        'campaign_creation_with_zero_prior_earnings':True,'full':full,'partial':partial,'stopped':stopped,
        'cent_aggregation':cent,'cent_partial':cent_partial,'after_offer_ordinary_commission':plain,'tiny_fraction':tiny,
        'micro_commission':micro,'displayed_micro_egp':'0.00001','frozen_rate_preserved':True,
        'previously_issued_unsettled_order_commission':old_pending_result,
        'unowned_campaign_denied':True,'discount_above_share_denied':True,'referral_preserved':True,
        'promotional_money_separate':True,'settlement_replay_no_duplicate':True}
    path=ROOT/'docs/telegram_commerce'/('acceptance_'+database+'.json');path.write_text(json.dumps(evidence,indent=2),encoding='utf-8')
    print(json.dumps({'passed':True,'evidence':str(path)}))

if __name__=='__main__':main()
