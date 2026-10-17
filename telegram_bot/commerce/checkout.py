"""Private Telegram checkout: frozen input, explicit consent and funded enqueue."""
from __future__ import annotations
from decimal import Decimal
from domain import decimal_json as json
import os
from pathlib import Path
import re
import secrets
import shutil
import time
from uuid import UUID
from domain.workbook import load_fast_workbook as load_workbook
from telegram_bot import views
from .bot_workflow import _serial_user
from .bridge import file_sha256
from .client import CommerceUnavailable
from .finance import FinanceService
from .order_input import quote_rows
from .payment_providers import CryptoInvoiceAdapter
from .payment_quotes import PaymentQuote, freeze_quote
from .payments import PaymentService
from .rates import asset_egp_rate


def egp(minor):
    from domain.decimal_money import egp_from_minor
    return egp_from_minor(minor)


def snapshot_input(root, source):
    source = Path(source)
    digest = file_sha256(source)
    storage = Path(root) / '.commerce' / 'inputs'
    storage.mkdir(parents=True, exist_ok=True)
    target = storage / (digest + '.xlsx')
    if not target.exists():
        pending = storage / (digest + '.' + secrets.token_hex(6) + '.pending')
        with source.open('rb') as stream, pending.open('xb') as output:
            shutil.copyfileobj(stream, output, 256 * 1024)
            output.flush(); os.fsync(output.fileno())
        if file_sha256(pending) != digest:
            raise ValueError('Input changed while capturing checkout')
        try: os.link(pending, target)
        except FileExistsError: pass
    if file_sha256(target) != digest:
        raise ValueError('Conflicting frozen input')
    return target, digest


