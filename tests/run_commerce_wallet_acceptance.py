"""Local PostgreSQL wallet funding, including committed but lost acknowledgments."""
import json
from pathlib import Path
import secrets
import sys
from datetime import datetime,timezone
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from test_commerce_wallets import WalletTests
from run_commerce_database_acceptance import DATA,sql
from run_commerce_bridge_acceptance import LocalClient
from telegram_bot.commerce.client import CommerceUnavailable
from telegram_bot.commerce.topup_store import TopupStore


class LostFundingAck(LocalClient):
    def __init__(self,database):super().__init__(database);self.lost=set()
    def rpc(self,name,args):
        result=super().rpc(name,args)
        if name in {'confirm_payment_request','admin_cash_topup'} and name not in self.lost:
            self.lost.add(name);raise CommerceUnavailable('Synthetic lost acknowledgment after funding commit')
        return result


def main():
    if Path(sql('postgres',statement='show data_directory')[0]).resolve()!=DATA.resolve():
        raise RuntimeError('Refusing writes to an unknown local PostgreSQL instance')
    database='commerce_wallet_'+secrets.token_hex(6)
    sql('postgres',statement=f'create database {database}')
    for path in sorted((ROOT/'supabase/migrations').glob('*commerce*.sql')):sql(database,file=path)
    fixture=WalletTests();fixture.setUp()
    try:
        client=LostFundingAck(database);bot=fixture.bot;bot._commerce_client=client
        quote=fixture.issue();fixture.callback('invoice',quote)
        fixture.invoices[8].update(status='paid',paid_at=datetime.now(timezone.utc).isoformat())
        fixture.callback('check',quote)
        if client.rpc('wallet_state',{'p_telegram_user_id':31})['customer']['available_minor']!=6000:
            raise AssertionError('Lost invoice acknowledgment did not preserve its committed wallet credit')
        bot._commerce_topup_store=TopupStore(fixture.root)
        fixture.callback('check',quote)
        with patch('telegram_bot.payment_manager.PAYMENT_MGR.find_user_by_account_id',return_value=None):
            bot._commerce_command('/credit_user 31 30 CASH-001',9,True,message_id=77)
        token=next(iter(bot._cash_topup_reviews))
        bot._cash_topup_callback('ca:approve:'+token,9,4,{'id':'cash-approval:1'})
        bot._cash_topup_callback('ca:approve:'+token,9,4,{'id':'cash-approval:2'})
        manual=fixture.issue('binance',message_id=124)
        fixture.callback('reference',manual)
        bot._commerce_text_input(31,'ORDER1',bot._waiting_user_inputs.pop(31))
        claim=client.rpc('list_payment_claims',{'p_limit':10,'p_offset':0})[0]['claim_id']
        record={'transactionId':'wallet-fixture-tx','orderId':'ORDER1','orderType':'C2C','currency':'USDT',
            'amount':'1.2','transactionTime':int(datetime.now(timezone.utc).timestamp()*1000),
            'receiverInfo':{'binanceId':'41','accountId':'42'},'fundsDetail':[{'currency':'USDT','amount':'1.2'}]}
        with patch('telegram_bot.crypto_pay.BINANCE_PAY.get_recent_transactions',return_value=[record]):
            bot._payment_review_callback('cm:review:'+claim,9,4,{'id':'review:1'})
            bot._payment_review_callback('cm:approve:'+claim,9,4,{'id':'approve:1'})
            bot._payment_review_callback('cm:approve:'+claim,9,4,{'id':'approve:2'})
        wallet=client.rpc('wallet_state',{'p_telegram_user_id':31})
        if wallet['customer']['available_minor']!=15000 or wallet['partner']['available_minor']!=0:
            raise AssertionError('Wallet funding or partner separation was incorrect')
        if bot.queue_service.get_jobs():raise AssertionError('Wallet topups created a checking job')
        if sql(database,statement='select count(*) from commerce.payments')[0]!='3':
            raise AssertionError('Funding replay duplicated a receipt')
        if sql(database,statement='select count(*) from commerce.users where first_paid_service_at is not null')[0]!='0':
            raise AssertionError('Wallet funding incorrectly became a first paid service')
        evidence={'passed':True,'at':datetime.now(timezone.utc).isoformat(),'database':database,
            'production':False,'external_provider_and_messages':'synthetic, no live invoices or messages',
            'wallet_available_minor':15000,'partner_available_minor':0,'receipts':3,'checking_jobs':0,
            'invoice_lost_ack_recovered':True,'cash_lost_ack_recovered':True,'manual_assignment_replayed':True,
            'funding_is_not_paid_service':True}
        path=ROOT/'docs/telegram_commerce'/('acceptance_'+database+'.json')
        path.write_text(json.dumps(evidence,indent=2),encoding='utf-8')
        print(json.dumps({'passed':True,'evidence':str(path)}))
    finally:fixture.doCleanups()


if __name__=='__main__':main()
