"""Read-only provider evidence for legacy reconciliation; no automatic decisions."""
from decimal import Decimal,InvalidOperation
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
from .legacy_wallets import reconcile
from .payment_providers import canonical_transaction_hash


def probe(root,client,*,binance_adapter=None):
    root=Path(root).resolve();plan=reconcile(root)
    if getattr(client,'base_url',None)!='https://pay.crypt.bot/api':raise ValueError('Production invoice provider required')
    identity=client.get_me()
    if not isinstance(identity,dict) or type(identity.get('app_id')) is not int or identity['app_id']<=0:
        raise ValueError('Authenticated invoice application unavailable')
    raw=(root/'pending_payments.json').read_bytes()
    if hashlib.sha256(raw).hexdigest()!=plan['payments_sha256']:raise ValueError('Legacy payment source changed')
    payments=json.loads(raw.decode('utf-8'),parse_float=Decimal);observations=[]
    for item in plan['unresolved_payments']+plan['paid_service_candidates']:
        token=item['token'];record=payments[token];draft=record.get('draft_info',{})
        result={'token_sha256':hashlib.sha256(token.encode()).hexdigest(),'legacy_state':record.get('status'),
            'legacy_declared_method':record.get('method',record.get('kind')),
            'financial_decision':'review_required','balances_changed':False}
        invoice_id=draft.get('invoice_id')
        if type(invoice_id) is int and invoice_id>0:
            invoice=client.get_invoice(invoice_id)
            result['provider']='cryptopay';result['invoice_id']=invoice_id;result['app_id']=identity['app_id']
            if not isinstance(invoice,dict):result['observation']='invoice_unavailable_in_authenticated_application'
            else:
                expected=draft.get('usdt_amount',record.get('amount_usdt'))
                try:
                    amount=Decimal(str(expected));actual=Decimal(str(invoice.get('amount')))
                    matched=(amount.is_finite() and actual.is_finite() and amount>0 and amount==actual
                        and invoice.get('invoice_id')==invoice_id and type(invoice.get('invoice_id')) is int
                        and invoice.get('payload')==token and invoice.get('asset')=='USDT'
                        and invoice.get('currency_type')=='crypto')
                except (InvalidOperation,ValueError,TypeError):matched=False
                result['binding_matches']=matched
                result['provider_status']=invoice.get('status')
                result['observation']='matching_provider_record' if matched else 'provider_record_differs_from_legacy_request'
                result['provider_fingerprint']=hashlib.sha256(json.dumps({key:invoice.get(key)
                    for key in ['invoice_id','asset','amount','currency_type','status','created_at','paid_at']},sort_keys=True).encode()).hexdigest()
                if matched and invoice.get('status')=='paid':result['financial_decision']='stored_invoice_paid_legacy_funding_and_delivery_review_required'
                elif matched and invoice.get('status')=='expired':result['financial_decision']='stored_invoice_expired_other_legacy_funding_review_required'
        elif 'bep20' in str(record.get('method','')).lower() or 'bsc' in str(record.get('method','')).lower():
            result['provider']='bsc'
            try:canonical_transaction_hash(record.get('tx_id'));result['observation']='hash_syntax_valid_contract_and_owner_review_required'
            except ValueError:result['observation']='reference_is_not_a_bsc_transaction_hash'
        else:result['observation']='legacy_payment_needs_provider_specific_review'
        if 'binance' in str(record.get('method','')).lower():
            result['declared_provider']='binance_pay'
            result['declared_transfer_verified']=False
            if binance_adapter is not None:
                try:
                    evidence=binance_adapter.verify(record.get('tx_id'),asset='USDT',
                        minimum=str(record.get('amount_usdt',draft.get('usdt_amount','0'))),not_before=record.get('created_at'))
                    result['declared_transfer_verified']=True
                    result['declared_transfer_proof_sha256']=evidence.proof_sha256
                    result['declared_transfer_reference_sha256']=hashlib.sha256(evidence.reference.encode()).hexdigest()
                except (ValueError,OverflowError,OSError):
                    result['declared_transfer_observation']='no_matching_eligible_deposit_confirmed'
            result['financial_decision']='declared_binance_funding_owner_and_delivery_review_required'
        observations.append(result)
    if hashlib.sha256((root/'pending_payments.json').read_bytes()).hexdigest()!=plan['payments_sha256']:
        raise ValueError('Legacy payment source changed during provider verification')
    return {'at':datetime.now(timezone.utc).isoformat(),'production_reads':True,'writes':False,
        'wallet_sha256':plan['wallet_sha256'],'payments_sha256':plan['payments_sha256'],'observations':observations}


def save_probe(root,client,*,binance_adapter=None):
    report=probe(root,client,binance_adapter=binance_adapter);root=Path(root).resolve()
    folder=root/'.commerce/legacy_migration';folder.mkdir(parents=True,exist_ok=True)
    path=folder/('provider_probe_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.json')
    with path.open('x',encoding='utf-8') as stream:json.dump(report,stream,indent=2)
    counts={}
    for item in report['observations']:counts[item['observation']]=counts.get(item['observation'],0)+1
    return {'private_evidence':str(path),'observations':counts,'balances_changed':False,'legacy_states_changed':False}


if __name__=='__main__':
    import argparse
    from telegram_bot.crypto_pay import CRYPTO_PAY
    from .provider_routes import ProviderRoutes,verifier_for
    parser=argparse.ArgumentParser();parser.add_argument('root',type=Path)
    args=parser.parse_args()
    try:
        route=ProviderRoutes(args.root).get('binance')
        verifier=verifier_for({'provider':route.provider,'receiver':route.receiver,'route_snapshot':route.snapshot})
    except (OSError,ValueError):verifier=None
    print(json.dumps(save_probe(args.root,CRYPTO_PAY,binance_adapter=verifier)))
