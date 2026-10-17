"""Read-only authority for cancelling an accepted legacy customer checkout."""
import json
import re

def accepted_stopped_legacy(root,job):
    mapping=job.get('column_mapping',{})
    if mapping.get('commerce_order_id'):return False
    owner=mapping.get('telegram_chat_id');receipt=mapping.get('customer_stop') or {}
    if type(owner) is not int or owner<=0 or receipt.get('actor')!=owner:return False
    reference=receipt.get('reference')
    if not isinstance(reference,str) or not reference.startswith(f'telegram-customer-stop:{owner}:'):return False
    marker=mapping.get('payment_token','')
    prefix=f'telegram:{owner}:'
    if not marker.startswith(prefix):return False
    token=marker[len(prefix):]
    if not re.fullmatch('[a-f0-9]{12}',token):return False
    document=json.loads((root/'pending_payments.json').read_text(encoding='utf-8-sig'))
    payment=document.get(token,{})
    return (str(payment.get('user_id'))==str(owner) and payment.get('job_id')==job.get('id')
        and payment.get('status') in {'approved','fulfilled'})
