"""Real isolated PostgreSQL + durable bridge, with synthetic Telegram receipts.

No production payments, outbound Telegram messages, compilation or deletion.
"""
from __future__ import annotations
import hashlib
import json
import re
import secrets
import sqlite3
import sys
import time
from datetime import datetime,timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from domain import decimal_json as json
from run_commerce_database_acceptance import DATA,sql
from telegram_bot.commerce.bridge import CommerceBridge
from telegram_bot.commerce.finance import FinanceService,VerifiedReceipt


class LocalClient:
    def __init__(self,database):self.database=database;self.calls=[]

    def rpc(self,name,arguments):
        allowed = {'confirm_receipt','create_order','reserve_order','wallet_state',
                   'bind_checker_job','record_terminal_results','confirm_result_delivery','settle_order',
                   'checkout_state','checkout_consent'}
        allowed|={'create_payment_request','payment_request','submit_payment_claim','payment_claim',
                  'list_payment_claims','approve_payment_claim','reject_payment_claim',
                  'admin_cash_topup','register_telegram_user','begin_payment_invoice',
                  'attach_payment_invoice','confirm_payment_request'}
        allowed|={'partner_finance','request_withdrawal','withdrawal_record','list_withdrawals',
                  'approve_withdrawal','complete_withdrawal','reject_withdrawal','attribute_referral','configure_partner','partner_withdrawals'}
        allowed|={'record_partner_payout','configure_payout_notice_channels'}
        allowed|={'configure_campaign_policy','campaign_policy','create_platform_campaign','activate_campaign',
                  'customer_campaigns','list_campaigns','pause_campaign','cancel_campaign_checkout'}
        allowed|={'issue_cash_credit_code','redeem_cash_credit_code','apply_customer_code','list_cash_credit_codes'}
        allowed|={'request_customer_stop','expire_promotional_credit'}
        allowed|={'partner_campaign_context','create_partner_campaign','list_partner_campaigns','pause_partner_campaign'}
        allowed|={'archive_plan','confirm_archive_file','admin_archive_index','customer_archive_index'}
        allowed|={'configure_support_actors','support_order','record_support_message','confirm_support_delivery',
                  'support_history','support_inbox','set_support_state'}
        if name not in allowed:raise ValueError('Local acceptance RPC not allowed')
        parameters=[]
        for key,value in arguments.items():
            if not re.fullmatch(r'p_[a-z][a-z0-9_]*',key):
                raise ValueError('Invalid test RPC parameter')
            type_name = ('text' if name=='attribute_referral' and key=='p_source' else
                         'jsonb' if key in {'p_rows','p_source','p_fx_snapshot','p_quote','p_route_snapshot','p_evidence','p_binding','p_rules','p_policy','p_record','p_receipt','p_actor_ids','p_channels'} else
                         'uuid' if key in {'p_order_id','p_payment_id','p_quote_id','p_claim_id','p_withdrawal_id','p_campaign_id'} else
                         'integer' if key in {'p_limit','p_offset'} else
                         'timestamptz' if key=='p_expires_at' else
                         'numeric' if key in {'p_amount_minor','p_credit_minor','p_funded_minor'} else
                         'bigint' if key in {'p_telegram_user_id','p_partner_telegram_id','p_message_id','p_policy_version','p_actor_telegram_id','p_invoice_id',
                            'p_credit_minor','p_max_redemptions','p_funded_minor','p_bound_telegram_id','p_chat_id','p_file_size'} else 'text')
            if value is None:literal='null'
            else:
                encoded=json.dumps(value,allow_nan=False) if type_name=='jsonb' else str(value)
                literal="'"+encoded.replace("'","''")+"'"
            parameters.append(f'{key} => {literal}::{type_name}')
        statement=f"set role service_role; select commerce_rpc.{name}({','.join(parameters)})"
        from decimal import Decimal
        result=json.loads(sql(self.database,statement=statement)[0].splitlines()[-1],parse_float=Decimal)
        self.calls.append(name)
        return result


def drain(bridge):
    for _ in range(10):
        if not bridge.worker.run_once():return
    raise AssertionError('Unexpected bridge command loop')


