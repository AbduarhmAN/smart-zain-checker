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

POLICY_FIELDS=[('max_percent_basis_points','أقصى نسبة خصم (%)','percent'),
    ('max_checks_per_user','أقصى عدد تشييكات للعميل في العرض','int'),
    ('max_discount_minor_per_user','أقصى خصم تراكمي للعميل بالجنيه','money'),
    ('max_total_discount_minor','أقصى ميزانية إجمالية للعرض بالجنيه','money'),
    ('max_duration_seconds','أقصى مدة للعرض منذ تفعيله بالأيام','days'),
    ('max_wallet_credit_minor','أقصى رصيد ترويجي يُمنح للعميل بالجنيه','money'),
    ('max_redemptions','أقصى عدد عملاء يمكنهم تفعيل كود العرض','int')]
OFFER_FIELDS=[('benefit_kind','نوع منفعة العرض','kind'),('benefit_value','قيمة المنفعة','value'),
    ('eligibility','العملاء المؤهلون عند التفعيل','eligibility'),('check_types','أنواع التشييكات المشمولة','types'),
    ('max_checks','حد التشييكات التراكمي للعميل عبر الملفات','int'),
    ('max_discount_minor','حد الخصم أو الرصيد الترويجي للعميل بالجنيه','money'),
    ('max_total_discount_minor','ميزانية العرض لجميع العملاء بالجنيه','money'),
    ('duration_seconds','مدة العرض منذ تفعيله بالأيام','days'),('max_redemptions','عدد العملاء الأقصى لتفعيل الكود','int'),
    ('code','كود العرض','code')]
KINDS={'percent':'نسبة خصم','fixed':'خصم مبلغ من كل تشييك','free_checks':'تشييكات مجانية محدودة','wallet_credit':'رصيد ترويجي للمحفظة'}
ELIGIBILITY={'new':'قبل أول خدمة مدفوعة','existing':'بعد أول خدمة مدفوعة','any':'كل العملاء'}
TYPES={'account':['account'],'service':['service'],'both':['account','service']}
TYPE_LABELS={'account':'تشييك حساب','service':'تشييك خدمة','both':'الحساب والخدمة'}
logger=logging.getLogger('CommerceCampaigns')


