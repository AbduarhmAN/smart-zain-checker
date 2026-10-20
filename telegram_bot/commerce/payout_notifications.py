"""Durable payout notices. Telegram acceptance and partner receipt are distinct."""
import logging
import threading
from uuid import UUID
from telegram_bot import views
from telegram_bot.authorization import admin_ids
from telegram_bot.config import settings
from .checkout import egp
from .client import CommerceUnavailable
from .withdrawal_receipts import receipt_panel,_stamp,_utc_stamp
from .partner_balances import advance_receipt_text

logger=logging.getLogger('PayoutNotifications')
KINDS={'paid':'تم تسجيل صرف الأرباح','confirmed':'أكد الشريك استلام الأرباح','disputed':'اعترض الشريك على استلام الأرباح'}
STATES={'pending':'بانتظار الإرسال','sending':'جارٍ الإرسال','sent':'وصل إلى تلقرام','rejected':'رفض تلقرام الإرسال',
        'uncertain':'وصول الرسالة غير مؤكد','waiting_config':'بانتظار إعداد القناة'}


def payout_notice_panel(notice):
    record=notice['payload'];kind=notice['kind']
    if notice['recipient_kind']=='partner' and kind=='paid':return receipt_panel(record)
    receipt=record.get('receipt') or {}
    cash=str(record.get('payout_reference') or '').startswith('cash:')
    text=('<b>'+KINDS[kind]+'</b>\n\n'
          'رقم السحب: <code>'+views.safe(record['id'])+'</code>\n'
          'الشريك: <code>'+str(record['telegram_user_id'])+'</code>\n'
          'المبلغ: '+egp(record['amount_minor'])+' جنيه\n'
          +('مرجع توثيق نقدي داخلي: <code>' if cash else 'مرجع التحويل: <code>')+views.safe(record.get('payout_reference'),250)+'</code>')
    text+=advance_receipt_text(record.get('advance'))
    text+='\nوقت تسجيل الصرف UTC: '+_utc_stamp(record.get('paid_at'))
    if receipt.get('id'):text+='\nرقم توثيق الاستلام: <code>'+views.safe(receipt['id'])+'</code>'
    if receipt.get('requested_at'):text+='\nوقت إنشاء التوثيق UTC: '+_utc_stamp(receipt['requested_at'])
    if kind=='paid':text+='\nالحالة: سجلت الإدارة الصرف؛ بانتظار تأكيد الشريك.'
    elif kind=='confirmed':text+='\nحُفظ تأكيد الشريك في سجل الاستلام؛ لم يحدث خصم جديد.'
    else:text+='\nحُفظ اعتراض الشريك للمراجعة. لم يُنفّذ تحويل أو استرداد تلقائي.'
    if receipt.get('responded_at'):text+='\nوقت الرد: '+_stamp(receipt['responded_at'])
    if receipt.get('responded_at'):text+='\nوقت الرد UTC: '+_utc_stamp(receipt['responded_at'])
    # Channel notices intentionally omit the partner's bank/account destination.
    rows=[] if notice['recipient_kind']=='channel' else [[views.button('سجل سحوباتي','pf:list:0')]]
    return views.Panel(text,rows)


class PayoutNotificationMixin:
    def _wake_payout_notifications(self):
        event=getattr(self,'_payout_notice_wake',None)
        if event is not None:event.set()

    def _configure_payout_channels(self):
        cfg=settings()
        dedicated=cfg.get('payout_channels',[])
        channels=sorted({chat for chat in (dedicated or cfg.get('admin_groups',[])) if type(chat) is int and chat<0})
        configured=cfg.get('chat_id')
        if not dedicated and type(configured) is int and configured<0 and configured not in channels:channels.append(configured)
        actors=sorted(admin_ids(self))
        if not actors:return
        signature=(actors[0],tuple(sorted(channels)))
        if getattr(self,'_payout_notice_route_signature',None)==signature:return
        self._commerce().rpc('configure_payout_notice_channels',{'p_actor_telegram_id':actors[0],'p_channels':channels})
        self._payout_notice_route_signature=signature

    def _payout_notification_once(self):
        notice=self._commerce().rpc('claim_payout_notification')
        if not notice:return False
        state='uncertain';message=None;attempted=False
        try:
            panel=payout_notice_panel(notice)
            text=views.copyable_message(panel.text)
            attempted=True
            response=self._api_call('sendMessage',{'chat_id':notice['recipient_chat_id'],
                'text':text,'parse_mode':'HTML',
                'reply_markup':{'inline_keyboard':panel.rows}})
            if isinstance(response,dict) and response.get('ok') is True:
                result=response.get('result') or {};message=result.get('message_id')
                if type(message) is int and message>0 and (result.get('chat') or {}).get('id')==notice['recipient_chat_id']:
                    state='sent'
            elif isinstance(response,dict) and response.get('ok') is False:state='rejected'
        except Exception:
            state='uncertain' if attempted else 'rejected'
            logger.warning('Payout notice delivery uncertain; saved notice retained.' if attempted else
                'Payout notice rendering failed before sending; saved for bounded retry.')
        finally:
            self._commerce().rpc('complete_payout_notification',{'p_id':notice['id'],'p_token':notice['token'],
                'p_state':state,'p_message_id':message if state=='sent' else None})
        return True

    def _payout_notice_status(self,actor,value,message_id):
        if actor not in admin_ids(self):raise ValueError('Payout administrator required')
        withdrawal=str(UUID(value))
        records=self._commerce().rpc('payout_notification_status',{'p_actor_telegram_id':actor,'p_withdrawal_id':withdrawal})
        lines=['<b>إشعارات صرف الأرباح</b>','رقم السحب: <code>'+withdrawal+'</code>']
        for record in records:
            lines.append(KINDS[record['kind']]+' · '+('الشريك' if record['recipient_kind']=='partner' else 'القناة')+
                '\n'+STATES.get(record['state'],record['state']))
        if not records:lines.append('لا توجد إشعارات محفوظة لهذا السحب.')
        self._show_panel(actor,views.Panel('\n\n'.join(lines),[[views.button('تحديث','wa:notices:'+withdrawal)],
            [views.button('توثيق الاستلام','wa:review:'+withdrawal)]]),message_id)

    def _start_payout_notifications(self):
        thread=getattr(self,'_payout_notice_thread',None)
        if thread and thread.is_alive():return
        self._payout_notice_wake=threading.Event()
        def run():
            while not self._stop_event.is_set():
                delay=self._payout_notice_cycle()
                self._payout_notice_wake.wait(delay)
                self._payout_notice_wake.clear()
        self._payout_notice_thread=threading.Thread(target=run,name='PayoutNotices',daemon=True)
        self._payout_notice_thread.start()

    def _payout_notice_cycle(self):
        try:
            self._configure_payout_channels()
            worked=self._payout_notification_once()
            self._payout_notice_failures=0
            return .1 if worked else 15
        except Exception as exc:
            failures=min(getattr(self,'_payout_notice_failures',0)+1,7)
            self._payout_notice_failures=failures
            missing=isinstance(exc,CommerceUnavailable) and exc.database_code=='PGRST202'
            delay=min(900,300*2**(failures-1)) if missing else min(300,15*2**(failures-1))
            logger.warning('Payout notice database functions are missing; apply migrations 36/37. Retry in %ss; saved notices retained.' if missing else
                'Payout notices temporarily unavailable; retry in %ss; saved notices retained.',delay)
            return delay
