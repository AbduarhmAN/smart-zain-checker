"""Owner-approved partner sheets, flat pricing and durable price notices."""
import logging
import secrets
import threading
import time
from decimal import InvalidOperation
from uuid import UUID
from telegram_bot import views
from telegram_bot.authorization import admin_ids
from .bot_workflow import _serial_user
from .checkout import egp
from .client import CommerceUnavailable
from .finance import money_minor_input
from .archive_transport import ArchiveRejected,ArchiveAmbiguous,UPLOAD_LIMIT,document_receipt
from .bridge import file_sha256
from .partner_roles import active_partner

logger=logging.getLogger('PartnerSheets')


class PartnerSheetMixin:
    def _commerce_command(self,text,actor,is_admin,username='',*,message_id=None):
        command=text.split()[0].split('@')[0].lower() if text.split() else ''
        if command=='/partner_checks':
            if actor in admin_ids(self):self._partner_sheet_admin('ps:list:0',actor,message_id,{})
            else:self._partner_sheet_customer('pc:home:0',actor,message_id)
            return True
        if command=='/partner_price':
            self._partner_sheet_admin('ps:settings:0',actor,message_id,{})
            return True
        return super()._commerce_command(text,actor,is_admin,username,message_id=message_id)

    def _partner_sheet_blocked(self,state,token,user,message_id):
        request=state.get('partner_sheet')
        if not request:return False
        if request['state']=='approved':return False
        rejected=request['state']=='rejected' or state.get('order_state')=='cancelled'
        self._show_panel(user,views.Panel('<b>طلب فحص الشريك</b>\n\n'+
            ('رفضت الإدارة هذا الشيت.' if rejected else 'أُرسل الشيت للإدارة. يلزم اعتماد الإدارة لهذا الشيت قبل ظهور الدفع أو بدء الفحص.')+
            '\nرقم الطلب: <code>'+views.safe(state['order_id'])+'</code>\nلن يُحجز أي مبلغ قبل الموافقة.',
            [[views.button('تحديث حالة الشيت','co:review:'+token)],
             [views.button('فحص الشركاء والسعر','pc:home:0')],[views.button('الرئيسية','home')]]),message_id)
        return True

    @_serial_user
    def _partner_sheet_customer(self,command,actor,message_id=None):
        try:
            _,action,value=command.split(':')
            context=self._commerce().rpc('partner_sheet_context',{'p_telegram_user_id':actor})
            if not active_partner(context):
                self._revoke_partner_role(actor)
                self._commerce_notice(actor,'الحساب بصلاحيات زبون','فحص الشركاء متاح للشراكات النشطة فقط. يمكنك إرسال ملفك من قائمة الزبون.',message_id)
                return
            if action=='open':
                order=str(UUID(value))
                with self._ui_lock:
                    token=next((k for k,v in self._sheet_drafts.items() if v.get('chat')==actor and v.get('commerce_order_id')==order),None)
                if token:self._commerce_start_checkout(token,actor,message_id)
                else:self._commerce_notice(actor,'طلب الشيت','لم نجد مراجعة هذا الطلب على الجهاز الحالي. افتح طلباتك أو تواصل مع الإدارة برقم الطلب: '+order,message_id)
                return
            if action!='home':raise ValueError('Unknown partner sheet action')
            if not context['is_partner']:raise ValueError('Active partner required')
            price=context.get('price')
            text='<b>فحص الشركاء بسعر التكلفة</b>\n\n'
            text+=('السعر الحالي للشيت كاملاً: <b>'+egp(price['amount_minor'])+' جنيه</b>\nنسخة السعر: '+str(price['version'])
                   if price else 'لم تحدد الإدارة سعر الشيت بعد.')
            text+='\n\nأرسل الشيت وحدد الأعمدة ثم اطلب مراجعة الرسوم. كل شيت يحتاج موافقة الإدارة أولاً، ثم موافقتك على الدفع. السعر يثبت عند اعتماد الشيت ولا توجد عمولة إحالة على طلبك. تُرد حصة الفحوص غير المنجزة وفق سياسة الاسترداد المعروضة.'
            with self._ui_lock:
                drafts=[(k,v.get('sheet_name','الشيت')) for k,v in self._sheet_drafts.items()
                        if v.get('chat')==actor and v.get('commerce_order_id')]
            rows=[[views.button('إرسال شيت للفحص','upload')]]
            rows.extend([[views.button('مراجعة '+str(name)[:40],'co:review:'+token)] for token,name in drafts[-8:]])
            rows.append([views.button('الرئيسية','home')])
            self._show_panel(actor,views.Panel(text,rows),message_id)
        except (CommerceUnavailable,OSError,ValueError,KeyError,TypeError):
            self._commerce_notice(actor,'تعذر عرض فحص الشريك','راجع حالة الشراكة والطلب مع الإدارة ثم أعد المحاولة.',message_id)

    @_serial_user
    def _partner_sheet_admin(self,command,actor,message_id=None,callback=None):
        if actor not in admin_ids(self):
            self._commerce_notice(actor,'غير مصرح','اعتماد الشيت وسعر الشركاء خاصان بالإدارة.',message_id);return
        try:
            _,action,value=command.split(':')
            if action in {'settings','edit'}:
                context=self._commerce().rpc('partner_sheet_context',{'p_telegram_user_id':actor});price=context.get('price')
                if action=='edit':
                    with self._ui_lock:self._waiting_user_inputs[actor]={'action':'partner_sheet_price','expires':time.monotonic()+600,'message_id':message_id}
                    self._commerce_notice(actor,'سعر الشيت للشريك','أرسل المبلغ الثابت للشيت كله بالجنيه. سيظهر لك تأكيد قبل الحفظ وإشعار الشركاء.',message_id);return
                status=self._commerce().rpc('partner_sheet_notification_status',{'p_actor_telegram_id':actor})
                text='<b>إعداد سعر فحص الشركاء</b>\n\n'
                text+=('سعر الشيت كاملاً: <b>'+egp(price['amount_minor'])+' جنيه</b>\nنسخة السعر: '+str(price['version']) if price else 'لم يُحدد سعر بعد؛ لا يمكن اعتماد شيت دون سعر.')
                text+='\n\nتعديل السعر يُنشئ إشعاراً لكل شريك نشط. الطلبات المعتمدة تحتفظ بسعرها؛ الطلبات المنتظرة تأخذ السعر الذي توافق عليه عند اعتمادها. كل إيراد هذه الطلبات للمنصة دون عمولة إحالة.'
                text+='\n\nالإشعارات: وصل '+str(status.get('sent',0))+' · بالانتظار '+str(status.get('pending',0))+' · جارٍ '+str(status.get('sending',0))+' · وصول غير مؤكد '+str(status.get('uncertain',0))+' · مرفوض من Telegram '+str(status.get('rejected',0))
                rows=[[views.button('تغيير السعر وإشعار الشركاء','ps:edit:0')],
                      [views.button('شيتات بانتظار موافقتي','ps:list:0')],[views.button('تحديث حالة الإشعارات','ps:settings:0')],[views.button('الرئيسية','home')]]
                self._show_panel(actor,views.Panel(text,rows),message_id);return
            if action=='save':
                review=getattr(self,'_partner_price_reviews',{}).get(value)
                reference=(callback or {}).get('id')
                if not review or review['actor']!=actor or review['expires']<=time.monotonic() or not isinstance(reference,str) or not reference.strip():
                    raise ValueError('Authenticated reviewed price required')
                # Stable reference survives an ambiguous response and a second click.
                result=self._commerce().rpc('configure_partner_sheet_price',{'p_actor_telegram_id':actor,
                    'p_amount_minor':review['amount_minor'],'p_reference':'telegram-sheet-price:'+value})
                self._commerce_notice(actor,'حُفظ سعر الشيت',egp(result['amount_minor'])+' جنيه للشيت كاملاً. أُدرجت إشعارات '+str(result['recipients'])+' شريك في سجل الإرسال. راجع الإعدادات لمعرفة الوصول.',message_id)
                return
            if action=='list':
                offset=max(0,int(value))
                page=self._commerce().rpc('partner_sheet_requests',{'p_actor_telegram_id':actor,'p_offset':offset})
                rows=[[views.button(str(item['telegram_user_id'])+' · '+str(item['source'].get('original_filename','الشيت'))[:35],
                                    'ps:review:'+item['order_id'])] for item in page['items']]
                if offset:rows.append([views.button('السابق','ps:list:'+str(max(0,offset-10)))])
                if len(page['items'])==10:rows.append([views.button('التالي','ps:list:'+str(offset+10))])
                rows.extend([[views.button('تحديث','ps:list:'+str(offset))],[views.button('سعر الشيت والإشعارات','ps:settings:0')],[views.button('الرئيسية','home')]])
                self._show_panel(actor,views.Panel('<b>شيتات الشركاء بانتظار موافقتي</b>\n\n'+
                    ('اختر الشيت لمراجعة تفاصيله واعتماد سعره.' if page['items'] else 'لا توجد شيتات معلقة في هذه الصفحة.'),rows),message_id);return
            if action in {'review','approve','reject','file'}:
                order=str(UUID(value))
                page=self._commerce().rpc('partner_sheet_requests',{'p_actor_telegram_id':actor,'p_order_id':order})
                if not page['items']:raise ValueError('Unknown partner sheet')
                request=page['items'][0];price=page.get('price')
                if action=='file':
                    digest=request['source']['file_sha256']
                    path=self.project_root/'.commerce'/'inputs'/(digest+'.xlsx')
                    if file_sha256(path)!=digest or path.stat().st_size>UPLOAD_LIMIT:raise ValueError('Verified sheet unavailable')
                    with path.open('rb') as stream:
                        message=self._archive_transport().api('sendDocument',{'chat_id':actor,'caption':'الشيت المرسل للموافقة\nرقم الطلب: '+order+'\nSHA-256: '+digest},
                            files={'document':(order+'.xlsx',stream,'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')})
                    document_receipt(message,actor,expected_size=path.stat().st_size)
                    return
                if action!='review':
                    review=getattr(self,'_partner_sheet_reviews',{}).get((actor,order))
                    receipt=(callback or {}).get('id')
                    if not review or review['expires']<=time.monotonic() or not isinstance(receipt,str) or not receipt.strip():
                        raise ValueError('Native reviewed sheet decision required')
                    self._commerce().rpc('decide_partner_sheet',{'p_actor_telegram_id':actor,'p_order_id':order,
                        'p_approve':action=='approve','p_price_version':review['version'],
                        'p_reference':'telegram-sheet-decision:'+receipt})
                    page=self._commerce().rpc('partner_sheet_requests',{'p_actor_telegram_id':actor,'p_order_id':order})
                    request=page['items'][0]
                source=request['source']
                text=('<b>مراجعة فحص الشريك</b>\n\nالشريك: <code>'+str(request['telegram_user_id'])+'</code>\nالملف: '+
                    views.safe(source.get('original_filename','الشيت'))+'\nالورقة رقم: '+str(int(source.get('sheet_index',0))+1)+
                    '\nالفحوص: '+str(request['requested_checks'])+'\nالطلب: <code>'+order+'</code>\nبصمة الملف: <code>'+views.safe(source['file_sha256'])+'</code>')
                rows=[[views.button('تحميل الشيت الأصلي للمراجعة','ps:file:'+order)]]
                if request['state']=='pending' and request['order_state']=='draft':
                    text+='\n\n'+('السعر المعروض للشيت كله: <b>'+egp(price['amount_minor'])+' جنيه</b>' if price else 'حدد سعر الشيت من الإعدادات أولاً.')
                    if not hasattr(self,'_partner_sheet_reviews'):self._partner_sheet_reviews={}
                    self._partner_sheet_reviews={k:v for k,v in self._partner_sheet_reviews.items() if v['expires']>time.monotonic()}
                    self._partner_sheet_reviews[(actor,order)]={'version':price['version'] if price else None,'expires':time.monotonic()+600}
                    if price:rows.append([views.button('أوافق على الشيت بهذا السعر','ps:approve:'+order,'success')])
                    rows.append([views.button('رفض هذا الشيت','ps:reject:'+order)])
                else:
                    text+='\nالحالة: '+{'approved':'معتمد','rejected':'مرفوض'}.get(request['state'],'ملغى')
                    if request.get('amount_minor') is not None:text+='\nالسعر المحفوظ: '+egp(request['amount_minor'])+' جنيه'
                rows.extend([[views.button('الشيتات المنتظرة','ps:list:0')],[views.button('إعداد سعر الشيت','ps:settings:0')]])
                self._show_panel(actor,views.Panel(text,rows),message_id);return
            raise ValueError('Unknown sheet administration action')
        except (CommerceUnavailable,OSError,ValueError,KeyError,TypeError,ArchiveRejected,ArchiveAmbiguous):
            self._commerce_notice(actor,'تعذر التأكد من إجراء الشيت','أعد فتح مراجعة الشيت أو الإعدادات للتحقق. إذا تغير السعر، راجعه من جديد قبل الموافقة.',message_id)

    def _commerce_text_input(self,actor,text,waiting):
        if waiting.get('action')!='partner_sheet_price':return super()._commerce_text_input(actor,text,waiting)
        try:
            if actor not in admin_ids(self) or waiting.get('expires',0)<=time.monotonic():raise ValueError('Valid admin input required')
            amount=money_minor_input(text.strip())
            if amount>100000000:raise ValueError('Sheet price exceeds supported range')
            token=secrets.token_hex(6)
            self._partner_price_reviews={k:v for k,v in getattr(self,'_partner_price_reviews',{}).items() if v['expires']>time.monotonic()}
            self._partner_price_reviews[token]={'actor':actor,'amount_minor':amount,'expires':time.monotonic()+600}
            self._show_panel(actor,views.Panel('<b>تأكيد سعر فحص الشركاء</b>\n\n'+egp(amount)+' جنيه للشيت كاملاً.\nسيُرسل إشعار بالسعر إلى الشركاء النشطين. الطلبات المعتمدة تبقى بسعرها المحفوظ.',
                [[views.button('حفظ السعر وإرسال الإشعارات','ps:save:'+token,'success')],[views.button('إلغاء','ps:settings:0')]]),waiting.get('message_id'))
        except (ValueError,TypeError,InvalidOperation):
            if actor in admin_ids(self) and waiting.get('expires',0)>time.monotonic():
                with self._ui_lock:self._waiting_user_inputs[actor]=waiting
            self._commerce_notice(actor,'راجع مبلغ الشيت','أدخل مبلغاً موجباً بالجنيه للشيت كله.',waiting.get('message_id'))
        return True

    def _partner_sheet_delivery_once(self):
        record=self._commerce().rpc('claim_partner_sheet_notification')
        if not record:return False
        payload=record['payload'];kind=payload['kind'];rows=[]
        if kind=='price':
            text='<b>تحديث سعر فحص الشركاء</b>\n\nالسعر الأساسي الجديد: <b>'+egp(payload['amount_minor'])+' جنيه للشيت كاملاً</b>.\nنسخة السعر: '+str(payload['version'])+'\nكل شيت يحتاج موافقة الإدارة. الطلبات المعتمدة تحتفظ بالسعر الذي اعتمدت به.'
            rows=[[views.button('السعر وفحص الشركاء','pc:home:0')]]
        elif kind=='request':
            text='<b>شيت شريك ينتظر موافقتك</b>\n\nالشريك: <code>'+str(payload['partner_telegram_id'])+'</code>\nالطلب: <code>'+payload['order_id']+'</code>'
            rows=[[views.button('مراجعة الشيت واعتماد السعر','ps:review:'+payload['order_id'])]]
        else:
            text='<b>قرار مراجعة الشيت</b>\n\n'+('وافقت الإدارة على الشيت بسعر ثابت '+egp(payload['amount_minor'])+' جنيه. افتح الطلب للموافقة على الدفع.' if payload['state']=='approved' else 'رفضت الإدارة طلب فحص هذا الشيت.')+'\nالطلب: <code>'+payload['order_id']+'</code>'
            rows=[[views.button('فتح طلب الشيت','pc:open:'+payload['order_id'])]]
        outcome='uncertain';message=None
        try:
            response=self._api_call('sendMessage',{'chat_id':record['recipient_telegram_id'],'text':text,'parse_mode':'HTML',
                'reply_markup':{'inline_keyboard':rows}})
            if isinstance(response,dict):
                if response.get('ok') is True:
                    message=(response.get('result') or {}).get('message_id')
                    destination=(response.get('result') or {}).get('chat',{}).get('id')
                    if type(message) is int and message>0 and type(destination) is int and destination==record['recipient_telegram_id']:outcome='sent'
                elif response.get('ok') is False:outcome='rejected'
        finally:
            self._commerce().rpc('complete_partner_sheet_notification',{'p_id':record['id'],'p_token':record['token'],
                'p_state':outcome,'p_message_id':message if outcome=='sent' else None})
        return True

    def _start_partner_sheet_notifications(self):
        thread=getattr(self,'_partner_sheet_notification_thread',None)
        if thread and thread.is_alive():return
        def run():
            while not self._stop_event.is_set():
                try:worked=self._partner_sheet_delivery_once()
                except Exception:
                    worked=False
                    logger.warning('Partner sheet notifications unavailable; durable delivery state retained.')
                self._stop_event.wait(0.1 if worked else 15)
        self._partner_sheet_notification_thread=threading.Thread(target=run,name='PartnerSheetNotices',daemon=True)
        self._partner_sheet_notification_thread.start()
