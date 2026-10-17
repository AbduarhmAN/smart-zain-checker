"""Reviewed funded cash-credit codes and one customer code input.

An admin approval is an explicit receipt attestation, not provider verification.
"""
from datetime import datetime,timedelta,timezone
from decimal import Decimal,InvalidOperation,localcontext
import re
import logging
import secrets
import sqlite3
import time
from uuid import UUID,uuid4
from telegram_bot import views
from .bot_workflow import _serial_user,REFERRAL_MESSAGES
from .checkout import egp
from .client import CommerceUnavailable
from .finance import positive_minor,positive_money,money_minor_input
from .referrals import normalize_referral_code

logger=logging.getLogger('CommerceCashCodes')

FIELDS=[('code','الكود الذي ستسلمه للعميل (أحرف إنجليزية وأرقام، 3–48 حرفًا)'),
    ('credit','قيمة الرصيد لكل عميل بالجنيه المصري'),('count','عدد الحسابات التي يمكنها استخدام الكود، وليس عدد صفوف الإكسل. كل حساب يستخدمه مرة واحدة'),
    ('receipt','مرجع إيصال النقد. لا يستخدم هذا الإيصال في شحن مباشر آخر'),
    ('owner','رقم Telegram لعميل محدد، أو 0 لكود يمكن تسليمه لأي عميل. تخصيصه لعميل يتطلب استخدامًا واحدًا'),
    ('days','مدة صلاحية الكود بالأيام، أو 0 بلا انتهاء. المال غير المستبدل يظل مسجلًا حتى لو انتهى الكود')]


def cash_funding_total(credit,count):
    with localcontext() as context:
        context.prec=100
        return positive_money(Decimal(credit)*count)


