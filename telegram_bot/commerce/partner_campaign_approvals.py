"""Owner review of partner codes; activation remains a database decision."""
from decimal import Decimal,InvalidOperation
from uuid import UUID

from telegram_bot import views
from .bot_workflow import _serial_user
from .checkout import egp
from .client import CommerceUnavailable
from .finance import positive_minor


def percent(value):
    number=Decimal(str(value))
    if not number.is_finite() or not 0<=number<=10000:raise ValueError('Invalid share')
    text=format(number/100,'f')
    return (text.rstrip('0').rstrip('.') if '.' in text else text)+'%'


class PartnerCampaignApprovalsMixin:
    def _partner_campaign_setting_panel(self,actor,partner,message_id=None):
        setting=self._commerce().rpc('partner_campaign_approval_setting',{
            'p_actor_telegram_id':actor,'p_partner_telegram_id':partner})
        if not isinstance(setting,dict) or type(setting.get('required')) is not bool:
            raise ValueError('Approval setting acknowledgment required')
        required=setting['required']
        text=('<b>موافقة أكواد الشريك</b>\n\nالشريك: <code>'+str(partner)+'</code>\n'
              'موافقتك قبل التفعيل: <b>'+('مفعّلة' if required else 'غير مفعّلة')+'</b>\n\n'
              'عند تفعيلها، يُحفظ الكود وتفاصيل الخصم ويصلك طلب مراجعة. يبقى الكود غير قابل للاستخدام حتى توافق عليه.\n'
              'يخص الإعداد الأكواد الجديدة لهذا الشريك. تعطيله لا يعتمد الطلبات المنتظرة، والأكواد السابقة تبقى بحالتها.')
        rows=[[views.button('إيقاف اشتراط الموافقة' if required else 'تفعيل اشتراط موافقتي',
                           'pa:require:'+str(partner)+':'+('0' if required else '1'),'primary')],
              [views.button('طلبات الأكواد المنتظرة','pa:list:0')],
              [views.button('‹ الشريك','pf:partner:'+str(partner)),views.button('‹ الشركاء','cp:list:0')]]
        self._show_panel(actor,views.Panel(text,rows),message_id)

    def _partner_campaign_review_panel(self,record):
        identifier=str(UUID(record['campaign_id']))
        code=record['code'];rules=record['rules']
        partner=positive_minor(record['partner_telegram_id'])
        rate=Decimal(str(record['basis_points']));discount=Decimal(str(rules['benefit_value']))
        if not rate.is_finite() or not discount.is_finite() or not 0<discount<=rate<=10000:
            raise ValueError('Reviewed share snapshot required')
        state=record.get('approval_state',record.get('state'))
        labels={'pending':'بانتظار موافقتك','approved':'تمت الموافقة','rejected':'مرفوض'}
        if state not in labels:raise ValueError('Known approval state required')
        eligible={'any':'جميع عملاء إحالة الشريك','new':'عملاء الإحالة قبل أول خدمة مدفوعة',
                  'existing':'عملاء الإحالة بعد أول خدمة مدفوعة'}[rules['eligibility']]
        types='، '.join({'account':'الحسابات','service':'الخدمات'}[kind] for kind in rules['check_types'])
        text=('<b>مراجعة كود الشريك</b>\n\n'
              'الكود: <code>'+views.safe(code)+'</code>\n'
              'الشريك: <code>'+str(partner)+'</code>\n'
              'الحالة: '+labels[state]+'\n\n'
              'خصم العميل: '+percent(discount)+'\n'
              'حصة الشريك الأصلية: '+percent(rate)+'\n'
              'حصة المنصة: '+percent(10000-rate)+'\n'
              'المتبقي للشريك بعد الخصم: '+percent(rate-discount)+'\n'
              'الخصم من حصة الطلب نفسه، دون استخدام أرباح سابقة.\n\n'
              'العملاء المؤهلون: '+eligible+'\n'
              'أنواع الفحص: '+types+'\n'
              'حد التشييكات لكل عميل: '+views.safe(rules['max_checks'])+'\n'
              'حد الخصم لكل عميل: '+egp(rules['max_discount_minor'])+' جنيه\n'
              'ميزانية الحملة: '+egp(rules['max_total_discount_minor'])+' جنيه\n'
              'مدة الاستفادة من أول تفعيل: '+format(Decimal(str(rules['duration_seconds']))/86400,'f')+' يوم\n'
              'عدد العملاء الأقصى: '+views.safe(rules['max_redemptions']))
        rows=[]
        if state=='pending':
            text+='\n\nالكود محفوظ وغير متاح للعملاء حتى موافقتك.'
            rows.append([views.button('✅ أوافق وأفعّل الكود','pa:approve:'+identifier,'success'),
                         views.button('❌ أرفض','pa:reject:'+identifier,'danger')])
        rows.append([views.button('‹ الطلبات المنتظرة','pa:list:0'),
                     views.button('إعداد الشريك','pa:settings:'+str(partner))])
        return views.Panel(text,rows)

    @_serial_user
    def _partner_campaign_approval_callback(self,command,actor,message_id,callback):
        if not self._role_owner(actor):
            self._commerce_notice(actor,'غير مصرح','موافقة أكواد الشركاء وإعدادها خاصة بمالك البوت.',message_id)
            return
        try:
            with self._ui_lock:self._waiting_user_inputs.pop(actor,None)
            parts=command.split(':')
            if len(parts) not in (3,4) or parts[0]!='pa':raise ValueError('Approval action required')
            action=parts[1]
            if action in {'settings','require','set'}:
                partner=positive_minor(int(parts[2]))
                if action=='settings':
                    if len(parts)!=3:raise ValueError('Settings identity required')
                    self._partner_campaign_setting_panel(actor,partner,message_id);return
                if len(parts)!=4 or parts[3] not in {'0','1'}:raise ValueError('Explicit setting required')
                required=parts[3]=='1'
                if action=='require':
                    self._show_panel(actor,views.Panel('<b>تأكيد إعداد أكواد الشريك</b>\n\nالشريك: <code>'+str(partner)+'</code>\n\n'+
                        ('كل كود جديد ينتظر موافقتك قبل أن يستخدمه العملاء.' if required else
                         'الأكواد الجديدة ستعمل مباشرة ضمن حصة الشريك وحدود الإدارة. الطلبات المنتظرة تحتاج قرارًا منفصلًا.'),
                        [[views.button('أؤكد '+('اشتراط موافقتي' if required else 'إيقاف الاشتراط'),
                            'pa:set:'+str(partner)+':'+parts[3],'success')],
                         [views.button('إلغاء','pa:settings:'+str(partner))]]),message_id);return
                receipt=callback.get('id')
                if not isinstance(receipt,str) or not receipt.strip():raise ValueError('Authenticated setting confirmation required')
                saved=self._commerce().rpc('configure_partner_campaign_approval',{
                    'p_actor_telegram_id':actor,'p_partner_telegram_id':partner,'p_required':required,
                    'p_reference':'telegram-partner-campaign-policy:'+str(actor)+':'+receipt})
                if not isinstance(saved,dict) or type(saved.get('required')) is not bool:
                    raise ValueError('Saved setting acknowledgment required')
                # A replayed receipt may predate a later explicit setting. Show
                # current authoritative state, rather than a guessed toggle.
                self._partner_campaign_setting_panel(actor,partner,message_id);return
            if action=='list':
                if len(parts)!=3:raise ValueError('Page required')
                offset=int(parts[2])
                if not 0<=offset<=1000000:raise ValueError('Invalid page')
                records=self._commerce().rpc('list_partner_campaign_approvals',{
                    'p_actor_telegram_id':actor,'p_offset':offset})
                if isinstance(records,dict):records=records['items']
                if not isinstance(records,list):raise ValueError('Approval list required')
                lines=['<b>أكواد الشركاء بانتظار موافقتك</b>'];rows=[]
                for record in records:
                    identifier=str(UUID(record['campaign_id']))
                    lines.append('الكود: <code>'+views.safe(record['code'])+'</code> · الشريك: <code>'+
                                 str(positive_minor(record['partner_telegram_id']))+'</code>')
                    rows.append([views.button('مراجعة '+str(record['code']),'pa:review:'+identifier)])
                if not records:lines.append('لا توجد أكواد تنتظر موافقتك.')
                pages=[]
                if offset:pages.append(views.button('الأحدث','pa:list:'+str(max(0,offset-10))))
                if len(records)==10:pages.append(views.button('الأقدم','pa:list:'+str(offset+10)))
                if pages:rows.append(pages)
                rows.append([views.button('‹ قسم الشركاء','menu:partners')])
                self._show_panel(actor,views.Panel('\n\n'.join(lines),rows),message_id);return
            if action not in {'review','approve','reject'} or len(parts)!=3:raise ValueError('Known approval action required')
            identifier=str(UUID(parts[2]))
            record=self._commerce().rpc('partner_campaign_approval_record',{
                'p_actor_telegram_id':actor,'p_campaign_id':identifier})
            if not isinstance(record,dict) or str(record.get('campaign_id'))!=identifier or record.get('reviewer_telegram_id')!=actor:
                raise ValueError('Owned approval record required')
            if action=='review':
                self._show_panel(actor,self._partner_campaign_review_panel(record),message_id);return
            if record.get('approval_state',record.get('state')) in {'approved','rejected'}:
                # Old buttons cannot replace a final decision or reactivate a
                # code the partner has subsequently paused.
                self._show_panel(actor,self._partner_campaign_review_panel(record),message_id);return
            receipt=callback.get('id')
            if not isinstance(receipt,str) or not receipt.strip():raise ValueError('Authenticated decision required')
            decision=self._commerce().rpc('decide_partner_campaign',{
                'p_actor_telegram_id':actor,'p_campaign_id':identifier,'p_approve':action=='approve',
                'p_reference':'telegram-partner-campaign-decision:'+str(actor)+':'+receipt})
            state=decision.get('state') if isinstance(decision,dict) else None
            if state not in {'approved','rejected'} or (state=='approved')!=(action=='approve'):
                raise ValueError('Decision acknowledgment differs')
            self._commerce_notice(actor,'تمت الموافقة على الكود' if state=='approved' else 'رُفض الكود',
                'الكود: '+str(record['code'])+'\n'+('أصبح متاحًا وفق الشروط المعتمدة.' if state=='approved' else 'بقي غير قابل للاستخدام.')+
                '\nحُفظ القرار وإشعار الشريك.',message_id)
        except (CommerceUnavailable,OSError,ValueError,KeyError,TypeError,IndexError,InvalidOperation):
            self._show_panel(actor,views.Panel('<b>تعذر التأكد من إجراء الكود</b>\n\n'
                'افتح الطلبات المنتظرة أو إعداد الشريك للتحقق من الحالة قبل إعادة الإجراء.',
                [[views.button('الطلبات المنتظرة','pa:list:0')],[views.button('‹ الشركاء','cp:list:0')]]),message_id)

    def _partner_campaign_notice(self,record):
        payload=record['payload'];kind=payload['kind']
        identifier=str(UUID(payload['campaign_id']))
        if kind=='campaign_request':
            actor=positive_minor(record['recipient_telegram_id'])
            if not self._role_owner(actor):raise ValueError('Current owner recipient required')
            saved=payload.get('approval_record')
            if saved is None:
                # Compatibility with earlier persisted notices. Migration 40
                # enriches new claims inside the claiming SQL transaction.
                saved=self._commerce().rpc('partner_campaign_approval_record',{
                    'p_actor_telegram_id':actor,'p_campaign_id':identifier})
            if (not isinstance(saved,dict) or saved.get('reviewer_telegram_id')!=actor or str(saved.get('campaign_id'))!=identifier
                    or saved.get('partner_telegram_id')!=payload['partner_telegram_id'] or saved.get('code')!=payload['code']):
                raise ValueError('Owned approval notification required')
            panel=self._partner_campaign_review_panel(saved)
            return panel.text,panel.rows
        if kind!='campaign_decision' or payload.get('state') not in {'approved','rejected'}:
            raise ValueError('Known campaign decision notification required')
        if positive_minor(record['recipient_telegram_id'])!=positive_minor(payload['partner_telegram_id']):
            raise ValueError('Partner notification recipient differs')
        approved=payload['state']=='approved'
        text=('<b>'+('وافقت الإدارة على كودك' if approved else 'رفضت الإدارة الكود')+'</b>\n\n'
              'الكود: <code>'+views.safe(payload['code'])+'</code>\n'+
              ('أصبح الكود متاحًا للعملاء وفق شروطه المعتمدة.' if approved else 'الكود غير صالح للاستخدام. يمكنك مراجعة الإدارة.') )
        return text,[[views.button('حملاتي وأكوادي','po:list:0')]]
