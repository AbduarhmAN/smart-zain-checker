"""Partner earnings and held withdrawals; external payout is recorded by an admin."""
import re
import logging
import time
from uuid import UUID,uuid4
from telegram_bot import views
from telegram_bot.authorization import admin_ids
from .bot_workflow import _serial_user
from .checkout import egp
from .client import CommerceUnavailable
from .finance import positive_minor,money_minor_input
from .legacy_wallets import minor
from .withdrawal_receipts import WithdrawalReceiptMixin,receipt_panel,RECEIPT_LABELS,_utc_stamp
from .admin_payouts import AdminPartnerPayoutMixin,ADMIN_PAYOUT_INPUTS
from .payout_notifications import PayoutNotificationMixin
from .partner_balances import partner_balance_text
from .partner_roles import PartnerRoleMixin,active_partner

logger=logging.getLogger('PartnerFinance')


class PartnerFinanceMixin(AdminPartnerPayoutMixin,PayoutNotificationMixin,WithdrawalReceiptMixin,PartnerRoleMixin):
    def _sync_partner_menus(self):
        """Role-specific native menus; ordinary customers never see partner tools."""
        known=set()
        for offset in range(0,10000,10):
            records=self._commerce().rpc('list_partners',{'p_offset':offset})
            for record in records:
                user=record['telegram_user_id'];positive_minor(user)
                if record.get('state')!='active' or record.get('ended_at'):continue
                known.add(user)
                self._remember_partner_sheet_access(user,record.get('partner_sheet_enabled',True))
                if user not in self.current_authorized_ids:self._set_partner_menu(user)
            if len(records)<10:break
        with self._ui_lock:
            removed=set(getattr(self,'_known_partner_ids',set()))-known
            self._known_partner_ids=known
        for user in removed:
            self._revoke_partner_role(user)
            try:self._reset_customer_menu(user)
            except Exception:logger.warning('Customer menu refresh deferred; partner role removed.')
        # Reconcile persisted Telegram scopes too: process memory is empty
        # after restart, while a retired partner's old command menu may remain.
        actors=sorted(admin_ids(self))
        if actors:
            try:
                for offset in range(0,10000,10):
                    ended=self._commerce().rpc('ended_partners',{'p_actor_telegram_id':actors[0],'p_offset':offset})
                    for record in ended:
                        user=positive_minor(record['telegram_user_id'])
                        self._revoke_partner_role(user)
                        self._reset_customer_menu(user)
                    if len(ended)<10:break
            except (CommerceUnavailable,OSError,ValueError,KeyError,TypeError):
                logger.warning('Retired partner command menus await reconciliation; server role checks remain active.')

    def _set_partner_menu(self,user,*,chat_known=False):
        sheet_enabled=getattr(self,'_partner_sheet_access_flags',{}).get(user,True)
        with self._ui_lock:
            ready=getattr(self,'_partner_menu_ready',set())
            if user in ready and getattr(self,'_partner_menu_sheet_access',{}).get(user,True)==sheet_enabled:return
        if not chat_known:
            response=self._api_call('getChat',{'chat_id':user})
            if not isinstance(response,dict) or response.get('ok') is not True:return
        commands=[
                        {'command':'start','description':'الرئيسية'},
                        {'command':'status','description':'حالة فحوصاتي وطلباتي'},
                        {'command':'earnings','description':'أرباح الشريك وكود الإحالة'},
                        {'command':'partner_checks','description':'فحص الشركاء والسعر الأساسي للشيت'},
                        {'command':'partner_campaigns','description':'حملاتي وخصوماتي من حصتي'},
                        {'command':'wallet','description':'رصيد محفظتي'},
                        {'command':'upload','description':'إرسال ملف XLSX'},
                        {'command':'archive','description':'استرجاع نتيجة سابقة'}]
        if not sheet_enabled:commands=[item for item in commands if item['command']!='partner_checks']
        response=self._api_call('setMyCommands',{'scope':{'type':'chat','chat_id':user},'commands':commands})
        if isinstance(response,dict) and response.get('ok') is True:
            with self._ui_lock:
                if not hasattr(self,'_partner_menu_ready'):self._partner_menu_ready=set()
                self._partner_menu_ready.add(user)
                if not hasattr(self,'_partner_menu_sheet_access'):self._partner_menu_sheet_access={}
                self._partner_menu_sheet_access[user]=sheet_enabled

    def _refresh_partner_menu_for_chat(self,user):
        if user in getattr(self,'_customer_menu_pending',set()):
            try:self._reset_customer_menu(user)
            except Exception:logger.warning('Customer menu refresh deferred; partner privileges remain denied.')
            return
        if user in getattr(self,'_known_partner_ids',set()) and user not in admin_ids(self):
            if self._has_active_partner_role(user):self._set_partner_menu(user,chat_known=True)
            else:
                with self._ui_lock:getattr(self,'_partner_menu_ready',set()).discard(user)
                self._reset_customer_menu(user)

    def _commerce_command(self,text,user_id,is_admin,username='',*,message_id=None):
        command=text.split()[0].split('@')[0].lower() if text.split() else ''
        if command=='/earnings' and user_id in admin_ids(self):
            self._admin_earnings(user_id,0,message_id);return True
        if command in {'/earnings','/partner_wallet'}:
            self._partner_finance_callback('pf:home:0',user_id,None,{});return True
        if command=='/withdraw':
            self._partner_finance_callback('pf:withdraw:0',user_id,None,{});return True
        if command=='/withdrawals':
            self._withdrawal_admin_callback('wa:list:0',user_id,None,{});return True
        return super()._commerce_command(text,user_id,is_admin,username,message_id=message_id)

    @_serial_user
    def _partner_finance_callback(self,command,user_id,message_id,callback):
        try:
            positive_minor(user_id);parts=command.split(':')
            if len(parts)!=3:raise ValueError('Invalid partner finance callback')
            if parts[1]=='admin':
                self._admin_earnings(user_id,int(parts[2]),message_id);return
            if parts[1]=='partner':
                self._admin_partner_earnings(user_id,positive_minor(int(parts[2])),message_id);return
            if parts[1] in {'receipt','ack','dispute'}:
                self._partner_receipt_action(parts[1],parts[2],user_id,message_id,callback);return
            if parts[1] in {'home','list','withdraw'}:
                with self._ui_lock:self._waiting_user_inputs.pop(user_id,None)
            profile=self._commerce().rpc('partner_finance',{'p_telegram_user_id':user_id})
            if not profile.get('is_partner'):
                self._commerce_notice(user_id,'أرباح الشريك','الحساب غير مسجل كشريك. راجع الإدارة لإعداد الشراكة.',message_id);return
            enabled=active_partner(profile)
            if not enabled:
                self._revoke_partner_role(user_id)
                try:self._reset_customer_menu(user_id)
                except Exception:logger.warning('Customer menu refresh deferred; partner privileges denied.')
                if parts[1] in {'withdraw','confirm'}:
                    self._commerce_notice(user_id,'الحساب بصلاحيات زبون','الشراكة غير نشطة. راجع الإدارة بخصوص مستحقاتك السابقة.',message_id);return
            with self._ui_lock:
                if not hasattr(self,'_known_partner_ids'):self._known_partner_ids=set()
                if not enabled:self._known_partner_ids.discard(user_id)
                else:self._known_partner_ids.add(user_id)
            if parts[1]=='list':
                offset=int(parts[2]);records=self._commerce().rpc('partner_withdrawals',{'p_telegram_user_id':user_id,'p_offset':offset})
                labels={'requested':'بانتظار المراجعة','approved':'معتمد بانتظار الدفع','paid':'مدفوع','rejected':'مرفوض، أُعيد الرصيد'}
                text='<b>سجل سحوباتك</b>\n\n'
                receipt_rows=[]
                for record in records:
                    text+=f"<code>{record['id']}</code>\n{egp(record['amount_minor'])} جنيه · {labels.get(record['state'],record['state'])}\n"
                    text+=views.safe(record['destination'],500)+'\n'
                    if record.get('payout_reference'):text+='الإيصال: '+views.safe(record['payout_reference'],250)+'\n'
                    if record.get('advance'):
                        extra=record['advance']
                        text+='دفعة مقدمة: '+egp(extra['amount_minor'])+' جنيه · السبب: '+views.safe(extra['reason'],500)+'\n'
                        text+='رصيد الحساب بعد هذا الصرف: '+egp(extra['account_after_minor'])+' جنيه\n'
                    if record.get('receipt_state'):
                        text+='الاستلام: '+RECEIPT_LABELS.get(record['receipt_state'],record['receipt_state'])+'\n'
                        receipt_rows.append([views.button('عرض توثيق '+egp(record['amount_minor'])+' جنيه','pf:receipt:'+record['id'])])
                    text+='\n'
                if not records:text+='لا توجد طلبات سحب في هذه الصفحة.'
                rows=receipt_rows
                if offset:rows.append([views.button('الأحدث','pf:list:'+str(max(0,offset-10)))])
                if len(records)==10:rows.append([views.button('الأقدم','pf:list:'+str(offset+10))])
                rows.append([views.button('أرباح الشريك','pf:home:0')])
                self._show_panel(user_id,views.Panel(text,rows),message_id);return
            if parts[1]=='withdraw':
                with self._ui_lock:self._waiting_user_inputs[user_id]={'action':'commerce_withdraw_amount',
                    'message_id':message_id,'expires':time.monotonic()+1800}
                self._commerce_notice(user_id,'مبلغ السحب',f"المتاح: {egp(profile.get('withdrawable_minor',max(0,profile['available_minor'])))} جنيه. أرسل مبلغ السحب بالجنيه؛ سيُحجز حتى تقرر الإدارة.",message_id)
                return
            if parts[1]=='confirm':
                token=str(UUID(parts[2]));review=getattr(self,'_withdrawal_reviews',{}).get(token)
                if not review or review['user']!=user_id or review['expires']<=time.monotonic():raise ValueError('Withdrawal review expired or unowned')
                result=self._commerce().rpc('request_withdrawal',{'p_telegram_user_id':user_id,'p_key':'withdrawal:'+token,
                    'p_amount_minor':review['amount'],'p_destination':review['destination']})
                if result['state']=='insufficient_balance':
                    self._commerce_notice(user_id,'الرصيد غير كافٍ','المتاح الآن: '+egp(result['available_minor'])+' جنيه.',message_id)
                else:self._commerce_notice(user_id,'حُفظ طلب السحب','رقم الطلب: '+result['withdrawal_id']+'. المبلغ محجوز في محفظة أرباحك حتى الدفع أو الرفض.',message_id)
                return
            if parts[1]!='home':raise ValueError('Unknown partner action')
            text=(f"<b>أرباح الشريك</b>\n\nكود الإحالة: <code>{views.safe(profile.get('referral_code'))}</code>\n"
                f"أرباح اليوم: {egp(profile['today_earned_minor'])} جنيه\n"
                +partner_balance_text(profile)+'\n'+
                f"محجوز للسحب: {egp(profile['reserved_minor'])} جنيه\nالمسحوب: {egp(profile['withdrawn_minor'])} جنيه\n"
                f"عمولات معلقة: {egp(profile['pending_commission_minor'])} جنيه\n\n"
                f"العملاء المحالون: {profile['clients']}\nالتشييكات المدفوعة الصحيحة: {profile['paid_checks']}\n"
                f"المحصّل بعد الخصم والاسترداد: {egp(profile['collected_minor'])} جنيه\n"
                f"طلبات تنتظر التسوية: {profile['awaiting_settlement_orders']}\n\n"
                'العمولة تصبح متاحة بعد اكتمال الفحص وتأكيد تسليم النتيجة. السعر والنسبة وشروط الاستحقاق محفوظة لكل طلب.')
            rows=[[views.button('طلب سحب أرباح','pf:withdraw:0','success')]] if enabled else []
            if not enabled:
                text=text.replace('<b>أرباح الشريك</b>','<b>سجل المستحقات السابقة</b>')
                if profile.get('partnership_ended_at'):
                    text+='\n\nانتهت الشراكة في: '+_utc_stamp(profile['partnership_ended_at'])
                text+='\nالحساب الآن بصلاحيات زبون. سجل المستحقات محفوظ؛ راجع الإدارة بشأن التسوية.'
                if profile.get('partnership_end_event_id'):rows.append([views.button('عرض إشعار انتهاء الشراكة','pl:view:'+profile['partnership_end_event_id'])])
            else:rows.append([views.button('📄 فحص الشركاء','pc:home:0'),views.button('🎁 حملاتي','po:list:0')])
            rows.append([views.button('🧾 سحوباتي','pf:list:0'),views.button('💼 المحفظة','wallet'),views.button('‹ الرئيسية','home')])
            self._show_panel(user_id,views.Panel(text,rows),message_id)
        except (CommerceUnavailable,OSError,ValueError,KeyError,TypeError) as exc:
            self._earnings_failure_log(exc)
            self._commerce_notice(user_id,'تعذر التأكد من الأرباح','أعد فتح لوحة الأرباح للتحقق من الحالة دون إنشاء طلب مالي جديد.',message_id)

    @staticmethod
    def _earnings_failure_log(exc):
        logger.warning('Earnings view failed: kind=%s http=%s database=%s reason=%s',
            type(exc).__name__,getattr(exc,'http_status',None),getattr(exc,'database_code',None),getattr(exc,'reason',None))

    def _admin_partner_earnings(self,actor,partner,message_id=None):
        if actor not in admin_ids(self):
            self._commerce_notice(actor,'غير مصرح','بيانات أرباح الشركاء متاحة للإدارة فقط.',message_id);return
        self._cancel_admin_partner_payout(actor)
        profile=self._commerce().rpc('partner_finance',{'p_telegram_user_id':partner})
        if not profile.get('is_partner'):
            self._commerce_notice(actor,'الحساب ليس شريكًا','اختر شريكًا من قائمة الشركاء.',message_id);return
        text=('<b>أرباح الشريك</b>\n\nالشريك: <code>'+str(partner)+'</code>\n'
            'كود الإحالة: <code>'+views.safe(profile.get('referral_code'))+'</code>\n'
            +partner_balance_text(profile)+'\n'
            'المحجوز: '+egp(profile['reserved_minor'])+' جنيه\n'
            'المسحوب: '+egp(profile['withdrawn_minor'])+' جنيه\n'
            'العمولات المعلقة: '+egp(profile['pending_commission_minor'])+' جنيه')
        rows=[[views.button('صرف الأرباح','wa:paypartner:'+str(partner),'primary')]]
        if self._role_owner(actor):rows[0].append(views.button('موافقة الأكواد','pa:settings:'+str(partner)))
        rows.extend([[views.button('أرباحي وأرباح الشركاء','pf:admin:0')],[views.button('الشركاء','cp:list:0')]])
        self._show_panel(actor,views.Panel(text,rows),message_id)

    def _admin_earnings(self,user_id,offset=0,message_id=None):
        if user_id not in admin_ids(self):
            self._commerce_notice(user_id,'غير مصرح','أرباح المنصة وبيانات الشركاء متاحة للإدارة فقط.',message_id);return
        self._cancel_admin_partner_payout(user_id)
        try:
            offset=max(0,offset)
            data=self._commerce().rpc('admin_earnings_summary',{'p_actor_telegram_id':user_id,'p_offset':offset,'p_limit':8})
            lines=['<b>أرباحي وأرباح الشركاء</b>',
                'إيراد الفحص المسوّى: '+egp(data['gross_service_minor'])+' جنيه',
                'عمولات الشركاء: '+egp(data['partner_commission_minor'])+' جنيه',
                '<b>حصتك بعد العمولات: '+egp(data['platform_revenue_minor'])+' جنيه</b>',
                'حصتك اليوم بتوقيت بغداد: '+egp(data['today_platform_revenue_minor'])+' جنيه',
                'طلبات تنتظر التسوية: '+str(data['awaiting_settlement_orders']),
                'الإيراد لا يشمل أرصدة شحن العملاء أو المبالغ المحجوزة، ولا يطرح تكاليف التشغيل.']
            payout_rows=[]
            for partner in data['partners']:
                label=partner.get('telegram_username') or partner.get('referral_code') or str(partner['telegram_user_id'])
                lines.append('<b>'+views.safe(label)+'</b>\nإجمالي العمولات: '+egp(partner['earned_minor'])+' جنيه\n'
                    'رصيد الحساب: <b>'+egp(partner.get('account_balance_minor',partner['available_minor']))+' جنيه</b>\n'
                    'المتاح: '+egp(partner.get('withdrawable_minor',max(0,partner['available_minor'])))+' جنيه · المحجوز: '+egp(partner['reserved_minor'])+' جنيه\n'
                    'المسحوب: '+egp(partner['withdrawn_minor'])+' جنيه · المعلق: '+egp(partner['pending_minor'])+' جنيه')
                if partner.get('advance_outstanding_minor',0)>0:
                    lines.append('سبب السالب أو المبلغ المستحق: دفعات مقدمة غير مغطاة '+egp(partner['advance_outstanding_minor'])+' جنيه. افتح حساب الشريك لعرض تفاصيلها.')
                payout_rows.append([views.button(str(label),'pf:partner:'+str(positive_minor(partner['telegram_user_id']))),
                    views.button('صرف الأرباح','wa:paypartner:'+str(positive_minor(partner['telegram_user_id'])),'primary')])
            if not data['partners']:lines.append('لا يوجد شركاء في هذه الصفحة.')
            rows=payout_rows
            if offset:rows.append([views.button('الأحدث',f'pf:admin:{max(0,offset-8)}')])
            if offset+8<data['partners_total']:rows.append([views.button('الأقدم',f'pf:admin:{offset+8}')])
            rows.extend([[views.button('↻ تحديث',f'pf:admin:{offset}'),views.button('🧾 توثيق الصرف','wa:receipts:0')],
                [views.button('🤝 الشركاء','cp:list:0'),views.button('‹ المال','menu:money'),views.button('🏠 الرئيسية','home')]])
            self._show_panel(user_id,views.Panel('\n\n'.join(lines),rows),message_id)
        except (CommerceUnavailable,OSError,ValueError,KeyError,TypeError) as exc:
            self._earnings_failure_log(exc)
            self._commerce_notice(user_id,'تعذر عرض الأرباح','تعذر جلب الإيرادات والعمولات المسجلة الآن. أعد فتح الصفحة للتحقق.',message_id)

    def _commerce_text_input(self,user_id,text,waiting):
        if waiting.get('action') in ADMIN_PAYOUT_INPUTS:
            return self._admin_partner_payout_text(user_id,text,waiting)
        if waiting.get('action') not in {'commerce_withdraw_amount','commerce_withdraw_destination','commerce_payout_receipt'}:
            return super()._commerce_text_input(user_id,text,waiting)
        return self._partner_finance_text(user_id,text,waiting)

    @_serial_user
    def _partner_finance_text(self,user_id,text,waiting):
        try:
            if waiting.get('expires',0)<time.monotonic():raise ValueError('Expired financial prompt')
            action=waiting['action'];message_id=waiting.get('message_id')
            if action in {'commerce_withdraw_amount','commerce_withdraw_destination'} and not active_partner(
                    self._commerce().rpc('partner_finance',{'p_telegram_user_id':user_id})):
                self._revoke_partner_role(user_id)
                self._commerce_notice(user_id,'الحساب بصلاحيات زبون','الشراكة غير نشطة؛ لم يُنشأ طلب سحب جديد. راجع الإدارة لمستحقاتك السابقة.',message_id)
                return True
            if action=='commerce_withdraw_amount':
                amount=money_minor_input(text)
                with self._ui_lock:self._waiting_user_inputs[user_id]={'action':'commerce_withdraw_destination',
                    'amount':amount,'message_id':message_id,'expires':time.monotonic()+1800}
                self._commerce_notice(user_id,'جهة الاستلام','أرسل وسيلة الاستلام وبياناتها العامة، مثل اسم البنك ورقم الحساب أو عنوان المحفظة. لا ترسل كلمات مرور أو مفاتيح سرية.',message_id)
            elif action=='commerce_withdraw_destination':
                destination=text.strip()
                if not 3<=len(destination)<=500:raise ValueError('Withdrawal destination required')
                token=str(uuid4())
                with self._ui_lock:
                    reviews=getattr(self,'_withdrawal_reviews',{})
                    self._withdrawal_reviews={key:value for key,value in reviews.items() if value['expires']>time.monotonic()}
                    if len(self._withdrawal_reviews)>=128:raise ValueError('Withdrawal review limit reached')
                    self._withdrawal_reviews[token]={'user':user_id,'amount':waiting['amount'],'destination':destination,'expires':time.monotonic()+180}
                self._show_panel(user_id,views.Panel(f"<b>مراجعة طلب السحب</b>\n\nالمبلغ: {egp(waiting['amount'])} جنيه\n"
                    f"جهة الاستلام: {views.safe(destination,500)}\n\nالتأكيد يحجز المبلغ؛ الدفع يتم بعد مراجعة الإدارة.",
                    [[views.button('أؤكد طلب السحب','pf:confirm:'+token,'success')],[views.button('إلغاء','pf:home:0')]]),message_id)
            else:
                if user_id not in self.current_authorized_ids:raise ValueError('Payout recorder must be admin')
                parts=text.split(maxsplit=1)
                if len(parts)!=2 or not re.fullmatch(r'[a-z0-9_]{1,40}',parts[0]) or not 1<=len(parts[1])<=200:
                    raise ValueError('Payout provider and receipt required')
                incoming=positive_minor(waiting.get('_telegram_input_message_id'));token=str(uuid4())
                record=self._commerce().rpc('withdrawal_record',{'p_withdrawal_id':waiting['withdrawal_id']})
                if record['state']!='approved':raise ValueError('Approved withdrawal required')
                with self._ui_lock:
                    reviews=getattr(self,'_payout_reviews',{})
                    self._payout_reviews={key:value for key,value in reviews.items() if value['expires']>time.monotonic()}
                    if len(self._payout_reviews)>=128:raise ValueError('Payout review limit reached')
                    self._payout_reviews[token]={'actor':user_id,'withdrawal_id':record['id'],'provider':parts[0],
                        'receipt':parts[1],'proof':f'telegram-payout:{user_id}:{incoming}','expires':time.monotonic()+180}
                self._show_panel(user_id,views.Panel(f"<b>تأكيد دفع السحب</b>\n\nالشريك: <code>{record['telegram_user_id']}</code>\n"
                    f"المبلغ: {egp(record['amount_minor'])} جنيه\nالجهة: {views.safe(record['destination'],500)}\n"
                    f"الإيصال: {views.safe(parts[0]+':'+parts[1],250)}\n\nأكد فقط بعد دفع المبلغ فعليًا وتحققك من الإيصال.",
                    [[views.button('أؤكد الدفع وأسجل الإيصال','wa:paid:'+token,'success')],[views.button('إلغاء','wa:list:0')]]),message_id)
        except (CommerceUnavailable,OSError,ValueError,KeyError,TypeError):
            self._commerce_notice(user_id,'راجع البيانات','تعذر إكمال المراجعة. افتح الطلب للتحقق من حالته قبل طلب أو دفع مبلغ آخر.',waiting.get('message_id'))
        return True

    @_serial_user
    def _withdrawal_admin_callback(self,command,actor_id,message_id,callback):
        if actor_id not in self.current_authorized_ids:
            self._commerce_notice(actor_id,'غير مصرح','مراجعة السحوبات خاصة بالإدارة.',message_id);return
        try:
            parts=command.split(':')
            if len(parts)!=3:raise ValueError('Invalid withdrawal callback')
            action,value=parts[1:]
            if action=='notices':
                self._payout_notice_status(actor_id,value,message_id);return
            if action in {'paypartner','payconfirm','paycash'}:
                self._admin_partner_payout_callback(action,value,actor_id,message_id,callback);return
            if action in {'receipts','requestack'}:
                self._admin_receipt_action(action,value,actor_id,message_id,callback);return
            if action=='list':
                offset=int(value);records=self._commerce().rpc('list_withdrawals',{'p_offset':offset})
                rows=[[views.button(f"{record['telegram_user_id']} · {egp(record['amount_minor'])} جنيه · {record['state']}",
                    'wa:review:'+record['id'])] for record in records]
                if offset:rows.append([views.button('السابق','wa:list:'+str(max(0,offset-10)))])
                if len(records)==10:rows.append([views.button('التالي','wa:list:'+str(offset+10))])
                rows.append([views.button('المدفوعات وتأكيد الاستلام','wa:receipts:0')])
                rows.append([views.button('الرئيسية','home')])
                self._show_panel(actor_id,views.Panel('<b>طلبات سحب الأرباح</b>\n\n'+
                    ('اختر الطلب لمراجعته.' if records else 'لا توجد سحوبات معلقة.'),rows),message_id);return
            if action=='paid':
                review=getattr(self,'_payout_reviews',{}).get(str(UUID(value)))
                if not review or review['actor']!=actor_id or review['expires']<=time.monotonic():raise ValueError('Payout review expired or unowned')
                self._commerce().rpc('complete_withdrawal',{'p_withdrawal_id':review['withdrawal_id'],'p_actor_telegram_id':actor_id,
                    'p_provider':review['provider'],'p_reference':review['receipt'],'p_proof':review['proof']})
                self._show_panel(actor_id,views.Panel('<b>حُفظ إيصال الدفع</b>\n\n'
                    'انتقل المبلغ المحجوز إلى سجل المسحوب مرة واحدة. هذا تسجيل من الإدارة؛ اطلب الآن تأكيد استلام الشريك.',
                    [[views.button('طلب تأكيد الاستلام','wa:requestack:'+review['withdrawal_id'])],
                     [views.button('سجل التوثيق','wa:receipts:0')]]),message_id);return
            withdrawal_id=str(UUID(value));record=self._commerce().rpc('withdrawal_record',{'p_withdrawal_id':withdrawal_id})
            if action=='review':
                if record['state']=='paid':
                    self._show_panel(actor_id,receipt_panel(record,admin=True),message_id);return
                rows=[]
                if record['state']=='requested':rows.append([views.button('اعتماد الطلب','wa:approve:'+withdrawal_id,'success')])
                if record['state']=='approved':rows.append([views.button('تسجيل إيصال الدفع','wa:receipt:'+withdrawal_id)])
                if record['state'] in {'requested','approved'}:rows.append([views.button('رفض وإعادة المبلغ للمتاح','wa:reject:'+withdrawal_id)])
                rows.append([views.button('طلبات السحب','wa:list:0')])
                self._show_panel(actor_id,views.Panel(f"<b>مراجعة السحب</b>\n\nالشريك: <code>{record['telegram_user_id']}</code>\n"
                    f"المبلغ: {egp(record['amount_minor'])} جنيه\nالجهة: {views.safe(record['destination'],500)}\nالحالة: {record['state']}",rows),message_id)
            elif action in {'approve','reject'}:
                if not isinstance(callback.get('id'),str) or not callback['id'].strip():raise ValueError('Authenticated decision callback required')
                self._commerce().rpc('approve_withdrawal' if action=='approve' else 'reject_withdrawal',{
                    'p_withdrawal_id':withdrawal_id,'p_actor_telegram_id':actor_id,'p_reference':'telegram-withdrawal:'+callback['id']})
                self._commerce_notice(actor_id,'حُفظ قرار السحب','اعتمد الطلب؛ سجل الإيصال بعد الدفع الفعلي.' if action=='approve' else 'رُفض الطلب وأعيد المبلغ المحجوز إلى المتاح.',message_id)
            elif action=='receipt':
                if record['state']!='approved':raise ValueError('Approved withdrawal required')
                with self._ui_lock:self._waiting_user_inputs[actor_id]={'action':'commerce_payout_receipt',
                    'withdrawal_id':withdrawal_id,'message_id':message_id,'expires':time.monotonic()+1800}
                self._commerce_notice(actor_id,'إيصال الدفع الفعلي','أرسل نوع الوسيلة ثم مرجع الإيصال، مثل bank TX001 أو binance_pay TXID. التسجيل لا ينفذ تحويلًا خارجيًا.',message_id)
            else:raise ValueError('Unknown withdrawal action')
        except (CommerceUnavailable,OSError,ValueError,KeyError,TypeError):
            self._commerce_notice(actor_id,'تعذر التأكد من السحب','راجع الطلب وإيصال الدفع قبل تنفيذ تحويل آخر؛ إعادة التسجيل لا تكرر المبلغ.',message_id)