class CashCreditMixin:
    def _commerce_cancel_pending(self,user_id):
        with self._ui_lock:
            reviews=getattr(self,'_cash_code_reviews',{})
            self._cash_code_reviews={key:value for key,value in reviews.items() if value['actor']!=user_id}
        return super()._commerce_cancel_pending(user_id)

    def _commerce_command(self,text,user_id,is_admin,username='',*,message_id=None):
        parts=text.split();command=parts[0].split('@')[0].lower() if parts else ''
        if command in {'/cash_codes','/cash_code'}:
            self._cash_code_callback('cg:'+('new' if command=='/cash_code' else 'list')+':0',user_id,message_id,{});return True
        if command=='/code':
            if len(parts)==2 and type(message_id) is int and message_id>0:
                self._apply_customer_code(user_id,parts[1],message_id,'telegram-code:'+str(user_id)+':'+str(message_id))
            else:self._customer_code_prompt(user_id,message_id)
            return True
        return super()._commerce_command(text,user_id,is_admin,username,message_id=message_id)

    def _customer_code_prompt(self,user_id,message_id=None):
        with self._ui_lock:
            self._waiting_user_inputs[user_id]={'action':'commerce_customer_code','expires':time.monotonic()+1800,'message_id':message_id}
        body='أرسل كود الرصيد أو العرض وحده؛ لا يتكرر الرصيد عند إعادة الرسالة.'
        if self._legacy_balance_pending_minor(user_id):
            body+='\nرصيدك السابق محفوظ منفصلًا إلى حين مراجعته ونقله. يُضاف رصيد الكود إلى المحفظة الجديدة ويُستخدم في الطلبات الجديدة.'
        self._commerce_notice(user_id,'أدخل الكود',body,message_id)

    @_serial_user
    def _apply_customer_code(self,user_id,code,message_id,reference):
        try:
            positive_minor(user_id)
            normalized=normalize_referral_code(code)
            result=self._commerce().rpc('apply_customer_code',{'p_telegram_user_id':user_id,'p_code':normalized,'p_reference':reference})
            kind,state=result.get('kind'),result.get('state',result.get('status'))
            messages={'redeemed':'أُضيف رصيد مدفوع إلى محفظتك.','already_redeemed':'استُخدم هذا الكود لحسابك بالفعل؛ لم نضف الرصيد مرتين.',
                'invalid_code':'الكود غير صالح.','unowned_code':'هذا الكود مخصص لحساب آخر.','inactive_code':'الكود متوقف أو انتهت صلاحيته.',
                'code_exhausted':'بلغ الكود حد الاستخدام.','activated':'تم تفعيل العرض.','already_activated':'العرض مفعّل بالفعل؛ لم تتجدد مدته أو رصيده.',
                'inactive_offer':'العرض غير متاح.','ineligible_customer':'الحساب غير مؤهل للعرض.','budget_exhausted':'نفدت ميزانية العرض.',
                'user_suspended':'حسابك موقوف؛ تواصل مع الإدارة.'}
            body=('تم قبول الكود.' if state in {'attributed','already_attributed','first_source_preserved'} else
                  'هذا الكود غير متاح لحسابك.') if kind=='referral' else messages.get(state,'راجع حالة الكود.')
            if kind=='cash_credit' and state in {'redeemed','already_redeemed'}:
                self._topups().mark_owner(user_id)
                body+='\nالقيمة: '+egp(result['credit_minor'])+' جنيه. الشحن لا ينشئ خدمة مدفوعة أو عمولة.'
            self._show_panel(user_id,views.Panel('<b>نتيجة الكود</b>\n\n'+views.safe(body),
                [[views.button('المحفظة','wallet')],[views.button('عروضي وحدودها','of:list:0')]]),message_id)
        except (CommerceUnavailable,OSError,ValueError,KeyError,TypeError,sqlite3.Error):self._wallet_failure(user_id,message_id)

    def _cash_step(self,user_id,token,review,message_id):
        field,label=FIELDS[review['step']]
        with self._ui_lock:
            self._waiting_user_inputs[user_id]={'action':'commerce_cash_code_value','token':token,'field':field,
                'expires':time.monotonic()+1800,'message_id':message_id}
        rows=[]
        if field=='code':rows.append([views.button('توليد كود تلقائي','cg:generate:'+token)])
        if field=='receipt':
            values=review['values']
            label=('هذا كود رصيد مدفوع، وليس هدية ترويجية.\nالقيمة لكل حساب: '+egp(values['credit'])+' جنيه\nعدد الحسابات: '+str(values['count'])+
                '\nالإجمالي المحسوب: '+egp(values['funded'])+' جنيه\n\n'
                'أرسل مرجع إيصال الاستلام فقط؛ لا تحتاج إعادة كتابة الإجمالي. ستؤكد استلامه مرة واحدة قبل إصدار الكود. '
                'لا يستخدم هذا الإيصال في شحن مباشر آخر. للأكواد المجانية استخدم حملة رصيد ترويجي.')
        rows.append([views.button('إلغاء','home')])
        self._show_panel(user_id,views.Panel('<b>إنشاء كود رصيد نقدي</b>\n\n'+label,rows),message_id)

    def _cash_save_failure(self,user_id,message_id,token,exc):
        logger.warning('Cash code save failed: kind=%s http=%s database=%s reason=%s',
            type(exc).__name__,getattr(exc,'http_status',None),getattr(exc,'database_code',None),getattr(exc,'reason',None))
        reason=getattr(exc,'reason',None)
        body=('رفضت قاعدة البيانات الحفظ بسبب صلاحيات الخدمة. لم يُعتمد الكود بهذه المحاولة. '
            'احتفظ بنفس الكود والإيصال؛ يلزم إصلاح صلاحيات النظام قبل إعادة التأكيد.'
            if reason=='permission_denied' else
            'الكود أو مرجع الإيصال مستخدم بالفعل، أو انتهت صلاحية الكود. راجع البيانات قبل المحاولة.'
            if reason in {'cash_conflict','duplicate_record'} else
            'راجع قيمة الرصيد وعدد الاستخدامات: النقد المستلم يجب أن يساوي الإجمالي.' if reason=='cash_invalid' else
            'لم نتأكد من حفظ الكود. افتح قائمة الأكواد أولًا؛ لا تدخل إيصالًا جديدًا لمعالجة مشكلة الاتصال.')
        rows=[[views.button('عرض الأكواد المحفوظة','cg:list:0')]]
        review=getattr(self,'_cash_code_reviews',{}).get(token)
        if review and review['actor']==user_id and review['expires']>time.monotonic() and 'arguments' in review:
            rows.append([views.button('إعادة التأكيد بنفس الإيصال','cg:save:'+token)])
        self._show_panel(user_id,views.Panel('<b>لم يكتمل تأكيد الكود</b>\n\n'+body,rows),message_id)

    @_serial_user
    def _cash_code_callback(self,command,user_id,message_id,callback):
        action=None;token=None
        try:
            if user_id not in self.current_authorized_ids:raise ValueError('Admin required')
            parts=command.split(':')
            if len(parts)!=3 or parts[0]!='cg':raise ValueError('Cash code callback required')
            action,token=parts[1:]
            if action=='list':
                records=self._commerce().rpc('list_cash_credit_codes',{'p_offset':int(token)})
                text='<b>أكواد الرصيد النقدي</b>\n\n'
                for record in records:
                    text+=f"<code>{views.safe(record['code'])}</code> · {egp(record['credit_minor'])} جنيه لكل عميل\n"
                    text+=f"الاستخدام: {record['redemptions']} / {record['max_redemptions']} · مبلغ غير مستبدل: {egp(record['cash_funded_minor']-record['cash_redeemed_minor'])} جنيه\n"
                    text+='العميل: '+str(record.get('bound_telegram_user_id') or 'يمكن تسليمه لأي حساب')+'\n'
                    text+='ينتهي: '+views.safe(record.get('expires_at') or 'بلا انتهاء')+'\nإيصال: <code>'+views.safe(record['receipt_reference'],128)+'</code>\n\n'
                if not records:text+='لا توجد أكواد نقدية.\n'
                rows=[[views.button('إنشاء كود بعد استلام النقد','cg:new:0')]]
                if int(token)>0:rows.append([views.button('الأحدث','cg:list:'+str(max(0,int(token)-5)))])
                if len(records)==5:rows.append([views.button('الأقدم','cg:list:'+str(int(token)+5))])
                rows.append([views.button('‹ الرئيسية','home')]);self._show_panel(user_id,views.Panel(text,rows),message_id);return
            if action=='new':
                token=str(uuid4())
                with self._ui_lock:
                    reviews=getattr(self,'_cash_code_reviews',{})
                    self._cash_code_reviews={key:value for key,value in reviews.items() if value['expires']>time.monotonic()}
                    if len(self._cash_code_reviews)>=128:raise ValueError('Review capacity reached')
                    review={'actor':user_id,'step':0,'values':{},'expires':time.monotonic()+1800}
                    self._cash_code_reviews[token]=review
                self._cash_step(user_id,token,review,message_id);return
            if action=='generate':
                review=getattr(self,'_cash_code_reviews',{}).get(str(UUID(token)))
                field='code'
                if not review or review['actor']!=user_id or review['expires']<=time.monotonic() or FIELDS[review['step']][0]!=field:
                    raise ValueError('Matching active input required')
                raw='CASH_'+secrets.token_hex(12).upper()
                with self._ui_lock:self._waiting_user_inputs.pop(user_id,None)
                self._commerce_text_input(user_id,raw,{'action':'commerce_cash_code_value','token':token,'field':field,
                    'expires':review['expires'],'message_id':message_id})
                return
            if action=='funded':
                self._show_panel(user_id,views.Panel('<b>مراجعة كود قديمة</b>\n\n'
                    'أعد فتح إنشاء الكود؛ الإجمالي يُحسب تلقائيًا وتؤكد استلامه مرة واحدة عند الإصدار. لم يُصدر كود من هذا الزر.',
                    [[views.button('إنشاء كود','cg:new:0')],[views.button('الأكواد المحفوظة','cg:list:0')]]),message_id)
                return
            if action!='save':raise ValueError('Unknown cash action')
            review=getattr(self,'_cash_code_reviews',{}).get(str(UUID(token)))
            if not review or review['actor']!=user_id or review['expires']<=time.monotonic() or 'arguments' not in review:
                raise ValueError('Matching active cash review required')
            if not isinstance(callback.get('id'),str) or not callback['id'].strip():raise ValueError('Approval receipt required')
            if 'approval' not in review:review['approval']='telegram-cash-code:'+str(user_id)+':'+callback['id']
            args=dict(review['arguments'],p_actor_telegram_id=user_id,p_approval_reference=review['approval'])
            result=self._commerce().rpc('issue_cash_credit_code',args)
            self._show_panel(user_id,views.Panel('<b>حُفظ كود الرصيد المدفوع</b>\n\n<code>'+views.safe(result['code'])+'</code>\n'
                'يستبدله العميل عبر زر «إدخال كود» في المحفظة. الإيصال محفوظ مرة واحدة، والشحن لا ينتج عمولة.',
                [[views.button('أكواد الرصيد','cg:list:0')],[views.button('‹ الرئيسية','home')]]),message_id)
        except (CommerceUnavailable,OSError,ValueError,KeyError,TypeError,sqlite3.Error,IndexError) as exc:
            if action=='save':self._cash_save_failure(user_id,message_id,token,exc)
            else:self._wallet_failure(user_id,message_id)

    def _commerce_text_input(self,user_id,text,waiting):
        action=waiting.get('action')
        if action=='redeem':
            # Old keyboards and in-flight prompts use the same owned new RPC.
            waiting=dict(waiting,action='commerce_customer_code',expires=waiting.get('expires',time.monotonic()+1800))
            action='commerce_customer_code'
        if action not in {'commerce_customer_code','commerce_cash_code_value'}:
            return super()._commerce_text_input(user_id,text,waiting)
        try:
            if waiting.get('expires',0)<=time.monotonic():raise ValueError('Code prompt expired')
            if action=='commerce_customer_code':
                incoming=positive_minor(waiting.get('_telegram_input_message_id'))
                self._apply_customer_code(user_id,text,waiting.get('message_id'),'telegram-code:'+str(user_id)+':'+str(incoming));return True
            review=getattr(self,'_cash_code_reviews',{}).get(waiting['token'])
            if user_id not in self.current_authorized_ids or not review or review['actor']!=user_id or review['expires']<=time.monotonic():
                raise ValueError('Authenticated matching reviewer required')
            field=FIELDS[review['step']][0]
            if field!=waiting['field']:raise ValueError('Stale cash code input')
            raw=text.strip()
            if field=='code':value=normalize_referral_code(raw)
            elif field=='credit':value=money_minor_input(raw)
            elif field=='receipt':
                if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}',raw):raise ValueError('Invalid cash receipt')
                value=raw.upper()
            else:
                if field=='count':
                    exact=Decimal(raw)
                    if not exact.is_finite() or exact<=0 or exact>9223372036854775807 or exact!=exact.to_integral_value():
                        raise ValueError('Positive whole account count required')
                    value=int(exact)
                else:
                    if not raw.isdecimal():raise ValueError('Whole number required')
                    value=int(raw)
                    if value>9223372036854775807:raise ValueError('Cash limit is invalid')
            values=dict(review['values']);values[field]=value
            if field=='count':values['funded']=cash_funding_total(values['credit'],value)
            if field=='owner' and value and values['count']!=1:raise ValueError('Bound code permits one redemption')
            if field=='days':
                review['expires_at']=(datetime.now(timezone.utc)+timedelta(days=value)).isoformat() if value else None
            review['values']=values
            review['step']+=1
            if review['step']<len(FIELDS):self._cash_step(user_id,waiting['token'],review,waiting.get('message_id'));return True
            expiry=review['expires_at']
            values['funded']=cash_funding_total(values['credit'],values['count'])
            review['arguments']={'p_code':values['code'],'p_credit_minor':values['credit'],'p_max_redemptions':values['count'],
                'p_funded_minor':values['funded'],'p_receipt_reference':values['receipt'],
                'p_bound_telegram_id':values['owner'] or None,'p_expires_at':expiry}
            review['expires']=time.monotonic()+180
            text=f"<b>راجع كود الرصيد النقدي</b>\n\nالكود: <code>{values['code']}</code>\nالقيمة لكل عميل: {egp(values['credit'])} جنيه\nعدد العملاء: {values['count']}\n"
            text+=f"إجمالي النقد المحسوب: {egp(values['funded'])} جنيه\nالإيصال: <code>{values['receipt']}</code>\nالعميل: {values['owner'] or 'يمكن تسليمه لأي حساب'}\n"
            text+='الصلاحية: '+(str(values['days'])+' يوم' if values['days'] else 'بلا انتهاء')+'\n\nتؤكد الموافقة استلام المبلغ فعليًا. لا يستخدم الإيصال نفسه في شحن آخر.'
            self._show_panel(user_id,views.Panel(text,[[views.button('استلمت '+egp(values['funded'])+' جنيه، إصدار الكود','cg:save:'+waiting['token'],'success')],
                [views.button('إلغاء','home')]]),waiting.get('message_id'))
        except (OSError,ValueError,KeyError,TypeError,OverflowError,InvalidOperation):
            if action=='commerce_cash_code_value':
                with self._ui_lock:self._waiting_user_inputs[user_id]=waiting
            errors={'code':'أدخل كودًا من 3 إلى 48 حرفًا إنجليزيًا أو رقمًا.',
                'credit':'أدخل قيمة موجبة بالجنيه، مثل 10 أو 0.00001. لا تُقرّب الكسور.',
                'count':'أدخل عدد الحسابات كعدد صحيح موجب مثل 10. الإجمالي يُحسب تلقائيًا؛ لا ترسل المبلغ في هذه الخطوة.',
                'receipt':'أرسل مرجع الإيصال فقط: من 3 إلى 128 حرفًا أو رقمًا، ويُسمح بـ _ . : - . لا تحتاج إعادة إدخال الإجمالي.',
                'owner':'أدخل رقم Telegram أو 0 لأي حساب. تخصيص الكود لحساب واحد يتطلب أن يكون عدد الاستخدامات 1.',
                'days':'أدخل مدة صالحة كعدد أيام، أو 0 دون انتهاء.'}
            self._commerce_notice(user_id,'راجع هذا الحقل',errors.get(waiting.get('field'),'انتهت المراجعة أو تغيرت الخطوة. أعد فتح إنشاء الكود.'),waiting.get('message_id'))
        return True
