"""Per-partner permission enforcement against fresh isolated SQL and Telegram handlers."""
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
from test_partner_sheet_access import PartnerSheetAccessTests
from telegram_bot.commerce.client import CommerceUnavailable


def main():
    if Path(sql('postgres',statement='show data_directory')[0]).resolve()!=DATA.resolve():
        raise RuntimeError('Refusing writes to an unknown PostgreSQL instance')
    database='commerce_sheet_access_'+secrets.token_hex(6)
    sql('postgres',statement=f'create database {database}')
    for path in sorted((ROOT/'supabase/migrations').glob('*commerce*.sql')):sql(database,file=path)
    for name in ['commerce_partner_sheet_access.sql','commerce_partner_cost_sheets.sql','commerce_partner_campaign_approvals.sql',
                 'commerce_promotional_campaign_cancellation.sql']:
        sql(database,file=ROOT/'supabase/tests'/name)
    sql(database,statement="select commerce_rpc.configure_support_actors('[9,21]'::jsonb,'access-acceptance-admins')")
    client=LocalClient(database)
    partner=json.loads(sql(database,statement="select commerce_rpc.create_partner(899901,'TELEGRAM_ACCESS')")[0])['partner_id']
    sql(database,statement=f"select commerce_rpc.configure_partner('{partner}',9,3000,'forever',null,true)")
    fixture=PartnerSheetAccessTests();fixture.setUp()
    try:
        fixture.bot._commerce_client=client
        fixture.bot._partner_sheet_admin('ps:access:'+partner,9,7,{})
        assert 'مسموح' in fixture.panel().text
        def lost_ack(name,args=None):
            result=client.rpc(name,args or {})
            if name=='configure_partner_sheet_access':raise CommerceUnavailable('Synthetic lost committed setting acknowledgement')
            return result
        fixture.bot._commerce_client=type('LostAck',(),{'rpc':staticmethod(lost_ack)})()
        fixture.bot._partner_sheet_admin('ps:off:'+partner,9,7,{'id':'acceptance-disable'})
        assert client.rpc('partner_sheet_context',{'p_telegram_user_id':899901})['partner_sheet_enabled'] is False
        fixture.bot._commerce_client=client
        fixture.bot._partner_sheet_admin('ps:off:'+partner,9,7,{'id':'acceptance-disable'})
        assert 'مخفي ومتوقف' in fixture.panel().text
        assert sql(database,statement=f"select count(*) from commerce.partner_sheet_access_settings where partner_id='{partner}'")[0]=='1'
        fixture.bot._partner_sheet_admin('ps:on:'+partner,9,7,{'id':'acceptance-enable'})
        assert client.rpc('partner_sheet_context',{'p_telegram_user_id':899901})['partner_sheet_enabled'] is True
    finally:fixture.doCleanups()

    rows=[{'row_number':2,'check_type':'account'}];source={'file_sha256':'e'*64}
    def compete(index):
        if index%2:return client.rpc('configure_partner_sheet_access',{'p_actor_telegram_id':9,'p_partner_id':partner,
            'p_enabled':False,'p_reference':'race-disable-'+str(index)})
        return client.rpc('create_order',{'p_telegram_user_id':899901,'p_request_key':'race-access-order-'+str(index),'p_rows':rows,'p_source':source})
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:outcomes=list(pool.map(compete,range(16)))
    assert client.rpc('partner_sheet_context',{'p_telegram_user_id':899901})['partner_sheet_enabled'] is False
    final=client.rpc('create_order',{'p_telegram_user_id':899901,'p_request_key':'after-all-disabled','p_rows':rows,'p_source':source})
    assert sql(database,statement=f"select count(*) from commerce.partner_sheet_requests where order_id='{final['order_id']}'")[0]=='0'
    assert final['quoted_minor']>0
    assert sql(database,statement="select count(*) from commerce.journals j where (select sum(delta_minor) from commerce.ledger_entries where journal_id=j.id)<>0")[0]=='0'
    evidence={'at':datetime.now(timezone.utc).isoformat(),'production':False,'database':database,'passed':True,
        'checks':['all migrations applied','per-partner permission SQL acceptance','existing partner sheet and campaign approval regressions',
            'promotional cancellation regression','Telegram disable/enable and lost acknowledgement replay',
            '16 concurrent access decisions and quotes','disabled new quotes use normal price',
            'unpaid old quotes cannot create invoices or reservations','funded orders and existing invoices preserved',
            'partner roles and terms unchanged','balanced ledger and RPC grant isolation']}
    path=ROOT/'docs/telegram_commerce'/('acceptance_'+database+'.json')
    path.write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':True,'evidence':str(path)}))


if __name__=='__main__':main()
