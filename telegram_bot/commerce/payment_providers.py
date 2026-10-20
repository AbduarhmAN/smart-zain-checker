"""Provider evidence from authenticated/read-only clients, before customer binding.

A blockchain hash or exchange transaction proves a deposit, not its customer.
Only an issued invoice payload or an explicit authenticated admin assignment
may convert this evidence into a financial receipt.
"""
from __future__ import annotations
from dataclasses import asdict,dataclass
from datetime import datetime,timezone
from decimal import Decimal,localcontext
import hashlib
import json
import re
from .finance import positive_minor
from .payment_quotes import PaymentQuote,decimal_amount,utc_time

TRANSFER_TOPIC='0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'


def _text(value,limit=256):
    if not isinstance(value,str) or not value.strip() or len(value)>limit:
        raise ValueError('Invalid provider reference')
    return value.strip()


def hex_data(value,bytes_count):
    if not isinstance(value,str) or not re.fullmatch(r'0x[0-9a-fA-F]{'+str(bytes_count*2)+'}',value):
        raise ValueError('Invalid provider hex data')
    return value.lower()


def quantity(value):
    if not isinstance(value,str) or not re.fullmatch(r'0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)',value):
        raise ValueError('Invalid provider hex quantity')
    return int(value,16)


def canonical_transaction_hash(value):
    value=_text(value)
    if not value.lower().startswith('0x'):value='0x'+value
    return hex_data(value,32)


@dataclass(frozen=True)
class TransferEvidence:
    provider: str
    reference: str
    asset: str
    amount: str
    received_at: str
    receiver: str
    proof_json: str
    invoice_payload: str | None = None

    def __post_init__(self):
        _text(self.provider,80);_text(self.reference);_text(self.receiver)
        if self.asset not in {'USDT','USDC'}:raise ValueError('Unsupported payment asset')
        decimal_amount(self.amount);utc_time(self.received_at)
        proof=json.loads(self.proof_json)
        if not isinstance(proof,dict):raise ValueError('Provider proof must be an object')
        json.dumps(proof,allow_nan=False)

    @property
    def proof_sha256(self):return hashlib.sha256(self.proof_json.encode('utf-8')).hexdigest()

    def as_dict(self):return asdict(self)


def _proof(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)


class CryptoInvoiceAdapter:
    def __init__(self,client,*,app_id):
        positive_minor(app_id)
        if client.base_url!='https://pay.crypt.bot/api':
            raise ValueError('Testnet/unknown provider cannot fund the production ledger')
        identity=client.get_me()
        if not isinstance(identity,dict) or type(identity.get('app_id')) is not int or identity['app_id']!=app_id:
            raise ValueError('Authenticated invoice application does not match configuration')
        self.client,self.app_id=client,app_id

    def verify(self,quote: PaymentQuote,invoice_id):
        positive_minor(invoice_id)
        invoice=self.client.get_invoice(invoice_id)
        if not isinstance(invoice,dict) or type(invoice.get('invoice_id')) is not int or invoice['invoice_id']!=invoice_id:
            raise ValueError('Invoice is unavailable or does not match')
        if (invoice.get('status')!='paid' or invoice.get('currency_type')!='crypto'
                or invoice.get('asset')!=quote.asset or invoice.get('payload')!=quote.payload
                or decimal_amount(invoice.get('amount'))!=decimal_amount(quote.asset_amount)):
            raise ValueError('Invoice payment does not match the frozen request')
        paid=utc_time(invoice.get('paid_at'))
        created=utc_time(invoice.get('created_at'))
        if not utc_time(quote.observed_at)<=created<=paid<=utc_time(quote.expires_at):
            raise ValueError('Invoice payment is outside its quote window')
        proof={'app_id':self.app_id,'invoice_id':invoice_id,'asset':quote.asset,'amount':quote.asset_amount,
               'payload':quote.payload,'created_at':created.isoformat(),'paid_at':paid.isoformat()}
        return TransferEvidence('cryptopay',f'{self.app_id}:{invoice_id}',quote.asset,quote.asset_amount,
            paid.isoformat(),str(self.app_id),_proof(proof),quote.payload)