class CommerceCheckoutMixin:
    def _commerce_legacy_payment(self,token):
        from telegram_bot.payment_manager import PAYMENT_MGR
        return bool(PAYMENT_MGR.get_pending_payment(token))

    def _commerce_checkout_enabled(self, user_id):
        if os.environ.get('ZAIN_COMMERCE_CHECKOUT', '').strip() == '1':
            return True
        pilot = os.environ.get('ZAIN_COMMERCE_PILOT_IDS', '').split(',')
        if str(user_id) in {value.strip() for value in pilot if value.strip().isdigit()}:return True
        root=getattr(self,'project_root',Path('.'))
        if (root/'.commerce/topups.sqlite').exists():
            from .topup_store import TopupStore
            if TopupStore(root).is_owner(user_id):return True
        policy=root/'.commerce/runtime-policy.json'
        try:
            value=json.loads(policy.read_text(encoding='utf-8')) if policy.exists() else {}
        except (OSError,ValueError):value={}
        if isinstance(value,dict) and value.get('checkout_enabled') is True:return True
        # Active partners always use the approval-gated backend, even when
        # legacy checkout remains enabled for ordinary customers.
        try:
            context=self._commerce().rpc('partner_sheet_context',{'p_telegram_user_id':user_id})
            from .partner_roles import active_partner
            enabled=active_partner(context)
            if not enabled and hasattr(self,'_revoke_partner_role'):self._revoke_partner_role(user_id)
            return enabled
        except Exception:
            return False

    def _checkout_save(self):
        if self._persist_sheet_drafts() is not True:
            raise OSError('Checkout must be durable before external operations')

    def _checkout_draft(self, token, user_id):
        if not re.fullmatch(r'[0-9a-f]{12}', token):
            raise ValueError('Invalid checkout token')
        with self._ui_lock:
            draft = self._sheet_drafts.get(token)
        if not draft or draft.get('chat') != user_id or not draft.get('validation', {}).get('ok'):
            raise ValueError('Unknown or unowned checkout')
        if not draft.get('commerce_request_key') and draft.get('expires', 0) <= time.monotonic():
            raise ValueError('Expired sheet review')
        if Path(draft['path']).resolve().parent != self.project_root.resolve():
            raise ValueError('Checkout source must be a received workspace file')
        if not draft.get('commerce_input'):
            from telegram_bot.sheet_workflow import file_version
            if tuple(draft.get('version',())) != file_version(draft['path']):
                raise ValueError('Input changed since review')
        if draft.get('commerce_input'):
            path = Path(draft['commerce_input']).resolve()
            expected = (self.project_root / '.commerce' / 'inputs' /
                        (draft['commerce_source']['file_sha256'] + '.xlsx')).resolve()
            if path != expected or file_sha256(path) != draft['commerce_source']['file_sha256']:
                raise ValueError('Frozen checkout input changed')
            source = draft['commerce_source']
            if (source['mapping'] != draft['mapping'] or source['mode'] != draft['mode']
                    or source['amount_target'] != draft['amount_target'] or source['sheet_index'] != draft['sheet_index']):
                raise ValueError('Checkout selection changed after pricing')
        return draft

    def _checkout_state(self, draft, user_id):
        state = self._commerce().rpc('checkout_state', {
            'p_order_id': str(UUID(draft['commerce_order_id'])), 'p_telegram_user_id': user_id})
        if not isinstance(state, dict) or state.get('order_id') != draft['commerce_order_id']:
            raise ValueError('Checkout state is unavailable')
        if any(state.get('source_snapshot', {}).get(key) != value for key, value in draft['commerce_source'].items()):
            raise ValueError('Server checkout differs from frozen input')
        if state['requested_checks'] != len(draft['commerce_rows']):
            raise ValueError('Server checkout differs from executable checks')
        return state

    @_serial_user
    def _commerce_start_checkout(self, token, user_id, message_id):
        try:
            draft = self._checkout_draft(token, user_id)
            if not self._commerce_checkout_enabled(user_id) and not draft.get('commerce_request_key'):
                raise ValueError('Commerce checkout is not enabled')
            if not draft.get('commerce_request_key') and self._commerce_legacy_payment(token):
                raise ValueError('A legacy payment attempt must keep its original funding path')
            if draft.get('invoice_id') or draft.get('invoice') or draft.get('promo_applied') or draft.get('job_id'):
                raise ValueError('An existing legacy payment needs its original payment screen')
            if not draft.get('commerce_request_key'):
                if hasattr(self,'_history_existing_paid_input') and self._history_existing_paid_input(token,draft,user_id,message_id):return
                path, digest = snapshot_input(self.project_root, draft['path'])
                workbook = load_workbook(path, read_only=True, data_only=True)
                try:
                    rows = quote_rows(workbook.worksheets[draft['sheet_index']], draft['mapping'],
                                      draft['mode'], draft['amount_target'])
                finally: workbook.close()
                if not draft.get('admin') and len(rows)>4000:raise ValueError('Customer sheets are limited to 4000 checks')
                source = {'file_sha256': digest, 'sheet_index': draft['sheet_index'],
                          'mapping': json.loads(json.dumps(draft['mapping'])),
                          'mode': draft['mode'], 'amount_target': draft['amount_target']}
                for key in ('original_telegram_file_id','original_telegram_file_unique_id','original_filename'):
                    if draft.get(key):source[key]=draft[key]
                with self._ui_lock:
                    draft.update(commerce_request_key=f'telegram:{user_id}:{token}', commerce_input=str(path),
                                 commerce_source=source, commerce_rows=rows, expires=time.monotonic() + 86400)
                self._checkout_save()
            if not draft.get('commerce_order_id'):
                self._checkout_save()
                result = self._commerce().rpc('create_order', {'p_telegram_user_id': user_id,
                    'p_request_key': draft['commerce_request_key'], 'p_rows': draft['commerce_rows'],
                    'p_source': draft['commerce_source']})
                draft['commerce_order_id'] = str(UUID(result['order_id']))
                self._checkout_save()
            state = self._checkout_state(draft, user_id)
            if getattr(self,'_partner_sheet_blocked',lambda *args:False)(state,token,user_id,message_id):return
            if state.get('order_state')=='cancelled':
                self._commerce_notice(user_id,'الطلب ملغى','أُعيدت حصته المحجوزة من العرض. أرسل الملف من جديد لإنشاء سعر جديد.',message_id)
                return
            if state.get('order_state')=='closed':
                self._commerce_notice(user_id,'الطلب مكتمل',f"رقم الطلب: {draft['commerce_order_id']}",message_id)
                return
            if state.get('reservation_state') == 'held':
                self._checkout_enqueue(token, draft, user_id, state)
                self._checkout_success(draft, user_id, message_id)
                return
            text = (f"<b>مراجعة الطلب</b>\n\nالملف: {views.safe(draft['sheet_name'])}\n"
                    f"عدد التشييكات: {state['requested_checks']}\nالمبلغ: <b>{egp(state['quoted_minor'])} جنيه مصري</b>\n"
                    f"رقم الطلب: <code>{draft['commerce_order_id']}</code>\n\n"
                    'تُحسب النتيجة الصحيحة بالسعر المعروض بعد الخصم، بما فيها عدم وجود نتيجة. '
                    'قيمة الأعطال النهائية تُرد إلى محفظتك بعد تسليم الملف. '
                    'حصة الرصيد الترويجي تعود ترويجيًا وتخضع لصلاحية العرض. '
                    'باختيار أحد أزرار الموافقة أدناه توافق على هذا الاسترداد للمحفظة.')
            pricing=state['pricing_snapshot']
            if state.get('partner_sheet'):
                text=(f"<b>فحص الشريك · شيت معتمد</b>\n\nالملف: {views.safe(draft['sheet_name'])}\n"
                      f"عدد التشييكات: {state['requested_checks']}\nالسعر الثابت للشيت كاملاً: <b>{egp(state['quoted_minor'])} جنيه</b>\n"
                      f"رقم الطلب: <code>{draft['commerce_order_id']}</code>\n\n"
                      'اعتمدت الإدارة هذا الشيت بهذا السعر. لا تُحتسب عمولة إحالة على الطلب. '
                      'تُرد حصة الفحوص غير المنجزة أو المتعذرة نهائيًا إلى محفظتك بعد تسليم الملف؛ '
                      'توزع قيمة الشيت على الفحوص لتحديد حصة الاسترداد فقط. '
                      'باختيار الدفع توافق على سياسة الاسترداد للمحفظة.')
            if pricing.get('pricing_version'):
                text+='\n\nسعر العميل الأساسي: '+egp(pricing['base_minor'])+' جنيه'
                text+='\nأرقام الحسابات: '+str(pricing['account_count'])
                text+='\nأرقام الخدمة ضمن أول '+str(pricing['service_threshold'])+': '+str(pricing['service_standard_count'])
                text+='\nأرقام الخدمة الإضافية: '+str(pricing['service_excess_count'])+' × '+egp(pricing['service_excess_minor'])+' جنيه'
            if pricing.get('discount_total_minor'):
                text+=f"\n\nالسعر قبل العرض: {egp(pricing['base_total_minor'])} جنيه\nالخصم: {egp(pricing['discount_total_minor'])} جنيه\n"
                offer=pricing.get('applied_campaign') or {}
                text+='العرض: '+views.safe(offer.get('name'))+f" · {offer.get('discounted_checks',0)} تشييك مشمول"
                if pricing.get('partner_future_offer'):
                    text+='\nالحد الأعلى المحجوز: '+egp(state['quoted_minor'])+' جنيه'
                    text+='\nالتكلفة المتوقعة عند اكتمال الفحص: '+egp(pricing['estimated_final_minor'])+' جنيه'
                    text+='\nتُحجز القيمة قبل الخصم مؤقتًا، ثم يُعاد الخصم المستحق وقيمة الفحوص غير المنجزة إلى محفظتك بعد تسليم النتيجة. يُحسب الخصم على الفحوص الصحيحة المؤهلة؛ قد يقل عند الإيقاف أو انتهاء الاستحقاق. هذا العرض يستخدم رصيدك المدفوع.'
                else:text+='\nالسعر المعروض محفوظ لهذا الطلب.'
            funding = draft.get('commerce_funding')
            rows = []
            if funding in {None,'wallet'}:
                rows.append([views.button('أوافق · تنفيذ العرض' if state['quoted_minor']==0 else 'أوافق · الدفع بالمحفظة', f'co:wallet:{token}', 'success')])
            if state['quoted_minor']>0 and funding in {None,'invoice'}:
                rows.append([views.button('أوافق · فاتورة Crypto Pay', f'co:invoice:{token}')])
            if state['quoted_minor']>0 and funding in {None,'transfer'}:
                rows.extend(getattr(self,'_commerce_transfer_buttons',lambda value:[])(token))
            if draft.get('commerce_quote') and funding=='invoice':
                rows.append([views.button('التحقق من دفع الفاتورة', f'co:check:{token}')])
            if funding=='transfer' and draft.get('commerce_quote'):
                rows.append([views.button('إرسال مرجع التحويل',f'co:sendref:{token}')])
            if funding is None:rows.append([views.button('إلغاء السعر وإعادة حصة العرض','co:cancel:'+token)])
            rows.append([views.button('الرئيسية', 'home')])
            self._show_panel(user_id, views.Panel(text, rows), message_id)
        except (CommerceUnavailable, OSError, ValueError, KeyError):
            self._checkout_failure(token, user_id, message_id)

    def _checkout_failure(self, token, user_id, message_id):
        self._show_panel(user_id, views.Panel('<b>تعذر إكمال الطلب</b>\n\n'
            'احتفظنا بحالة الطلب. أعد فتحه لمراجعة الحالة دون دفع جديد. إذا استمر التعذر، تواصل مع الإدارة.',
            [[views.button('مراجعة حالة الطلب', f'co:review:{token}')], [views.button('الرئيسية', 'home')]]), message_id)

    def _checkout_consent(self, draft, user_id, state, callback_id):
        reference = draft.get('commerce_consent')
        if not reference:
            if not isinstance(callback_id, str) or not callback_id.strip():
                raise ValueError('Authenticated callback receipt required')
            reference = f"refund-wallet:v1:telegram:{user_id}:{callback_id}"
            draft['commerce_consent'] = reference
            self._checkout_save()
        result = self._commerce().rpc('checkout_consent', {
            'p_order_id': draft['commerce_order_id'], 'p_telegram_user_id': user_id,
            'p_policy_version': state['pricing_snapshot']['policy_version'], 'p_reference': reference})
        draft['commerce_consent'] = result['reference']
        self._checkout_save()

    def _checkout_adapter(self):
        from telegram_bot.crypto_pay import CRYPTO_PAY
        identity = CRYPTO_PAY.get_me()
        if not isinstance(identity, dict):
            raise ValueError('Payment application is unavailable')
        return CryptoInvoiceAdapter(CRYPTO_PAY, app_id=identity.get('app_id'))

    @_serial_user
    def _commerce_checkout_callback(self, command, user_id, message_id, callback):
        parts = command.split(':')
        if len(parts) != 3: return
        _, action, token = parts
        if action == 'review':
            self._commerce_start_checkout(token, user_id, message_id)
            return
        try:
            draft = self._checkout_draft(token, user_id)
            state = self._checkout_state(draft, user_id)
            if getattr(self,'_partner_sheet_blocked',lambda *args:False)(state,token,user_id,message_id):return
            if action=='cancel':
                self._commerce().rpc('cancel_campaign_checkout',{'p_order_id':draft['commerce_order_id'],'p_telegram_user_id':user_id})
                self._commerce_notice(user_id,'أُلغي السعر','أُعيدت حصة العرض المحجوزة. أرسل الملف مجددًا إذا أردت طلبًا جديدًا.',message_id);return
            if state['order_state']=='cancelled':raise ValueError('Cancelled checkout')
            if state.get('reservation_state') == 'held':
                self._checkout_enqueue(token, draft, user_id, state)
                self._checkout_success(draft, user_id, message_id)
                return
            if state['order_state'] == 'closed':
                self._commerce_notice(user_id, 'الطلب مكتمل', f"رقم الطلب: {draft['commerce_order_id']}", message_id)
                return
            if action not in {'wallet', 'invoice', 'check'}:
                raise ValueError('Unknown checkout action')
            if state['quoted_minor']==0 and action!='wallet':raise ValueError('Free offer does not require a provider invoice')
            selected = 'wallet' if action == 'wallet' else 'invoice'
            if draft.get('commerce_funding') not in {None, selected}:
                raise ValueError('Another funding method is already selected')
            if action == 'check' and not draft.get('commerce_consent'):
                raise ValueError('Consent must precede invoice creation')
            self._checkout_consent(draft, user_id, state, callback.get('id'))
            draft['commerce_funding'] = selected
            self._checkout_save()
            payment_id = None
            if selected == 'invoice':
                adapter = self._checkout_adapter()
                service = PaymentService(self._commerce(), admin_ids=self.current_authorized_ids)
                if not draft.get('commerce_quote'):
                    rate, source = asset_egp_rate(adapter.client, 'USDT')
                    frozen = freeze_quote(user_id, state['quoted_minor'], 'USDT', rate, source,
                                          order_id=draft['commerce_order_id'])
                    draft['commerce_quote'] = frozen.as_dict()
                    self._checkout_save()
                frozen = PaymentQuote(**draft['commerce_quote'])
                self._checkout_save()
                service.issue(frozen, 'cryptopay', str(adapter.app_id))
                if action == 'invoice':
                    invoice = service.create_invoice(frozen.id, user_id, adapter)
                    details = adapter.client.get_invoice(invoice['invoice_id'])
                    url = details.get('bot_invoice_url') or details.get('pay_url') if isinstance(details, dict) else None
                    if not isinstance(url, str) or not url.startswith('https://t.me/'):
                        raise ValueError('Verified provider invoice URL unavailable')
                    self._show_panel(user_id, views.Panel(
                        f"<b>فاتورة الدفع</b>\n\nالمبلغ: {frozen.asset_amount} {frozen.asset}\n"
                        f"قيمة الطلب: {egp(frozen.amount_minor)} جنيه مصري\n"
                        f"رقم الطلب: <code>{draft['commerce_order_id']}</code>\n"
                        'المبلغ وسعر الصرف محفوظان لهذه الفاتورة. مصدر سعر الصرف: ExchangeRate-API وCrypto Pay.',
                        [[{'text': 'فتح فاتورة الدفع', 'url': url}],
                         [views.button('التحقق من الدفع', f'co:check:{token}')],
                         [views.button('مراجعة الطلب', f'co:review:{token}')]]), message_id)
                    return
                confirmed = service.confirm_invoice(frozen.id, user_id, adapter)
                payment_id = confirmed['payment_id']
            reservation = FinanceService(self._commerce()).reserve(draft['commerce_order_id'], user_id,
                draft['commerce_consent'], payment_id=payment_id)
            if reservation.get('state') == 'insufficient_balance':
                draft['commerce_funding'] = None
                self._checkout_save()
                self._commerce_notice(user_id, 'الرصيد غير كافٍ',
                    f"المطلوب {egp(state['quoted_minor'])} جنيه. المتاح {egp(reservation['available_minor'])} جنيه"
                    +(' من الرصيد المدفوع المؤهل لهذا العرض' if reservation.get('cash_only') else '')+'. افتح الطلب لاختيار الفاتورة.', message_id)
                return
            state = self._checkout_state(draft, user_id)
            self._checkout_enqueue(token, draft, user_id, state)
            self._checkout_success(draft, user_id, message_id)
        except (CommerceUnavailable, OSError, ValueError, KeyError, TimeoutError):
            self._checkout_failure(token, user_id, message_id)

    def _checkout_enqueue(self, token, draft, user_id, state):
        if state.get('partner_sheet') and state['partner_sheet'].get('state')!='approved':
            raise ValueError('Owner approval required for partner sheet')
        if state.get('reservation_state') != 'held' or state.get('payment_state') != 'reserved':
            raise ValueError('Held reservation required before checking')
        if not hasattr(self, 'commerce_bridge'):
            raise ValueError('Durable commerce bridge is unavailable')
        draft['commerce_quoted_minor'] = state['quoted_minor']
        draft['commerce_pricing_snapshot'] = state['pricing_snapshot']
        job_id = self._enqueue_paid_draft(token, draft, user_id)
        self.commerce_bridge.bind_job(draft['commerce_order_id'], job_id)
        draft['commerce_job_id'] = str(job_id)
        self._checkout_save()

    def _checkout_success(self, draft, user_id, message_id):
        self._commerce_notice(user_id, 'تم حجز قيمة الطلب',
            f"أُضيف الملف إلى الطابور. ستصلك النتيجة بعد الفحص. رقم الطلب: {draft['commerce_order_id']}", message_id)

    def _recover_commerce_checkouts(self):
        with self._ui_lock:
            candidates = list(self._sheet_drafts.items())
        recovered = 0
        for token, draft in candidates:
            if not draft.get('commerce_order_id') or not draft.get('commerce_consent'):
                continue
            if draft.get('commerce_job_id'):
                existing=self.queue_service.get_job(draft['commerce_job_id'])
                if existing and existing.column_mapping.get('commerce_order_id')==draft['commerce_order_id']:
                    continue
            try:
                checked = self._checkout_draft(token, draft['chat'])
                state = self._checkout_state(checked, draft['chat'])
                if state.get('payment_state')=='unpaid' and checked.get('commerce_quote'):
                    quote=PaymentQuote(**checked['commerce_quote'])
                    request,_=PaymentService(self._commerce()).load(quote.id,draft['chat'])
                    if request.get('payment_id') and state.get('refund_consent_reference'):
                        FinanceService(self._commerce()).reserve(quote.order_id,draft['chat'],
                            state['refund_consent_reference'],payment_id=request['payment_id'])
                        state=self._checkout_state(checked,draft['chat'])
                if state.get('reservation_state') == 'held':
                    self._checkout_enqueue(token, checked, draft['chat'], state)
                    recovered += 1
            except (CommerceUnavailable, OSError, ValueError, KeyError):
                continue
        return recovered
