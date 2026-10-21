"""Native support UI. Staff sends require explicit review and an authenticated click."""
from __future__ import annotations
import json
import sqlite3
import time
from pathlib import Path
from uuid import UUID
from telegram_bot import views
from .support import SupportService,reply_text
from .client import CommerceUnavailable
from .bot_workflow import _serial_user
from ..official_support import CHANNEL_ONLY,support_button,support_text


class SupportUiMixin:
    def _official_support(self,actor,message_id=None,order=None):
        with self._ui_lock:
            waiting=self._waiting_user_inputs.get(actor,{})
            if waiting.get('action','').startswith(('commerce_support_','commerce_vodafone_complaint','commerce_vodafone_reply')):
                self._waiting_user_inputs.pop(actor,None)
        service=getattr(self,'_support_service',None)
        if service is not None:service.store.end_context(actor)
        return self._show_panel(actor,views.Panel(support_text(order),[[support_button()],[views.button('الرئيسية','home')]]),message_id)

    def _support(self):
        if not hasattr(self,'_support_service'):
            self._support_service=SupportService(self.project_root,client=self._commerce(),bot_fingerprint=self._bot_fingerprint)
        self._support_service.start()
        return self._support_service

    def _existing_support(self):
        if CHANNEL_ONLY:return None
        if hasattr(self,'_support_service'):return self._support_service
        root=getattr(self,'project_root',None)
        if isinstance(root,Path) and (root/'.commerce'/'support.sqlite').is_file():return self._support()
        return None

    def _capture_support_arrival(self,update):
        if CHANNEL_ONLY:return
        service=self._existing_support()
        if service is None:return
        message=update.get('message') or update.get('edited_message') or {}
        # Validate before cursor advancement; this path performs only bounded local I/O.
        try:key=service.capture(update,self.current_authorized_ids)
        except ValueError:
            update['_support_error']='invalid_support_message'
            return
        if key:update['_support_event']=key

    def _support_handle_message(self,update):
        if CHANNEL_ONLY:
            if update.get('_support_event') or update.get('_support_error'):
                message=update.get('message') or update.get('edited_message') or {}
                actor=(message.get('from') or {}).get('id')
                if type(actor) is int and actor>0 and (message.get('chat') or {}).get('id')==actor:
                    self._official_support(actor);return True
            return False
        if not update.get('message') and not update.get('edited_message'):return False
        service=self._existing_support()
        if service is None:return False
        if update.get('_support_error'):
            message=update.get('message') or update.get('edited_message') or {}
            actor=(message.get('from') or {}).get('id')
            if type(actor) is int and (message.get('chat') or {}).get('id')==actor:
                self._commerce_notice(actor,'راجع رسالة الدعم','أرسل نصًا أو مرفقًا مدعومًا. رد الإدارة يجب أن يكون نصًا لا يزيد عن 3000 حرف.')
            return True
        key=update.get('_support_event')
        if not key:
            key=service.capture(update,self.current_authorized_ids)
        if not key:return False
        event=service.store.event(key);message=update.get('message') or update.get('edited_message') or {}
        actor=(message.get('from') or {}).get('id')
        if not event or actor!=event['p_actor_telegram_id'] or (message.get('chat') or {}).get('id')!=actor:
            raise ValueError('Support event does not belong to this sender')
        if event['p_record']['kind']=='staff_reply':
            self._support_reply_review(key,actor)
        elif event['p_record']['kind']=='staff_edit':
            self._show_panel(actor,views.Panel('<b>حُفظ تعديل رسالة المصدر</b>\n\n'
                'الرد المقترح السابق الذي لم يُرسل أُلغي. لإرسال النص المعدل، افتح «كتابة رد» وأرسله كرسالة جديدة للمراجعة. الردود المرسلة تبقى محفوظة كما أُرسلت.',
                [[views.button('كتابة رد',f'sp:compose:{event["p_order_id"]}')],[views.button('سجل الدعم',f'sp:history:{event["p_order_id"]}:0')]]))
        else:
            self._show_panel(actor,views.Panel('<b>رسالتك محفوظة</b>\n\n'
                'حفظنا الرسالة وربطناها بالطلب. التعديلات تُحفظ كسجل إضافي دون تغيير النص الأصلي.\n'
                f"رقم الطلب: <code>{event['p_order_id']}</code>",
                [[views.button('سجل الدعم',f'sp:history:{event["p_order_id"]}:0')],
                 [views.button('إرسال رسالة أخرى',f'sp:compose:{event["p_order_id"]}')],[views.button('الرئيسية','home')]]))
        return True

    def _support_begin(self,actor,order,role,message_id=None):
        if CHANNEL_ONLY:return self._official_support(actor,message_id,order)
        if role=='admin' and actor not in self.current_authorized_ids:raise ValueError('Only support administrators may compose a reply')
        me=(self._api_call('getMe',{}) or {}).get('result') or {}
        if me.get('is_bot') is not True or type(me.get('id')) is not int:raise ValueError('Verified support bot identity unavailable')
        self._support().begin(actor,order,role,me['id'])
        self._commerce_cancel_pending(actor)
        with self._ui_lock:self._waiting_user_inputs.pop(actor,None)
        label='اكتب الرد للعميل' if role=='admin' else 'اكتب رسالتك للدعم'
        body=('أرسل نص الرد، بحد أقصى 3000 حرف. ستراجعه قبل إرساله للعميل.' if role=='admin' else
            'أرسل رسالتك أو المرفق المتعلق بهذا الطلب. تُحفظ هذه الرسالة والتعديلات اللاحقة ضمن سجل الدعم. لإلغاء الإرسال، ارجع للرئيسية.')
        self._show_panel(actor,views.Panel(f'<b>{label}</b>\n\nرقم الطلب: <code>{order}</code>\n\n{body}',
            [[views.button('سجل الدعم',f'sp:history:{order}:0')],[views.button('إلغاء','home')]]),message_id)

    def _commerce_command(self,text,actor,is_admin,username='',*,message_id=None):
        parts=text.split();command=parts[0].split('@')[0].lower() if parts else ''
        if command not in {'/support','/support_inbox','/support_reply'}:
            return super()._commerce_command(text,actor,is_admin,username,message_id=message_id)
        if CHANNEL_ONLY and command!='/support_inbox':
            self._official_support(actor,message_id,parts[1] if len(parts)==2 else None);return True
        try:
            if command=='/support_inbox':self._support_inbox(actor,0,message_id)
            elif len(parts)==2:self._support_begin(actor,str(UUID(parts[1])),'admin' if command=='/support_reply' else 'customer',message_id)
            else:
                with self._ui_lock:self._waiting_user_inputs[actor]={'action':'commerce_support_order','expires':time.monotonic()+600,'message_id':message_id}
                self._commerce_notice(actor,'الدعم المرتبط بطلب','أرسل رقم الطلب الكامل لفتح مراسلة تخصه. للأعضاء الإداريين: /support_inbox و/support_reply رقم_الطلب.',message_id)
        except (ValueError,CommerceUnavailable,OSError,sqlite3.Error,KeyError):self._support_failed(actor,message_id)
        return True

    def _commerce_text_input(self,actor,text,waiting):
        if waiting.get('action')!='commerce_support_order':return super()._commerce_text_input(actor,text,waiting)
        if CHANNEL_ONLY:self._official_support(actor,waiting.get('message_id'));return True
        try:
            if waiting.get('expires',0)<=time.monotonic():raise ValueError('Expired support selection')
            self._support_begin(actor,str(UUID(text.strip())),'customer',waiting.get('message_id'))
        except (ValueError,CommerceUnavailable,OSError,sqlite3.Error,KeyError):self._support_failed(actor,waiting.get('message_id'))
        return True

    def _support_failed(self,actor,message_id=None):
        self._commerce_notice(actor,'تعذر إكمال الدعم','راجع رقم الطلب وصلاحية الوصول، ثم افتح الدعم من جديد. الرسائل التي حفظناها تبقى محفوظة عند انقطاع الاتصال.',message_id)

    def _support_inbox(self,actor,offset,message_id=None):
        if actor not in self.current_authorized_ids:raise ValueError('Support inbox is administrative')
        threads=self._commerce().rpc('support_inbox',{'p_actor_telegram_id':actor,'p_offset':offset})
        lines=['<b>سجل الدعم السابق · للقراءة فقط</b>' if CHANNEL_ONLY else '<b>طلبات الدعم</b>'];rows=[]
        for item in threads:
            order=str(UUID(item['order_id']));label=('مفتوح' if item['state']=='open' else 'مغلق')
            lines.append(f"\n<code>{order}</code>\n{label} · Telegram {item['owner_telegram_id']} · "+('يحتاج ردًا' if item['needs_reply'] else 'يوجد رد محفوظ'))
            rows.append([views.button('فتح الطلب · '+order[:8],f'sp:history:{order}:0')])
        if not threads:lines.append('لا توجد مراسلات دعم محفوظة بعد.')
        if offset:rows.append([views.button('السابق',f'sp:inbox:{max(0,offset-5)}')])
        if len(threads)==5:rows.append([views.button('التالي',f'sp:inbox:{offset+5}')])
        rows.append([views.button('الرئيسية','home')]);self._show_panel(actor,views.Panel('\n'.join(lines),rows),message_id)

    def _support_history(self,actor,order,offset,message_id=None):
        role='admin' if actor in self.current_authorized_ids else 'customer'
        history=self._commerce().rpc('support_history',{'p_order_id':order,'p_actor_telegram_id':actor,'p_role':role,'p_offset':offset})
        lines=['<b>سجل الدعم</b>',f'<code>{order}</code>'];rows=[]
        labels={'customer_message':'رسالة العميل','customer_edit':'تعديل العميل','staff_reply':'رد الدعم','staff_edit':'تعديل مصدر الرد'}
        for item in history['messages']:
            preview=item['body'].encode('utf-16-le')[:1400].decode('utf-16-le',errors='ignore')
            lines.append('\n'+labels[item['kind']]+' · '+views.safe(item['delivery_state'])+'\n'+views.safe(preview,700))
            if item['attachments']:lines.append(f"مرفقات محفوظة: {len(item['attachments'])}")
            for attachment in item['attachments'][:2]:lines.append('مرفق: '+views.safe(attachment.get('file_name') or attachment['kind'],60))
            if not CHANNEL_ONLY and role=='admin' and item['kind']=='staff_reply' and item['delivery_state']!='confirmed':
                rows.append([views.button('مراجعة الرد وحالته',f'sp:review:{item["event_key"]}')])
        if not history['messages']:lines.append('لا توجد رسائل محفوظة في هذه الصفحة بعد.')
        if offset:rows.append([views.button('رسائل أحدث',f'sp:history:{order}:{max(0,offset-3)}')])
        if len(history['messages'])==3:rows.append([views.button('رسائل أقدم',f'sp:history:{order}:{offset+3}')])
        rows.append([support_button()] if CHANNEL_ONLY else [views.button('كتابة رد' if role=='admin' else 'إرسال رسالة',f'sp:compose:{order}')])
        if role=='admin':
            state='open' if history.get('thread_state')=='closed' else 'closed'
            if not CHANNEL_ONLY:rows.append([views.button('إعادة فتح الدعم' if state=='open' else 'إغلاق طلب الدعم',f'sp:{state}:{order}')])
            rows.append([views.button('طلبات الدعم','sp:inbox:0')])
        rows.append([views.button('الرئيسية','home')]);self._show_panel(actor,views.Panel('\n'.join(lines),rows),message_id)

    def _support_reply_review(self,key,actor,message_id=None):
        if CHANNEL_ONLY:return self._official_support(actor,message_id)
        if actor not in self.current_authorized_ids:raise ValueError('Support reply review is administrative')
        service=self._support();event=service.store.event(key)
        if not event or event['p_actor_telegram_id']!=actor or event['p_record']['kind']!='staff_reply':raise ValueError('Owned support reply required')
        state=service.store.delivery_state(key);rows=[]
        if state in {'started','ambiguous'}:
            status='لم يتأكد إرسال الرد. راجع المحادثة مع العميل؛ لن نعيد إرساله تلقائيًا.'
        elif state=='confirmed':status='أُرسل الرد وحُفظ إيصال الرسالة.'
        elif state=='superseded':status='عُدّلت رسالة مصدر هذا الرد، فألغينا المقترح السابق. افتح كتابة رد لإنشاء المقترح المحدث.'
        else:
            status='الرد مقترح ولم يُرسل للعميل. أرسل بعد مراجعته.'
            rows.append([views.button('إرسال الرد للعميل',f'sp:send:{key}','success')])
        text=f"<b>مراجعة رد الدعم</b>\n\nرقم الطلب: <code>{event['p_order_id']}</code>\n\n{views.safe(event['p_record']['body'],3000)}\n\n{status}"
        rows.extend([[views.button('سجل الطلب',f'sp:history:{event["p_order_id"]}:0')],[views.button('الرئيسية','home')]])
        self._show_panel(actor,views.Panel(text,rows),message_id)

    @_serial_user
    def _support_send(self,key,actor,message_id=None):
        if CHANNEL_ONLY:return self._official_support(actor,message_id)
        if actor not in self.current_authorized_ids:raise ValueError('Explicit staff authority required')
        service=self._support();event=service.store.event(key)
        if not event or event['p_actor_telegram_id']!=actor or event['p_record']['kind']!='staff_reply':raise ValueError('Owned proposed reply required')
        saved=service.record_response(key)
        if not isinstance(saved,dict) or saved.get('state')!='stored':
            self._commerce_notice(actor,'الرد قيد الحفظ','احتفظنا بالرد محليًا. انتظر مزامنته ثم افتح مراجعته لإرساله.',message_id);return
        profile=self._commerce().rpc('support_order',{'p_order_id':event['p_order_id'],'p_actor_telegram_id':actor,'p_role':'admin'})
        me=(self._api_call('getMe',{}) or {}).get('result') or {}
        if me.get('id')!=event['p_record']['source']['bot_id']:raise ValueError('Support bot identity changed')
        service.prepare_delivery(key,actor,profile['owner_telegram_id'])
        claim=service.store.claim_delivery(key)
        if claim is None:self._support_reply_review(key,actor,message_id);return
        try:
            response=self._api_call('sendMessage',{'chat_id':profile['owner_telegram_id'],'text':reply_text(event['p_order_id'],event['p_record']['body'])})
            if not isinstance(response,dict) or response.get('ok') is not True:
                rejected=isinstance(response,dict) and response.get('ok') is False and type(response.get('error_code')) is int
                service.store.delivery_failed(key,claim,rejected=rejected)
            else:service.capture_delivery(key,actor,response.get('result'))
        except Exception:
            service.store.delivery_failed(key,claim,rejected=False)
            raise
        self._support_reply_review(key,actor,message_id)

    def _support_callback(self,command,actor,message_id=None,callback=None):
        try:
            parts=command.split(':');action=parts[1]
            if CHANNEL_ONLY and action not in {'inbox','history'}:
                self._official_support(actor,message_id,parts[2] if action=='compose' and len(parts)>2 else None);return
            if action=='inbox':self._support_inbox(actor,int(parts[2]),message_id)
            elif action=='history':self._support_history(actor,str(UUID(parts[2])),int(parts[3]),message_id)
            elif action=='compose':self._support_begin(actor,str(UUID(parts[2])),'admin' if actor in self.current_authorized_ids else 'customer',message_id)
            elif action=='review':self._support_reply_review(str(UUID(parts[2])),actor,message_id)
            elif action=='send':self._support_send(str(UUID(parts[2])),actor,message_id)
            elif action in {'open','closed'}:
                if actor not in self.current_authorized_ids or not callback or not callback.get('id'):raise ValueError('Support state requires an authenticated staff click')
                order=str(UUID(parts[2]));self._commerce().rpc('set_support_state',{'p_order_id':order,
                    'p_actor_telegram_id':actor,'p_state':action,'p_reference':'telegram-support-state:'+str(actor)+':'+callback['id']})
                self._support_history(actor,order,0,message_id)
            else:raise ValueError('Unknown support action')
        except (ValueError,CommerceUnavailable,OSError,sqlite3.Error,KeyError,IndexError,TypeError):self._support_failed(actor,message_id)
