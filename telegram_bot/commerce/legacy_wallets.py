"""Read-only reconciliation of legacy wallets before an explicit atomic cutover.

No JSON balance is silently rounded, inferred from an unverified payment, or
credited to Supabase here. Private retained evidence identifies the exact source.
"""
from datetime import datetime,timezone
from decimal import Decimal,InvalidOperation
import hashlib
import json
from pathlib import Path
import re


def minor(value):
    if isinstance(value,bool) or not isinstance(value,(str,int,Decimal)):
        raise ValueError('Exact legacy EGP amount required')
    try: amount=Decimal(value)
    except (InvalidOperation,ValueError):raise ValueError('Invalid legacy money') from None
    if not amount.is_finite() or amount<0 or amount>Decimal('92233720368547758.07'):
        raise ValueError('Legacy money is not nonnegative exact piastres')
    if amount!=0 and amount.as_tuple().exponent < -2:
        # Trailing zeroes are harmless, but a sub-cent amount is never rounded.
        amount=amount.normalize()
        if amount.as_tuple().exponent < -2:raise ValueError('Sub-cent legacy money cannot be imported')
    scaled=amount*100
    if scaled!=scaled.to_integral_value() or (amount>0 and scaled==0):
        raise ValueError('Legacy money is not nonnegative exact piastres')
    return int(scaled)


def _read(path):
    raw=Path(path).read_bytes()
    if len(raw)>32*1024*1024:raise ValueError('Legacy source exceeds audit size limit')
    data=json.loads(raw.decode('utf-8'),parse_float=Decimal)
    if not isinstance(data,dict):raise ValueError('Legacy source must be an object')
    return raw,data,hashlib.sha256(raw).hexdigest()


def reconcile(root):
    root=Path(root).resolve()
    wallet_path=root/'user_wallets.json';payment_path=root/'pending_payments.json'
    wallet_raw,wallets,wallet_hash=_read(wallet_path)
    payment_raw,payments,payment_hash=_read(payment_path)
    accounts=[];seen_accounts=set();conflicts=[]
    for uid,value in sorted(wallets.items()):
        try:
            if not re.fullmatch(r'[1-9][0-9]{0,18}',uid) or int(uid)>9223372036854775807:
                raise ValueError('Canonical Telegram user ID required')
            if not isinstance(value,dict):raise ValueError('Legacy account is not an object')
            account=value.get('account_id')
            if type(account) is not int or not 100000000<=account<=999999999 or account in seen_accounts:
                raise ValueError('Unique legacy account ID required')
            seen_accounts.add(account)
            balance=minor(value.get('balance'))
            history=value.get('history')
            if not isinstance(history,list):raise ValueError('Legacy history is missing')
            net=0;seen_keys=set()
            for entry in history:
                if not isinstance(entry,dict) or entry.get('type') not in {'credit','debit'}:
                    raise ValueError('Unknown legacy journal entry')
                amount=minor(entry.get('amount'))
                net+=amount if entry['type']=='credit' else -amount
                if net<0:raise ValueError('Legacy history overspent the wallet')
                if 'balance' in entry and minor(entry['balance'])!=net:
                    raise ValueError('Intermediate legacy balance differs from history')
                key=entry.get('idempotency_key')
                if key:
                    if not isinstance(key,str) or key in seen_keys:raise ValueError('Duplicate legacy monetary key')
                    seen_keys.add(key)
            if balance!=net:raise ValueError('Legacy opening balance does not reconcile')
            accounts.append({'telegram_user_id':int(uid),'legacy_account_id':account,
                'available_minor':balance,'history_entries':len(history),
                'source_fingerprint':hashlib.sha256(json.dumps(value,sort_keys=True,default=str).encode()).hexdigest()})
        except (ValueError,TypeError,KeyError) as exc:
            conflicts.append({'legacy_user_key':uid,'reason':str(exc)})
    unresolved=[];states={};paid_candidates=[]
    for token,payment in sorted(payments.items()):
        if not isinstance(payment,dict):
            unresolved.append({'token':token,'reason':'Malformed payment record'});continue
        state=payment.get('status','unknown');states[state]=states.get(state,0)+1
        if state in {'ready','pending','approved'}:
            unresolved.append({'token':token,'state':state,'user_id':payment.get('user_id'),
                'reason':'Legacy invoice or transfer must be reconciled before disabling its funding path'})
        elif state=='fulfilled':
            paid_candidates.append({'token':token,'user_id':payment.get('user_id'),'job_id':payment.get('job_id'),
                'reason':'Queue fulfillment alone does not prove completed paid service or Telegram delivery'})
        elif state!='rejected':
            unresolved.append({'token':token,'state':state,'reason':'Unknown legacy payment state'})
    if wallet_path.read_bytes()!=wallet_raw or payment_path.read_bytes()!=payment_raw:
        raise ValueError('Legacy sources changed during reconciliation; take a new snapshot')
    return {'version':1,'at':datetime.now(timezone.utc).isoformat(),'wallet_sha256':wallet_hash,
        'payments_sha256':payment_hash,'accounts':accounts,'conflicts':conflicts,'unresolved_payments':unresolved,
        'paid_service_candidates':paid_candidates,'legacy_payment_states':states,
        'cutover_ready':not conflicts and not unresolved,'total_available_minor':sum(row['available_minor'] for row in accounts),
        'supabase_balances_changed':False,'legacy_balances_changed':False}


def save_preview(root):
    root=Path(root).resolve();plan=reconcile(root)
    folder=root/'.commerce'/'legacy_migration';folder.mkdir(parents=True,exist_ok=True)
    name=plan['wallet_sha256']+'_'+plan['payments_sha256']
    path=folder/(name+'.json')
    if not path.exists():
        with path.open('x',encoding='utf-8') as stream:json.dump(plan,stream,ensure_ascii=False,indent=2)
    return {'plan':str(path),'wallet_accounts':len(plan['accounts']),'conflicts':len(plan['conflicts']),
        'unresolved_legacy_payments':len(plan['unresolved_payments']),'paid_service_candidates':len(plan['paid_service_candidates']),
        'total_available_minor':plan['total_available_minor'],'cutover_ready':plan['cutover_ready'],
        'supabase_balances_changed':False,'legacy_balances_changed':False}


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('root',type=Path)
    args=parser.parse_args();print(json.dumps(save_preview(args.root)))