def main():
    if Path(sql('postgres',statement='show data_directory')[0]).resolve()!=DATA.resolve():
        raise RuntimeError('Refusing writes to a different PostgreSQL instance')
    database='commerce_bridge_'+secrets.token_hex(6)
    sql('postgres',statement=f'create database {database}')
    files=[]
    for path in sorted((ROOT/'supabase/migrations').glob('*commerce*.sql')):
        sql(database,file=path)
        files.append({'path':str(path.relative_to(ROOT)),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    client=LocalClient(database)
    finance=FinanceService(client)
    finance.confirm_payment(887001,'bridge-topup',VerifiedReceipt('fixture','bridge-tx',100000,'synthetic-proof'))
    order=client.rpc('create_order',{'p_telegram_user_id':887001,'p_request_key':'bridge-order',
        'p_rows':[{'row_number':2,'check_type':'account'},{'row_number':3,'check_type':'service'},
                  {'row_number':4,'check_type':'account'}],
        'p_source':{'file_sha256':'a'*64}})['order_id']
    finance.reserve(order,887001,'synthetic-explicit-refund-consent')
    folder=ROOT/'scratch'/database
    folder.mkdir(parents=True,exist_ok=True)
    result=folder/'synthetic.xlsx';result.write_bytes(b'PK\x03\x04synthetic-acceptance')
    bridge=CommerceBridge(folder,client=client)
    completion=bridge.capture_completion({'commerce_order_id':order,'job_id':'bridge-job',
        'telegram_chat_id':887001,'result_file':str(result),'total':3,
        'commerce_records':[{'row':2,'status':'match','live_sar':0},
                            {'row':3,'status':'not_found'},{'row':4,'status':'error'}]})
    # Simulate lost remote RPC acknowledgement: the remote commit happened,
    # but the local command lease was never marked complete.
    abandoned=bridge.outbox.lease(now=time.time()-120,lease_seconds=1)
    client.rpc(abandoned['method'],abandoned['arguments'])
    restarted=CommerceBridge(folder,client=client);restarted.recover();drain(restarted)
    wallet=client.rpc('wallet_state',{'p_telegram_user_id':887001})['customer']
    if wallet['available_minor']!=91000 or wallet['reserved_minor']!=9000:
        raise AssertionError('Result capture changed the reservation before delivery')
    # A acknowledged receipt is kept even if SQLite enqueue is temporarily down.
    from unittest.mock import patch
    message={'chat':{'id':887001},'message_id':91,'document':{'file_id':'synthetic-file'}}
    with patch.object(restarted,'_enqueue_receipt',side_effect=sqlite3.OperationalError('fixture storage interruption')):
        try:restarted.capture_delivery(completion,message)
        except sqlite3.Error:pass
        else:raise AssertionError('Storage interruption was not injected')
    resumed=CommerceBridge(folder,client=client);resumed.recover();drain(resumed)
    settled=client.rpc('settle_order',{'p_order_id':order})
    if settled['charged_minor']!=6000 or settled['refunded_minor']!=3000:
        raise AssertionError('Valid zero/not_found or technical refund amount is wrong')
    wallet=client.rpc('wallet_state',{'p_telegram_user_id':887001})['customer']
    if wallet['available_minor']!=94000 or wallet['reserved_minor']!=0:
        raise AssertionError('Financial settlement changed money more than once')
    if sql(database,statement=f"select count(*) from commerce.refunds where order_id='{order}'")[0]!='1':
        raise AssertionError('Refund duplicated')
    if sql(database,statement="select count(*) from commerce.journals j where (select sum(delta_minor::numeric) from commerce.ledger_entries e where e.journal_id=j.id)<>0")[0]!='0':
        raise AssertionError('Unbalanced ledger')
    evidence={'run_at':datetime.now(timezone.utc).isoformat(),'database':database,
        'production':False,'telegram_receipts':'synthetic; no external send',
        'checks':['remote acknowledgement lost and replay recovered','capture does not settle before delivery',
            'durable receipt reconstructs missing commands','valid zero and not_found charged',
            'technical failure refunded once','repeated settlement preserves balances','ledger balanced'],
        'charged_minor':6000,'refunded_minor':3000,'available_minor':94000,
        'outbox':resumed.outbox.status(),'deliveries':resumed.outbox.delivery_counts(),'migrations':files}
    path=ROOT/'docs/telegram_commerce'/f'acceptance_{database}.json'
    path.write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':True,'evidence':str(path)}))


if __name__=='__main__':main()