class BinanceTransferAdapter:
    def __init__(self,client,*,receiver_uid=None,receiver_pay_id=None):
        if not receiver_uid and not receiver_pay_id:raise ValueError('A configured receiver identity is required')
        for value in (receiver_uid,receiver_pay_id):
            if value is not None and (isinstance(value,bool) or not str(value).isdigit() or int(value)<=0):
                raise ValueError('Invalid receiver identity')
        self.client=client
        self.uid=str(receiver_uid) if receiver_uid else None
        self.pay_id=str(receiver_pay_id) if receiver_pay_id else None

    def verify(self,query_id,*,asset='USDT',minimum='0',not_before=None):
        query=_text(query_id)
        minimum=decimal_amount(minimum,allow_zero=True)
        transactions=self.client.get_recent_transactions(limit=100)
        if not isinstance(transactions,list):raise ValueError('Payment history is unavailable')
        matches=[]
        for tx in transactions:
            if not isinstance(tx,dict):continue
            # A payer UID is not a transfer ID. An alias never becomes the ledger key.
            if query not in {str(tx.get('transactionId','')),str(tx.get('orderId',''))}:continue
            receiver=tx.get('receiverInfo')
            if not isinstance(receiver,dict):raise ValueError('Deposit receiver identity is missing')
            known=[(self.uid,receiver.get('binanceId')),(self.pay_id,receiver.get('accountId'))]
            present=[(expected,str(actual)) for expected,actual in known if expected is not None and actual is not None]
            if not present or any(expected!=actual for expected,actual in present):
                raise ValueError('Deposit receiver does not match the configured account')
            if tx.get('orderType') not in {'PAY','C2C'} or tx.get('currency')!=asset:
                raise ValueError('Transfer type or asset is not eligible')
            amount=decimal_amount(tx.get('amount'))
            if amount<minimum:raise ValueError('Transfer amount is insufficient')
            funds=tx.get('fundsDetail')
            if not isinstance(funds,list) or not funds:
                raise ValueError('Transfer asset breakdown is missing')
            with localcontext() as context:
                context.prec=120
                total=Decimal(0)
                for fund in funds:
                    if not isinstance(fund,dict) or fund.get('currency')!=asset:
                        raise ValueError('Mixed-asset transfer requires reconciliation')
                    total+=decimal_amount(fund.get('amount'))
                if total!=amount:raise ValueError('Transfer amount differs from its asset breakdown')
            timestamp=positive_minor(tx.get('transactionTime'))
            seconds,milliseconds=divmod(timestamp,1000)
            received=datetime.fromtimestamp(seconds,timezone.utc).replace(microsecond=milliseconds*1000)
            if not_before is not None and received<utc_time(not_before):raise ValueError('Transfer predates its payment request')
            canonical=_text(tx.get('transactionId'))
            receiver_key='uid:'+self.uid if self.uid and receiver.get('binanceId') is not None else 'pay:'+self.pay_id
            proof={'transaction_id':canonical,'order_id':tx.get('orderId'),'order_type':tx['orderType'],
                'asset':asset,'amount':format(amount,'f'),'receiver':receiver_key,'received_at':received.isoformat()}
            matches.append(TransferEvidence('binance_pay',canonical,asset,format(amount,'f'),
                received.isoformat(),receiver_key,_proof(proof)))
        unique={}
        for item in matches:
            if item.reference in unique and unique[item.reference]!=item:
                raise ValueError('Conflicting provider records for one transfer')
            unique[item.reference]=item
        if len(unique)!=1:raise ValueError('Transfer was not found uniquely')
        return next(iter(unique.values()))


@dataclass(frozen=True)
class TokenSpec:
    asset: str
    contract: str
    decimals: int

    def __post_init__(self):
        if self.asset not in {'USDT','USDC'}:raise ValueError('Unsupported token')
        hex_data(self.contract,20)
        if type(self.decimals) is not int or not 0<=self.decimals<=36:raise ValueError('Explicit token decimals required')


