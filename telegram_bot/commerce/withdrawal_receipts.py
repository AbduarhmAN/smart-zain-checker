"""Two-party payout evidence; acknowledgements never move money."""
from uuid import UUID
from datetime import datetime,timedelta,timezone
from telegram_bot import views
from telegram_bot.authorization import admin_ids
from .checkout import egp
from .partner_balances import advance_receipt_text


RECEIPT_LABELS={'requested':'بانتظار تأكيد الشريك','confirmed':'أكد الشريك الاستلام','disputed':'الشريك يعترض على الاستلام'}


def _stamp(value):
    if not value:return 'غير مسجل'
    try:
        instant=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        if instant.tzinfo is None:raise ValueError('Explicit stored timezone required')
        return instant.astimezone(timezone(timedelta(hours=3))).strftime('%Y-%m-%d %H:%M:%S')+' بغداد'
    except (TypeError,ValueError):return views.safe(value)


def _utc_stamp(value):
    if not value:return 'غير مسجل'
    try:
        instant=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        if instant.tzinfo is None:raise ValueError('Explicit stored timezone required')
        return instant.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    except (TypeError,ValueError):return views.safe(value)


def receipt_panel(record,*,admin=False):
    receipt=record.get('receipt') or {}
    state=receipt.get('state')
    cash=str(record.get('payout_reference') or '').startswith('cash:')
    text=('<b>توثيق استلام أرباح الشريك</b>\n\n'
        f"رقم السحب: <code>{views.safe(record['id'])}</code>\n"
        f"المبلغ: <b>{egp(record['amount_minor'])} جنيه</b>\n"
        f"الجهة: {views.safe(record.get('destination'),500)}\n"
        f"{'مرجع توثيق نقدي داخلي' if cash else 'مرجع التحويل'}: <code>{views.safe(record.get('payout_reference'),250)}</code>\n"
        f"وقت تسجيل الدفع: {_stamp(record.get('paid_at'))}\n"
        f"وقت تسجيل الدفع UTC: {_utc_stamp(record.get('paid_at'))}\n"
        f"الحالة: {RECEIPT_LABELS.get(state,'الدفع مسجّل؛ لم يُطلب تأكيد الاستلام بعد')}")
    text+=advance_receipt_text(record.get('advance'))
    if receipt:
        text+='\nرقم التوثيق: <code>'+views.safe(receipt['id'])+'</code>'
        if receipt.get('paid_actor_id'):
            text+='\nمسجل الدفع: <code>'+views.safe(receipt['paid_actor_id'])+'</code>'
        if receipt.get('recipient_telegram_id'):
            text+='\nالمستلم: <code>'+views.safe(receipt['recipient_telegram_id'])+'</code>'
        text+='\nوقت إنشاء التوثيق UTC: '+_utc_stamp(receipt.get('requested_at'))
        if receipt.get('responded_at'):
            text+='\nوقت رد الشريك: '+_stamp(receipt['responded_at'])
            text+='\nوقت رد الشريك UTC: '+_utc_stamp(receipt['responded_at'])
    rows=[]
    if admin:
        if not state or state=='requested':
            import urllib.parse
            link = f"https://t.me/ZainCheckerbot?start=ack_{record['id']}"
            share_text = urllib.parse.quote(f"تأكيد استلام أرباح بمبلغ {egp(record['amount_minor'])} جنيه:\n{link}")
            share_url = f"https://t.me/share/url?url={urllib.parse.quote(link)}&text={share_text}"
            rows.append([views.button('إرسال طلب تأكيد للشريك' if not state else 'إعادة إرسال طلب التأكيد',
                                      'wa:requestack:'+record['id']),
                         views.url_button('🔗 مشاركة الرابط للشريك', share_url)])
        rows.append([views.button('سجل التوثيق','wa:receipts:0')])
    elif state=='requested':
        text+='\n\nرسالة من الإدارة: تم صرف أرباحك بالمبلغ والمرجع أعلاه. أكّد فقط إذا استلمته فعليًا. ردك يوثق الاستلام ولا ينفّذ خصمًا جديدًا.'
        rows.extend([[views.button('أؤكد أنني استلمت المبلغ','pf:ack:'+receipt['id'],'success')],
                     [views.button('لم أستلم / أعترض','pf:dispute:'+receipt['id'])]])
    if not admin:rows.append([views.button('سجل سحوباتي','pf:list:0')])
    return views.Panel(text,rows)


