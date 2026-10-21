"""Private partnership notices, explicit retirement and UTC delivery evidence."""
import hashlib
import logging
import threading
import time
from uuid import UUID
from telegram_bot import views
from telegram_bot.authorization import admin_ids
from .bot_workflow import _serial_user
from .client import CommerceUnavailable
from .withdrawal_receipts import _utc_stamp
from .partner_roles import PartnerRoleMixin
from telegram_bot.presentation import audit_body_html, DIVIDER

logger=logging.getLogger('PartnerLifecycle')
DELIVERY={'pending':'بانتظار الإرسال','sending':'جارٍ الإرسال','sent':'قبل تيليجرام الرسالة؛ لا يثبت قراءتها',
          'rejected':'رفض تيليجرام الإرسال','uncertain':'وصول غير مؤكد','waiting_config':'بانتظار إعداد قناة التوثيق'}


def lifecycle_panel(event,*,admin=False,person_name=None):
    title={'added':'إشعار إضافة الشريك','activated':'إشعار تفعيل الشراكة','ended':'إشعار انتهاء الشراكة','reactivated':'إشعار استئناف الشراكة'}.get(event.get('kind',''),'إشعار الشراكة')
    text='<b>'+title+'</b>\n\n'
    if person_name and event.get('recipient_telegram_id'):
        text+='الاسم: <b>'+views.safe(person_name(event['recipient_telegram_id']))+'</b>\nرقم حساب تيليجرام: <code>'+str(event['recipient_telegram_id'])+'</code>\n\n'
    text+=audit_body_html(event['body'][:3200])
    text+='\n\nبصمة النص الأصلي SHA-256: <code>'+views.safe(event['body_sha256'])+'</code>'
    if event.get('acknowledged_at'):
        text+='\nرد حساب الشريك بالاطلاع في: '+_utc_stamp(event['acknowledged_at'])
    else:text+='\nلم يُسجل رد الشريك بالاطلاع بعد.'
    rows=[]
    if admin:
        text+='\nمنفذ الإجراء: '+('<code>'+str(event['actor_telegram_id'])+'</code>' if event.get('actor_telegram_id') else 'خادم المنصة؛ لم تُسجل هوية مدير لهذا الإدخال')
        text+='\n\n<b>حالة الإرسال</b>'
        for delivery in event.get('deliveries',[]):
            text+='\n'+('الشريك' if delivery['recipient_kind']=='partner' else 'قناة التوثيق')+' · '+DELIVERY.get(delivery['state'],delivery['state'])
            if delivery.get('telegram_message_id'):text+='\nرقم رسالة تيليجرام: '+str(delivery['telegram_message_id'])
            if delivery.get('telegram_date'):text+='\nوقت الرسالة لدى تيليجرام بالتوقيت العالمي: '+_utc_stamp(delivery['telegram_date'])
            if delivery.get('completed_at'):text+='\nوقت تسجيل نتيجة الإرسال بالتوقيت العالمي: '+_utc_stamp(delivery['completed_at'])
        if event.get('kind')=='ended' and event.get('partner_id'):
            rows.append([views.button('🔄 إعادة تفعيل الشراكة',f"pl:reopen:{event['partner_id']}")])
        rows.extend([[views.button('تحديث حالة التوثيق','pl:view:'+event['id'])],
                     [views.button('الشركاء السابقون','pl:ended:0')],[views.button('الشركاء','cp:list:0')]])
    elif not event.get('acknowledged_at'):
        rows.append([views.button('اطلعت على الإشعار فقط','pl:ack:'+event['id'])])
    rows.extend([[views.button('الرئيسية','home')]])
    return views.Panel(text,rows)


