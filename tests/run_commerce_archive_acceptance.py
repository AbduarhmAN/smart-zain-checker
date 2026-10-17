"""Isolated PG archive/index acceptance; optional synthetic live channel probe.

The --live-telegram option sends FOUR clearly labeled synthetic XLSX messages
to the user's verified private archive only. No Zain requests or production
financial records, builds, compilation or deletion are performed.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import secrets
import sys
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import Mock
from zipfile import ZipFile,ZIP_DEFLATED

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from run_commerce_database_acceptance import DATA,sql
from run_commerce_bridge_acceptance import LocalClient
from telegram_bot.commerce.archive import ArchiveDispatcher
from telegram_bot.commerce.archive_transport import ArchiveTransport,document_receipt
from telegram_bot.commerce.archive_config import ArchiveConfig
from telegram_bot.commerce.bridge import CommerceBridge,file_sha256
from telegram_bot.commerce.finance import FinanceService,VerifiedReceipt


def synthetic_xlsx(path,kind):
    """A real minimal workbook containing only synthetic test labels."""
    parts={
        '[Content_Types].xml':'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
        '_rels/.rels':'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        'xl/workbook.xml':'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Synthetic test" sheetId="1" r:id="rId1"/></sheets></workbook>',
        'xl/_rels/workbook.xml.rels':'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        'xl/worksheets/sheet1.xml':f'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>SYNTHETIC ARCHIVE TEST</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>{kind} - no customer data or real payments</t></is></c></row></sheetData></worksheet>'}
    with ZipFile(path,'x',compression=ZIP_DEFLATED) as output:
        for name,data in parts.items():output.writestr(name,data)


def main(live=False):
    if Path(sql('postgres',statement='show data_directory')[0]).resolve()!=DATA.resolve():raise RuntimeError('Refusing another PG instance')
    database='commerce_archive_'+secrets.token_hex(6);sql('postgres',statement=f'create database {database}')
    migrations=[]
    for path in sorted((ROOT/'supabase/migrations').glob('*commerce*.sql')):
        sql(database,file=path);migrations.append({'version':path.stem,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    folder=ROOT/'scratch'/database;folder.mkdir(parents=True)
    original=folder/'synthetic_original.xlsx';result=folder/'synthetic_result.xlsx'
    synthetic_xlsx(original,'original');synthetic_xlsx(result,'result')
    client=LocalClient(database);finance=FinanceService(client)
    finance.confirm_payment(889990,'archive-local-fixture',VerifiedReceipt('fixture','archive-fixture-receipt',100000,'synthetic-only'))
    config=ArchiveConfig(folder);receipts=[];binary_calls=0;reuse_calls=0
    if live:
        from dotenv import load_dotenv
        load_dotenv(ROOT/'.env')
        from telegram_bot.config import settings
        selected=ArchiveConfig(ROOT).load()
        if not selected or selected['chat_id']!=-1004319960104 or selected['bot_id']!=8613566630:
            raise ValueError('Probe requires the user-selected archive and bot')
        transport=ArchiveTransport(settings()['bot_token'])
        data=config.bind(transport.api,selected['chat_id'],source_reference=selected['selection_reference'])
        if data['bot_id']!=selected['bot_id']:raise ValueError('Selected bot identity differs')
        raw_send=transport.send
        def counted_send(conf,**kwargs):
            nonlocal binary_calls,reuse_calls
            if kwargs.get('file_id'):reuse_calls+=1
            else:binary_calls+=1
            # Test marker appears on every channel message, including final archive captions.
            kwargs['caption']='اختبار الأرشيف · بيانات وهمية فقط\n'+kwargs['caption']
            receipt=raw_send(conf,**kwargs);receipts.append(receipt);return receipt
        transport.send=counted_send
        original_receipt=transport.send(data,file_id=None,path=original,sha256=file_sha256(original),
            caption='تجهيز أصل تجريبي لإعادة استخدام معرّف الملف. لا يحتوي بيانات عملاء.',filename=original.name)
        result_receipt=transport.send(data,file_id=None,path=result,sha256=file_sha256(result),
            caption='تجهيز نتيجة تجريبية. لا توجد مدفوعات أو حسابات عملاء حقيقية.',filename=result.name)
    else:
        transport=Mock();me={'id':77,'is_bot':True};chat={'id':-1001234567890,'type':'channel'}
        config.bind(lambda method,data:{'getMe':me,'getChat':chat,'getChatMember':{'status':'administrator','can_post_messages':True}}[method],chat['id'],source_reference='synthetic-only')
        original_receipt={'file_id':'synthetic-original'};result_receipt={'file_id':'synthetic-result'}
        def counted_send(conf,**kwargs):
            nonlocal reuse_calls
            reuse_calls+=1
            receipt={'chat_id':conf['chat_id'],'message_id':reuse_calls,'file_id':kwargs['file_id'],
                'file_unique_id':'synthetic-unique-'+str(reuse_calls),'file_size':Path(kwargs['path']).stat().st_size}
            receipts.append(receipt);return receipt
        transport.send.side_effect=counted_send
    config.configure_policy(original_timing='after_completion',protection='plaintext_sha256',original_days=15,result_days=15,
        reference='synthetic-probe:user-approved-after-completion-sha256-15-days')
    config.enable(verification_reference='isolated-archive-acceptance')
    digest=file_sha256(original);inputs=folder/'.commerce'/'inputs';inputs.mkdir(parents=True)
    (inputs/(digest+'.xlsx')).write_bytes(original.read_bytes())
    order=client.rpc('create_order',{'p_telegram_user_id':889990,'p_request_key':'synthetic-archive-order',
        'p_rows':[{'row_number':2,'check_type':'account'},{'row_number':3,'check_type':'service'}],
        'p_source':{'file_sha256':digest,'original_telegram_file_id':original_receipt['file_id']}})['order_id']
    finance.reserve(order,889990,'synthetic-refund-consent')
    bridge=CommerceBridge(folder,client=client)
    completed=bridge.capture_completion({'commerce_order_id':order,'job_id':'synthetic-archive-job','telegram_chat_id':889990,
        'result_file':str(result),'total':2,'commerce_records':[{'row':2,'status':'not_found'},{'row':3,'status':'error'}]})
    while bridge.worker.run_once():pass
    bridge.capture_delivery(completed,{'chat':{'id':889990},'message_id':700,'document':{'file_id':result_receipt['file_id']}})
    while bridge.worker.run_once():pass
    worker=ArchiveDispatcher(folder,client=client,transport=transport);worker.recover()
    for _ in range(4):
        if not worker.run_once():break
    if worker.store.counts()!={'confirmed':2}:raise AssertionError('Archive worker failed: '+str(worker.status()))
    index=client.rpc('admin_archive_index',{'p_order_id':order})
    owned=client.rpc('customer_archive_index',{'p_order_id':order,'p_telegram_user_id':889990})
    if len(index['files'])!=2 or len(owned['files'])!=1 or 'telegram_chat_id' in owned['files'][0]:raise AssertionError('Archive access mismatch')
    if reuse_calls!=2 or binary_calls!=(2 if live else 0):raise AssertionError('Result/original were uploaded repeatedly')
    downloads=[]
    if live:
        for record in index['files']:downloads.append(transport.verify_download(record['telegram_file_id'],record['sha256']))
    worker.recover();assert not worker.run_once()
    balance=client.rpc('wallet_state',{'p_telegram_user_id':889990})['customer']
    if balance['available_minor']!=97000 or balance['reserved_minor']!=0:raise AssertionError('Archive altered settlement')
    evidence={'at':datetime.now(timezone.utc).isoformat(),'database':database,'production_financial_records':False,
        'live_telegram':live,'telegram_data':'synthetic XLSX only','order_id':order,'binary_uploads':binary_calls,
        'file_id_reuses':reuse_calls,'documents_confirmed':2,'verified_downloads':downloads,'index':index,
        'states':worker.store.counts(),'wallet_unchanged_by_archive':True,'automatic_deletion':False,
        'retained_fixture':str(folder),'migrations':migrations}
    path=ROOT/'docs/telegram_commerce'/('acceptance_'+database+'.json');path.write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':True,'evidence':str(path),'live_telegram':live,'uploads':binary_calls,'reuses':reuse_calls}))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--live-telegram',action='store_true')
    main(parser.parse_args().live_telegram)