class BscTransferAdapter:
    def __init__(self,rpc,*,receiver,token:TokenSpec):
        self.rpc,self.receiver,self.token=rpc,hex_data(receiver,20),token

    def verify(self,tx_hash,*,minimum='0',not_before=None):
        tx=canonical_transaction_hash(tx_hash)
        minimum=decimal_amount(minimum,allow_zero=True)
        if quantity(self.rpc('eth_chainId',[]))!=56:raise ValueError('RPC is not BSC mainnet')
        receipt=self.rpc('eth_getTransactionReceipt',[tx])
        if not isinstance(receipt,dict) or receipt.get('status')!='0x1' or hex_data(receipt.get('transactionHash'),32)!=tx:
            raise ValueError('Successful matching transaction receipt is required')
        height=quantity(receipt.get('blockNumber'));block_hash=hex_data(receipt.get('blockHash'),32)
        final=self.rpc('eth_getBlockByNumber',['finalized',False])
        block=self.rpc('eth_getBlockByNumber',[hex(height),False])
        if (not isinstance(final,dict) or quantity(final.get('number'))<height or not isinstance(block,dict)
                or quantity(block.get('number'))!=height or hex_data(block.get('hash'),32)!=block_hash
                or tx not in [hex_data(value,32) for value in block.get('transactions',[])]) :
            raise ValueError('Transaction is not in a canonical finalized block')
        received=datetime.fromtimestamp(quantity(block.get('timestamp')),timezone.utc)
        if not_before is not None and received<utc_time(not_before):raise ValueError('Transfer predates its payment request')
        raw_decimals=self.rpc('eth_call',[{'to':self.token.contract,'data':'0x313ce567'},hex(height)])
        if int(hex_data(raw_decimals,32),16)!=self.token.decimals:raise ValueError('Token decimals differ from configured contract')
        canonical=self.rpc('eth_getBlockByNumber',[hex(height),False])
        if not isinstance(canonical,dict) or hex_data(canonical.get('hash'),32)!=block_hash:
            raise ValueError('Canonical block changed during verification')
        logs=receipt.get('logs')
        if not isinstance(logs,list):raise ValueError('Transfer logs are missing')
        units=0;indices=set();matched=[]
        expected_topic='0x'+'0'*24+self.receiver[2:]
        for log in logs:
            if not isinstance(log,dict) or str(log.get('address','')).lower()!=self.token.contract.lower():continue
            topics=log.get('topics')
            if not isinstance(topics,list) or len(topics)!=3 or str(topics[0]).lower()!=TRANSFER_TOPIC:continue
            if hex_data(topics[2],32)!=expected_topic:continue
            sender=hex_data(topics[1],32)
            if sender[2:26]!='0'*24:raise ValueError('Invalid transfer sender encoding')
            if (log.get('removed') is not False or hex_data(log.get('transactionHash'),32)!=tx
                    or hex_data(log.get('blockHash'),32)!=block_hash or quantity(log.get('blockNumber'))!=height):
                raise ValueError('Transfer log is not canonical')
            index=quantity(log.get('logIndex'))
            if index in indices:raise ValueError('Duplicate transfer log')
            indices.add(index)
            value=int(hex_data(log.get('data'),32),16)
            units+=value;matched.append({'log_index':index,'sender':sender[-40:],'units':str(value)})
        with localcontext() as context:
            context.prec=120
            amount=Decimal(units).scaleb(-self.token.decimals)
        if amount<=0 or amount<minimum:raise ValueError('No sufficient approved-token transfer to receiver')
        proof={'chain_id':56,'tx_hash':tx,'block_hash':block_hash,'block_number':height,
               'contract':self.token.contract.lower(),'decimals':self.token.decimals,'receiver':self.receiver,
               'asset':self.token.asset,'amount':format(amount,'f'),'received_at':received.isoformat(),'logs':matched}
        # One transaction cannot be recycled as several assets/log-index aliases.
        return TransferEvidence('bsc',f'56:{tx}',self.token.asset,format(amount,'f'),received.isoformat(),
            self.receiver,_proof(proof))
