"""Reviewed administration limits and bounded platform offers in Telegram."""
import time
import logging
import secrets
from decimal import Decimal,InvalidOperation
from uuid import UUID
from telegram_bot import views
from .bot_workflow import _serial_user
from .checkout import egp
from .client import CommerceUnavailable
from .finance import positive_minor
from .legacy_wallets import minor
from .referrals import normalize_referral_code

POLICY_FIELDS=[
    ('max_percent_basis_points','أقصى نسبة خصم مسموحة (%)','percent'),
    ('max_checks_per_user','أقصى عدد فحوص مجانية/مخصومة لكل عميل','int'),
    ('max_discount_minor_per_user','أقصى مبلغ خصم للعميل الواحد (بالجنيه)','money'),
    ('max_total_discount_minor','أقصى ميزانية إجمالية للعرض لجميع العملاء (بالجنيه)','money'),
    ('max_duration_seconds','أقصى مدة لصلاحية العرض لكل عميل (بالأيام)','days'),
    ('max_wallet_credit_minor','أقصى رصيد ترويجي يُمنح لمحفظة العميل (بالجنيه)','money'),
    ('max_redemptions','أقصى عدد عملاء مسموح لهم بتفعيل الكود','int')
]
OFFER_FIELDS=[
    ('benefit_kind','نوع العرض والخصم','kind'),
    ('benefit_value','قيمة الخصم أو المنفعة','value'),
    ('eligibility','العملاء المؤهلون للاستفادة','eligibility'),
    ('check_types','أنواع الأرقام المشمولة بالعرض','types'),
    ('max_checks','عدد الفحوص المجانية الممنوحة لكل عميل','int'),
    ('max_discount_minor','أقصى خصم يستفيده العميل الواحد (بالجنيه)','money'),
    ('max_total_discount_minor','الميزانية الإجمالية لكود العرض (بالجنيه)','money'),
    ('duration_seconds','مدة صلاحية العرض للعميل بعد التفعيل (بالأيام)','days'),
    ('max_redemptions','العدد الأقصى للعملاء المستفيدين من الكود','int'),
    ('code','اسم كود العرض (الرمز الترويجي)','code')
]
KINDS={
    'free_checks':'🎁 فحوص مجانية (فحص عدد محدد من الأرقام مجاناً)',
    'percent':'🏷 نسبة خصم مئوية (مثلاً 50% أو 100%)',
    'fixed':'💵 خصم مبلغ ثابت من كل رقم',
    'wallet_credit':'💼 رصيد هدية في المحفظة'
}
ELIGIBILITY={
    'any':'👥 كل العملاء (الحاليون والجدد)',
    'new':'🆕 العملاء الجدد فقط (قبل أول فحص مدفوع)',
    'existing':'⭐ العملاء الحاليون فقط (من لديهم فحص سابق)'
}
TYPES={'account':['account'],'service':['service'],'both':['account','service']}
TYPE_LABELS={
    'both':'✅ الحسابات والخدمات معاً (شامل كل الأرقام)',
    'account':'🏢 أرقام الحسابات فقط',
    'service':'📱 أرقام الخدمات فقط'
}

