"""Administration-initiated payout records; bank transfers remain external."""
from decimal import InvalidOperation,localcontext
import re
import time
from uuid import UUID, uuid4
from telegram_bot import views
from telegram_bot.authorization import admin_ids
from .checkout import egp
from .client import CommerceUnavailable
from .bot_workflow import _serial_user
from .finance import money_minor_input, positive_minor
from .withdrawal_receipts import receipt_panel

ADMIN_PAYOUT_INPUTS = {'commerce_admin_payout_amount', 'commerce_admin_payout_destination',
                       'commerce_admin_payout_receipt'}


class AdminPartnerPayoutMixin:
    def _cancel_admin_partner_payout(self, actor):
        with self._ui_lock:
            waiting=getattr(self,'_waiting_user_inputs',{})
            if waiting.get(actor, {}).get('action') in ADMIN_PAYOUT_INPUTS:
                waiting.pop(actor, None)
            reviews = getattr(self, '_admin_partner_payout_reviews', {})
            self._admin_partner_payout_reviews = {k: v for k, v in reviews.items()
                if v['actor'] != actor and v['expires'] > time.monotonic()}

    def _admin_partner_payout_callback(self, action, value, actor, message_id, callback):
        if actor not in admin_ids(self):
            raise ValueError('Payout administrator required')
        if action == 'paypartner':
            self._cancel_admin_partner_payout(actor)
            partner = positive_minor(int(value))
            profile = self._commerce().rpc('partner_finance', {'p_telegram_user_id': partner})
            if not profile.get('is_partner'):
                self._commerce_notice(actor, 'الحساب ليس شريكًا', 'اختر شريكًا من قائمة الشركاء.', message_id)
                return
            with self._ui_lock:
                self._waiting_user_inputs[actor] = {'action': 'commerce_admin_payout_amount',
                    'partner': partner, 'payout_token':str(uuid4()),'message_id': message_id, 'expires': time.monotonic() + 1800}
            self._commerce_notice(actor, 'صرف أرباح للشريك',
                f"الشريك: {partner}\nرصيد الحساب: {egp(profile.get('account_balance_minor',profile['available_minor']))} جنيه.\n"
                f"المتاح للصرف: {egp(profile.get('withdrawable_minor',max(0,profile['available_minor'])))} جنيه.\n"
                'إذا زاد الصرف على المتاح فستظهر الزيادة دفعة مقدمة على الأرباح التالية، مع مراجعة السبب والرصيد السالب.\n'
                'أرسل المبلغ الذي حوّلته له بالفعل. ستراجع جهة التحويل والإيصال قبل التسجيل والإرسال.', message_id)
            return
        if action=='paycash':
            token=str(UUID(value))
            waiting=self._waiting_user_inputs.get(actor)
            if (not waiting or waiting.get('payout_token')!=token or waiting.get('expires',0)<=time.monotonic()
                    or waiting.get('action') not in {'commerce_admin_payout_destination','commerce_admin_payout_receipt'}):
                raise ValueError('Matching active cash payout review required')
            if not isinstance(callback.get('id'),str) or not callback['id'].strip():
                raise ValueError('Native cash selection required')
            review={'actor':actor,'partner':positive_minor(waiting['partner']),'amount':waiting['amount'],
                'destination':'نقدًا باليد','provider':'cash','receipt':'CASH-'+UUID(token).hex.upper(),
                'proof':'telegram-admin-cash-payout:'+str(actor)+':'+callback['id'],'expires':time.monotonic()+180}
            self._store_admin_payout_review(token,review,message_id)
            with self._ui_lock:self._waiting_user_inputs.pop(actor,None)
            return
        if action != 'payconfirm':
            raise ValueError('Unknown payout action')
        token = str(UUID(value))
        review = getattr(self, '_admin_partner_payout_reviews', {}).get(token)
        if not review or review['actor'] != actor or review['expires'] <= time.monotonic():
            raise ValueError('Payout review expired or unowned')
        if not isinstance(callback.get('id'), str) or not callback['id'].strip():
            raise ValueError('Native payout confirmation required')
        arguments={
            'p_actor_telegram_id': actor, 'p_partner_telegram_id': review['partner'],
            'p_amount_minor': review['amount'], 'p_destination': review['destination'],
            'p_provider': review['provider'], 'p_reference': review['receipt'],
            'p_proof': review['proof'], 'p_key': 'admin-payout:' + token}
        advance=review.get('advance_minor',0)>0
        if advance:
            arguments.update(p_reason=review['advance_reason'],p_expected_available_minor=review['available_before'])
        try:
            record = self._commerce().rpc('record_partner_advance_payout' if advance else 'record_partner_payout',arguments)
        except CommerceUnavailable as exc:
            if exc.reason=='partner_balance_changed':
                self._commerce_notice(actor,'تغير رصيد الشريك','افتح مراجعة صرف جديدة لمراجعة الزيادة والرصيد الحالي قبل تأكيدها. لم يُسجّل صرف من هذا التأكيد.',message_id)
                return
            raise
        if record.get('state') == 'insufficient_balance':
            self._commerce_notice(actor, 'الرصيد المتاح غير كافٍ',
                'لم يُسجّل صرف جديد. المتاح الآن: ' + egp(record['available_minor']) +
                ' جنيه. راجع السحوبات المحجوزة وبيانات التحويل.', message_id)
            return
        receipt = record.get('receipt') or {}
        if (record.get('state') != 'paid' or record.get('telegram_user_id') != review['partner']
                or record.get('amount_minor') != review['amount'] or not receipt.get('id')
                or receipt.get('recipient_telegram_id') != review['partner']):
            raise ValueError('Saved payout differs from review')
        if advance:
            saved=record.get('advance') or {}
            if (saved.get('reason')!=review['advance_reason'] or saved.get('amount_minor',0)<=0
                    or (record.get('replayed') is not True and (saved.get('amount_minor')!=review['advance_minor']
                        or saved.get('available_after_minor')!=review['available_after']))):
                raise ValueError('Saved advance differs from reviewed amount and reason')
        # Receipt triggers save both partner and channel notices in the same
        # transaction. Only the durable sender delivers them after commit.
        self._wake_payout_notifications()
        panel = receipt_panel(record, admin=True)
        panel.rows.insert(0,[views.button('حالة إشعارات الشريك والقناة','wa:notices:'+record['id'])])
        notice = ('حُفظ صرف الأرباح وإشعارات الشريك والقناة. تابع وصول الرسائل من حالة الإشعارات؛ بانتظار تأكيد الشريك.'
                  if receipt.get('state')=='requested' and record.get('replayed') is not True else 'هذا الصرف مسجّل سابقًا. راجع المبلغ والسبب وحالة تأكيد الشريك أدناه.')
        if receipt.get('state')!='requested':notice='هذا الصرف مسجّل سابقًا، ورد الشريك محفوظ في السجل.'
        self._show_panel(actor, views.Panel(notice + '\n\n' + panel.text, panel.rows), message_id)

    def _store_admin_payout_review(self,token,review,message_id):
        profile=self._commerce().rpc('partner_finance',{'p_telegram_user_id':review['partner']})
        if not profile.get('is_partner'):raise ValueError('Partner account required')
        with localcontext() as context:
            context.prec=100
            available=profile['available_minor']
            withdrawable=profile.get('withdrawable_minor',max(0,available))
            review.update(available_before=available,available_after=available-review['amount'],
                account_after=profile.get('account_balance_minor',available+profile.get('reserved_minor',0))-review['amount'],
                advance_minor=max(0,review['amount']-withdrawable),earned_minor=min(review['amount'],withdrawable))
        if review['advance_minor']>0:review['advance_reason']='دفعة مقدمة على أرباح الشريك القادمة'
        with self._ui_lock:
            reviews=getattr(self,'_admin_partner_payout_reviews',{})
            self._admin_partner_payout_reviews={k:v for k,v in reviews.items() if v['expires']>time.monotonic()}
            if len(self._admin_partner_payout_reviews)>=128:raise ValueError('Payout review limit reached')
            self._admin_partner_payout_reviews[token]=review
        cash=review['provider']=='cash'
        extra=('\nمن الأرباح المتاحة: '+egp(review['earned_minor'])+' جنيه'
               '\nالزيادة كدفعة مقدمة: <b>'+egp(review['advance_minor'])+' جنيه</b>'
               '\nالسبب: '+views.safe(review['advance_reason'])+
               '\nرصيد الحساب بعد الصرف: <b>'+egp(review['account_after'])+' جنيه</b>'
               '\nتُغطّى الزيادة من الأرباح التالية.\n') if review['advance_minor']>0 else ''
        self._show_panel(review['actor'],views.Panel(
            '<b>مراجعة صرف أرباح الشريك</b>\n\n'
            f"الشريك: <code>{review['partner']}</code>\nالمبلغ: {egp(review['amount'])} جنيه\n"
            f"جهة التسليم: {views.safe(review['destination'],500)}\n"
            +('مرجع توثيق داخلي: ' if cash else 'الإيصال: ')
            +'<code>'+views.safe(review['provider']+':'+review['receipt'],250)+'</code>\n\n'
            +('هذا صرف نقدي؛ لا يلزم رقم عملية خارجي. ' if cash else '')
            +extra+'أكد فقط بعد تسليم المبلغ فعليًا. سيُوثق الصرف وتُرسل له رسالة بالمبلغ والسبب لتأكيد الاستلام أو الاعتراض.',
            [[views.button('أؤكد الصرف وتسجيل الزيادة دفعة مقدمة' if review['advance_minor']>0 else 'تم صرف الأرباح، تسجيل وإشعار الشريك','wa:payconfirm:'+token,'primary')],
             [views.button('إلغاء','pf:admin:0')]]),message_id)

    def _cash_payout_rows(self,waiting):
        return [[views.button('نقدًا باليد، دون رقم عملية','wa:paycash:'+waiting['payout_token'])],
                [views.button('إلغاء','pf:admin:0')]]

    @_serial_user
    def _admin_partner_payout_text(self, actor, text, waiting):
        try:
            if actor not in admin_ids(self):
                raise PermissionError('Payout administrator required')
            if waiting.get('expires', 0) <= time.monotonic():
                raise ValueError('Expired payout prompt')
            UUID(waiting['payout_token'])
            action = waiting['action']; message_id = waiting.get('message_id')
            partner = positive_minor(waiting['partner'])
            next_waiting = dict(waiting)
            if action == 'commerce_admin_payout_amount':
                next_waiting.update(action='commerce_admin_payout_destination', amount=money_minor_input(text))
                with self._ui_lock: self._waiting_user_inputs[actor] = next_waiting
                self._show_panel(actor,views.Panel('<b>جهة تسليم أرباح الشريك</b>\n\n'
                    'أرسل جهة التحويل، أو اختر نقدًا باليد دون رقم عملية.',self._cash_payout_rows(next_waiting)),message_id)
            elif action == 'commerce_admin_payout_destination':
                destination = text.strip()
                if not 3 <= len(destination) <= 500:
                    raise ValueError('Payout destination required')
                next_waiting.update(action='commerce_admin_payout_receipt', destination=destination)
                with self._ui_lock: self._waiting_user_inputs[actor] = next_waiting
                self._show_panel(actor,views.Panel('<b>إيصال صرف الأرباح</b>\n\n'
                    'للتحويل أرسل وسيلة الدفع ثم مرجع الإيصال، مثال: bank TX001. '
                    'إذا سلّمت المبلغ نقدًا اختر الزر أدناه؛ لا تحتاج اختراع رقم عملية.',self._cash_payout_rows(next_waiting)),message_id)
            elif action == 'commerce_admin_payout_receipt':
                parts = text.split(maxsplit=1)
                if len(parts) != 2 or not re.fullmatch(r'[a-z0-9_]{1,40}', parts[0]) or not 1 <= len(parts[1].strip()) <= 200:
                    raise ValueError('Payout provider and receipt required')
                incoming = positive_minor(waiting.get('_telegram_input_message_id'))
                token = waiting['payout_token']
                review = {'actor': actor, 'partner': partner, 'amount': waiting['amount'],
                    'destination': waiting['destination'], 'provider': parts[0], 'receipt': parts[1].strip(),
                    'proof': f'telegram-admin-payout:{actor}:{incoming}', 'expires': time.monotonic() + 180}
                self._store_admin_payout_review(token,review,message_id)
            else:
                raise ValueError('Unknown payout input')
        except PermissionError:
            self._commerce_notice(actor, 'غير مصرح', 'صرف أرباح الشركاء خاص بالإدارة.', waiting.get('message_id'))
        except (CommerceUnavailable, OSError, ValueError, KeyError, TypeError, InvalidOperation):
            if actor in admin_ids(self) and waiting.get('expires',0)>time.monotonic() and waiting.get('payout_token'):
                with self._ui_lock:self._waiting_user_inputs[actor]=waiting
                messages={'commerce_admin_payout_amount':'أرسل مبلغًا موجبًا بالجنيه مثل 10 أو 0.00001.',
                    'commerce_admin_payout_destination':'أرسل جهة التحويل من 3 إلى 500 حرف، أو اختر نقدًا باليد.',
                    'commerce_admin_payout_receipt':'للتحويل أرسل الوسيلة ومرجع الإيصال مثل bank TX001، أو اختر نقدًا باليد دون رقم عملية.'}
                rows=self._cash_payout_rows(waiting) if waiting.get('action')!='commerce_admin_payout_amount' else [[views.button('إلغاء','pf:admin:0')]]
                self._show_panel(actor,views.Panel('<b>راجع هذا الحقل</b>\n\n'+messages.get(waiting.get('action'),'أعد فتح مراجعة الصرف.')+
                    '\nالبيانات السابقة محفوظة. لم يُسجّل صرف من هذه الخطوة.',rows),waiting.get('message_id'))
            else:self._commerce_notice(actor,'انتهت مراجعة الصرف','أعد فتح صرف أرباح الشريك للمراجعة؛ لم يُسجّل صرف من هذه الخطوة.',waiting.get('message_id'))
        return True
