"""Manual Vodafone Cash requests, proof review and request-linked complaints."""
import hashlib
import json
import re
import sqlite3
import time
from uuid import UUID, uuid5, NAMESPACE_URL

from telegram_bot import views
from .bot_workflow import _serial_user
from .checkout import egp
from .client import CommerceUnavailable
from .finance import positive_minor
from .legacy_wallets import minor
from ..official_support import CHANNEL_ONLY

RECEIVER = '01050740996'
RECEIVER_NAME = 'عبدالرحمن'
STATES = {'awaiting_evidence':'بانتظار البيانات يدوياً', 'pending':'بانتظار مراجعة الأدمن',
          'approved':'مقبول وأُضيف الرصيد', 'rejected':'مرفوض'}


def phone_number(text):
    value=''.join(str(int(c)) if c.isdecimal() else c for c in str(text).strip())
    value=re.sub(r'[\s()-]', '', value)
    if value.startswith('+20'):value='0'+value[3:]
    elif value.startswith('0020'):value='0'+value[4:]
    if not re.fullmatch(r'01[0125][0-9]{8}',value):raise ValueError('Egyptian sender phone required')
    return value


def telegram_proof(message, owner):
    if ((message.get('chat') or {}).get('id')!=owner or (message.get('from') or {}).get('id')!=owner
        or (message.get('chat') or {}).get('type','private')!='private' or (message.get('from') or {}).get('is_bot')):
        raise ValueError('Private owner receipt required')
    result={'chat_id':owner,'sender_id':owner,'message_id':positive_minor(message.get('message_id'))}
    if message.get('photo'):
        media=message['photo'][-1];kind='photo'
    elif message.get('document'):
        media=message['document'];kind='document'
        if media.get('mime_type') not in {'image/jpeg','image/png','application/pdf'}:
            raise ValueError('Receipt must be an image or PDF')
    else:
        body=message.get('text','').strip()
        bounded_text(body,3000)
        return {**result,'kind':'text','body':body}
    for field,limit in [('file_id',512),('file_unique_id',256)]:
        if not isinstance(media.get(field),str) or not 1<=len(media[field])<=limit:raise ValueError('Telegram receipt identity required')
    if media.get('file_size',0)>20*1024*1024:raise ValueError('Receipt attachment too large')
    return {**result,'kind':kind,'file_id':media['file_id'],'file_unique_id':media['file_unique_id']}