FIELD_EXPLANATIONS = {
    'max_percent_basis_points': (
        'أقصى نسبة خصم مئوية مسموح بها في أي عرض بالإدارة.\n\n'
        '🎯 الهدف: وضع سقف لنسبة التخفيض، بحيث لا يمكن إنشاء أي عرض يتجاوز هذه النسبة.\n'
        '📝 المطلوب: أرسل رقماً صحيحاً من 1 إلى 100.\n'
        '💡 مثال: اكتب 100 للسماح بعروض مجانية بالكامل (خصم 100%)، أو 50 لخصم نصف السعر فقط.'
    ),
    'max_checks_per_user': (
        'أقصى عدد فحوص (مرات فحص) مشمولة بالعرض للعميل الواحد.\n\n'
        '🎯 الهدف: تحديد عدد مرات الاستفادة المسموحة لكل عميل.\n'
        '📌 في عروض الفحص المجاني: فحصة واحدة (1) تعني فحص ملف كامل مجاناً حتى 3400 عميل (يشمل أول 500 رقم خدمة مجاناً وسقف 1500 رقم خدمة للملف).\n'
        '📝 المطلوب: أرسل رقماً صحيحاً يمثل عدد مرات الفحص للعميل.\n'
        '💡 مثال: اكتب 1 إذا أردت السماح للعميل بفحص ملف مجاني واحد فقط، أو 3 لثلاثة ملفات.'
    ),
    'max_discount_minor_per_user': (
        'أقصى توفير مالي للعميل الواحد بالجنيه (سقف الخصم للعميل).\n\n'
        '🎯 الهدف: وضع سقف مالي بالجنيه لا يمكن لأي عميل توفير أكثر منه في هذا العرض.\n'
        '📝 المطلوب: أرسل المبلغ بالجنيه (مثلاً 50 أو 100).\n'
        '💡 مثال: اكتب 50 ليكون سقف التوفير 50 جنيهاً كحد أقصى لكل عميل.'
    ),
    'max_total_discount_minor': (
        'الميزانية الإجمالية القصوى المخصصة للعرض لجميع العملاء بالجنيه.\n\n'
        '🎯 الهدف: السقف المالي الكلي الذي تتكفل به المنصة لكامل العرض، ويتوقف العرض تلقائياً عند استهلاكه بالكامل.\n'
        '📝 المطلوب: أرسل إجمالي الميزانية بالجنيه.\n'
        '💡 مثال: اكتب 3000 لتخصيص ميزانية 3000 جنيه للعرض.'
    ),
    'max_duration_seconds': (
        'أقصى مدة صلاحية مسموحة للعرض بالأيام.\n\n'
        '🎯 الهدف: وضع سقف زمني لأيام صلاحية العروض لضمان عدم إنشاء عروض تمتد لفترات أطول من المخطط.\n'
        '📝 المطلوب: أرسل عدد الأيام كعدد صحيح.\n'
        '💡 مثال: اكتب 30 للسماح بعروض تستمر حتى 30 يوماً كحد أقصى.'
    ),
    'max_wallet_credit_minor': (
        'أقصى رصيد ترويجي مجاني يُمنح لمحفظة العميل بالجنيه.\n\n'
        '🎯 الهدف: وضع سقف للمبالغ التي يمكن إضافتها كرصيد هدية مباشر في محفظة العميل الواحد.\n'
        '📝 المطلوب: أرسل المبلغ بالجنيه.\n'
        '💡 مثال: اكتب 50 للسماح بهدية رصيد أقصاها 50 جنيهاً.'
    ),
    'max_redemptions': (
        'العدد الأقصى للعملاء المسموح لهم بتفعيل هذا الكود.\n\n'
        '🎯 الهدف: تحديد عدد المستفيدين (مثلاً لأول 100 عميل فقط)، ويتوقف الكود فور اكتمال هذا العدد.\n'
        '📝 المطلوب: أرسل عدد العملاء كعدد صحيح.\n'
        '💡 مثال: اكتب 100 ليكون العرض متاحاً لأول 100 عميل يفعّلون الكود.'
    ),
    'benefit_value': (
        'قيمة الخصم أو المنفعة المقدمة في هذا العرض.\n\n'
        '🎯 الهدف: تحديد مقدار الخصم الذي سيحصل عليه العميل.\n'
        '📝 المطلوب: أرسل النسبة (من 1 إلى 100) إذا كان العرض نسبة مئوية، أو المبلغ بالجنيه إذا كان خصماً ثابتاً أو رصيد محفظة.\n'
        '💡 مثال: اكتب 100 لخصم كامل 100%، أو 10 لخصم 10 جنيهات.'
    ),
    'max_checks': (
        'عدد مرات الفحص (الفحوص) الممنوحة للعميل في هذا العرض.\n\n'
        '🎯 الهدف: تحديد عدد الملفات/الفحوص التي يمكن للعميل فحصها والاستفادة من العرض عليها.\n'
        '📌 في عروض الفحص المجاني: الرقم (1) يعني فحص ملف كامل مجاناً حتى 3400 عميل (يشمل أول 500 رقم خدمة مجاناً وسقف 1500 رقم خدمة للملف).\n'
        '📝 المطلوب: أرسل رقماً صحيحاً يمثل عدد الفحوص.\n'
        '💡 مثال: اكتب 1 ليكون لكل عميل فحص ملف واحد مجاناً.'
    ),
    'max_discount_minor': (
        'أقصى مبلغ خصم يستفيده العميل الواحد بالجنيه في هذا العرض.\n\n'
        '🎯 الهدف: وضع سقف مالي بالجنيه للعميل الواحد في هذا الكود.\n'
        '📌 في عروض الفحص المجاني: حدد سقفاً كافياً لتغطية تكلفة الملف (مثلاً 500 أو 1000 جنيه أو نفس ميزانية العرض الكلية).\n'
        '📝 المطلوب: أرسل المبلغ بالجنيه.\n'
        '💡 مثال: اكتب 1000 ليكون سقف الخصم 1000 جنيه للعميل.'
    ),
    'duration_seconds': (
        'مدة صلاحية العرض لكل عميل بالأيام.\n\n'
        '🎯 الهدف: تحديد عدد الأيام المتاحة للعميل لاستخدام العرض بعد تفعيل الكود.\n'
        '📝 المطلوب: أرسل عدد الأيام كعدد صحيح.\n'
        '💡 مثال: اكتب 7 لتكون صلاحية العرض 7 أيام تبدأ من لحظة إدخال العميل للكود.'
    ),
}
logger=logging.getLogger('CommerceCampaigns')



def campaign_limit_issues(values,policy):
    limits=policy.get('rules',{}) if isinstance(policy,dict) else {}
    pairs=[('max_checks','max_checks_per_user','فحوص العميل (أقصى عدد فحوص)','count'),
        ('max_discount_minor','max_discount_minor_per_user','أقصى خصم للعميل الواحد','money'),
        ('max_total_discount_minor','max_total_discount_minor','ميزانية الحملة الكلية للعرض','money'),
        ('duration_seconds','max_duration_seconds','مدة صلاحية العرض','days'),
        ('max_redemptions','max_redemptions','عدد العملاء المستفيدين','count')]
    if values.get('benefit_kind')=='percent':pairs.append(('benefit_value','max_percent_basis_points','نسبة الخصم','percent'))
    if values.get('benefit_kind')=='wallet_credit':pairs.append(('benefit_value','max_wallet_credit_minor','الرصيد الترويجي','money'))
    def display(value,kind):
        return (egp(value)+' جنيه' if kind=='money' else str(Decimal(value)/100)+'%' if kind=='percent'
            else str(Decimal(value)/86400)+' يوم' if kind=='days' else str(value))
    issues=[]
    for key,limit_key,label,kind in pairs:
        actual,maximum=values.get(key),limits.get(limit_key)
        if type(actual) is int and type(maximum) is int and actual>maximum:
            issues.append(f'{label}: اخترت {display(actual,kind)}؛ الحد الحالي {display(maximum,kind)} (الحد الأقصى المسموح به في الإدارة)')
    if values.get('max_discount_minor',0)>values.get('max_total_discount_minor',0):
        issues.append('حد المنفعة للعميل لا يمكن أن يتجاوز الميزانية الإجمالية لكامل العرض.')
    if values.get('benefit_kind') in {'fixed','wallet_credit'} and values.get('benefit_value',0)>values.get('max_discount_minor',0):
        issues.append('قيمة المنفعة المحددة تتجاوز سقف الخصم المسموح به للعميل الواحد.')
    return issues


def value_input(text,kind):
    if kind=='money':return positive_minor(minor(text.strip()))
    if kind=='percent':
        value=Decimal(text.strip())*100
        if not value.is_finite() or value!=value.to_integral_value() or not 0<value<=10000:raise ValueError('Percentage required')
        return int(value)
    if kind=='code':return normalize_referral_code(text.strip())
    if not text.strip().isascii() or not text.strip().isdigit():raise ValueError('Whole number required')
    value=positive_minor(int(text.strip()))
    return positive_minor(value*86400) if kind=='days' else value


