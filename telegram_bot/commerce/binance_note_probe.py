"""Read-only live note echo probe. It never creates a payment or credits money."""
from datetime import datetime,timezone,timedelta
import hashlib
import hmac
import json
from pathlib import Path
import secrets
from .payment_providers import BinanceTransferAdapter
from .payment_quotes import utc_time


def prepare(root):
    root=Path(root).resolve();folder=root/'.commerce/binance_note_probes';folder.mkdir(parents=True,exist_ok=True)
    code='ZTEST'+secrets.token_hex(12);now=datetime.now(timezone.utc)
    record={'version':1,'note':code,'issued_at':now.isoformat(),'expires_at':(now+timedelta(hours=24)).isoformat(),
            'financial_writes':False,'activation':False}
    path=folder/(secrets.token_hex(12)+'.json')
    with path.open('x',encoding='utf-8') as stream:json.dump(record,stream,indent=2)
    return {'probe':str(path),'note':code,'expires_at':record['expires_at'],'financial_writes':False}


def check(root,path,client):
    root=Path(root).resolve();path=Path(path).resolve()
    if path.parent!=root/'.commerce/binance_note_probes':raise ValueError('Owned retained echo probe required')
    record=json.loads(path.read_text(encoding='utf-8'))
    if record.get('version')!=1 or datetime.now(timezone.utc)>utc_time(record['expires_at']):raise ValueError('Current echo probe required')
    transactions=client.get_recent_transactions(limit=100)
    if not isinstance(transactions,list) or not transactions:return {'verified':False,'reason':'provider_history_unavailable','financial_writes':False}
    account_ids={str(tx.get('uid','')) for tx in transactions if isinstance(tx,dict)}
    if len(account_ids)!=1 or '' in account_ids:return {'verified':False,'reason':'authenticated_account_context_requires_review','financial_writes':False}
    own_uid=next(iter(account_ids));matches=[]
    for tx in transactions:
        if not isinstance(tx,dict) or not isinstance(tx.get('note'),str):continue
        if hmac.compare_digest(tx['note'].encode('utf-8'),record['note'].encode('ascii')):matches.append(tx)
    if len(matches)!=1:return {'verified':False,'reason':'one_exact_echo_not_found','financial_writes':False}
    tx=matches[0]
    if tx.get('orderType')!='C2C' or str((tx.get('receiverInfo') or {}).get('binanceId',''))!=own_uid:
        return {'verified':False,'reason':'test_is_not_incoming_to_the_authenticated_account','financial_writes':False}
    class Snapshot:
        def get_recent_transactions(self,limit):return transactions
    evidence=BinanceTransferAdapter(Snapshot(),receiver_uid=own_uid).verify(str(tx.get('transactionId','')),
        asset=tx.get('currency'),not_before=record['issued_at'])
    if utc_time(evidence.received_at)>utc_time(record['expires_at']):raise ValueError('Echo test transfer is outside the window')
    proof={'note_echo_verified':True,'probe_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
           'verified_at':datetime.now(timezone.utc).isoformat(),'receiver_uid':own_uid,
           'payer_uid':str((tx.get('payerInfo') or {}).get('binanceId','')),'proof_json':evidence.proof_json,
           'note_sha256':hashlib.sha256(record['note'].encode()).hexdigest(),'financial_writes':False,'activation':False}
    receipt=path.with_name(path.stem+'.verified.'+secrets.token_hex(6)+'.json')
    with receipt.open('x',encoding='utf-8') as stream:json.dump(proof,stream,indent=2)
    return {'verified':True,'private_evidence':str(receipt),'financial_writes':False,'automatic_credit_enabled':False}


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['prepare','check']);parser.add_argument('--probe')
    args=parser.parse_args();root=Path(__file__).resolve().parents[2]
    if args.action=='prepare':print(json.dumps(prepare(root)))
    else:
        from telegram_bot.crypto_pay import BINANCE_PAY
        print(json.dumps(check(root,args.probe,BINANCE_PAY)))
