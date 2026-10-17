"""Bind personal Binance Pay deposits to private notes issued before payment."""
import hmac
import json
from pathlib import Path
import re
import threading
from uuid import UUID
from .payments import PaymentService
from .payment_providers import TransferEvidence, BinanceTransferAdapter, _proof
from .payment_quotes import utc_time


def enabled(root):
    path=Path(root)/'.commerce/runtime-policy.json'
    try:
        value=json.loads(path.read_text(encoding='utf-8'))
        return isinstance(value,dict) and value.get('binance_note_verified') is True
    except (OSError,ValueError):return False


class BinanceNoteService:
    def __init__(self,client,adapter_factory):
        self.client,self.adapter_factory=client,adapter_factory

    def issue(self,quote_id,user_id,payer_uid):
        request,_=PaymentService(self.client).load(quote_id,user_id)
        if request['provider']!='binance_pay' or not isinstance(payer_uid,str) or not re.fullmatch(r'[1-9][0-9]{0,31}',payer_uid):
            raise ValueError('Owned Binance request and canonical payer UID required')
        return self.client.rpc('issue_binance_note',{'p_quote_id':str(UUID(quote_id)),
            'p_telegram_user_id':user_id,'p_payer_uid':payer_uid})

    def confirm(self,quote_id,user_id,*,transactions=None):
        request,quote=PaymentService(self.client).load(quote_id,user_id)
        challenge=self.client.rpc('binance_note',{'p_quote_id':quote.id,'p_telegram_user_id':user_id})
        if request['provider']!='binance_pay' or challenge.get('quote_id')!=quote.id or not re.fullmatch(r'ZC[a-f0-9]{32}',challenge.get('challenge','')):
            raise ValueError('Stored private payment challenge required')
        adapter=self.adapter_factory(request)
        if not isinstance(adapter,BinanceTransferAdapter):raise ValueError('Authenticated Binance adapter required')
        transactions=adapter.client.get_recent_transactions(limit=100) if transactions is None else transactions
        if not isinstance(transactions,list):raise ValueError('Authenticated payment history unavailable')
        matches=[]
        for tx in transactions:
            if not isinstance(tx,dict) or not isinstance(tx.get('note'),str):continue
            if not hmac.compare_digest(tx['note'].encode('utf-8'),challenge['challenge'].encode('ascii')):continue
            payer=(tx.get('payerInfo') or {}).get('binanceId')
            if isinstance(payer,bool) or str(payer)!=challenge['payer_uid'] or tx.get('orderType')!='C2C':
                raise ValueError('Private payment note has a different sender or transfer type')
            # Reuse exact currency/amount/direction/receiver checks against a
            # snapshot of the authenticated response. Never query a user TXID.
            class Snapshot:
                def get_recent_transactions(self,limit):return transactions
            verifier=BinanceTransferAdapter(Snapshot(),receiver_uid=adapter.uid,receiver_pay_id=adapter.pay_id)
            evidence=verifier.verify(str(tx.get('transactionId','')),asset=quote.asset,
                minimum=quote.asset_amount,not_before=challenge['issued_at'])
            PaymentService(self.client).validate_evidence(request,quote,evidence,{'kind':'private_binance_note'})
            proof=json.loads(evidence.proof_json)
            proof.update(payment_note=tx['note'],payer_uid=str(payer))
            matches.append(TransferEvidence(evidence.provider,evidence.reference,evidence.asset,evidence.amount,
                evidence.received_at,evidence.receiver,_proof(proof)))
        unique={}
        for evidence in matches:
            if evidence.reference in unique and unique[evidence.reference]!=evidence:raise ValueError('Conflicting payment evidence')
            unique[evidence.reference]=evidence
        if len(unique)!=1:raise ValueError('One exact private-note payment required; missing or ambiguous deposits need review')
        evidence=next(iter(unique.values()))
        return self.client.rpc('confirm_binance_note',{'p_quote_id':quote.id,'p_evidence':evidence.as_dict()}),quote


class BinanceNoteMonitor:
    def __init__(self,client,adapter_factory,history_client,*,on_confirmed=None,limit=20):
        if type(limit) is not int or not 1<=limit<=100:raise ValueError('Bounded note monitor required')
        self.client,self.factory,self.history=client,adapter_factory,history_client
        self.on_confirmed,self.limit=on_confirmed,limit;self.after=None;self.lock=threading.Lock()

    def tick(self):
        if not self.lock.acquire(blocking=False):return {'checked':0,'confirmed':0,'busy':True}
        try:
            rows=self.client.rpc('pending_binance_notes',{'p_after':self.after,'p_limit':self.limit})
            if not isinstance(rows,list) or len(rows)>self.limit:raise ValueError('Invalid note scan page')
            if not rows:self.after=None;return {'checked':0,'confirmed':0}
            transactions=self.history.get_recent_transactions(limit=100)
            service=BinanceNoteService(self.client,self.factory);confirmed=0
            for row in rows:
                try:
                    result,quote=service.confirm(row['quote_id'],row['telegram_user_id'],transactions=transactions)
                    confirmed+=1
                    if self.on_confirmed:self.on_confirmed(quote,result)
                except (ValueError,RuntimeError,OSError,KeyError,TypeError):pass
            self.after=str(UUID(rows[-1]['quote_id']))
            return {'checked':len(rows),'confirmed':confirmed}
        finally:self.lock.release()