class CampaignsMixin:
    @staticmethod
    def _campaign_fields(review):
        if review['policy']:return POLICY_FIELDS
        return [field for field in OFFER_FIELDS if field[0]!='benefit_kind'] if review.get('partner') else OFFER_FIELDS

    @staticmethod
    def _campaign_prefix(review):
        return 'po' if review.get('partner') else 'oa'

    def _commerce_command(self,text,user_id,is_admin,username='',*,message_id=None):
        parts=text.split();command=parts[0].split('@')[0].lower() if parts else ''
        if command in {'/offers','/offer'}:
            if len(parts)==2 and command=='/offer':
                if type(message_id) is int and message_id>0:self._activate_offer(user_id,parts[1],None,f'telegram-offer:{user_id}:{message_id}')
                else:self._offer_failure(user_id)
            else:self._offers_callback('of:list:0',user_id,None,{})
            return True
        if command=='/campaigns':
            if is_admin:self._campaign_admin_callback('oa:list:0',user_id,None,{})
            else:self._partner_campaign_callback('po:list:0',user_id,None,{})
            return True
        if command=='/partner_campaigns':
            self._partner_campaign_callback('po:list:0',user_id,None,{});return True
        if command=='/offer_limits':
            self._campaign_admin_callback('oa:limits:0',user_id,None,{});return True
        return super()._commerce_command(text,user_id,is_admin,username,message_id=message_id)

    def _offer_failure(self,user_id,message_id=None):
        self._commerce_notice(user_id,'تعذر إكمال العرض','راجع العروض وحالة الطلب قبل تكرار العملية.',message_id)

    @_serial_user
    def _activate_offer(self,user_id,code,message_id,reference):
        try:
            positive_minor(user_id)
            if not self._uses_commerce_wallet(user_id):
                self._commerce_notice(user_id,'العروض غير مفعّلة لهذا الحساب','طلباتك المدفوعة محفوظة. يمكنك متابعة الطلبات من «طلباتي ومدفوعاتي».',message_id);return
            normalized=normalize_referral_code(code)
            self._topups().mark_owner(user_id)
            result=self._commerce().rpc('activate_campaign',{'p_telegram_user_id':user_id,'p_code':normalized,'p_reference':reference})
            labels={
                'activated':'تم تفعيل العرض بنجاح! 🎁 استمتع بالمزايا الممنوحة لك.',
                'already_activated':'⚠️ هذا العرض مفعّل مسبقاً في حسابك، يمكنك الاستفادة من الفحوصات المتبقية.',
                'invalid_code':'❌ كود العرض غير صحيح، تأكد من كتابة الرمز بشكل دقيق.',
                'inactive_offer':'⏸ هذا العرض غير متاح حالياً أو تم إيقافه.',
                'ineligible_customer':'ℹ️ عذراً، هذا العرض مخصص لفئة أخرى من الحسابات (مثل العملاء الجدد فقط).',
                'code_exhausted':'⏳ عذراً، اكتمل الحد الأقصى لعدد العملاء المستفيدين من هذا الكود.',
                'budget_exhausted':'📉 عذراً، انتهت الميزانية الإجمالية المخصصة لهذا العرض.'
            }
            text=labels.get(result['state'],'حالة العرض غير معروفة، يرجى مراجعة الدعم.')
            if result.get('wallet_credit_minor'):text+='\n\n💼 تم إيداع رصيد ترويجي: '+egp(result['wallet_credit_minor'])+' جنيه في محفظتك.'
            self._show_panel(user_id,views.Panel('<b>'+text+'</b>',
                [[views.button('عروضي وحدودها','of:list:0')],[views.button('💼 محفظتي','wallet')]]),message_id)
        except (CommerceUnavailable,OSError,ValueError,KeyError,TypeError):self._offer_failure(user_id,message_id)

    @_serial_user
    def _offers_callback(self,command,user_id,message_id,callback):
        try:
            positive_minor(user_id)
            if not self._uses_commerce_wallet(user_id):
                self._commerce_notice(user_id,'العروض غير مفعّلة لهذا الحساب','طلباتك المدفوعة محفوظة. يمكنك متابعة الطلبات من «طلباتي ومدفوعاتي».',message_id);return
            parts=command.split(':')
            if len(parts)!=3:raise ValueError('Offer callback required')
            with self._ui_lock:self._waiting_user_inputs.pop(user_id,None)
            if parts[1]=='activate':
                with self._ui_lock:self._waiting_user_inputs[user_id]={'action':'commerce_offer_code','expires':time.monotonic()+1800,'message_id':message_id}
                self._commerce_notice(user_id,'🎟 تفعيل كود عرض','أرسل كود العرض الآن (مثلاً: FREE2026).\nتبدأ صلاحية العرض فور التفعيل وتطبق المزايا على طلباتك تلقائياً.',message_id);return
            if parts[1]!='list':raise ValueError('Unknown offer action')
            offset=int(parts[2]);records=self._commerce().rpc('customer_campaigns',{'p_telegram_user_id':user_id,'p_offset':offset})
            text='<b>🎁 عروضك وحدودها المتاحة</b>\n\n'
            for record in records:
                kind=record['benefit_kind'];value=record['benefit_value']
                benefit=(str(Decimal(value)/100)+'%' if kind=='percent' else egp(value)+' جنيه لكل فحص' if kind=='fixed'
                    else 'فحوص مجانية ضمن الحدود أدناه' if kind=='free_checks' else egp(value)+' جنيه رصيد ترويجي')
                text+=f"📌 كود: <code>{views.safe(record.get('code') or record['name'])}</code> · {KINDS[kind]}\n• الميزة: {benefit}\n"
                if kind!='wallet_credit':
                    text+=f"• الأنواع المشمولة: {', '.join(TYPE_LABELS[t] for t in record['check_types'])}\n"
                    text+=f"• حد الفحوص: {record['max_checks']} · المستخدم: {record['consumed_checks']} · المحجوز لطلبات: {record['reserved_checks']}\n"
                else:text+='• الرصيد يُضاف مرة واحدة؛ المتبقي غير المستخدم ينتهي بانتهاء مدة العرض.\n'
                text+=f"• سقف التوفير للطلب: {egp(record['max_discount_minor'])} جنيه · المستهلك: {egp(record['discount_minor'])} جنيه · المحجوز: {egp(record['reserved_discount_minor'])} جنيه\n"
                text+='• المتاح من ميزانية العرض: '+egp(record.get('global_available_discount_minor',0))+' جنيه\n'
                text+='• ينتهي في: '+views.safe(record['expires_at'])+'\n• الحالة: '+views.safe(record['state'])+'\n\n'
            if not records:text+='لا توجد عروض مفعّلة لحسابك حالياً.\n\n'
            text+='💡 ملاحظة: يتم تطبيق الخصم الأكبر والأفضل لطلبك تلقائياً عند الدفع دون تداخل.'
            rows=[]
            if offset:rows.append([views.button('‹ الأحدث','of:list:'+str(max(0,offset-5)))])
            if len(records)==5:rows.append([views.button('الأقدم ›','of:list:'+str(offset+5))])
            rows.extend([[views.button('🎟 إدخال كود عرض جديد','of:activate:0')],[views.button('💼 محفظتي','wallet'),views.button('🏠 الرئيسية','home')]])
            self._show_panel(user_id,views.Panel(text,rows),message_id)
        except (CommerceUnavailable,OSError,ValueError,KeyError,TypeError):self._offer_failure(user_id,message_id)

    def _campaign_review(self,token,actor_id):
        review=getattr(self,'_campaign_reviews',{}).get(token)
        if not review or review['actor']!=actor_id or review['expires']<=time.monotonic() or (not review.get('partner') and actor_id not in self.current_authorized_ids):
            raise ValueError('Authenticated campaign reviewer required')
        return review

    def _campaign_step(self,token,actor_id,message_id):
        review=self._campaign_review(token,actor_id);fields=self._campaign_fields(review);prefix=self._campaign_prefix(review)
        while review['step']<len(fields):
            key,label,kind=fields[review['step']]
            if key=='benefit_value' and review['values'].get('benefit_kind')=='free_checks':
                review['values'][key]=1;review['step']+=1;continue
            if review['values'].get('benefit_kind')=='wallet_credit' and key in {'check_types','max_checks'}:
                review['values'][key]=['account','service'] if key=='check_types' else 1;review['step']+=1;continue
            choices=KINDS if kind=='kind' else ELIGIBILITY if kind=='eligibility' else TYPE_LABELS if kind=='types' else None
            if choices:
                header = f'<b>الخطوة ({review["step"]+1}/{len(fields)}): {label}</b>\n\n'
                if kind == 'kind':
                    header += 'اختر نوع الخصم أو المنفعة التي سيحصل عليها العميل عند تفعيل هذا الكود:'
                elif kind == 'eligibility':
                    header += 'حدد فئة العملاء المسموح لهم بالاستفادة من هذا الكود:'
                elif kind == 'types':
                    header += 'اختر نوع الأرقام المشمولة بالتخفيض في هذا العرض:'
                rows=[[views.button(title,f'{prefix}:set:{token}:{value}')] for value,title in choices.items()]
                rows.append([views.button('‹ إلغاء الرجوع للعروض',prefix+':list:0')])
                self._show_panel(actor_id,views.Panel(header,rows),message_id)
            else:
                if kind=='value':label='نسبة الخصم من 1 إلى 100 (%)' if review['values']['benefit_kind']=='percent' else 'قيمة الخصم بالجنيه'
                with self._ui_lock:self._waiting_user_inputs[actor_id]={'action':'commerce_offer_admin_value','token':token,'expires':review['expires'],'message_id':message_id}
                if kind=='code':
                    self._show_panel(actor_id,views.Panel(f'<b>الخطوة ({review["step"]+1}/{len(fields)}): {label}</b>\n\n'
                        'اكتب اسم الكود الترويجي الذي سيقوم العميل بإدخاله (مثلاً: FREE2026 أو EID100).\n'
                        'يمكنك كتابة الاسم في رسالة، أو الضغط على زر التوليد التلقائي أدناه:',
                        [[views.button('🎲 توليد كود تلقائي',prefix+':generate:'+token)],[views.button('‹ إلغاء',prefix+':list:0')]]),message_id)
                else:
                    step_title = f'الخطوة ({review["step"]+1}/{len(fields)}): {label}'
                    step_body = FIELD_EXPLANATIONS.get(key, 'أرسل القيمة المطلوبة للمتابعة.')
                    self._commerce_notice(actor_id, step_title, step_body, message_id)
            return
        review['ready']=True;review['expires']=time.monotonic()+180
        labels={key:label for key,label,_ in OFFER_FIELDS+POLICY_FIELDS}
        if review['policy']:
            title = '<b>⚖️ مراجعة واعتماد حدود الإدارة</b>\n\nهذه الحدود هي السقف الأقصى للأمان لحماية ميزانية المنصة، ولن يُسمح بإنشاء أي عرض يتجاوزها:'
        elif review.get('partner'):
            title = '<b>🤝 مراجعة كود خصم الشريك</b>\n\nتفاصيل العرض الترويجي المقتطع من حصتك في الطلب:'
        else:
            title = '<b>🎁 مراجعة وتأكيد العرض الجديد</b>\n\nتفاصيل العرض الترويجي المموّل من المنصة قبل تفعيله للعملاء:'
        lines=[title]
        for key,value in review['values'].items():
            if key in {'max_percent_basis_points'} or (key=='benefit_value' and review['values'].get('benefit_kind')=='percent'):
                val_str = str(Decimal(value)/100)+'%'
            elif '_minor' in key or (key=='benefit_value' and review['values'].get('benefit_kind') in {'fixed','wallet_credit'}):
                val_str = egp(value)+' جنيه'
            elif key.endswith('_seconds'):
                val_str = str(value//86400)+' يوم'
            elif key=='benefit_kind':
                val_str = KINDS[value]
            elif key=='eligibility':
                val_str = ELIGIBILITY[value]
            elif key=='check_types':
                val_str = '، '.join(TYPE_LABELS[t] for t in value)
            elif key in {'max_checks_per_user', 'max_checks'}:
                val_str = f'{value} فحص'
            elif key=='max_redemptions':
                val_str = f'{value} عميل'
            else:
                val_str = str(value)
            
            field_name = labels.get(key, key)
            lines.append(f'• <b>{field_name}</b>: ' + ('<code>'+views.safe(val_str)+'</code>' if key=='code' else views.safe(val_str)))

        if review['policy']:
            lines.append('━━━━━━━━━━━━━━━\n💡 <b>ملاحظة:</b> تؤثر هذه الحدود في إنشاء العروض الجديدة لحماية الميزانية؛ العروض السابقة والأسعار المحفوظة تبقى ثابتة دون تغيير.')
        elif review.get('partner'):
            rate=review['partner_context']['basis_points'];discount=review['values']['benefit_value']
            lines.append(f'━━━━━━━━━━━━━━━\n📊 <b>حساب الحصص:</b>\n• حصتك الأصلية: {Decimal(rate)/100}%\n• خصم العميل: {Decimal(discount)/100}%\n• صافي حصتك بعد الخصم: {Decimal(rate-discount)/100}%\n\n'
                '💡 الخصم يُقتطع مباشرة من حصتك في فحص الأرقام المنجزة، ولا يُمس رصيد أرباحك السابقة. تبقى حصة المنصة ثابتة ومحفوظة.')
            if review['partner_context'].get('approval_required'):
                lines.append('⚠️ يتطلب هذا الكود موافقة الإدارة قبل أن يصبح متاحاً للعملاء.')
        else:
            lines.append('━━━━━━━━━━━━━━━\n💡 <b>ملاحظة هامة:</b>\n• تبدأ صلاحية العرض لكل عميل من لحظة تفعيله للكود.\n• لا تُدمج العروض معاً في نفس الفحص.\n• يتم تطبيق الخصم تلقائياً عند فحص الأرقام المشمولة.')

        save_label = ('✅ اعتماد وحفظ حدود الإدارة' if review['policy'] else '📤 إرسال للموافقة' if review.get('partner') and review['partner_context'].get('approval_required') else '🚀 تأكيد وتفعيل العرض الآن')
        self._show_panel(actor_id,views.Panel('\n\n'.join(lines),[[views.button(save_label,prefix+':save:'+token,'success')],
            [views.button('‹ إلغاء المسودة',prefix+':list:0')]]),message_id)

    def _campaign_save_problem(self,actor_id,message_id,token,body,*,can_change_limits=False):
        rows=[]
        review=getattr(self,'_campaign_reviews',{}).get(token)
        prefix=self._campaign_prefix(review) if review else 'oa'
        if review and review['actor']==actor_id and review['expires']>time.monotonic():
            review['expires']=time.monotonic()+1800
            if not review.get('submitted'):rows.append([views.button('✏️ تعديل البيانات',prefix+':edit:'+token)])
            rows.append([views.button('🔄 إعادة التأكيد بنفس المسودة',prefix+':save:'+token)])
            if can_change_limits and not review['policy']:rows.append([views.button('⚖️ تعديل حدود الإدارة أولاً','oa:limits:'+token)])
        rows.append([views.button('‹ العروض المحفوظة',prefix+':list:0')])
        self._show_panel(actor_id,views.Panel('<b>⚠️ تعذر إكمال حفظ العرض</b>\n\n'+views.safe(body,3000),rows),message_id)

    @_serial_user
    def _campaign_admin_callback(self,command,actor_id,message_id,callback):
        if actor_id not in self.current_authorized_ids:self._commerce_notice(actor_id,'غير مصرح','إنشاء العروض وضبطها خاص بالإدارة.',message_id);return
        return self._campaign_callback(command,actor_id,message_id,callback)

    @_serial_user
    def _partner_campaign_callback(self,command,actor_id,message_id,callback):
        return self._campaign_callback(command,actor_id,message_id,callback,partner=True)

    def _campaign_callback(self,command,actor_id,message_id,callback,*,partner=False):
        action=None;review=None
        try:
            parts=command.split(':');action=parts[1]
            prefix='po' if partner else 'oa';context=None
            if parts[0]!=prefix:raise ValueError('Campaign role mismatch')
            if partner:
                context=self._commerce().rpc('partner_campaign_context',{'p_telegram_user_id':actor_id})
                if not context.get('is_partner') or context.get('state')!='active' or context.get('partnership_ended_at'):
                    self._revoke_partner_role(actor_id)
                    self._commerce_notice(actor_id,'الحساب بصلاحيات عميل','الحملات متاحة للشركاء النشطين فقط. مستحقاتك السابقة محفوظة.',message_id);return
                if action=='limits':raise ValueError('Administration sets campaign policy')
            if action in {'edit','generate','set','save'}:
                review=self._campaign_review(parts[2],actor_id)
                if bool(review.get('partner'))!=partner:raise ValueError('Campaign review role mismatch')
            if action=='list':
                with self._ui_lock:
                    self._waiting_user_inputs.pop(actor_id,None)
                    self._campaign_reviews={k:v for k,v in getattr(self,'_campaign_reviews',{}).items() if v['actor']!=actor_id}
                records=self._commerce().rpc('list_partner_campaigns' if partner else 'list_campaigns',
                    {'p_offset':int(parts[2]),**({'p_telegram_user_id':actor_id} if partner else {})})
                if partner:
                    text = (f'<b>🎁 حملاتي وأكواد الخصم الخاصة بي</b>\n\n'
                            f'• حصتك المصرح بها من الأرباح: {Decimal(context["basis_points"])/100}%\n'
                            '• الخصم الممنوح للعملاء يُقتطع تلقائياً من حصتك في فحص الأرقام بالطلب نفسه دون الحاجة لأرباح سابقة.\n\n')
                else:
                    text = ('<b>🎁 إدارة العروض والأكواد الترويجية</b>\n\n'
                            'يمكنك ضبط حدود الأمان للإدارة، أو إنشاء عرض ترويجي جديد للعملاء بمدة وميزانية محددة.\n\n')
                rows=[]
                for record in records:
                    approval_state=record.get('approval_state')
                    label='ملغاة' if record.get('promotion_cancelled_at') else 'بانتظار موافقة الإدارة' if approval_state=='pending' else 'مرفوض' if approval_state=='rejected' else ('نشط' if record['state']=='active' else record['state'])
                    text+=f"<code>{views.safe(record.get('code') or record['name'])}</code> · {views.safe(record['state'])} · {record['activations']} عميل\n"
                    if label!=record['state']:
                        text+=f"• الحالة: {views.safe(label)}\n"
                    text+=f"• المنفعة المستهلكة: {egp(record['used_discount_minor'])} جنيه · المحجوزة: {egp(record['held_discount_minor'])} جنيه\n"
                    if not partner:
                        text+='• التمويل: '+('حصة الشريك' if record.get('funder_kind')=='partner' else 'ميزانية المنصة')+'\n'
                    text+='\n'
                    if record['state']=='active':rows.append([views.button('⏸ إيقاف العرض: '+record['name'],prefix+':pause:'+record['id'])])
                    if not partner and record.get('funder_kind')=='platform' and record.get('benefit_kind')=='wallet_credit':
                        if record['state'] in {'active','paused'}:
                            rows.append([views.button('🗑 إلغاء واسترجاع المتبقي: '+record['name'],'oa:cancelpromo:'+record['id'],'danger')])
                        if record.get('reclaimed_promotional_minor'):
                            text+='• الرصيد الترويجي المسترجع: '+egp(record['reclaimed_promotional_minor'])+' جنيه\n\n'
                if not records:
                    text += 'لا توجد عروض منشأة حالياً.\n\n'
                offset=int(parts[2])
                if offset:rows.append([views.button('‹ الأحدث',prefix+':list:'+str(max(0,offset-10)))])
                if len(records)==10:rows.append([views.button('الأقدم ›',prefix+':list:'+str(offset+10))])
                if partner:
                    if context.get('approval_required'):text+='ℹ️ ملاحظة: الأكواد الجديدة تحتاج موافقة الإدارة قبل تفعيلها للعملاء.\n'
                    policy=context.get('policy') or {};limits=policy.get('rules',{})
                    if limits:
                        text+='⚖️ حدود الإدارة: نسبة حتى '+str(Decimal(min(context['basis_points'],limits['max_percent_basis_points']))/100)+'%'
                        text+=f" · {limits['max_checks_per_user']} فحص لكل عميل · {limits['max_redemptions']} عميل · {limits['max_duration_seconds']//86400} يوم\n"
                        text+='حد الخصم للعميل: '+egp(limits['max_discount_minor_per_user'])+' جنيه · ميزانية الحملة: '+egp(limits['max_total_discount_minor'])+' جنيه\n'
                    if context.get('state')=='active':rows.append([views.button('➕ إنشاء كود خصم جديد','po:new:0')])
                    else:text+='⚠️ الشراكة غير نشطة حالياً؛ يرجى مراجعة الإدارة.\n'
                    rows.append([views.button('💰 أرباح الشريك','pf:home:0')])
                else:rows.append([views.button('⚖️ مراجعة حدود الإدارة','oa:limits:0'),views.button('➕ إنشاء عرض جديد','oa:new:0')])
                rows.append([views.button('🏠 الرئيسية','home')])
                self._show_panel(actor_id,views.Panel(text,rows),message_id)
            elif action in {'cancelpromo','cancelpromook'}:
                if partner:raise ValueError('Administration cancels promotional grants')
                campaign_id=str(UUID(parts[2]))
                args={'p_actor_telegram_id':actor_id,'p_campaign_id':campaign_id}
                if action=='cancelpromo':
                    record=self._commerce().rpc('promotion_cancellation_record',args)
                    body='الحملة: <code>'+views.safe(record['code'])+'</code>\n'
                    body+='الرصيد الترويجي غير المستخدم: '+egp(record['available_minor'])+' جنيه\n'
                    body+='المحجوز لطلبات قائمة: '+egp(record['reserved_minor'])+' جنيه\n\n'
                    body+='سيُلغى كود الحملة ويُسحب الرصيد الترويجي المتبقي من محافظ العملاء. النقد المدفوع محفوظ. '
                    body+='الطلبات المحجوزة تستمر، وأي رصيد ترويجي يعود منها لاحقًا يُسترجع تلقائيًا.'
                    self._show_panel(actor_id,views.Panel('<b>إلغاء الحملة واسترجاع المتبقي</b>\n\n'+body,
                        [[views.button('تأكيد الإلغاء والاسترجاع','oa:cancelpromook:'+campaign_id,'danger')],
                         [views.button('رجوع للحملات','oa:list:0')]]),message_id)
                else:
                    if not isinstance(callback.get('id'),str) or not callback['id'].strip():raise ValueError('Authenticated cancellation required')
                    self._commerce().rpc('cancel_promotional_campaign',dict(args,p_reference='telegram-promotion-cancel:'+str(actor_id)+':'+callback['id']))
                    try:
                        record=self._commerce().rpc('reclaim_cancelled_promotion',args)
                    except (CommerceUnavailable,OSError,ValueError,KeyError,TypeError):
                        self._commerce_notice(actor_id,'أُلغيت الحملة','أُوقف الكود. تُستكمل معالجة الرصيد الترويجي تلقائيًا؛ راجع قائمة الحملات للتحقق من المبلغ المسترجع.',message_id);return
                    body='الحملة: <code>'+views.safe(record['code'])+'</code>\n'
                    body+='إجمالي الرصيد الترويجي المسترجع: '+egp(record['reclaimed_minor'])+' جنيه\n'
                    if record['available_minor']:body+='متبقٍ للمعالجة التلقائية: '+egp(record['available_minor'])+' جنيه\n'
                    body+='المحجوز لطلبات قائمة: '+egp(record['reserved_minor'])+' جنيه\n\nالنقد المدفوع محفوظ؛ أي رصيد ترويجي يعود من الطلبات يُسترجع تلقائيًا.'
                    self._show_panel(actor_id,views.Panel('<b>أُلغيت الحملة</b>\n\n'+body,[[views.button('الحملات','oa:list:0')]]),message_id)
            elif action in {'limits','new'}:
                if partner and context.get('state')!='active':raise ValueError('Active partnership required')
                if action=='new' and not self._commerce().rpc('campaign_policy',{}):
                    self._commerce_notice(actor_id,'حدود الإدارة مطلوبة أولاً ⚖️','يرجى ضبط حدود الإدارة أولاً عبر الأمر /offer_limits أو بالضغط على «مراجعة حدود الإدارة» لتحديد السقف الأقصى للخصومات والميزانية قبل إنشاء أول عرض.',message_id);return
                parent=None
                if action=='limits' and parts[2]!='0':
                    parent=self._campaign_review(parts[2],actor_id)
                    if parent['policy']:raise ValueError('Offer review required')
                    parent['expires']=time.monotonic()+1800
                token=secrets.token_hex(6)
                with self._ui_lock:
                    reviews=getattr(self,'_campaign_reviews',{})
                    self._campaign_reviews={k:v for k,v in reviews.items() if v['expires']>time.monotonic()
                        and (v['actor']!=actor_id or parent is not None and k==parts[2])}
                    if len(self._campaign_reviews)>=128:raise ValueError('Campaign review limit reached')
                    self._campaign_reviews[token]={'actor':actor_id,'policy':action=='limits','values':{},'step':0,'expires':time.monotonic()+1800}
                    if partner:self._campaign_reviews[token].update(partner=True,partner_context=context,values={'benefit_kind':'percent'})
                    if parent is not None:self._campaign_reviews[token]['return_to']=parts[2]
                self._campaign_step(token,actor_id,message_id)
            elif action=='edit':
                review=self._campaign_review(parts[2],actor_id)
                if review.get('submitted'):raise ValueError('Submitted review must retain identical arguments')
                review['step']=0;review['ready']=False
                review['expires']=time.monotonic()+1800
                review['values']={'benefit_kind':'percent'} if partner else {}
                review.pop('limits_validated',None)
                self._campaign_step(parts[2],actor_id,message_id)
            elif action=='generate':
                review=self._campaign_review(parts[2],actor_id)
                if review['policy'] or self._campaign_fields(review)[review['step']][0]!='code':raise ValueError('Code input required')
                with self._ui_lock:self._waiting_user_inputs.pop(actor_id,None)
                self._commerce_text_input(actor_id,'OFFER_'+secrets.token_hex(12).upper(),{
                    'action':'commerce_offer_admin_value','token':parts[2],'expires':review['expires'],'message_id':message_id})
            elif action=='set':
                review=self._campaign_review(parts[2],actor_id);key,_,kind=self._campaign_fields(review)[review['step']];value=parts[3]
                if kind=='kind' and value in KINDS:parsed=value
                elif kind=='eligibility' and value in ELIGIBILITY:parsed=value
                elif kind=='types' and value in TYPES:parsed=TYPES[value]
                else:raise ValueError('Unexpected campaign choice')
                review['values'][key]=parsed;review['step']+=1;self._campaign_step(parts[2],actor_id,message_id)
            elif action=='save':
                review=self._campaign_review(parts[2],actor_id)
                if not review.get('ready') or not callback.get('id'):raise ValueError('Reviewed authenticated decision required')
                reference='telegram-campaign:'+str(actor_id)+':'+callback['id']
                if review['policy']:
                    self._commerce().rpc('configure_campaign_policy',{'p_actor_telegram_id':actor_id,'p_rules':review['values'],'p_reference':reference})
                else:
                    if not review.get('limits_validated'):
                        policy=self._commerce().rpc('campaign_policy',{})
                        issues=campaign_limit_issues(review['values'],policy)
                        if partner and review['values']['benefit_value']>context['basis_points']:
                            issues.append('الخصم يتجاوز حصتك المصرح بها: '+str(Decimal(context['basis_points'])/100)+'%.')
                        if not policy:issues=['لم تُعتمد حدود الإدارة بعد. يُرجى ضبط حدود الإدارة أولاً.']
                        if issues:
                            self._campaign_save_problem(actor_id,message_id,parts[2],
                                'المسودة محفوظة ولكن هناك تعارض مع حدود الأمان المصرح بها:\n\n'+'\n'.join('• '+iss for iss in issues)+'\n\nيرجى تعديل القيم لتتوافق مع الحدود، أو تعديل حدود الإدارة أولاً.',can_change_limits=not partner)
                            return
                        review['limits_validated']=True
                    rules={k:v for k,v in review['values'].items() if k!='code'}
                    review['submitted']=True
                    creation=self._commerce().rpc('create_partner_campaign' if partner else 'create_platform_campaign',
                        {('p_telegram_user_id' if partner else 'p_actor_telegram_id'):actor_id,'p_key':'telegram-campaign:'+parts[2],
                        'p_code':review['values']['code'],'p_rules':rules,'p_reference':reference})
                    if partner:
                        if not isinstance(creation,dict):raise ValueError('Campaign acknowledgment required')
                        str(UUID(creation['campaign_id']))
                        approval_state=creation.get('approval_state')
                        if context.get('approval_required') and approval_state not in {'pending','approved','rejected','not_required'}:
                            raise ValueError('Approval acknowledgment required')
                        if approval_state in {'pending','rejected'}:
                            text=('<b>حُفظ الكود بانتظار موافقة الإدارة ⏳</b>' if approval_state=='pending' else '<b>الكود مرفوض من الإدارة ❌</b>')
                            text+='\n\nالكود: <code>'+views.safe(review['values']['code'])+'</code>\n'
                            text+=('تم حفظ التفاصيل ورفعها للإدارة للمراجعة والموافقة قبل التفعيل.' if approval_state=='pending' else
                                   'تم رفض تفعيل هذا الكود بموجب قرار الإدارة المحفوظ.')
                            self._show_panel(actor_id,views.Panel(text,[[views.button('🎁 حملاتي وأكوادي','po:list:0')],
                                [views.button('🏠 الرئيسية','home')]]),message_id);return
                if review['policy']:
                    if review.get('return_to'):
                        self._campaign_step(review['return_to'],actor_id,message_id)
                    else:
                        self._commerce_notice(actor_id,'تم اعتماد حدود الإدارة بنجاح ✅',
                            'تم حفظ سقوف الأمان الجديدة للعروض بنجاح.\nيمكنك الآن الضغط على زر «عرض جديد» من قائمة العروض لإنشاء عروض ترويجية للعملاء ضمن هذه الحدود.',message_id)
                elif partner:
                    self._commerce_notice(actor_id,'تم إنشاء كود الخصم بنجاح 🎉',
                        f'تم تفعيل كود الخصم <code>{views.safe(review["values"]["code"])}</code> بنجاح لعملائك.\nيتم اقتطاع الخصم مباشرة من حصتك في كل طلب. تابع إحصائيات الاستخدام عبر /partner_campaigns.',message_id)
                else:
                    self._commerce_notice(actor_id,'تم تفعيل العرض بنجاح 🚀',
                        f'تم إنشاء وتفعيل كود العرض <code>{views.safe(review["values"]["code"])}</code> بنجاح!\nأصبح بإمكان العملاء الآن تفعيل الكود والاستفادة منه فوراً. تابع الإحصائيات عبر قائمة العروض /campaigns.',message_id)
            elif action=='pause':
                if not callback.get('id'):raise ValueError('Authenticated pause required')
                self._commerce().rpc('pause_partner_campaign' if partner else 'pause_campaign',{'p_campaign_id':str(UUID(parts[2])),
                    ('p_telegram_user_id' if partner else 'p_actor_telegram_id'):actor_id,'p_reference':'telegram-campaign:'+callback['id']})
                self._commerce_notice(actor_id,'تم إيقاف العرض مؤقتاً ⏸','تم إيقاف إمكانية تفعيل الكود للعملاء الجدد، مع الحفاظ على صلاحيات الطلبات الجارية المسجلة مسبقاً.',message_id)
            else:raise ValueError('Unknown campaign action')
        except (CommerceUnavailable,OSError,ValueError,KeyError,TypeError,IndexError) as exc:
            logger.warning('Campaign action failed: action=%s kind=%s http=%s database=%s reason=%s',action,
                type(exc).__name__,getattr(exc,'http_status',None),getattr(exc,'database_code',None),getattr(exc,'reason',None))
            if action=='save' and review is not None:
                reason=getattr(exc,'reason',None)
                if reason in {'campaign_limits','campaign_policy_missing','campaign_invalid','duplicate_record','partner_share_limit','partner_inactive'}:
                    review['submitted']=False;review.pop('limits_validated',None)
                body=('الخصم يتجاوز حصتك الحالية؛ عدّل نسبة الخصم.' if reason=='partner_share_limit' else
                    'الشراكة غير نشطة حاليًا؛ راجع الإدارة.' if reason=='partner_inactive' else
                    'الحملة تتجاوز الحدود المصرح بها. عدّل بيانات الحملة.' if reason=='campaign_limits' else
                    'كود العرض مستخدم بالفعل. اختر كودًا جديدًا.' if reason=='duplicate_record' else
                    'هذه المسودة محفوظة ببيانات مختلفة. راجع العروض المحفوظة قبل إنشاء عرض جديد.' if reason=='campaign_conflict' else
                    'لم نتأكد من الحفظ. راجع العروض المحفوظة أو أعد التأكيد بنفس المسودة؛ لا تنشئ طلبًا جديدًا بسبب تعذر الاتصال.')
                self._campaign_save_problem(actor_id,message_id,parts[2],body,can_change_limits=not partner and reason in {'campaign_limits','campaign_policy_missing'})
            else:self._offer_failure(actor_id,message_id)

    def _commerce_text_input(self,user_id,text,waiting):
        if waiting.get('action') not in {'commerce_offer_code','commerce_offer_admin_value'}:return super()._commerce_text_input(user_id,text,waiting)
        try:
            if waiting.get('expires',0)<=time.monotonic():raise ValueError('Expired offer prompt')
            if waiting['action']=='commerce_offer_code':
                incoming=positive_minor(waiting.get('_telegram_input_message_id'))
                self._activate_offer(user_id,text,waiting.get('message_id'),f'telegram-offer:{user_id}:{incoming}')
            else:
                review=self._campaign_review(waiting['token'],user_id);fields=self._campaign_fields(review)
                if review.get('partner'):
                    context=self._commerce().rpc('partner_campaign_context',{'p_telegram_user_id':user_id})
                    if not context.get('is_partner') or context.get('state')!='active' or context.get('partnership_ended_at'):
                        self._revoke_partner_role(user_id)
                        self._commerce_notice(user_id,'الحساب بصلاحيات عميل','الشراكة غير نشطة؛ لم تُنشأ حملة من هذه المسودة.',waiting.get('message_id'))
                        return True
                key,_,kind=fields[review['step']]
                if kind=='value':kind='percent' if review['values']['benefit_kind']=='percent' else 'money'
                review['values'][key]=value_input(text,kind);review['step']+=1
                self._campaign_step(waiting['token'],user_id,waiting.get('message_id'))
        except (CommerceUnavailable,OSError,ValueError,KeyError,TypeError,IndexError,InvalidOperation):
            if waiting.get('action')=='commerce_offer_admin_value':
                with self._ui_lock:self._waiting_user_inputs[user_id]=waiting
                review=getattr(self,'_campaign_reviews',{}).get(waiting.get('token'),{})
                fields=self._campaign_fields(review) if review else []
                step=review.get('step',0)
                hint=''
                if 0<=step<len(fields):
                    key,lbl,_=fields[step]
                    hint='\n\n'+FIELD_EXPLANATIONS.get(key,'')
                self._commerce_notice(user_id,'⚠️ القيمة المدخلة غير صحيحة','يرجى إرسال رقم صحيح ومطابق للشروط المطلوبة.'+hint,waiting.get('message_id'))
            else:self._offer_failure(user_id,waiting.get('message_id'))
        return True
