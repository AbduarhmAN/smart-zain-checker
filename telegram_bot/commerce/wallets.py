"""Private customer wallet, durable topups and authenticated cash approval."""
from datetime import datetime,timezone
from decimal import Decimal
import re
import json
import sqlite3
import time
from uuid import UUID,uuid5,NAMESPACE_URL,uuid4
from telegram_bot import views
from .bot_workflow import _serial_user
from .checkout import egp
from .partner_balances import partner_balance_text
from .client import CommerceUnavailable
from .finance import positive_minor
from .legacy_wallets import minor
from .payment_quotes import PaymentQuote,freeze_quote,utc_time
from .payments import PaymentService
from .rates import asset_egp_rate
from .topup_store import TopupStore


class WalletPaymentsMixin:
    def _legacy_balance_pending_minor(self,user_id):
        root=getattr(self,'project_root',None)
        if root is None:return 0
        transition=root/'.commerce/legacy-transition-state.json'
        if transition.exists():
            state=json.loads(transition.read_text(encoding='utf-8'))
            if state.get('state')=='imported':return 0
        path=root/'user_wallets.json'
        if not path.exists():return 0
        wallets=json.loads(path.read_text(encoding='utf-8-sig'),parse_float=Decimal)
        return minor(wallets.get(str(user_id),{}).get('balance',0))

    def _topups(self):
        with self._ui_lock:
            if not hasattr(self,'_commerce_topup_store'):self._commerce_topup_store=TopupStore(self.project_root)
            return self._commerce_topup_store

    def _uses_commerce_wallet(self,user_id):
        if self._commerce_checkout_enabled(user_id):return True
        root=getattr(self,'project_root',None)
        if root is None:return False
        if (root/'.commerce/topups.sqlite').exists() and self._topups().is_owner(user_id):return True
        return any(draft.get('chat')==user_id and draft.get('commerce_request_key')
                   for draft in getattr(self,'_sheet_drafts',{}).values())

    def _wallet_state(self,user_id):
        positive_minor(user_id)
        self._commerce().rpc('register_telegram_user',{'p_telegram_user_id':user_id,'p_username':None})
        state=self._commerce().rpc('wallet_state',{'p_telegram_user_id':user_id})
        if not isinstance(state,dict) or 'available_minor' not in state.get('customer',{}):
            raise ValueError('Customer wallet state is unavailable')
        if state.get('is_partner'):
            with self._ui_lock:
                if not hasattr(self,'_known_partner_ids'):self._known_partner_ids=set()
                if state.get('partner',{}).get('partnership_ended_at'):self._known_partner_ids.discard(user_id)
                else:self._known_partner_ids.add(user_id)
        return state

    def _wallet_display_balance(self,user_id):
        if self._uses_commerce_wallet(user_id):return Decimal(self._wallet_state(user_id)['customer']['available_minor'])/100
        from telegram_bot.payment_manager import PAYMENT_MGR
        return PAYMENT_MGR.get_wallet_balance(user_id)

    def _wallet_account_label(self,user_id):
        if self._uses_commerce_wallet(user_id):return str(user_id)
        from telegram_bot.payment_manager import PAYMENT_MGR
        return PAYMENT_MGR.get_user_account_id(user_id)

    def _show_wallet_panel(self,user_id,message_id):
        if not self._uses_commerce_wallet(user_id):return super()._show_wallet_panel(user_id,message_id)
        try:
            state=self._wallet_state(user_id);customer=state['customer'];partner=state.get('partner',{})
            text=(f"<b>محفظتك بالجنيه المصري</b>\n\nرقم Telegram: <code>{user_id}</code>\n"
                f"المتاح: <b>{egp(customer['available_minor'])} جنيه</b>\n"
                f"منه رصيد ترويجي: {egp(customer.get('promotional_available_minor',0))} جنيه\n"
                f"المحجوز للطلبات: {egp(customer.get('reserved_minor',0))} جنيه\n\n"
                'يُستخدم الرصيد الترويجي أولًا، باستثناء العروض المخصصة للدفع من الرصيد المدفوع. استرداد حصته يعود ترويجيًا ويخضع لصلاحية العرض. المتبقي غير المستخدم يُسحب عند انتهاء المدة.')
            if state.get('is_partner'):
                text+=('\n\n<b>حساب الشريك</b>\n'+partner_balance_text({'available_minor':0,**partner})+'\n'+
                    f"أرباح الشريك المحجوزة: {egp(partner.get('reserved_minor',0))} جنيه\n"
                    f"أرباح مسحوبة: {egp(partner.get('withdrawn_minor',0))} جنيه\n"
                    'رصيد الطلبات وأرباح الشريك منفصلان. شحن المحفظة لا يُحسب خدمة مدفوعة أو عمولة.')
            if customer.get('expired_promotional_minor'):
                text+='\n\nرصيد ترويجي انتهت صلاحيته: '+egp(customer['expired_promotional_minor'])+' جنيه.'
            if customer.get('reclaimed_promotional_minor'):
                text+='\n\nرصيد ترويجي استُرجع بعد إلغاء حملته: '+egp(customer['reclaimed_promotional_minor'])+' جنيه.'
            if customer.get('next_promotional_expiry'):
                text+='\nموعد انتهاء أقرب رصيد ترويجي: '+views.safe(customer['next_promotional_expiry'])
            legacy=self._legacy_balance_pending_minor(user_id)
            if legacy:
                text+='\n\nرصيد سابق محفوظ بانتظار النقل: '+egp(legacy)+' جنيه. يظهر منفصلًا ولا يدخل في الرصيد المتاح أعلاه. طلباتك السابقة محفوظة في سجل الطلبات.'
            rows=[[views.button('شحن المحفظة','cw:topup:0','success'),views.button('كود رصيد أو عرض','cc:redeem:0')],
                [views.button('طلبات الشحن','cw:list:0'),views.button('سجل المحفظة','oh:ledger:0')],
                [views.button('عروضي وحدودها','of:list:0'),views.button('طلباتي','oh:list:0')]]
            if state.get('is_partner'):
                rows.append([views.button('أرباح الشريك','pf:home:0'),views.button('حملاتي','po:list:0')])
            rows.append([views.button('الرئيسية','home')])
            self._show_panel(user_id,views.Panel(text,rows),message_id)
        except (CommerceUnavailable,OSError,ValueError,KeyError):self._wallet_failure(user_id,message_id)

    def _show_wallet_topup_menu(self,user_id,message_id):
        try:
            if self._uses_commerce_wallet(user_id):return self._wallet_callback('cw:topup:0',user_id,message_id,{})
            return super()._show_wallet_topup_menu(user_id,message_id)
        except (CommerceUnavailable,OSError,ValueError,KeyError):
            self._wallet_failure(user_id,message_id)

    def _wallet_failure(self,user_id,message_id):
        self._show_panel(user_id,views.Panel('<b>تعذر التأكد من حالة المحفظة</b>\n\n'
            'أعد فتح طلب الشحن نفسه للتحقق. لا تنشئ دفعة ثانية لمعالجة تعذر الاتصال.',
            [[views.button('طلبات الشحن السابقة','cw:list:0')],[views.button('المحفظة','wallet')]]),message_id)

    @_serial_user
    def _wallet_callback(self,command,user_id,message_id,callback):
        try:
            positive_minor(user_id)
            parts=command.split(':')
            if len(parts)!=3:raise ValueError('Invalid wallet callback')
            action=parts[1];value=parts[2]
            if action=='topup':
                if not self._uses_commerce_wallet(user_id):raise ValueError('Commerce wallet is not enabled')
                from telegram_bot.config import show_crypto_payments
                if not show_crypto_payments():
                    rows = [
                        [views.button('دفع فودافون كاش', 'vc:start:0'), views.button('طلباتي فودافون كاش', 'vc:list:0')],
                        [views.button('🎁 إدخال كود رصيد أو عرض', 'cc:redeem:0')],
                        [views.button('المحفظة', 'wallet')]
                    ]
                    self._show_panel(user_id, views.Panel('<b>شحن المحفظة</b>\n\nاختر فودافون كاش للدفع والمراجعة اليدوية، أو أدخل كود رصيد.', rows), message_id)
                    return
                methods=[views.button('Crypto Pay · USDT','cw:amount:cryptopay')]
                methods.extend(views.button('Binance Pay' if route.name=='binance' else route.asset+' على BSC',
                    'cw:amount:'+route.name) for route in self._transfer_routes().all())
                rows=[[views.button('دفع فودافون كاش','vc:start:0'),views.button('طلباتي فودافون كاش','vc:list:0')]]
                rows.extend(methods[index:index+2] for index in range(0,len(methods),2))
                rows.append([views.button('المحفظة','wallet')])
                self._show_panel(user_id,views.Panel('<b>شحن المحفظة</b>\n\nاختر الوسيلة ثم أدخل مبلغ الشحن بالجنيه.',rows),message_id)

            elif action=='amount':
                if not self._uses_commerce_wallet(user_id):raise ValueError('Commerce wallet is not enabled')
                if value!='cryptopay':self._transfer_routes().get(value)
                self._waiting_user_inputs[user_id]={'action':'commerce_topup_amount','method':value,
                    'message_id':message_id,'expires':time.monotonic()+1800}
                self._commerce_notice(user_id,'مبلغ الشحن','أرسل مبلغًا موجبًا بالجنيه، بمنزلتين عشريتين كحد أقصى، مثل 60.00.',message_id)
            elif action=='list':
                records=self._topups().recent(user_id)
                rows=[[views.button(egp(record['quote']['amount_minor'])+' جنيه · '+record['quote']['asset'],
                    'cw:open:'+record['quote']['id'])] for record in records]
                rows.append([views.button('المحفظة','wallet')])
                self._show_panel(user_id,views.Panel('<b>طلبات الشحن السابقة</b>\n\n'+
                    ('افتح الطلب للتحقق من حالته قبل دفع مبلغ آخر.' if records else 'لا توجد طلبات شحن محفوظة.'),rows),message_id)
            elif action in {'open','invoice','check','reference'}:
                record=self._topups().get(value,user_id);quote=PaymentQuote(**record['quote'])
                service=PaymentService(self._commerce())
                service.issue(quote,record['provider'],record['receiver'],route_snapshot=record.get('route_snapshot',{}))
                request,_=service.load(quote.id,user_id)
                if request.get('payment_id'):
                    self._show_wallet_panel(user_id,message_id);return
                if action=='reference':
                    if record['provider']=='cryptopay':raise ValueError('Use the issued invoice')
                    self._waiting_user_inputs[user_id]={'action':'commerce_topup_reference','quote_id':quote.id,
                        'message_id':message_id,'expires':time.monotonic()+1800}
                    self._commerce_notice(user_id,'مرجع التحويل','أرسل رقم العملية أو TxID وحده؛ سيعتمد بعد مراجعة وصوله وصاحبه.',message_id)
                    return
                if action=='check':
                    if record['provider']!='cryptopay':raise ValueError('Raw transfers require admin assignment')
                    service.confirm_invoice(quote.id,user_id,self._checkout_adapter())
                    self._show_wallet_panel(user_id,message_id);return
                rows=[]
                text=f"<b>طلب شحن محفوظ</b>\n\nقيمة الشحن: {egp(quote.amount_minor)} جنيه\nالمبلغ: <b>{quote.asset_amount} {quote.asset}</b>\n"
                if record['provider']=='cryptopay':
                    if action=='invoice':
                        adapter=self._checkout_adapter()
                        result=service.create_invoice(quote.id,user_id,adapter)
                        invoice=adapter.client.get_invoice(result['invoice_id'])
                        url=invoice.get('bot_invoice_url') or invoice.get('pay_url') if isinstance(invoice,dict) else None
                        if not isinstance(url,str) or not url.startswith('https://t.me/'):raise ValueError('Provider invoice URL unavailable')
                        rows.append([views.url_button('فتح فاتورة الشحن',url)])
                    else:rows.append([views.button('إنشاء أو فتح الفاتورة','cw:invoice:'+quote.id)])
                    rows.append([views.button('التحقق من دفع الفاتورة','cw:check:'+quote.id)])
                else:
                    address=record['receiver'].split(':',1)[1] if record['provider']=='binance_pay' else record['receiver']
                    text+=f"المستلم: <code>{views.safe(address,256)}</code>\n"
                    if record['provider']=='bsc':text+=f"الشبكة: BSC (BEP20)\nالعقد: <code>{record['route_snapshot']['contract']}</code>\n"
                    text+='\nأرسل المبلغ المطابق خلال صلاحية السعر، ثم قدم المرجع لمراجعة الإدارة.\n'
                    if record['provider']=='binance_pay':
                        from .binance_notes import enabled
                        if enabled(self.project_root):
                            rows.append([views.button('تجهيز رمز ملاحظة الدفع الخاص','bn:issue:'+quote.id)])
                            text+='\nالتأكيد التلقائي يتطلب رمز الطلب في ملاحظة Binance قبل التحويل. نسخ TXID وحده لا يكفي.'
                    rows.append([views.button('إرسال مرجع للمراجعة','cw:reference:'+quote.id)])
                if datetime.now(timezone.utc)>utc_time(quote.expires_at):
                    text+='\nانتهت صلاحية السعر. لا ترسل تحويلًا جديدًا؛ إن كنت دفعت أثناء الصلاحية، تابع التحقق أو مراجعة المرجع.'
                text+='\nمصدر سعر الصرف: ExchangeRate-API وCrypto Pay.'
                rows.append([views.button('المحفظة','wallet')])
                self._show_panel(user_id,views.Panel(text,rows),message_id)
        except (CommerceUnavailable,OSError,ValueError,KeyError,TypeError,sqlite3.Error):self._wallet_failure(user_id,message_id)

    def _commerce_text_input(self,user_id,text,waiting):
        action=waiting.get('action','')
        if action in {'commerce_topup_amount','commerce_topup_reference'}:
            return self._wallet_text_input(user_id,text,waiting)
        if action in {'topup_tx','redeem'} and self._uses_commerce_wallet(user_id):
            self._commerce_notice(user_id,'المحفظة الجديدة','افتح طلب الشحن من المحفظة الجديدة. الأكواد والمحاولات القديمة تُراجع ضمن التحويل المالي.',waiting.get('message_id'))
            return True
        return super()._commerce_text_input(user_id,text,waiting)

    @_serial_user
    def _wallet_text_input(self,user_id,text,waiting):
        try:
            if waiting.get('expires',0)<time.monotonic():raise ValueError('Expired wallet prompt')
            if waiting['action']=='commerce_topup_reference':
                record=self._topups().get(waiting['quote_id'],user_id)
                reference=text.strip()
                if record['provider']=='bsc':
                    from .payment_providers import canonical_transaction_hash
                    reference=canonical_transaction_hash(reference)
                claim=self._commerce().rpc('submit_payment_claim',{'p_quote_id':record['quote']['id'],
                    'p_telegram_user_id':user_id,'p_reference':reference})
                self._commerce_notice(user_id,'مرجع الشحن','حالة المرجع: '+claim['state']+'. لا يزيد الرصيد قبل الاعتماد.',waiting.get('message_id'))
                return True
            if not self._uses_commerce_wallet(user_id):raise ValueError('Commerce wallet is not enabled')
            amount=positive_minor(minor(text.strip()))
            incoming=positive_minor(waiting.get('_telegram_input_message_id'))
            quote_id=str(uuid5(NAMESPACE_URL,f'zain:wallet:{user_id}:{incoming}'))
            record=self._topups().find(quote_id,user_id)
            if record is None:
                method=waiting['method'];adapter=self._checkout_adapter()
                if method=='cryptopay':provider,receiver,asset,snapshot='cryptopay',str(adapter.app_id),'USDT',{}
                else:
                    route=self._transfer_routes().get(method)
                    provider,receiver,asset,snapshot=route.provider,route.receiver,route.asset,route.snapshot
                rate,source=asset_egp_rate(adapter.client,asset)
                quote=freeze_quote(user_id,amount,asset,rate,source,quote_id=quote_id)
                record={'quote':quote.as_dict(),'provider':provider,'receiver':receiver,'route_snapshot':snapshot}
                self._topups().put(record)
            elif record['quote']['amount_minor']!=amount:
                raise ValueError('Telegram message replay changed its requested amount')
            self._wallet_callback('cw:open:'+quote_id,user_id,waiting.get('message_id'),{})
        except (CommerceUnavailable,OSError,ValueError,KeyError,TypeError,sqlite3.Error):self._wallet_failure(user_id,waiting.get('message_id'))
        return True

    def _commerce_command(self,text,user_id,is_admin,username='',*,message_id=None):
        parts=text.split();command=parts[0].split('@')[0].lower() if parts else ''
        if command=='/start' and self._uses_commerce_wallet(user_id):
            try:self._commerce().rpc('register_telegram_user',{'p_telegram_user_id':user_id,'p_username':username or None})
            except CommerceUnavailable:self._wallet_failure(user_id,None);return True
        if command=='/credit_user' and len(parts)>=2:
            from telegram_bot.payment_manager import PAYMENT_MGR
            found=PAYMENT_MGR.find_user_by_account_id(parts[1])
            if not isinstance(found,(tuple,list)):found=None
            target=int(found[0]) if found else int(parts[1]) if parts[1].isdigit() else None
            if target and self._uses_commerce_wallet(target):
                try:
                    if not is_admin or user_id not in self.current_authorized_ids:raise ValueError('Admin required')
                    positive_minor(message_id)
                    if len(parts)!=4 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}',parts[3]):
                        raise ValueError('Receipt reference required')
                    amount=positive_minor(minor(parts[2]));token=str(uuid4())
                    with self._ui_lock:
                        drafts=getattr(self,'_cash_topup_reviews',{})
                        self._cash_topup_reviews={k:v for k,v in drafts.items() if v['expires']>time.monotonic()}
                        if len(self._cash_topup_reviews)>=128:raise ValueError('Cash review limit reached')
                        self._cash_topup_reviews[token]={'actor':user_id,'user':target,'amount':amount,
                            'receipt':parts[3].upper(),'message_id':message_id,'expires':time.monotonic()+180}
                    self._show_panel(user_id,views.Panel(f"<b>تأكيد إيصال شحن نقدي</b>\n\nالعميل: <code>{target}</code>\n"
                        f"المبلغ: {egp(amount)} جنيه\nمرجع الإيصال: <code>{parts[3].upper()}</code>\n\nتؤكد الموافقة استلام المبلغ فعليًا لهذا العميل.",
                        [[views.button('أؤكد الاستلام وأشحن المحفظة','ca:approve:'+token,'success')],
                         [views.button('إلغاء','home')]]))
                except (ValueError,KeyError,TypeError):
                    self._commerce_notice(user_id,'راجع إيصال الشحن','للمحفظة الجديدة: /credit_user USER_ID AMOUNT RECEIPT_REFERENCE. يلزم مرجع إيصال واحد لا يُعاد استخدامه.')
                return True
        return super()._commerce_command(text,user_id,is_admin,username,message_id=message_id)

    @_serial_user
    def _cash_topup_callback(self,command,actor_id,message_id,callback):
        try:
            positive_minor(actor_id)
            parts=command.split(':')
            if len(parts)!=3:raise ValueError('Invalid cash callback')
            if parts[1]!='approve':return
            token=str(UUID(parts[2]));review=getattr(self,'_cash_topup_reviews',{}).get(token)
            if actor_id not in self.current_authorized_ids or not review or review['actor']!=actor_id or review['expires']<=time.monotonic():
                raise ValueError('Authenticated matching cash reviewer required')
            if not isinstance(callback.get('id'),str) or not callback['id'].strip():raise ValueError('Cash approval callback required')
            self._topups().mark_owner(review['user'])
            result=self._commerce().rpc('admin_cash_topup',{'p_telegram_user_id':review['user'],'p_amount_minor':review['amount'],
                'p_receipt_reference':review['receipt'],'p_actor_telegram_id':actor_id,
                'p_approval_reference':f"telegram:{actor_id}:{review['message_id']}:{callback['id']}"})
            self._commerce_notice(actor_id,'حُفظ إيصال الشحن','سُجل الإيصال وزاد رصيد العميل مرة واحدة.' if not result.get('replayed') else
                'هذا الإيصال مسجل بالفعل؛ لم يُضف رصيد جديد.',message_id)
        except (CommerceUnavailable,OSError,ValueError,KeyError,TypeError,sqlite3.Error):self._wallet_failure(actor_id,message_id)