def campaign_limit_issues(values,policy):
    limits=policy.get('rules',{}) if isinstance(policy,dict) else {}
    pairs=[('max_checks','max_checks_per_user','تشييكات العميل','count'),
        ('max_discount_minor','max_discount_minor_per_user','حد المنفعة للعميل','money'),
        ('max_total_discount_minor','max_total_discount_minor','ميزانية الحملة','money'),
        ('duration_seconds','max_duration_seconds','مدة الحملة','days'),
        ('max_redemptions','max_redemptions','عدد العملاء','count')]
    if values.get('benefit_kind')=='percent':pairs.append(('benefit_value','max_percent_basis_points','نسبة الخصم','percent'))
    if values.get('benefit_kind')=='wallet_credit':pairs.append(('benefit_value','max_wallet_credit_minor','الرصيد الترويجي','money'))
    def display(value,kind):
        return (egp(value)+' جنيه' if kind=='money' else str(Decimal(value)/100)+'%' if kind=='percent'
            else str(Decimal(value)/86400)+' يوم' if kind=='days' else str(value))
    issues=[]
    for key,limit_key,label,kind in pairs:
        actual,maximum=values.get(key),limits.get(limit_key)
        if type(actual) is int and type(maximum) is int and actual>maximum:
            issues.append(label+': اخترت '+display(actual,kind)+'؛ الحد الحالي '+display(maximum,kind))
    if values.get('max_discount_minor',0)>values.get('max_total_discount_minor',0):
        issues.append('حد المنفعة للعميل لا يمكن أن يتجاوز ميزانية الحملة كلها.')
    if values.get('benefit_kind') in {'fixed','wallet_credit'} and values.get('benefit_value',0)>values.get('max_discount_minor',0):
        issues.append('قيمة المنفعة تتجاوز الحد الذي حددته للعميل.')
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
            labels={'activated':'تم تفعيل العرض','already_activated':'العرض مفعّل سابقًا؛ لم تتكرر المنفعة',
                'invalid_code':'كود العرض غير صالح','inactive_offer':'العرض غير متاح حاليًا',
                'ineligible_customer':'الحساب غير مؤهل لهذا العرض','code_exhausted':'بلغ العرض عدد العملاء المحدد',
                'budget_exhausted':'نفدت ميزانية العرض'}
            text=labels.get(result['state'],'راجع حالة العرض')
            if result.get('wallet_credit_minor'):text+='\nرصيد ترويجي: '+egp(result['wallet_credit_minor'])+' جنيه. الشحن لا يبدأ أول خدمة مدفوعة.'
            self._show_panel(user_id,views.Panel('<b>'+text+'</b>',
                [[views.button('عروضي وحدودها','of:list:0')],[views.button('المحفظة','wallet')]]),message_id)
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
                self._commerce_notice(user_id,'تفعيل عرض','أرسل كود العرض. تبدأ مدته عند أول تفعيل ناجح، وإعادة إدخاله لا تعيد المدة أو الرصيد.',message_id);return
            if parts[1]!='list':raise ValueError('Unknown offer action')
            offset=int(parts[2]);records=self._commerce().rpc('customer_campaigns',{'p_telegram_user_id':user_id,'p_offset':offset})
            text='<b>عروضك وحدودها</b>\n\n'
            for record in records:
                kind=record['benefit_kind'];value=record['benefit_value']
                benefit=(str(Decimal(value)/100)+'%' if kind=='percent' else egp(value)+' جنيه لكل تشييك' if kind=='fixed'
                    else 'تشييكات مجانية ضمن الحدود أدناه' if kind=='free_checks' else egp(value)+' جنيه رصيد ترويجي')
                text+=f"<code>{views.safe(record.get('code') or record['name'])}</code> · {KINDS[kind]}\n{benefit}\n"
                if kind!='wallet_credit':
                    text+=f"الأنواع: {', '.join(TYPE_LABELS[t] for t in record['check_types'])}\n"
                    text+=f"حد التشييكات: {record['max_checks']} · المستخدم: {record['consumed_checks']} · المحجوز لطلبات: {record['reserved_checks']}\n"
                else:text+='الرصيد يُضاف مرة واحدة؛ المتبقي غير المستخدم ينتهي بانتهاء مدة العرض. المبالغ المحجوزة لطلبات تبقى محمية حتى التسوية.\n'
                text+=f"حد المنفعة: {egp(record['max_discount_minor'])} جنيه · المستخدم: {egp(record['discount_minor'])} · المحجوز: {egp(record['reserved_discount_minor'])}\n"
                text+='المتاح من ميزانية العرض العامة: '+egp(record.get('global_available_discount_minor',0))+' جنيه\n'
                text+='ينتهي: '+views.safe(record['expires_at'])+'\nحالة العرض: '+views.safe(record['state'])+'\n\n'
            if not records:text+='لا توجد عروض مفعّلة لحسابك.\n\n'
            text+='يُختار أكبر خصم مؤهل للطلب تلقائيًا، دون جمع العروض. حدود الخصم تُحجز عند تثبيت السعر؛ حصة العطل الفني تُعاد للعرض بعد التسوية.'
            rows=[]
            if offset:rows.append([views.button('الأحدث','of:list:'+str(max(0,offset-5)))])
            if len(records)==5:rows.append([views.button('الأقدم','of:list:'+str(offset+5))])
            rows.extend([[views.button('إدخال كود عرض','of:activate:0')],[views.button('المحفظة','wallet'),views.button('الرئيسية','home')]])
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
                rows=[[views.button(title,f'{prefix}:set:{token}:{value}')] for value,title in choices.items()]
                rows.append([views.button('إلغاء',prefix+':list:0')])
                self._show_panel(actor_id,views.Panel('<b>'+label+'</b>',rows),message_id)
            else:
                if kind=='value':label='نسبة الخصم من 0 إلى 100 (%)' if review['values']['benefit_kind']=='percent' else 'قيمة المنفعة بالجنيه'
                with self._ui_lock:self._waiting_user_inputs[actor_id]={'action':'commerce_offer_admin_value','token':token,'expires':review['expires'],'message_id':message_id}
                if kind=='code':
                    self._show_panel(actor_id,views.Panel('<b>'+label+'</b>\n\nاكتب الكود أو ولّد كودًا تلقائيًا.',
                        [[views.button('توليد كود تلقائي',prefix+':generate:'+token)],[views.button('إلغاء',prefix+':list:0')]]),message_id)
                else:self._commerce_notice(actor_id,label,'أرسل قيمة موجبة. الأعداد والمدد صحيحة، والمبالغ بدقة القرش. جميع حدود العرض مطلوبة قبل تفعيله.',message_id)
            return
        review['ready']=True;review['expires']=time.monotonic()+180
        labels={key:label for key,label,_ in OFFER_FIELDS+POLICY_FIELDS};lines=['<b>مراجعة حدود الإدارة</b>' if review['policy'] else '<b>مراجعة خصم من حصتك في الطلب</b>' if review.get('partner') else '<b>مراجعة عرض تموله المنصة</b>']
        for key,value in review['values'].items():
            if key in {'max_percent_basis_points'} or key=='benefit_value' and review['values'].get('benefit_kind')=='percent':value=str(Decimal(value)/100)+'%'
            elif key.endswith('_minor') or key=='benefit_value' and review['values'].get('benefit_kind') in {'fixed','wallet_credit'}:value=egp(value)+' جنيه'
            elif key.endswith('_seconds'):value=str(value//86400)+' يوم'
            elif key=='benefit_kind':value=KINDS[value]
            elif key=='eligibility':value=ELIGIBILITY[value]
            elif key=='check_types':value='، '.join(TYPE_LABELS[t] for t in value)
            lines.append(labels[key]+': '+('<code>'+views.safe(value)+'</code>' if key=='code' else views.safe(value)))
        lines.append('المدة تبدأ من تفعيل العميل. لا يُجمع مع عرض آخر، ولا يغير مصدر الإحالة. الخصم الثابت يُطبق على كل تشييك مشمول ضمن الحدود.' if not review['policy'] else 'تؤثر الحدود في إنشاء العروض التالية؛ الأسعار والعروض المحفوظة تبقى ثابتة.')
        if review.get('partner'):
            rate=review['partner_context']['basis_points'];discount=review['values']['benefit_value']
            lines.append(f'حصتك الأصلية: {Decimal(rate)/100}% · خصم العميل: {Decimal(discount)/100}% · المتبقي لك: {Decimal(rate-discount)/100}%.\n'
                'الخصم من حصة الفحوص الصحيحة في الطلب نفسه. لا يحتاج أرباحًا سابقة ولا يُسحب من محفظة أرباحك. تبقى حصة المنصة الأصلية محفوظة. '
                'يُحجز الحد الأعلى للطلب، ثم تُعاد قيمة الخصم والفحوص غير المنجزة إلى محفظة العميل بعد تسليم النتيجة. العرض يخص عملاء إحالتك ويستخدم رصيدهم المدفوع.')
        self._show_panel(actor_id,views.Panel('\n\n'.join(lines),[[views.button('حفظ الحدود' if review['policy'] else 'أؤكد إنشاء وتفعيل العرض',prefix+':save:'+token,'success')],
            [views.button('إلغاء',prefix+':list:0')]]),message_id)

    def _campaign_save_problem(self,actor_id,message_id,token,body,*,can_change_limits=False):
        rows=[]
        review=getattr(self,'_campaign_reviews',{}).get(token)
        prefix=self._campaign_prefix(review) if review else 'oa'
        if review and review['actor']==actor_id and review['expires']>time.monotonic():
            review['expires']=time.monotonic()+1800
            if not review.get('submitted'):rows.append([views.button('تعديل البيانات',prefix+':edit:'+token)])
            rows.append([views.button('إعادة التأكيد بنفس المسودة',prefix+':save:'+token)])
            if can_change_limits and not review['policy']:rows.append([views.button('تعديل حدود الإدارة','oa:limits:'+token)])
        rows.append([views.button('العروض المحفوظة',prefix+':list:0')])
        self._show_panel(actor_id,views.Panel('<b>لم يكتمل حفظ العرض</b>\n\n'+views.safe(body,3000),rows),message_id)

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
                    self._commerce_notice(actor_id,'الحساب بصلاحيات زبون','الحملات متاحة للشركاء النشطين فقط. مستحقاتك السابقة محفوظة.',message_id);return
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
                text=('<b>حملاتي وخصوماتي</b>\n\nحصتك المصرح بها: '+str(Decimal(context['basis_points'])/100)+'%. '
                    'خصم العميل يُقتطع من حصتك الأصلية في طلب الحملة نفسه، دون استخدام أرباح سابقة.\n\n' if partner else
                    '<b>إدارة العروض</b>\n\nحدد حدود الإدارة أولًا، ثم أنشئ عرضًا بمدة وميزانية وحدود معلنة.\n\n');rows=[]
                for record in records:
                    text+=f"<code>{views.safe(record.get('code') or record['name'])}</code> · {views.safe(record['state'])} · {record['activations']} عميل\n"
                    text+=f"المنفعة المستهلكة: {egp(record['used_discount_minor'])} · المحجوزة: {egp(record['held_discount_minor'])} جنيه\n\n"
                    if not partner:text+='تمويل: '+('حصة الشريك في الطلب' if record.get('funder_kind')=='partner' else 'المنصة')+'\n\n'
                    if record['state']=='active':rows.append([views.button('إيقاف '+record['name'],prefix+':pause:'+record['id'])])
                offset=int(parts[2])
                if offset:rows.append([views.button('الأحدث',prefix+':list:'+str(max(0,offset-10)))])
                if len(records)==10:rows.append([views.button('الأقدم',prefix+':list:'+str(offset+10))])
                if partner:
                    policy=context.get('policy') or {};limits=policy.get('rules',{})
                    if limits:
                        text+='حدود الإدارة: نسبة حتى '+str(Decimal(min(context['basis_points'],limits['max_percent_basis_points']))/100)+'%'
                        text+=f" · {limits['max_checks_per_user']} تشييك لكل عميل · {limits['max_redemptions']} عميل · {limits['max_duration_seconds']//86400} يوم\n"
                        text+='حد الخصم للعميل: '+egp(limits['max_discount_minor_per_user'])+' جنيه · إجمالي الحملة: '+egp(limits['max_total_discount_minor'])+' جنيه\n'
                    if context.get('state')=='active':rows.append([views.button('إنشاء خصم من حصتي','po:new:0')])
                    else:text+='الشراكة غير نشطة حاليًا؛ راجع الإدارة.\n'
                    rows.append([views.button('أرباح الشريك','pf:home:0')])
                else:rows.append([views.button('حدود الإدارة','oa:limits:0'),views.button('عرض جديد','oa:new:0')])
                rows.append([views.button('الرئيسية','home')])
                self._show_panel(actor_id,views.Panel(text,rows),message_id)
            elif action in {'limits','new'}:
                if partner and context.get('state')!='active':raise ValueError('Active partnership required')
                if action=='new' and not self._commerce().rpc('campaign_policy',{}):
                    self._commerce_notice(actor_id,'حدود الإدارة مطلوبة','ابدأ بالأمر /offer_limits لاعتماد حدود النسبة والتشييكات والميزانية والمدة وعدد العملاء.',message_id);return
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
                        if not policy:issues=['لم تُعتمد حدود الإدارة بعد.']
                        if issues:
                            self._campaign_save_problem(actor_id,message_id,parts[2],
                                'المسودة محفوظة. صحح القيم التالية ضمن الحدود المصرح بها:\n\n'+'\n'.join(issues),can_change_limits=not partner)
                            return
                        review['limits_validated']=True
                    rules={k:v for k,v in review['values'].items() if k!='code'}
                    review['submitted']=True
                    self._commerce().rpc('create_partner_campaign' if partner else 'create_platform_campaign',
                        {('p_telegram_user_id' if partner else 'p_actor_telegram_id'):actor_id,'p_key':'telegram-campaign:'+parts[2],
                        'p_code':review['values']['code'],'p_rules':rules,'p_reference':reference})
                if review['policy'] and review.get('return_to'):
                    self._campaign_step(review['return_to'],actor_id,message_id)
                else:self._commerce_notice(actor_id,'حُفظ الإعداد','استخدم /partner_campaigns للمتابعة. الخصم من حصة الطلب نفسه؛ لم يُحجز رصيد أرباح سابق.' if partner else 'استخدم /campaigns للمتابعة. المنفعة وشروطها وقرار الإدارة محفوظة.',message_id)
            elif action=='pause':
                if not callback.get('id'):raise ValueError('Authenticated pause required')
                self._commerce().rpc('pause_partner_campaign' if partner else 'pause_campaign',{'p_campaign_id':str(UUID(parts[2])),
                    ('p_telegram_user_id' if partner else 'p_actor_telegram_id'):actor_id,'p_reference':'telegram-campaign:'+callback['id']})
                self._commerce_notice(actor_id,'أُوقف العرض','أُوقف التفعيل والاقتباسات الجديدة؛ أسعار الطلبات المحفوظة تبقى ثابتة.',message_id)
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
                        self._commerce_notice(user_id,'الحساب بصلاحيات زبون','الشراكة غير نشطة؛ لم تُنشأ حملة من هذه المسودة.',waiting.get('message_id'))
                        return True
                key,_,kind=fields[review['step']]
                if kind=='value':kind='percent' if review['values']['benefit_kind']=='percent' else 'money'
                review['values'][key]=value_input(text,kind);review['step']+=1
                self._campaign_step(waiting['token'],user_id,waiting.get('message_id'))
        except (CommerceUnavailable,OSError,ValueError,KeyError,TypeError,IndexError,InvalidOperation):
            if waiting.get('action')=='commerce_offer_admin_value':
                with self._ui_lock:self._waiting_user_inputs[user_id]=waiting
                self._commerce_notice(user_id,'راجع قيمة الحقل','أعد إدخال قيمة صحيحة للحقل نفسه. بيانات المسودة السابقة محفوظة.',waiting.get('message_id'))
            else:self._offer_failure(user_id,waiting.get('message_id'))
        return True
