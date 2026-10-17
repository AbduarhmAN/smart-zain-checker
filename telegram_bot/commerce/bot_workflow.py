"""Referral and partner controls for private Telegram conversations.

Administration is checked from the authenticated Telegram sender before RPCs.
Neither the legacy wallet nor paid-job execution is changed by these controls.
"""
from __future__ import annotations
from decimal import Decimal,InvalidOperation
import logging
import secrets
import time
import threading
from functools import wraps
from uuid import UUID
from telegram_bot import views
from .client import CommerceClient,CommerceUnavailable
from .referrals import referral_arguments,referral_link

logger = logging.getLogger('CommerceWorkflow')


REFERRAL_MESSAGES = {
    'attributed':'تم حفظ كود الإحالة لحسابك. ستظل تفاصيل العروض منفصلة عن مصدر الإحالة.',
    'already_attributed':'هذا الكود مرتبط بحسابك بالفعل.',
    'first_source_preserved':'حسابك مرتبط بإحالة سابقة؛ احتفظنا بمصدر الإحالة الأول.',
    'invalid_code':'الكود غير صالح أو غير مفعّل. راجعه مع الشريك.',
    'inactive_partner':'كود الشريك غير مفعّل حاليًا.',
    'self_referral':'لا يمكنك استخدام كود إحالتك على حسابك نفسه.',
    'prior_paid_service':'كود الإحالة متاح قبل أول خدمة مدفوعة فقط.',
    'user_suspended':'حسابك موقوف؛ تواصل مع الإدارة.',
    'code_exhausted':'بلغ كود الإحالة حد الاستخدام المتاح.'
}


def _serial_user(method):
    @wraps(method)
    def wrapped(self,command_or_chat,chat_or_text,*args,**kwargs):
        chat = command_or_chat if type(command_or_chat) is int else chat_or_text
        with self._ui_lock:
            if not hasattr(self,'_commerce_user_locks'):
                self._commerce_user_locks = {}
            lock = self._commerce_user_locks.setdefault(chat,threading.RLock())
        # Never hold the global UI lock over RPCs; different users stay independent.
        with lock:
            return method(self,command_or_chat,chat_or_text,*args,**kwargs)
    return wrapped