class WithdrawalReceiptMixin:
    def _partner_receipt_action(self,action,value,user_id,message_id,callback):
        if action=='receipt':
            record=self._commerce().rpc('partner_withdrawal_receipt',{
                'p_withdrawal_id':str(UUID(value)),'p_telegram_user_id':user_id})
        else:
            reference=callback.get('id')
            if not isinstance(reference,str) or not reference.strip():raise ValueError('Native receipt response required')
            record=self._commerce().rpc('respond_withdrawal_receipt',{'p_receipt_id':str(UUID(value)),
                'p_telegram_user_id':user_id,'p_accept':action=='ack','p_reference':'telegram-receipt-response:'+reference})
            self._wake_payout_notifications()
        if record.get('telegram_user_id')!=user_id:raise ValueError('Receipt owner differs from authenticated user')
        self._show_panel(user_id,receipt_panel(record),message_id)

    def _admin_receipt_action(self,action,value,actor_id,message_id,callback):
        if actor_id not in admin_ids(self):raise ValueError('Payout administrator required')
        if action=='receipts':
            offset=max(0,int(value))
            page=self._commerce().rpc('paid_withdrawal_receipts',{
                'p_actor_telegram_id':actor_id,'p_offset':offset,'p_limit':10})
            rows=[]
            for record in page['items']:
                state=(record.get('receipt') or {}).get('state')
                label=f"{record['telegram_user_id']} · {egp(record['amount_minor'])} جنيه · "+RECEIPT_LABELS.get(state,'الدفع مسجّل')
                rows.append([views.button(label,'wa:review:'+record['id'])])
            if offset:rows.append([views.button('الأحدث','wa:receipts:'+str(max(0,offset-10)))])
            if offset+10<page['total']:rows.append([views.button('الأقدم','wa:receipts:'+str(offset+10))])
            rows.append([views.button('طلبات السحب','wa:list:0')])
            self._show_panel(actor_id,views.Panel('<b>سجل دفع الأرباح وتأكيد الاستلام</b>\n\n'+
                ('اختر السحب لعرض المبلغ والإيصال ورد الشريك.' if page['items'] else 'لا توجد مدفوعات مسجلة بعد.'),rows),message_id)
            return
        reference=callback.get('id')
        if not isinstance(reference,str) or not reference.strip():raise ValueError('Native receipt request required')
        record=self._commerce().rpc('prepare_withdrawal_receipt',{'p_withdrawal_id':str(UUID(value)),
            'p_actor_telegram_id':actor_id,'p_reference':'telegram-receipt-request:'+reference})
        receipt=record.get('receipt') or {}
        if receipt.get('state')=='requested':
            response=self._show_panel(record['telegram_user_id'],receipt_panel(record))
            delivered=isinstance(response,dict) and response.get('ok') is True
            text=('أُرسل طلب التأكيد للشريك. سيبقى بانتظار رده.' if delivered else
                  'حُفظ طلب التأكيد، لكن لم نتأكد من وصول الرسالة. يمكن للشريك فتحه من سجل سحوباته، أو يمكنك إعادة الإرسال.')
            panel=receipt_panel(record,admin=True)
            self._show_panel(actor_id,views.Panel(text+'\n\n'+panel.text,panel.rows),message_id)
        else:
            self._show_panel(actor_id,receipt_panel(record,admin=True),message_id)