def review_digest(record):
    return hashlib.sha256(json.dumps({key:record[key] for key in
        ('id','amount_minor','receiver','sender_phone','proof')},sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def bounded_text(value,limit):
    if not value or len(value.encode('utf-16-le'))//2>limit:raise ValueError('Message exceeds supported length')
    return value


class VodafoneCashMixin:
    def _vodafone_get(self,request_id,actor,admin=False):
        if admin and actor not in self.current_authorized_ids:raise ValueError('Admin required')
        return self._commerce().rpc('vodafone_request',{'p_request_id':str(UUID(request_id)),
            'p_actor_telegram_id':actor,'p_admin':admin})

    def _vodafone_prompt(self,actor,step,request_id=None,message_id=None,**extra):
        if CHANNEL_ONLY and step in {'complaint','reply'}:
            self._official_support(actor,message_id,request_id);return
        waiting={'action':'commerce_vodafone_'+step,'request_id':request_id,'message_id':message_id,
                 'expires':time.monotonic()+1800,**extra}
        with self._ui_lock:self._waiting_user_inputs[actor]=waiting
        bodies={'amount':'أرسل مبلغ الشحن الذي تريده بالجنيه المصري، مثل 60 أو 60.50.',
            'phone':'أرسل رقم الهاتف الذي أرسلت منه المبلغ يدوياً. مثال الصيغة: 010xxxxxxxx.',
            'proof':'أرسل صورة إشعار يدوي. سيُراجع الأدمن وصول المبلغ قبل القبول.',
            'transaction':'بعد مطابقة الإشعار مع الإرسال اليدوي، أرسل رقم العملية الحقيقي من سجل محفظة الاستلام. لا تستخدم رقم الطلب بدلًا منه.',
            'reject':'اكتب سبب الرفض ليظهر للعميل.',
            'complaint':'اكتب المشكلة أو الشكوى المتعلقة بهذا الطلب، بحد أقصى 3000 حرف. ستصل للأدمن مع رقم الطلب.',
            'reply':'اكتب ردك على شكوى العميل، بحد أقصى 3000 حرف. ستراجعه قبل الإرسال.'}
        body=bodies[step]
        if request_id:body=f'رقم الطلب: <code>{request_id}</code>\n\n'+body
        back='vc:open:'+request_id if request_id else 'wallet'
        response=self._show_panel(actor,views.Panel('<b>فودافون كاش</b>\n\n'+body,[[views.button('رجوع',back)]],
            photo='vodafone_cash' if step in {'amount','phone','proof'} else None),message_id)
        result=response.get('result') if isinstance(response,dict) and response.get('ok') else None
        if isinstance(result,dict) and type(result.get('message_id')) is int:
            with self._ui_lock:
                if self._waiting_user_inputs.get(actor) is waiting:waiting['message_id']=result['message_id']

    def _vodafone_show(self,record,actor,message_id=None,admin=False):
        request_id=record['id'];state=record['state']
        if not admin and state=='approved':self._topups().mark_owner(actor)
        text=(f'<b>طلب فودافون كاش</b>\n\nرقم الطلب: <code>{request_id}</code>\n'
              f"الحالة: <b>{STATES[state]}</b>\nالمبلغ: <b>{egp(record['amount_minor'])} جنيه مصري</b>\n"
              f"أرسل إلى: <code>{views.safe(record['receiver'])}</code>\nباسم: {views.safe(record['receiver_name'])}\n")
        if record.get('sender_phone'):text+=f"رقم المُرسل: <code>{views.safe(record['sender_phone'])}</code>\n"
        if admin:text+=f"رقم العميل: <code>{record['telegram_user_id']}</code>\n"
        if record.get('rejection_reason'):text+='سبب الرفض: '+views.safe(record['rejection_reason'],1000)+'\n'
        if state=='awaiting_evidence':text+='\nأرسل المبلغ المحدد يدوياً، ثم أرسل رقم الهاتف وإشعار الدفع اليدوي. لا يُضاف الرصيد قبل مراجعة الأدمن.'
        elif state=='pending':text+='\nوصل طلبك للمراجعة. احتفظ برقم الطلب ولا تكرر الدفع يدوياً بسبب انتظار القبول.'
        elif state=='approved':text+='\nأُضيف المبلغ إلى محفظتك. افتح طلب الفحص وادفع من رصيد المحفظة.'
        for item in record.get('messages',[])[-3:]:
            text+='\n\n'+('الأدمن: ' if item['actor_role']=='admin' else 'العميل: ')+views.safe(item['body'],350)
        if admin:
            rows=[]
            if state=='pending':rows.append([views.button('مراجعة الإشعار',f'vc:review:{request_id}')])
            rows.extend([[views.support_button()],[views.button('قائمة المراجعة','vc:inbox:0')]])
        else:
            rows=[]
            if state=='awaiting_evidence':rows.append([views.button('أرسلت يدوي · تقديم الإشعار',f'vc:phone:{request_id}')])
            rows.extend([[views.support_button()],
                [views.button('تحديث الحالة',f'vc:open:{request_id}')],[views.button('طلباتي بفودافون كاش','vc:list:0')],[views.button('المحفظة','wallet')]])
        self._show_panel(actor,views.Panel(text,rows,photo=None if admin else 'vodafone_cash'),message_id)

    def _vodafone_notify(self,record,*,staff=False,title=None,body=None):
        targets=sorted(self.current_authorized_ids) if staff else [record['telegram_user_id']]
        state_label = STATES.get(record['state'], record['state'])
        if staff:
            text=(f"🔔 <b>{views.safe(title or 'طلب فودافون كاش يحتاج مراجعتك')}</b>\n\n"
                  f"🆔 رقم الطلب: <code>{record['id']}</code>\n"
                  f"💰 المبلغ: <b>{egp(record['amount_minor'])} جنيه</b>\n"
                  f"📱 رقم المُرسل: <code>{views.safe(record.get('sender_phone') or 'قيد التقديم')}</code>\n"
                  f"👤 رقم العميل: <code>{record['telegram_user_id']}</code>\n"
                  f"الحالة: <b>{state_label}</b>")
            keyboard = [[views.button('🔍 مراجعة الإشعار فوراً', f"vc:review:{record['id']}")],
                        [views.button('📋 تفاصيل الطلب', f"vc:admin:{record['id']}")]]
        else:
            text=f"<b>{views.safe(title or 'تحديث طلب فودافون كاش')}</b>\nرقم الطلب: <code>{record['id']}</code>\nالحالة: {state_label}\nالمبلغ: {egp(record['amount_minor'])} جنيه"
            if record.get('rejection_reason'):text+='\nسبب الرفض: '+views.safe(record['rejection_reason'],200 if body else 1000)
            if body:text+='\n\n'+views.safe(body,3000)
            keyboard = [[views.button('فتح الطلب', 'vc:open:' + record['id'])]]

        for target in targets:
            try:self._api_call('sendMessage',{'chat_id':target,'text':text,'parse_mode':'HTML',
                'reply_markup':json.dumps({'inline_keyboard':keyboard})})
            except Exception:pass  # The durable request/list remains available after delivery failure.

    @_serial_user
    def _vodafone_callback(self,command,actor,message_id,callback):
        try:
            _,action,value=command.split(':',2)
            if CHANNEL_ONLY and action in {'complaint','reply','send'}:
                self._official_support(actor,message_id,value);return
            with self._ui_lock:
                if self._waiting_user_inputs.get(actor,{}).get('action','').startswith('commerce_vodafone_'):
                    self._waiting_user_inputs.pop(actor,None)
            if action=='start':
                self._vodafone_prompt(actor,'amount',message_id=message_id);return
            if action in {'list','inbox'}:
                admin=action=='inbox'
                if admin and actor not in self.current_authorized_ids:raise ValueError('Admin required')
                offset=int(value)
                records=self._commerce().rpc('list_vodafone_requests',{'p_actor_telegram_id':actor,'p_admin':admin,'p_offset':offset})
                rows=[[views.button(egp(r['amount_minor'])+' جنيه · '+STATES[r['state']],
                    ('vc:admin:' if admin else 'vc:open:')+r['id'])] for r in records]
                if offset:rows.append([views.button('السابق',f'vc:{action}:{max(0,offset-8)}')])
                if len(records)==8:rows.append([views.button('التالي',f'vc:{action}:{offset+8}')])
                rows.append([views.button('الرئيسية','home')])
                heading='طلبات اعتماد فودافون كاش' if admin else 'طلبات فودافون كاش'
                self._show_panel(actor,views.Panel('<b>'+heading+'</b>\n\n'+('اختر الطلب لعرض تفاصيله.' if records else 'لا توجد طلبات.'),rows,
                    photo=None if admin else 'vodafone_cash'),message_id);return
            request_id=str(UUID(value))
            admin=action in {'admin','review','transaction','reject','approve','decline','reply','send'}
            record=self._vodafone_get(request_id,actor,admin)
            if action in {'open','admin'}:self._vodafone_show(record,actor,message_id,admin);return
            if action=='phone':
                if record['state']!='awaiting_evidence':self._vodafone_show(record,actor,message_id);return
                self._vodafone_prompt(actor,'phone',request_id,message_id);return
            if action in {'complaint','reply'}:
                self._vodafone_prompt(actor,action,request_id,message_id);return
            if action=='review':
                if record['state']!='pending':self._vodafone_show(record,actor,message_id,True);return
                proof=record['proof'];method={'photo':'sendPhoto','document':'sendDocument','text':'sendMessage'}[proof['kind']]
                payload={'chat_id':actor}
                panel_text=(f"<b>🔎 مراجعة إشعار يدوي (فودافون كاش)</b>\n\nرقم الطلب: <code>{request_id}</code>\n"
                            f"المبلغ: <b>{egp(record['amount_minor'])} جنيه</b>\nالمُرسل: <code>{record.get('sender_phone') or 'غير محدد'}</code>\n"
                            f"العميل: <code>{record['telegram_user_id']}</code>\n\n"
                            f"طابق الإشعار مع سجل محفظة الاستلام يدوياً. الصورة وحدها لا تؤكد وصول المبلغ.")
                buttons=[[views.button('وصل يدوي · إدخال رقم العملية',f'vc:transaction:{request_id}')],
                         [views.button('رفض مع ذكر السبب',f'vc:reject:{request_id}')],
                         [views.button('قائمة المراجعة','vc:inbox:0')]]
                if proof['kind']=='text':
                    payload['text']=f"{panel_text}\n\n<b>نص الإشعار:</b>\n{views.safe(proof['body'], 2000)}"
                    payload['parse_mode']='HTML'
                    payload['reply_markup']=json.dumps({'inline_keyboard':buttons})
                else:
                    payload['photo' if proof['kind']=='photo' else 'document']=proof['file_id']
                    payload['caption']=panel_text
                    payload['parse_mode']='HTML'
                    payload['reply_markup']=json.dumps({'inline_keyboard':buttons})
                delivered=self._api_call(method,payload)
                if not isinstance(delivered,dict) or delivered.get('ok') is not True:raise ValueError('Receipt preview delivery unavailable')
                with self._ui_lock:
                    reviews={k:v for k,v in getattr(self,'_vodafone_reviews',{}).items() if v['expires']>time.monotonic()}
                    if len(reviews)>=128:raise ValueError('Review cache full')
                    reviews[(actor,request_id)]={'expires':time.monotonic()+600,'digest':review_digest(record)}
                    self._vodafone_reviews=reviews
                return
            review=getattr(self,'_vodafone_reviews',{}).get((actor,request_id))
            if not review or review['expires']<=time.monotonic() or review['digest']!=review_digest(record):raise ValueError('Review the receipt again')
            if action in {'transaction','reject'}:
                self._vodafone_prompt(actor,action,request_id,message_id);return
            if action in {'approve','decline'}:
                expected='approve' if action=='approve' else 'reject'
                if review.get('action')!=expected or not callback.get('id'):raise ValueError('Explicit matching review required')
                result=self._commerce().rpc('review_vodafone_request',{'p_request_id':request_id,'p_actor_telegram_id':actor,
                    'p_action':expected,'p_value':review['value'],'p_review_reference':f"telegram:{actor}:{callback['id']}"})
                if result['state']=='approved':
                    self._topups().mark_owner(result['telegram_user_id'])
                    try:
                        from .archive_config import ArchiveConfig
                        archive_cfg = ArchiveConfig(self.project_root).load()
                        if archive_cfg and archive_cfg.get('enabled') and archive_cfg.get('chat_id'):
                            live_text = (
                                f"🧾 <b>توثيق شحن محفظة (فودافون كاش - يدوي)</b>\n\n"
                                f"💰 <b>المبلغ:</b> {egp(result['amount_minor'])} جنيه مصري\n"
                                f"🆔 <b>رقم الطلب:</b> <code>{request_id}</code>\n"
                                f"👤 <b>العميل:</b> <code>{result['telegram_user_id']}</code>\n"
                                f"📱 <b>رقم المُرسل:</b> <code>{views.safe(result.get('sender_phone') or 'غير محدد')}</code>\n"
                                f"🔢 <b>رقم العملية:</b> <code>{views.safe(review.get('value') or '—')}</code>\n"
                                f"✅ <b>الحالة:</b> تم التحقق يدوياً واعتماد الرصيد بنجاح."
                            )
                            self._api_call('sendMessage', {
                                'chat_id': archive_cfg['chat_id'],
                                'text': live_text,
                                'parse_mode': 'HTML'
                            })
                    except Exception as live_err:
                        pass
                self._vodafone_show(result,actor,message_id,True)
                self._vodafone_notify(result,title='قُبل طلب الشحن وأُضيف الرصيد' if expected=='approve' else 'رُفض طلب الشحن');return
            if action=='send':
                if review.get('action')!='reply' or not callback.get('id'):raise ValueError('Reviewed reply required')
                result=self._commerce().rpc('vodafone_message',{'p_request_id':request_id,'p_actor_telegram_id':actor,
                    'p_admin':True,'p_message_id':review['source_message_id'],'p_body':review['value']})
                self._vodafone_notify(result,title='رد الأدمن على شكواك',body=review['value'])
                self._vodafone_show(result,actor,message_id,True);return
            raise ValueError('Unknown payment action')
        except (CommerceUnavailable,OSError,ValueError,KeyError,TypeError,sqlite3.Error):
            self._vodafone_failure(actor,message_id)

    def _vodafone_failure(self,actor,message_id=None):
        rows=[[views.button('طلباتي بفودافون كاش','vc:list:0')],[views.button('المحفظة','wallet')]]
        if actor in self.current_authorized_ids:rows.insert(0,[views.button('مراجعة فودافون كاش','vc:inbox:0')])
        self._show_panel(actor,views.Panel('<b>راجع طلب فودافون كاش</b>\n\nلم نتأكد من اكتمال الإجراء. افتح الطلب نفسه للتحقق؛ لا تكرر الدفع يدوياً. للمشرف: أعد مراجعة الإشعار إذا انتهت صلاحية التأكيد.',rows,
            photo='vodafone_cash'),message_id)

    def _vodafone_handle_media(self,message,actor):
        with self._ui_lock:waiting=self._waiting_user_inputs.get(actor,{})
        if waiting.get('action')!='commerce_vodafone_proof' or not (message.get('photo') or message.get('document')):return False
        return self._vodafone_submit_proof(actor,message,waiting)

    @_serial_user
    def _vodafone_submit_proof(self,actor,message,waiting):
        try:
            if waiting['expires']<=time.monotonic():raise ValueError('Receipt prompt expired')
            proof=telegram_proof(message,actor)
            record=self._commerce().rpc('submit_vodafone_request',{'p_request_id':waiting['request_id'],
                'p_telegram_user_id':actor,'p_sender_phone':waiting['sender_phone'],'p_proof':proof})
            with self._ui_lock:self._waiting_user_inputs.pop(actor,None)
            self._vodafone_show(record,actor,waiting.get('message_id'))
            self._vodafone_notify(record,staff=True,title='طلب فودافون كاش يحتاج مراجعتك')
        except (CommerceUnavailable,OSError,ValueError,KeyError,TypeError,sqlite3.Error):
            with self._ui_lock:self._waiting_user_inputs[actor]=waiting
            self._vodafone_failure(actor,waiting.get('message_id'))
        return True

    @_serial_user
    def _commerce_text_input(self,actor,text,waiting):
        if not waiting.get('action','').startswith('commerce_vodafone_'):return super()._commerce_text_input(actor,text,waiting)
        if CHANNEL_ONLY and waiting['action'] in {'commerce_vodafone_complaint','commerce_vodafone_reply'}:
            self._official_support(actor,waiting.get('message_id'),waiting.get('request_id'));return True
        try:
            if waiting['expires']<=time.monotonic():raise ValueError('Payment prompt expired')
            action=waiting['action'].removeprefix('commerce_vodafone_');request_id=waiting.get('request_id');message_id=waiting.get('message_id')
            if action=='amount':
                amount=positive_minor(minor(text.strip()));incoming=positive_minor(waiting.get('_telegram_input_message_id'))
                request_id=str(uuid5(NAMESPACE_URL,f'zain:vodafone:{actor}:{incoming}'))
                record=self._commerce().rpc('create_vodafone_request',{'p_request_id':request_id,'p_telegram_user_id':actor,'p_amount_minor':amount})
                self._vodafone_show(record,actor,message_id)
            elif action=='phone':
                sender=phone_number(text)
                record=self._vodafone_get(request_id,actor)
                if record['state']!='awaiting_evidence':raise ValueError('Request already submitted')
                self._vodafone_prompt(actor,'proof',request_id,message_id,sender_phone=sender)
            elif action=='proof':return self._vodafone_submit_proof(actor,waiting.get('_telegram_message',{}),waiting)
            elif action=='complaint':
                bounded_text(text.strip(),3000)
                result=self._commerce().rpc('vodafone_message',{'p_request_id':request_id,'p_actor_telegram_id':actor,
                    'p_admin':False,'p_message_id':positive_minor(waiting.get('_telegram_input_message_id')),'p_body':text.strip()})
                self._vodafone_show(result,actor,message_id)
                self._vodafone_notify(result,staff=True,title='شكوى مرتبطة بطلب فودافون كاش',body=text.strip())
            elif action in {'transaction','reject','reply'}:
                record=self._vodafone_get(request_id,actor,True)
                value=text.strip()
                if action=='transaction':
                    value=''.join(str(int(c)) if c.isdecimal() else c for c in value).upper()
                    if not re.fullmatch(r'[A-Z0-9][A-Z0-9_.:-]{2,127}',value):raise ValueError('Actual transaction reference required')
                else:bounded_text(value,3000 if action=='reply' else 1000)
                with self._ui_lock:
                    reviews=getattr(self,'_vodafone_reviews',{})
                    review=reviews.get((actor,request_id))
                    if action=='reply':review={'expires':time.monotonic()+600,'digest':review_digest(record)}
                    if not review or review['expires']<=time.monotonic() or review['digest']!=review_digest(record):raise ValueError('Fresh reviewer required')
                    review.update(action={'transaction':'approve','reject':'reject','reply':'reply'}[action],value=value,
                        source_message_id=positive_minor(waiting.get('_telegram_input_message_id')))
                    reviews[(actor,request_id)]=review;self._vodafone_reviews=reviews
                callback_action={'transaction':'approve','reject':'decline','reply':'send'}[action]
                label={'transaction':'أؤكد الوصول يدوياً وأعتمد الشحن','reject':'تأكيد الرفض وإبلاغ العميل','reply':'إرسال الرد للعميل'}[action]
                self._show_panel(actor,views.Panel(f'<b>تأكيد مراجعة فودافون كاش</b>\n\nرقم الطلب: <code>{request_id}</code>\n\n'+views.safe(value,3000),
                    [[views.button(label,f'vc:{callback_action}:{request_id}')],[views.button('رجوع',f'vc:admin:{request_id}')]]),message_id)
            else:raise ValueError('Unknown payment input')
        except (CommerceUnavailable,OSError,ValueError,KeyError,TypeError,sqlite3.Error):
            with self._ui_lock:self._waiting_user_inputs[actor]=waiting
            self._vodafone_failure(actor,waiting.get('message_id'))
        return True

    def _commerce_command(self,text,actor,is_admin,username='',*,message_id=None):
        command=text.split()[0].split('@')[0].lower() if text.split() else ''
        if command not in {'/vodafone','/vodafone_requests','/vodafone_reviews'}:
            return super()._commerce_command(text,actor,is_admin,username,message_id=message_id)
        action={'/vodafone':'start','/vodafone_requests':'list','/vodafone_reviews':'inbox'}[command]
        self._vodafone_callback(f'vc:{action}:0',actor,None,{})
        return True