class PartnerLifecycleMixin(PartnerRoleMixin):
    def _commerce_cancel_pending(self,actor):
        inherited=getattr(super(),'_commerce_cancel_pending',None)
        if inherited is not None:inherited(actor)
        with self._ui_lock:
            self._partner_end_reviews={k:v for k,v in getattr(self,'_partner_end_reviews',{}).items()
                                       if k[0]!=actor and v['expires']>time.monotonic()}

    @_serial_user
    def _partner_lifecycle_callback(self,command,actor,message_id=None,callback=None):
        try:
            parts = command.split(':')
            if len(parts) < 3: raise ValueError('Invalid command')
            action = parts[1]
            value = ':'.join(parts[2:])
            if action in {'view','ack'}:
                event_id=str(UUID(value))
                if action=='ack':
                    reference=(callback or {}).get('id')
                    if not isinstance(reference,str) or not reference.strip():raise ValueError('Native acknowledgement required')
                    event=self._commerce().rpc('acknowledge_partner_notice',{'p_event_id':event_id,'p_telegram_user_id':actor,
                        'p_reference':'telegram-partner-notice-ack:'+reference})
                else:event=self._commerce().rpc('partner_lifecycle_event',{'p_event_id':event_id,'p_actor_telegram_id':actor})
                if event.get('recipient_telegram_id')!=actor and actor not in admin_ids(self):raise ValueError('Unowned notice')
                self._show_panel(actor,lifecycle_panel(event,admin=actor in admin_ids(self),person_name=getattr(self,'_telegram_person_name',None)),message_id)
                return
            if actor not in admin_ids(self):raise PermissionError('Partner administrator required')
            if action=='ended':
                offset=max(0,int(value));records=self._commerce().rpc('ended_partners',{'p_actor_telegram_id':actor,'p_offset':offset})
                rows=[];lines=['<b>الشركاء السابقون</b>', 'السجل المالي وتوثيق الشراكة محفوظان.']
                for record in records:
                    name=self._telegram_person_name(record['telegram_user_id'])
                    lines.extend(['',DIVIDER,'<b>'+views.safe(name,70)+'</b>',
                        'رقم حساب تيليجرام: <code>'+str(record['telegram_user_id'])+'</code>',
                        'رمز الشريك: <code>'+views.safe(record.get('code'))+'</code>',
                        'تاريخ انتهاء الشراكة: '+_utc_stamp(record['ended_at'])])
                    rows.append([views.button(name[:50]+' · عرض السجل','pl:view:'+record['event_id'])])
                if offset:rows.append([views.button('الأحدث','pl:ended:'+str(max(0,offset-10)))])
                if len(records)==10:rows.append([views.button('الأقدم','pl:ended:'+str(offset+10))])
                rows.append([views.button('الشركاء الحاليون','cp:list:0')])
                if not records:lines.append('لا توجد شراكات منتهية في هذه الصفحة.')
                self._show_panel(actor,views.Panel('\n'.join(lines),rows),message_id)
                return
            if action not in {'remove','endconfirm','reopen','reopenconfirm','rok'}:raise ValueError('Unknown lifecycle action')
            if action in {'reopen','reopenconfirm','rok'}:
                vparts = value.split(':')
                partner = str(UUID(vparts[0]))
                target_tg = vparts[1] if len(vparts)>1 else ''
                if action=='reopen':
                    text = ('<b>مراجعة استئناف وإعادة تفعيل الشراكة</b>\n\n'
                            + (f'الشريك: <code>{target_tg}</code>\n\n' if target_tg else '')
                            + 'عند التأكيد، ستتم استعادة ملف الشريك في قائمة الشركاء وتعيين حالته بانتظار ضبط الشروط، '
                            'مع إرسال إشعار توثيق باستئناف الشراكة للشريك وقناة التوثيق.\n\n'
                            'ستنتقل بعدها مباشرة لتحديد النسبة والاستحقاق وتفعيل كود الإحالة.')
                    rows = [
                        [views.button('✅ تأكيد إعادة تفعيل الشراكة', f'pl:rok:{partner}', 'success')],
                        [views.button('إلغاء', 'pl:ended:0')]
                    ]
                    self._show_panel(actor, views.Panel(text, rows), message_id)
                    return
                # reopenconfirm or rok
                receipt = (callback or {}).get('id') or f"manual-{int(time.time())}"
                res = self._commerce().rpc('reactivate_partner', {
                    'p_actor_telegram_id': actor,
                    'p_partner_id': partner,
                    'p_reference': 'telegram-partner-reopen:' + receipt
                })
                pid = str(UUID(res['partner_id']))
                target_user = res.get('telegram_user_id') or (int(target_tg) if target_tg.isdigit() else None)
                if target_user:
                    self._grant_partner_role(int(target_user))
                    try: self._sync_partner_menus()
                    except Exception: pass
                rows = [
                    [views.button('⚙️ ضبط شروط ونسبة الشريك', f'cp:terms:{pid}')],
                    [views.button('‹ قائمة الشركاء', 'cp:list:0')]
                ]
                notice = '\nحُفظ إشعار استئناف الشراكة للشريك وقناة التوثيق.' if res.get('notice_event_id') else ''
                self._show_panel(actor, views.Panel(
                    f"<b>تمت إعادة الشراكة بنجاح!</b>\nالكود: <code>{views.safe(res.get('code',''))}</code>\n\n"
                    "حدد النسبة والاستحقاق للشريك الآن لتفعيل كود الإحالة." + notice,
                    rows), message_id)
                return
            partner=str(UUID(value))
            if action=='remove':
                preview=self._commerce().rpc('partner_end_preview',{'p_actor_telegram_id':actor,'p_partner_id':partner})
                if preview.get('ended_at'):
                    self._revoke_partner_role(preview['telegram_user_id'])
                    try:self._reset_customer_menu(preview['telegram_user_id'])
                    except Exception:logger.warning('Customer menu refresh deferred; partnership already ended on server.')
                    self._partner_lifecycle_callback('pl:view:'+preview['event_id'],actor,message_id,callback)
                    return
                with self._ui_lock:
                    reviews={k:v for k,v in getattr(self,'_partner_end_reviews',{}).items() if k[0]!=actor and v['expires']>time.monotonic()}
                    reviews[(actor,partner)]={'terms_version':preview.get('terms_version'),'expires':time.monotonic()+600}
                    self._partner_end_reviews=reviews
                text=('<b>مراجعة إنهاء الشراكة وإزالتها من القائمة الحالية</b>\n\n'
                    'سيُوقف كود الإحالة والميزات الجديدة المرتبطة بالشراكة عند التأكيد، وتبقى المستحقات والسجلات المالية محفوظة. '
                    'سيُحفظ نص الإشعار ويُرسل للشريك وقناة التوثيق، مع وقت التنفيذ UTC.\n\n'
                    '<b>صيغة الإشعار</b>\n'+views.safe(preview['preview'],3500)+
                    '\n\nسيُثبت وقت التنفيذ الفعلي ورقم توثيق جديد عند التأكيد. السجل تقني ولا يمثل إعفاءً قانونياً أو مخالصة.')
                self._show_panel(actor,views.Panel(text,[[views.button('تأكيد إنهاء الشراكة وإرسال الإشعار','pl:endconfirm:'+partner)],
                    [views.button('إلغاء','cp:list:0')]]),message_id)
                return
            review=getattr(self,'_partner_end_reviews',{}).get((actor,partner))
            receipt=(callback or {}).get('id')
            if not review or review['expires']<=time.monotonic() or not isinstance(receipt,str) or not receipt.strip():
                raise ValueError('Reviewed native end confirmation required')
            event=self._commerce().rpc('end_partner',{'p_actor_telegram_id':actor,'p_partner_id':partner,
                'p_expected_terms_version':review['terms_version'],'p_reference':'telegram-partner-end:'+receipt})
            self._revoke_partner_role(event['recipient_telegram_id'])
            try:self._reset_customer_menu(event['recipient_telegram_id'])
            except Exception:logger.warning('Customer menu refresh deferred; partnership already ended on server.')
            self._show_panel(actor,lifecycle_panel(event,admin=True,person_name=getattr(self,'_telegram_person_name',None)),message_id)
        except PermissionError:
            self._commerce_notice(actor,'غير مصرح','إنهاء الشراكة وسجل التوثيق الإداري خاصان بالإدارة.',message_id)
        except (CommerceUnavailable,OSError,ValueError,KeyError,TypeError):
            self._commerce_notice(actor,'تعذر التأكد من إجراء الشراكة','أعد فتح سجل الشراكة للتحقق من الحالة. لا يعني تعذر الاتصال أن الإجراء لم يُحفظ.',message_id)

    def _partner_lifecycle_notification_once(self):
        notice=self._commerce().rpc('claim_partner_lifecycle_notification')
        if not notice:return False
        state='uncertain';message=None;telegram_date=None;attempted=False
        try:
            if hashlib.sha256(notice['body'].encode('utf-8')).hexdigest()!=notice['body_sha256']:
                raise ValueError('Frozen notice differs from its stored hash')
            rows=[]
            if notice['recipient_kind']=='partner' and notice['kind']=='event':
                rows=[[views.button('اطلعت على الإشعار فقط','pl:ack:'+notice['event_id'])],
                      [views.button('عرض سجل الإشعار','pl:view:'+notice['event_id'])]]
            attempted=True
            response=self._api_call('sendMessage',{'chat_id':notice['recipient_chat_id'],'text':notice['body'],
                'reply_markup':{'inline_keyboard':rows}})
            if isinstance(response,dict) and response.get('ok') is True:
                native=response.get('result') or {};message=native.get('message_id');telegram_date=native.get('date')
                destination=(native.get('chat') or {}).get('id')
                if type(message) is int and message>0 and type(telegram_date) is int and 0<telegram_date<=time.time()+300 and type(destination) is int and destination==notice['recipient_chat_id']:
                    state='sent'
            elif isinstance(response,dict) and response.get('ok') is False:state='rejected'
        except Exception:
            state='uncertain' if attempted else 'rejected'
            logger.warning('Partnership notice could not be confirmed; delivery audit retained.')
        finally:
            self._commerce().rpc('complete_partner_lifecycle_notification',{'p_id':notice['id'],'p_token':notice['token'],
                'p_state':state,'p_message_id':message if state=='sent' else None,
                'p_telegram_timestamp':telegram_date if state=='sent' else None})
        if state=='sent' and notice.get('event_kind')=='ended' and notice['recipient_kind']=='partner':
            try:
                self._revoke_partner_role(notice['recipient_chat_id'])
                self._reset_customer_menu(notice['recipient_chat_id'])
            except Exception:logger.warning('Retired partner menu refresh unavailable; role controls remain on server.')
        elif state=='sent' and notice.get('event_kind') in ('activated','reactivated') and notice['recipient_kind']=='partner':
            try:
                self._grant_partner_role(notice['recipient_chat_id'])
                self._sync_partner_menus()
            except Exception:pass
        return True

    def _start_partner_lifecycle_notifications(self):
        thread=getattr(self,'_partner_lifecycle_thread',None)
        if thread and thread.is_alive():return
        def run():
            failures=0
            while not self._stop_event.is_set():
                try:
                    worked=self._partner_lifecycle_notification_once();failures=0;delay=.1 if worked else 15
                except Exception:
                    failures=min(failures+1,6);delay=min(300,15*2**(failures-1))
                    logger.warning('Partnership notices unavailable; retry in %ss; audit retained.',delay)
                self._stop_event.wait(delay)
        self._partner_lifecycle_thread=threading.Thread(target=run,name='PartnerLifecycleNotices',daemon=True)
        self._partner_lifecycle_thread.start()
