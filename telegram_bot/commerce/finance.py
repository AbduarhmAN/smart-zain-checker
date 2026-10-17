"""Typed financial commands for verified provider adapters and the checker.

This module never verifies a user-supplied transfer ID on its own. Receipt
creation belongs to a provider adapter or an authenticated cash approval.
"""
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal,InvalidOperation
from uuid import UUID
from .client import CommerceClient


def positive_minor(value):
    if type(value) is not int or not 0 < value <= 9223372036854775807:
        raise ValueError('Positive integer piastres required')
    return value

def positive_money(value):
    if isinstance(value,bool) or not isinstance(value,(int,Decimal)):
        raise ValueError('Exact positive money required')
    result=Decimal(value)
    if not result.is_finite() or not 0<result<=Decimal('9223372036854775807') or len(result.as_tuple().digits)>80 or abs(result.adjusted())>80:
        raise ValueError('Exact positive money outside bounds')
    return int(result) if result==result.to_integral_value() else result

def money_minor_input(text):
    with __import__('decimal').localcontext() as context:
        context.prec=100
        return positive_money(Decimal(text.strip())*100)


def result_rows(records):
    """Translate committed checker records; a row label is not payment proof.

    The engine defines not_found as a valid negative query. Match/mismatch
    require a valid amount (including zero). Technical error/review without
    a verified result is refundable. Pending/deferred rows prevent settlement.
    """
    rows,seen = [],set()
    for record in records:
        row = record.get('row')
        positive_minor(row)
        if row in seen:
            raise ValueError('Duplicate completed row identity')
        seen.add(row)
        status = record.get('status')
        if status in {'match','mismatch'}:
            raw = record.get('live_sar',record.get('live_amount'))
            try:
                amount = Decimal(str(raw))
            except (InvalidOperation,ValueError):
                raise ValueError('Verified amount is missing') from None
            if isinstance(raw,bool) or not amount.is_finite() or amount < 0:
                raise ValueError('Verified amount is invalid')
            state,verified = 'succeeded',True
        elif status == 'not_found':
            state,verified = 'not_found',True
        elif status == 'cancelled':
            state,verified = 'cancelled',False
        elif status == 'error':
            state,verified = 'failed',False
        elif status == 'needs_review':
            # Current engine writes live_sar=None for exhausted technical review.
            state,verified = 'review',False
        else:
            raise ValueError('Only committed terminal checker outcomes are accepted')
        rows.append({'row_number':row,'state':state,'verified_result':verified})
    if not rows:
        raise ValueError('No completed rows')
    return sorted(rows,key=lambda item:item['row_number'])


@dataclass(frozen=True)
class VerifiedReceipt:
    provider: str
    reference: str
    amount_minor: int
    proof_reference: str
    currency: str = 'EGP'

    def __post_init__(self):
        positive_money(self.amount_minor)
        if self.currency != 'EGP':
            raise ValueError('Apply the frozen FX quote before creating an EGP receipt')
        for field,limit in [('provider',80),('reference',256),('proof_reference',1000)]:
            value = getattr(self,field)
            if not isinstance(value,str) or not value.strip() or len(value) > limit:
                raise ValueError(f'Invalid {field}')


class FinanceService:
    def __init__(self,client=None):
        self.client = CommerceClient() if client is None else client

    def confirm_payment(self,user_id,key,receipt: VerifiedReceipt,*,order_id=None,fx_snapshot=None):
        if not isinstance(receipt,VerifiedReceipt):
            raise ValueError('A verified provider receipt is required')
        positive_minor(user_id)
        if not isinstance(key,str) or not key.strip() or len(key)>160:
            raise ValueError('Persistent payment key required')
        return self.client.rpc('confirm_receipt',{
            'p_telegram_user_id':user_id,'p_key':key,'p_provider':receipt.provider,
            'p_reference':receipt.reference,'p_amount_minor':receipt.amount_minor,'p_proof':receipt.proof_reference,
            'p_purpose':'direct_order' if order_id else 'topup',
            'p_order_id':str(UUID(order_id)) if order_id else None,'p_fx_snapshot':fx_snapshot or {}})

    def reserve(self,order_id,user_id,consent_reference,*,payment_id=None):
        positive_minor(user_id)
        if not isinstance(consent_reference,str) or not consent_reference.strip() or len(consent_reference)>1000:
            raise ValueError('Explicit refund-policy consent receipt required')
        return self.client.rpc('reserve_order',{'p_order_id':str(UUID(order_id)),
            'p_telegram_user_id':user_id,'p_consent_reference':consent_reference,
            'p_payment_id':str(UUID(payment_id)) if payment_id else None})

    def terminal(self,order_id,job_id,records,result_sha256):
        if not isinstance(result_sha256,str) or len(result_sha256)!=64 or any(c not in '0123456789abcdef' for c in result_sha256):
            raise ValueError('Valid result-file SHA256 required')
        if not isinstance(job_id,str) or not job_id.strip() or len(job_id)>120:
            raise ValueError('Bound checker job required')
        return self.client.rpc('record_terminal_results',{'p_order_id':str(UUID(order_id)),
            'p_job_id':job_id,'p_rows':result_rows(records),'p_result_sha256':result_sha256})

    def delivery(self,order_id,user_id,telegram_message,result_sha256):
        return self.client.rpc('confirm_result_delivery',delivery_arguments(order_id,user_id,telegram_message,result_sha256))

    def settle(self,order_id):
        return self.client.rpc('settle_order',{'p_order_id':str(UUID(order_id))})


def delivery_arguments(order_id,user_id,telegram_message,result_sha256):
        positive_minor(user_id)
        if not isinstance(telegram_message,dict) or not isinstance(telegram_message.get('chat'),dict):
            raise ValueError('Delivery is not to the paying user')
        positive_minor(telegram_message['chat'].get('id'))
        if telegram_message['chat']['id'] != user_id:
            raise ValueError('Delivery is not to the paying user')
        positive_minor(telegram_message.get('message_id'))
        document = telegram_message.get('document')
        if not isinstance(document,dict):
            raise ValueError('Confirmed Telegram document receipt required')
        file_id = document.get('file_id')
        if not isinstance(file_id,str) or not file_id or len(file_id)>512:
            raise ValueError('Confirmed Telegram document receipt required')
        if not isinstance(result_sha256,str) or len(result_sha256)!=64 or any(c not in '0123456789abcdef' for c in result_sha256):
            raise ValueError('Valid result-file SHA256 required')
        return {'p_order_id':str(UUID(order_id)),
            'p_telegram_user_id':user_id,'p_message_id':telegram_message['message_id'],
            'p_file_id':file_id,'p_result_sha256':result_sha256}