class CommerceBotMixin:
    def _commerce_cancel_pending(self,chat_id):
        with self._ui_lock:
            drafts = getattr(self,'_commerce_partner_drafts',{})
            self._commerce_partner_drafts = {k:v for k,v in drafts.items() if v['chat'] != chat_id}
            waiting = self._waiting_user_inputs.get(chat_id,{})
            if waiting.get('action','').startswith('commerce_'):
                self._waiting_user_inputs.pop(chat_id,None)

    def _commerce(self):
        if not hasattr(self,'_commerce_client'):
            self._commerce_client = CommerceClient()
        return self._commerce_client

    def _commerce_notice(self,chat_id,title,body,message_id=None):
        self._show_panel(chat_id,views.Panel(f'<b>{views.safe(title)}</b>\n\n{views.safe(body,1800)}',
                                          [[views.button('‹ الرئيسية','home')]]),message_id)

    def _commerce_failed(self,chat_id,message_id=None):
        self._commerce_notice(chat_id,'تعذر الاتصال','لم نتأكد من حفظ الإجراء. راجع الحالة أو أعد نفس الإجراء للتحقق دون تكرار عملية مالية.',message_id)

    def _apply_referral(self,chat_id,code,source,message_id=None,*,quiet=False):
        try:
            arguments = referral_arguments(chat_id,code,source=source)
            result = self._commerce().rpc('attribute_referral',arguments)
            if quiet:return result.get('status')
            message = REFERRAL_MESSAGES.get(result.get('status'),'تعذر تحديد حالة الإحالة؛ راجع الإدارة.')
            if chat_id in self.current_authorized_ids:
                self._commerce_notice(chat_id,'كود الإحالة',message,message_id)
            else:
                accepted=result.get('status') in {'attributed','already_attributed','first_source_preserved'}
                self._commerce_notice(chat_id,'الكود','تم قبول الكود.' if accepted else 'هذا الكود غير متاح لحسابك.',message_id)
        except ValueError:
            if quiet:return None
            self._commerce_notice(chat_id,'راجع الكود','أدخل الكود وحده كما وصلك من الشريك.',message_id)
        except CommerceUnavailable:
            if quiet:return None
            self._commerce_failed(chat_id,message_id)

    @_serial_user
    def _commerce_command(self,text,chat_id,is_admin,username='',*,message_id=None):
        parts = text.split()
        command = parts[0].split('@')[0].lower() if parts else ''
        if command == '/start' and len(parts) == 2:
            arg = parts[1]
            if arg.startswith('ref_'):
                self._apply_referral(chat_id,arg,'deep_link',quiet=True)
                self._dispatch_ui('home',chat_id,message_id)
                return True
            if arg.startswith(('ack_','rc_')):
                wid = arg.partition('_')[2]
                try:
                    from .withdrawal_receipts import receipt_panel
                    if is_admin:
                        record = self._commerce().rpc('withdrawal_record',{'p_withdrawal_id':str(UUID(wid))})
                        self._show_panel(chat_id,receipt_panel(record,admin=True),message_id)
                    else:
                        record = self._commerce().rpc('partner_withdrawal_receipt',{'p_withdrawal_id':str(UUID(wid)),'p_telegram_user_id':chat_id})
                        self._show_panel(chat_id,receipt_panel(record,admin=False),message_id)
                    return True
                except Exception as exc:
                    logger.warning('Deep link receipt lookup failed: %s', exc)
                    self._dispatch_ui('home',chat_id,message_id)
                    return True
        if command in {'/ref','/referral'}:
            if len(parts) == 2:
                self._apply_referral(chat_id,parts[1],'manual')
            else:
                self._commerce_callback('referral',chat_id,None)
            return True
        if command not in {'/partner_add','/partners'}:
            return False
        if not is_admin:
            self._commerce_notice(chat_id,'غير مصرح','إعداد الشركاء خاص بمدير البوت.')
            return True
        try:
            if command == '/partners':
                self._commerce_callback('cp:list:0',chat_id,None)
            elif len(parts) != 3 or not parts[1].isdigit() or int(parts[1]) <= 0:
                self._commerce_notice(chat_id,'إضافة شريك','اكتب /partner_add ثم رقم مستخدم Telegram ثم الكود، مثال: /partner_add 123456 PARTNER1')
            else:
                from .referrals import normalize_referral_code
                from ..payment_manager import PAYMENT_MGR
                target_raw = int(parts[1])
                found = PAYMENT_MGR.find_user_by_account_id(target_raw)
                resolved_tg_id = int(found[0]) if found else target_raw
                result = self._commerce().rpc('add_partner_with_notice',{'p_actor_telegram_id':chat_id,'p_telegram_user_id':resolved_tg_id,
                                                             'p_code':normalize_referral_code(parts[2])})
                pid = str(UUID(result['partner_id']))
                if result.get('partnership_ended'):
                    self._commerce_notice(chat_id,'الشراكة منتهية','هذا الحساب محفوظ في سجل الشركاء السابقين. إضافة الكود مجدداً لا تعيد تفعيله.',message_id)
                    return True
                rows=[[views.button('إعداد الشريك',f'cp:terms:{pid}')],[views.button('‹ الشركاء','cp:list:0')]]
                if result.get('notice_event_id'):rows.insert(0,[views.button('توثيق إشعار الإضافة','pl:view:'+result['notice_event_id'])])
                notice='\nحُفظ إشعار الإضافة للشريك وقناة التوثيق؛ راجع حالته للتأكد من الوصول.' if result.get('notice_event_id') else ''
                self._show_panel(chat_id,views.Panel(f"<b>تم حفظ الشريك</b>\nالكود: <code>{views.safe(result['code'])}</code>\n\nحدد النسبة والاستحقاق لتفعيل الكود."+notice,rows))
        except (ValueError,KeyError,TypeError):
            self._commerce_notice(chat_id,'راجع البيانات','تأكد من رقم Telegram والكود.')
        except CommerceUnavailable:
            self._commerce_failed(chat_id)
        return True

    @_serial_user
    def _commerce_callback(self,command,chat_id,message_id):
        if command == 'referral':
            if chat_id not in self.current_authorized_ids:
                self._dispatch_ui('home',chat_id,message_id)
                return
            try:
                profile = self._commerce().rpc('referral_profile',{'p_telegram_user_id':chat_id})
                if profile.get('referral_code'):
                    body = f"كود الإحالة المحفوظ: {profile['referral_code']}. مصدر الإحالة الأول محفوظ لحسابك."
                    self._commerce_notice(chat_id,'إحالتك',body,message_id)
                    return
                if profile.get('registered') and not profile.get('eligible'):
                    self._commerce_notice(chat_id,'الإحالة','الإحالة الجديدة متاحة قبل أول خدمة مدفوعة فقط.',message_id)
                    return
                with self._ui_lock:
                    self._waiting_user_inputs[chat_id] = {'action':'commerce_referral','message_id':message_id,'expires':time.monotonic()+1800}
                self._commerce_notice(chat_id,'كود الإحالة','هذا كود يقدمه شريك المنصة لربط حسابك به. إن لم يرسل لك أحد كودًا فلا تحتاج إدخال شيء. الشحن والخصومات لا يغيّران الإحالة. أرسل الكود إذا كان لديك واحد.',message_id)
            except CommerceUnavailable:
                self._commerce_notice(chat_id,'تعذر قراءة الإحالة','لم نتمكن من عرض بيانات الإحالة الآن. لم نرسل إجراءً لحفظ أو تغيير الإحالة؛ محفظتك وطلباتك لا تتأثر بهذه القراءة.',message_id)
            return
        if not command.startswith('cp:'):
            return
        if chat_id not in self.current_authorized_ids:
            self._commerce_notice(chat_id,'غير مصرح','إعداد الشركاء خاص بمدير البوت.',message_id)
            return
        try:
            parts = command.split(':')
            action = parts[1]
            if action == 'list':
                self._commerce_cancel_pending(chat_id)
                offset = int(parts[2])
                if offset < 0:
                    raise ValueError('offset')
                partners = self._commerce().rpc('list_partners',{'p_offset':offset})
                bot_name = getattr(self,'_commerce_bot_name',None)
                if bot_name is None:
                    bot_result = self._api_call('getMe',{}) or {}
                    bot_name = (bot_result.get('result') or {}).get('username','')
                    if bot_name:
                        self._commerce_bot_name = bot_name
                lines = ['<b>الشركاء والإحالات</b>','لإضافة شريك: /partner_add رقم_المستخدم الكود']
                rows = []
                for partner in partners:
                    pid = str(UUID(partner['id']))
                    code = views.safe(partner['code'])
                    state = 'مفعّل' if partner['state'] == 'active' else 'غير مفعّل'
                    lines.append(f"\n<code>{code}</code> · {state} · Telegram <code>{views.safe(partner['telegram_user_id'])}</code>")
                    if bot_name:
                        url = referral_link(bot_name,partner['code'])
                        lines.append(f'<a href="{url}">رابط إحالة {code}</a>')
                    rate = partner.get('commission_basis_points')
                    if rate is not None:
                        mode = partner.get('entitlement_mode')
                        limit = partner.get('entitlement_checks') if mode == 'checks' else partner.get('entitlement_duration_seconds')
                        label = 'دائمًا' if mode == 'forever' else f'{limit} تشييك' if mode == 'checks' else f'{int(limit or 0)//86400} يوم'
                        lines.append(f'النسبة: {rate/100:g}% · الاستحقاق: {label}')
                    rows.append([views.button(str(partner['code']),'pf:partner:'+str(partner['telegram_user_id'])),
                        views.button('صرف الأرباح','wa:paypartner:'+str(partner['telegram_user_id']),'primary')])
                    rows.append([views.button('⚙️ شروط الشريك',f'cp:terms:{pid}'),
                        views.button('إنهاء الشراكة','pl:remove:'+pid)])
                if offset:
                    rows.append([views.button('‹ السابق',f'cp:list:{max(0,offset-10)}')])
                if len(partners) == 10:
                    rows.append([views.button('التالي ›',f'cp:list:{offset+10}')])
                rows.append([views.button('📊 الأرباح','pf:admin:0'),views.button('🕘 الشركاء السابقون','pl:ended:0')])
                rows.append([views.button('‹ قسم الشركاء','menu:partners'),views.button('🏠 الرئيسية','home')])
                self._show_panel(chat_id,views.Panel('\n'.join(lines),rows),message_id)
            elif action == 'terms':
                self._commerce_cancel_pending(chat_id)
                partner_id = str(UUID(parts[2]))
                with self._ui_lock:
                    self._waiting_user_inputs[chat_id] = {'action':'commerce_partner_rate','partner_id':partner_id,
                        'message_id':message_id,'expires':time.monotonic()+1800}
                self._commerce_notice(chat_id,'نسبة الشريك','أرسل نسبة الشريك من 0 إلى 100، مثال: 30. بعدها تختار نوع الاستحقاق.',message_id)
            elif action == 'mode':
                token,mode = parts[2],parts[3]
                draft = self._partner_draft(token,chat_id)
                if mode not in {'checks','duration','forever'}:
                    raise ValueError('mode')
                with self._ui_lock:
                    draft['mode'] = mode
                    draft['limit'] = None
                    draft['confirmed'] = False
                if mode == 'forever':
                    self._partner_confirmation(token,chat_id,message_id)
                else:
                    with self._ui_lock:
                        self._waiting_user_inputs[chat_id] = {'action':'commerce_partner_limit','token':token,'message_id':message_id,'expires':draft['expires']}
                    self._commerce_notice(chat_id,'حد الاستحقاق','أرسل عدد التشييكات المستحقة.' if mode == 'checks' else 'أرسل مدة الاستحقاق بالأيام، كعدد صحيح.',message_id)
            elif action == 'save':
                token = parts[2]
                draft = self._partner_draft(token,chat_id)
                if not draft.get('confirmed'):
                    raise ValueError('Missing reviewed terms')
                arguments = {'p_partner_id':draft['partner_id'],'p_actor_telegram_id':chat_id,
                             'p_basis_points':draft['basis_points'],'p_mode':draft['mode'],
                             'p_limit':draft['limit'],'p_activate':True}
                result=self._commerce().rpc('configure_partner',arguments)
                if isinstance(result,dict) and result.get('state')=='active':
                    try:self._sync_partner_menus()
                    except (CommerceUnavailable,OSError,ValueError,KeyError,TypeError):pass
                rows=[[views.button('عرض الشركاء','cp:list:0')],[views.button('‹ الرئيسية','home')]]
                if isinstance(result,dict) and result.get('notice_event_id'):
                    rows.insert(0,[views.button('توثيق إشعار التفعيل','pl:view:'+result['notice_event_id'])])
                notice='\nحُفظ إشعار التفعيل للشريك وقناة التوثيق.' if isinstance(result,dict) and result.get('notice_event_id') else ''
                self._show_panel(chat_id,views.Panel('<b>تم حفظ الشروط وتفعيل كود الشريك</b>'+notice,rows),message_id)
            else:
                raise ValueError('unknown action')
        except (ValueError,IndexError,KeyError,TypeError,InvalidOperation):
            self._commerce_notice(chat_id,'راجع الإعداد','انتهت المراجعة أو البيانات غير صحيحة. افتح إعداد الشريك من جديد.',message_id)
        except CommerceUnavailable:
            self._commerce_failed(chat_id,message_id)

    def _partner_draft(self,token,chat_id):
        with self._ui_lock:
            draft = getattr(self,'_commerce_partner_drafts',{}).get(token)
            if not draft or draft['chat'] != chat_id or draft['expires'] < time.monotonic():
                raise ValueError('Partner draft unavailable')
            return draft

    def _partner_confirmation(self,token,chat_id,message_id):
        draft = self._partner_draft(token,chat_id)
        mode = draft['mode']
        label = 'دائمًا' if mode == 'forever' else f"{draft['limit']} تشييك" if mode == 'checks' else f"{draft['limit']//86400} يوم"
        with self._ui_lock:
            draft['confirmed'] = True
        text = f"<b>مراجعة شروط الشريك</b>\n\nالنسبة: {draft['basis_points']/100:g}%\nالاستحقاق: {label}\n\nالحفظ يفعّل كود الإحالة. تُحفظ نسخة الشروط ولا تُغيّر أسعار الطلبات القديمة."
        self._show_panel(chat_id,views.Panel(text,[[views.button('حفظ وتفعيل',f'cp:save:{token}','success')],
                                               [views.button('إلغاء','cp:list:0')]]),message_id)

    @_serial_user
    def _commerce_text_input(self,chat_id,text,waiting):
        action = waiting.get('action','')
        if not action.startswith('commerce_'):
            return False
        message_id = waiting.get('message_id')
        try:
            if waiting.get('expires',0) < time.monotonic():
                raise ValueError('Expired prompt')
            if action == 'commerce_referral':
                self._apply_referral(chat_id,text,'manual',message_id)
                return True
            if chat_id not in self.current_authorized_ids:
                raise ValueError('Not administrator')
            if action == 'commerce_partner_rate':
                rate = Decimal(text.strip())
                if not rate.is_finite() or not 0 <= rate <= 100 or rate*100 != (rate*100).to_integral_value():
                    raise ValueError('Invalid percentage')
                token = secrets.token_hex(6)
                with self._ui_lock:
                    existing = getattr(self,'_commerce_partner_drafts',{})
                    # Bound in-memory reviews; expired entries hold no financial data.
                    self._commerce_partner_drafts = {k:v for k,v in existing.items() if v['expires'] > time.monotonic() and v['chat'] != chat_id}
                    if len(self._commerce_partner_drafts) >= 128:
                        raise ValueError('Too many open reviews')
                    self._commerce_partner_drafts[token] = {'chat':chat_id,'partner_id':waiting['partner_id'],
                        'basis_points':int(rate*100),'expires':time.monotonic()+1800}
                rows = [[views.button('بعدد التشييكات',f'cp:mode:{token}:checks')],
                        [views.button('لمدة محددة',f'cp:mode:{token}:duration'),views.button('دائمًا',f'cp:mode:{token}:forever')],
                        [views.button('إلغاء','cp:list:0')]]
                self._show_panel(chat_id,views.Panel('<b>كيف تحدد استحقاق الشريك؟</b>\nاختر العدد أو المدة أو دائمًا.',rows),message_id)
            elif action == 'commerce_partner_limit':
                draft = self._partner_draft(waiting['token'],chat_id)
                if not text.strip().isdigit():
                    raise ValueError('Integer limit required')
                value = int(text.strip())
                limit = value*86400 if draft['mode'] == 'duration' else value
                if not 0 < limit <= 9223372036854775807:
                    raise ValueError('Invalid limit')
                with self._ui_lock:
                    draft['limit'] = limit
                self._partner_confirmation(waiting['token'],chat_id,message_id)
            else:
                raise ValueError('Unknown prompt')
        except (ValueError,KeyError,InvalidOperation):
            self._commerce_notice(chat_id,'راجع الإدخال','البيانات غير صحيحة أو انتهت المراجعة. افتح نفس الإجراء من القائمة مجددًا.',message_id)
        return True
