"""Owner UI plus real isolated staff authority; no production role writes."""
from pathlib import Path
import sys
import secrets
import json
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from run_commerce_database_acceptance import DATA,sql
from run_commerce_bridge_acceptance import LocalClient
from test_admin_roles import AdminRoleTests
from telegram_bot.config import settings
from telegram_bot.commerce.client import CommerceUnavailable

class LostAckClient(LocalClient):
    lost=False
    def rpc(self,name,args):
        result=super().rpc(name,args)
        if name=='configure_support_actors' and not self.lost:
            self.lost=True;raise CommerceUnavailable('Synthetic lost role acknowledgment')
        return result

def main():
    if Path(sql('postgres',statement='show data_directory')[0]).resolve()!=DATA.resolve():raise RuntimeError('Refusing unknown database instance')
    database='commerce_admin_roles_'+secrets.token_hex(6)
    sql('postgres',statement='create database '+database)
    for path in sorted((ROOT/'supabase/migrations').glob('*commerce*.sql')):sql(database,file=path)
    sql(database,statement="select commerce_rpc.configure_support_actors('[9,21]','isolated owner settings')")
    fixture=AdminRoleTests();fixture.setUp()
    try:
        client=LostAckClient(database);fixture.bot._commerce.return_value=client
        token=fixture.grant_review()
        assert sql(database,statement='select count(*) from commerce.support_actors where active')[0]=='2'
        fixture.click('am:save:'+token)
        assert 31 not in settings()['authorized_ids']
        assert sql(database,statement='select count(*) from commerce.support_actors where active')[0]=='3'
        fixture.click('am:retry:'+token)
        assert 31 in settings()['authorized_ids']
        fixture.click('am:remove:31');token=next(iter(fixture.bot._admin_role_reviews))
        client.lost=False;fixture.click('am:save:'+token)
        assert 31 not in settings()['authorized_ids']
        fixture.click('am:retry:'+token)
        assert sql(database,statement="select count(*) from commerce.support_actors where telegram_user_id=31 and active")[0]=='0'
        assert sql(database,statement='select count(*) from commerce.support_actors where active')[0]=='2'
        assert sql(database,statement='select count(*) from commerce.ledger_entries')[0]=='0'
        assert fixture.bot._read_role_action()['state']=='done'
        proof={'at':datetime.now(timezone.utc).isoformat(),'database':database,'production':False,
            'native_review_before_grant':True,'grant_waits_for_backend_ack':True,'revoke_denies_immediately':True,
            'lost_grant_and_revoke_acknowledgments_recovered':True,'other_admins_preserved':True,
            'no_financial_records_written':True,'real_telegram_messages_sent':False}
        output=ROOT/'docs/telegram_commerce'/('admin_roles_acceptance_'+database.rsplit('_',1)[-1]+'.json')
        output.write_text(json.dumps(proof,indent=2),encoding='utf-8');print(json.dumps(proof))
    finally:fixture.doCleanups()

if __name__=='__main__':main()
