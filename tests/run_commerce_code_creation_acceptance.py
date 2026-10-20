"""Real local PostgreSQL code creation; no production credits or Telegram I/O.

All fixture files and databases are retained. No compilation or deletion.
"""
import atexit
import json
from pathlib import Path
import secrets
import sys
import tempfile
from datetime import datetime,timezone
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'tests'))
fixture_root=ROOT/'outputs'/'commerce_code_creation_fixtures'/secrets.token_hex(6)
fixture_root.mkdir(parents=True,exist_ok=False)
tempfile.tempdir=str(fixture_root)
import openpyxl.worksheet._writer as worksheet_writer
def retain_xml(writer):
    if writer.out in worksheet_writer.ALL_TEMP_FILES:worksheet_writer.ALL_TEMP_FILES.remove(writer.out)
worksheet_writer.WorksheetWriter.cleanup=retain_xml
atexit.unregister(worksheet_writer._openpyxl_shutdown)

from run_commerce_database_acceptance import DATA,sql
from run_commerce_bridge_acceptance import LocalClient
from test_commerce_cash_codes import CashCodeTests
from test_commerce_campaigns import CampaignTests

CURRENT_LIMITS={'max_redemptions':30,'max_checks_per_user':1,'max_duration_seconds':259200,
    'max_wallet_credit_minor':3000,'max_percent_basis_points':3000,'max_total_discount_minor':3000,
    'max_discount_minor_per_user':3000}


def main():
    if Path(sql('postgres',statement='show data_directory')[0]).resolve()!=DATA.resolve():
        raise RuntimeError('Refusing writes to an unknown PostgreSQL instance')
    database='commerce_code_creation_'+secrets.token_hex(6)
    sql('postgres',statement=f'create database {database}')
    for path in sorted((ROOT/'supabase/migrations').glob('*commerce*.sql')):sql(database,file=path)
    client=LocalClient(database)
    cash=CashCodeTests();cash.setUp()
    campaign=CampaignTests();campaign.setUp()
    try:
        cash.bot._commerce_client=client
        cash.callback('cg:new:0');cash_token=next(iter(cash.bot._cash_code_reviews))
        cash.callback('cg:generate:'+cash_token)
        for value in ['30.01','2']:cash.text(value)
        for value in ['LOCAL-RECEIPT-001','0','15']:cash.text(value)
        if sql(database,statement="select count(*) from commerce.codes where kind='cash_credit'")[0]!='0':
            raise AssertionError('A code was funded before authenticated confirmation')
        cash.callback('cg:save:'+cash_token);cash.callback('cg:save:'+cash_token)
        records=client.rpc('list_cash_credit_codes',{'p_offset':0})
        if len(records)!=1 or records[0]['cash_funded_minor']!=6002:
            raise AssertionError('Actual cash confirmation failed or duplicated funding')
        campaign.bot._commerce_client=client
        client.rpc('configure_campaign_policy',{'p_actor_telegram_id':9,'p_rules':CURRENT_LIMITS,'p_reference':'local-current-limits'})
        token=campaign.offer()
        values=dict(campaign.bot._campaign_reviews[token]['values'])
        campaign.callback('oa:save:'+token)
        panel=campaign.bot._show_panel.call_args.args[1]
        if 'الحد الحالي 1' not in panel.text or sql(database,statement='select count(*) from commerce.campaigns')[0]!='0':
            raise AssertionError('Current small limits were not explained before writing')
        campaign.callback('oa:limits:'+token)
        limits_token=next(reversed(campaign.bot._campaign_reviews))
        for value in ['50','10','100','1000','3','30','10']:campaign.text(value)
        campaign.callback('oa:save:'+limits_token)
        if campaign.bot._campaign_reviews[token]['values']!=values:
            raise AssertionError('Updating limits lost the campaign draft')
        campaign.callback('oa:save:'+token);campaign.callback('oa:save:'+token)
        if sql(database,statement='select count(*) from commerce.campaigns')[0]!='1':
            raise AssertionError('Actual campaign save failed or replay created another campaign')
        if sql(database,statement='select count(*) from commerce.payments')[0]!='0':
            raise AssertionError('Code creation credited a customer without redemption')
        if sql(database,statement='select count(*) from commerce.commissions')[0]!='0':
            raise AssertionError('Code creation generated a premature partner commission')
        proof={'passed':True,'at':datetime.now(timezone.utc).isoformat(),'production':False,'database':database,
            'checks':['Generated cash code retained until authenticated approval','Funding calculated at 6002 piastres',
                'Cash issuance replay funded one code once','Current small campaign limits reject before creating a campaign',
                'Explicit limit update returns to retained draft','Campaign save and replay create one campaign',
                'No customer payment or partner commission created'],'telegram_messages':'mocked','deletion':'blocked; fixtures retained'}
        path=ROOT/'docs/telegram_commerce'/('acceptance_'+database+'.json')
        path.write_text(json.dumps(proof,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'passed':True,'proof':str(path)}))
    finally:
        campaign.doCleanups();cash.doCleanups()


if __name__=='__main__':
    with (patch('urllib.request.urlopen',side_effect=AssertionError('External network forbidden')),
          patch('requests.Session.post',side_effect=AssertionError('External network forbidden')),
          patch('requests.Session.get',side_effect=AssertionError('External network forbidden')),
          patch('pathlib.Path.unlink',side_effect=AssertionError('File deletion forbidden')),
          patch('shutil.rmtree',side_effect=AssertionError('Directory deletion forbidden')),
          patch('os.remove',side_effect=AssertionError('File deletion forbidden')),
          patch('os.unlink',side_effect=AssertionError('File deletion forbidden'))):
        main()
