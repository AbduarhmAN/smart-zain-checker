"""Owner-approved partner sheets, flat pricing and durable price notices."""
import logging
import secrets
import threading
import time
from decimal import InvalidOperation
from uuid import UUID
from telegram_bot import views
from telegram_bot.authorization import admin_ids
from telegram_bot.identity import TelegramIdentityMixin
from .bot_workflow import _serial_user
from .checkout import egp
from .client import CommerceUnavailable
from .finance import money_minor_input
from .archive_transport import ArchiveRejected,ArchiveAmbiguous,UPLOAD_LIMIT,document_receipt
from .bridge import file_sha256
from .partner_roles import active_partner

logger=logging.getLogger('PartnerSheets')


class PartnerSheetMixin(TelegramIdentityMixin):
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
        if state.get('partner_sheet_access_blocked'):
            self._commerce_notice(user,'فحص بسعر الشريك متوقف','أوقفت الإدارة صلاحية فحص الملفات بسعر الشريك لحسابك. الطلبات المدفوعة والفواتير الصادرة محفوظة. يمكنك طلب ملف جديد بالسعر العادي.',message_id)
            return True
        if request['state']=='approved':return False
        rejected=request['state']=='rejected' or state.get('order_state')=='cancelled'
        self._show_panel(user,views.Panel('<b>طلب فحص الشريك</b>\n\n'+
            ('رفضت الإدارة هذا الملف.' if rejected else 'أُرسل الملف للإدارة. يلزم اعتماد الإدارة لهذا الملف قبل ظهور الدفع أو بدء الفحص.')+
            '\nرقم الطلب: <code>'+views.safe(state['order_id'])+'</code>\nلن يُحجز أي مبلغ قبل الموافقة.',
            [[views.button('تحديث حالة الملف','co:review:'+token)],
             [views.button('فحص الشركاء والسعر','pc:home:0')],[views.button('الرئيسية','home')]]),message_id)
        return True

    @_serial_user
    def _partner_sheet_customer(self,command,actor,message_id=None):
        try:
            _,action,value=command.split(':')
            context=self._commerce().rpc('partner_sheet_context',{'p_telegram_user_id':actor})
            if not active_partner(context):
                self._revoke_partner_role(actor)
                self._commerce_notice(actor,'الحساب بصلاحيات عميل','فحص الشركاء متاح للشراكات النشطة فقط. يمكنك إرسال ملفك من قائمة العميل.',message_id)
                return
            if action=='open':
                order=str(UUID(value))
                with self._ui_lock:
                    token=next((k for k,v in self._sheet_drafts.items() if v.get('chat')==actor and v.get('commerce_order_id')==order),None)
                if token:self._commerce_start_checkout(token,actor,message_id)
                else:self._commerce_notice(actor,'طلب الملف','لم نجد مراجعة هذا الطلب على الجهاز الحالي. افتح طلباتك أو تواصل مع الإدارة برقم الطلب: '+order,message_id)
                return
            if action!='home':raise ValueError('Unknown partner sheet action')
            if not context['is_partner']:raise ValueError('Active partner required')
            if context.get('partner_sheet_enabled',True) is False:
                self._commerce_notice(actor,'فحص بسعر الشريك متوقف','أوقفت الإدارة هذه الصلاحية لحسابك. الطلبات الجديدة تُحسب بالسعر العادي. راجع الإدارة لإعادة تفعيلها.',message_id);return
            price=context.get('price')
            text='<b>فحص الشركاء بسعر التكلفة</b>\n\n'
            text+=('السعر الحالي للملف كاملاً: <b>'+egp(price['amount_minor'])+' جنيه</b>\nنسخة السعر: '+str(price['version'])
                   if price else 'لم تحدد الإدارة سعر الملف بعد.')
            text+='\n\nأرسل الملف وحدد الأعمدة ثم اطلب مراجعة الرسوم. كل ملف يحتاج موافقة الإدارة أولاً، ثم موافقتك على الدفع. السعر يثبت عند اعتماد الملف ولا توجد عمولة إحالة على طلبك. تُرد حصة الفحوص غير المنجزة وفق سياسة الاسترداد المعروضة.'
            with self._ui_lock:
                drafts=[(k,v.get('sheet_name','الملف')) for k,v in self._sheet_drafts.items()
                        if v.get('chat')==actor and v.get('commerce_order_id')]
            rows=[[views.button('إرسال ملف للفحص','upload')]]
            rows.extend([[views.button('مراجعة '+str(name)[:40],'co:review:'+token)] for token,name in drafts[-8:]])
            rows.append([views.button('الرئيسية','home')])
            self._show_panel(actor,views.Panel(text,rows),message_id)
        except (CommerceUnavailable,OSError,ValueError,KeyError,TypeError):
            self._commerce_notice(actor,'تعذر عرض فحص الشريك','راجع حالة الشراكة والطلب مع الإدارة ثم أعد المحاولة.',message_id)

    @_serial_user
    def _partner_sheet_admin(self,command,actor,message_id=None,callback=None):
        if actor not in admin_ids(self):
            self._commerce_notice(actor,'غير مصرح','اعتماد الملف وسعر الشركاء خاصان بالإدارة.',message_id);return
        try:
            _,action,value=command.split(':')
            if action in {'access','on','off'}:
                partner=str(UUID(value))
                if action=='access':
                    record=self._commerce().rpc('partner_sheet_access',{'p_actor_telegram_id':actor,'p_partner_id':partner})
                else:
                    reference=(callback or {}).get('id')
                    if not isinstance(reference,str) or not reference.strip():raise ValueError('Authenticated access decision required')
                    record=self._commerce().rpc('configure_partner_sheet_access',{'p_actor_telegram_id':actor,'p_partner_id':partner,
                        'p_enabled':action=='on','p_reference':'telegram-sheet-access:'+str(actor)+':'+reference})
                    target=record['telegram_user_id']
                    remember=getattr(self,'_remember_partner_sheet_access',None)
                    if remember:remember(target,record['enabled'])
                    if record.get('state')=='active' and target not in admin_ids(self) and hasattr(self,'_set_partner_menu'):
                        try:self._set_partner_menu(target)
                        except Exception:logger.warning('Partner sheet command menu refresh deferred; server permission is already saved.')
                text='<b>صلاحية فحص الملفات بسعر الشريك</b>\n\nالاسم: <b>'+views.safe(self._telegram_person_name(record['telegram_user_id']))+'</b>'
                text+='\nرمز الشريك: <code>'+views.safe(record.get('code'))+'</code>\nرقم حساب تيليجرام: <code>'+str(record['telegram_user_id'])+'</code>\nالحالة: '+('مسموح' if record['enabled'] else 'مخفي ومتوقف')
                text+='\n\nإيقافها يخفي زر فحص الشركاء ويمنع استخدام السعر الخاص في الطلبات الجديدة. تبقى صلاحيات الإحالة والأرباح، وتُحفظ الطلبات المدفوعة والفواتير الصادرة.'
                rows=[[views.button('إخفاء وتعطيل فحص بسعر الشريك' if record['enabled'] else 'إظهار وتفعيل فحص بسعر الشريك',
                    'ps:'+('off' if record['enabled'] else 'on')+':'+partner,'danger' if record['enabled'] else 'success')],
                    [views.button('الشركاء','cp:list:0')],[views.button('سعر ملف الشريك','ps:settings:0')]]
                self._show_panel(actor,views.Panel(text,rows),message_id);return
            if action in {'settings','edit'}:
                context=self._commerce().rpc('partner_sheet_context',{'p_telegram_user_id':actor});price=context.get('price')
                if action=='edit':
                    with self._ui_lock:self._waiting_user_inputs[actor]={'action':'partner_sheet_price','expires':time.monotonic()+600,'message_id':message_id}
                    self._commerce_notice(actor,'سعر الملف للشريك','أرسل المبلغ الثابت للملف كله بالجنيه. سيظهر لك تأكيد قبل الحفظ وإشعار الشركاء.',message_id);return
                status=self._commerce().rpc('partner_sheet_notification_status',{'p_actor_telegram_id':actor})
                text='<b>تسعير ملفات الشركاء</b>\n\n'
                text+=('السعر للملف كاملًا: <b>'+egp(price['amount_minor'])+' جنيه</b>\nنسخة التسعير: '+str(price['version']) if price else 'لم يُحدد سعر بعد. يلزم تحديد السعر قبل اعتماد ملف.')
                text+='\n\n<b>نطاق التطبيق</b>\nكل ملف يحتاج موافقة الإدارة. تحتفظ الطلبات المعتمدة بسعرها، وتُراجع الطلبات المنتظرة بالسعر المعتمد عند الموافقة. إيراد هذه الفحوص للمنصة دون عمولة شريك.'
                text+='\n\n<b>إشعارات تحديث السعر</b>\nتم الإرسال: '+str(status.get('sent',0))+'\nبانتظار الإرسال: '+str(status.get('pending',0))+'\nجارٍ الإرسال: '+str(status.get('sending',0))+'\nنتيجة الإرسال غير مؤكدة: '+str(status.get('uncertain',0))+'\nرفض تيليجرام الإرسال: '+str(status.get('rejected',0))
                rows=[[views.button('تعديل السعر وإشعار الشركاء','ps:edit:0')],
                      [views.button('صلاحيات الشركاء','cp:list:0'),views.button('ملفات للموافقة','ps:list:0')],
                      [views.button('حالة الإشعارات','ps:settings:0'),views.button('الرئيسية','home')]]
                self._show_panel(actor,views.Panel(text,rows),message_id);return
            if action=='save':
                review=getattr(self,'_partner_price_reviews',{}).get(value)
                reference=(callback or {}).get('id')
                if not review or review['actor']!=actor or review['expires']<=time.monotonic() or not isinstance(reference,str) or not reference.strip():
                    raise ValueError('Authenticated reviewed price required')
                # Stable reference survives an ambiguous response and a second click.
                result=self._commerce().rpc('configure_partner_sheet_price',{'p_actor_telegram_id':actor,
                    'p_amount_minor':review['amount_minor'],'p_reference':'telegram-sheet-price:'+value})
                self._commerce_notice(actor,'حُفظ سعر الملف',egp(result['amount_minor'])+' جنيه للملف كاملاً. أُدرجت إشعارات '+str(result['recipients'])+' شريك في سجل الإرسال. راجع الإعدادات لمعرفة الوصول.',message_id)
                return
            if action=='list':
                offset=max(0,int(value))
                page=self._commerce().rpc('partner_sheet_requests',{'p_actor_telegram_id':actor,'p_offset':offset})
                rows=[[views.button(self._telegram_person_name(item['telegram_user_id'])[:35]+' · '+str(item['source'].get('original_filename','الملف'))[:35],
                                    'ps:review:'+item['order_id'])] for item in page['items']]
                if offset:rows.append([views.button('السابق','ps:list:'+str(max(0,offset-10)))])
                if len(page['items'])==10:rows.append([views.button('التالي','ps:list:'+str(offset+10))])
                rows.extend([[views.button('تحديث','ps:list:'+str(offset))],[views.button('سعر الملف والإشعارات','ps:settings:0')],[views.button('الرئيسية','home')]])
                self._show_panel(actor,views.Panel('<b>ملفات الشركاء بانتظار موافقتي</b>\n\n'+
                    ('اختر الملف لمراجعة تفاصيله واعتماد سعره.' if page['items'] else 'لا توجد ملفات معلقة في هذه الصفحة.'),rows),message_id);return
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
                        message=self._archive_transport().api('sendDocument',{'chat_id':actor,'caption':'الملف المرسل للموافقة\nرقم الطلب: '+order+'\nSHA-256: '+digest},
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
                text=('<b>مراجعة فحص الشريك</b>\n\nالشريك: <b>'+views.safe(self._telegram_person_name(request['telegram_user_id']))+'</b>\nرقم حساب تيليجرام: <code>'+str(request['telegram_user_id'])+'</code>\nالملف: '+
                    views.safe(source.get('original_filename','الملف'))+'\nالورقة رقم: '+str(int(source.get('sheet_index',0))+1)+
                    '\nالفحوص: '+str(request['requested_checks'])+'\nالطلب: <code>'+order+'</code>\nبصمة الملف: <code>'+views.safe(source['file_sha256'])+'</code>')
                rows=[[views.button('تحميل الملف الأصلي للمراجعة','ps:file:'+order)]]
                if request['state']=='pending' and request['order_state']=='draft':
                    text+='\n\n'+('السعر المعروض للملف كله: <b>'+egp(price['amount_minor'])+' جنيه</b>' if price else 'حدد سعر الملف من الإعدادات أولاً.')
                    if not hasattr(self,'_partner_sheet_reviews'):self._partner_sheet_reviews={}
                    self._partner_sheet_reviews={k:v for k,v in self._partner_sheet_reviews.items() if v['expires']>time.monotonic()}
                    self._partner_sheet_reviews[(actor,order)]={'version':price['version'] if price else None,'expires':time.monotonic()+600}
                    if price:rows.append([views.button('أوافق على الملف بهذا السعر','ps:approve:'+order,'success')])
                    rows.append([views.button('رفض هذا الملف','ps:reject:'+order)])
                else:
                    text+='\nالحالة: '+{'approved':'معتمد','rejected':'مرفوض'}.get(request['state'],'ملغى')
                    if request.get('amount_minor') is not None:text+='\nالسعر المحفوظ: '+egp(request['amount_minor'])+' جنيه'
                rows.extend([[views.button('الملفات المنتظرة','ps:list:0')],[views.button('إعداد سعر الملف','ps:settings:0')]])
                self._show_panel(actor,views.Panel(text,rows),message_id);return
            raise ValueError('Unknown sheet administration action')
        except (CommerceUnavailable,OSError,ValueError,KeyError,TypeError,ArchiveRejected,ArchiveAmbiguous):
            self._commerce_notice(actor,'تعذر التأكد من إجراء الملف','أعد فتح مراجعة الملف أو الإعدادات للتحقق. إذا تغير السعر، راجعه من جديد قبل الموافقة.',message_id)

    def _commerce_text_input(self,actor,text,waiting):
        if waiting.get('action')!='partner_sheet_price':return super()._commerce_text_input(actor,text,waiting)
        try:
            if actor not in admin_ids(self) or waiting.get('expires',0)<=time.monotonic():raise ValueError('Valid admin input required')
            amount=money_minor_input(text.strip())
            if amount>100000000:raise ValueError('Sheet price exceeds supported range')
            token=secrets.token_hex(6)
            self._partner_price_reviews={k:v for k,v in getattr(self,'_partner_price_reviews',{}).items() if v['expires']>time.monotonic()}
            self._partner_price_reviews[token]={'actor':actor,'amount_minor':amount,'expires':time.monotonic()+600}
            self._show_panel(actor,views.Panel('<b>تأكيد سعر فحص الشركاء</b>\n\n'+egp(amount)+' جنيه للملف كاملاً.\nسيُرسل إشعار بالسعر إلى الشركاء النشطين. الطلبات المعتمدة تبقى بسعرها المحفوظ.',
                [[views.button('حفظ السعر وإرسال الإشعارات','ps:save:'+token,'success')],[views.button('إلغاء','ps:settings:0')]]),waiting.get('message_id'))
        except (ValueError,TypeError,InvalidOperation):
            if actor in admin_ids(self) and waiting.get('expires',0)>time.monotonic():
                with self._ui_lock:self._waiting_user_inputs[actor]=waiting
            self._commerce_notice(actor,'راجع مبلغ الملف','أدخل مبلغاً موجباً بالجنيه للملف كله.',waiting.get('message_id'))
        return True

    def _partner_sheet_delivery_once(self):
        record=self._commerce().rpc('claim_partner_sheet_notification')
        if not record:return False
        payload=record['payload'];kind=payload['kind'];rows=[]
        if kind=='price':
            context=self._commerce().rpc('partner_sheet_context',{'p_telegram_user_id':record['recipient_telegram_id']})
            if context.get('partner_sheet_enabled',True) is False:
                self._commerce().rpc('complete_partner_sheet_notification',{'p_id':record['id'],'p_token':record['token'],
                    'p_state':'rejected','p_message_id':None})
                return True
        if kind in {'campaign_request','campaign_decision'}:
            text,rows=self._partner_campaign_notice(record)
        elif kind=='price':
            text='<b>تحديث سعر فحص الشركاء</b>\n\nالسعر الأساسي الجديد: <b>'+egp(payload['amount_minor'])+' جنيه للملف كاملاً</b>.\nنسخة السعر: '+str(payload['version'])+'\nكل ملف يحتاج موافقة الإدارة. الطلبات المعتمدة تحتفظ بالسعر الذي اعتمدت به.'
            rows=[[views.button('السعر وفحص الشركاء','pc:home:0')]]
        elif kind=='request':
            text='<b>ملف شريك ينتظر موافقتك</b>\n\nالشريك: <code>'+str(payload['partner_telegram_id'])+'</code>\nالطلب: <code>'+payload['order_id']+'</code>'
            rows=[[views.button('مراجعة الملف واعتماد السعر','ps:review:'+payload['order_id'])]]
        else:
            text='<b>قرار مراجعة الملف</b>\n\n'+('وافقت الإدارة على الملف بسعر ثابت '+egp(payload['amount_minor'])+' جنيه. افتح الطلب للموافقة على الدفع.' if payload['state']=='approved' else 'رفضت الإدارة طلب فحص هذا الملف.')+'\nالطلب: <code>'+payload['order_id']+'</code>'
            rows=[[views.button('فتح طلب الملف','pc:open:'+payload['order_id'])]]
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
