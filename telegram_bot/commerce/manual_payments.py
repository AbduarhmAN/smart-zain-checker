"""Customer transfer references and explicit authenticated admin assignment."""
from datetime import datetime,timezone
import time
from uuid import UUID
from telegram_bot import views
from .bot_workflow import _serial_user
from .checkout import egp
from .client import CommerceUnavailable
from .finance import FinanceService,positive_minor
from .payment_quotes import PaymentQuote,freeze_quote,utc_time
from .payments import PaymentService
from .provider_routes import ProviderRoutes,TransferRoute,verifier_for
from .rates import asset_egp_rate


class ManualPaymentsMixin:
    def _transfer_routes(self): return ProviderRoutes(self.project_root)

    def _commerce_transfer_buttons(self,token):
        from telegram_bot.config import show_crypto_payments
        if not show_crypto_payments():
            return []
        labels={'binance':'أوافق · تحويل Binance Pay','bscusdt':'أوافق · USDT على BSC','bscusdc':'أوافق · USDC على BSC'}
        saved=self._sheet_drafts.get(token,{}).get('commerce_transfer_route')
        routes=[TransferRoute(**saved)] if saved else self._transfer_routes().all()
        return [[views.button(labels[route.name],f'co:{route.name}:{token}')] for route in routes]


    def _commerce_checkout_callback(self,command,user_id,message_id,callback):
        parts=command.split(':')
        if len(parts)==3 and parts[1] in {'binance','bscusdt','bscusdc','sendref'}:
            self._manual_checkout(command,user_id,message_id,callback)
        else: super()._commerce_checkout_callback(command,user_id,message_id,callback)

    @_serial_user
    def _manual_checkout(self,command,user_id,message_id,callback):
        _,action,token=command.split(':')
        try:
            draft=self._checkout_draft(token,user_id)
            state=self._checkout_state(draft,user_id)
            if state['order_state']!='draft' or state['payment_state']!='unpaid':
                self._commerce_start_checkout(token,user_id,message_id);return
            if action=='sendref':
                if draft.get('commerce_funding')!='transfer' or not draft.get('commerce_quote'):
                    raise ValueError('Issued transfer request required')
                self._waiting_user_inputs[user_id]={'action':'commerce_transfer_reference','token':token,
                    'message_id':message_id,'expires':time.monotonic()+1800}
                self._commerce_notice(user_id,'مرجع التحويل','أرسل TxID أو رقم العملية وحده. وصول التحويل وتحديد صاحبه يحتاجان مراجعة الإدارة.',message_id)
                return
            saved=draft.get('commerce_transfer_route')
            route=TransferRoute(**saved) if saved and saved.get('name')==action else self._transfer_routes().get(action)
            if draft.get('commerce_funding') not in {None,'transfer'}:
                raise ValueError('Another funding path is selected')
            if draft.get('commerce_transfer_route') and draft['commerce_transfer_route']!=route.as_dict():
                raise ValueError('Transfer request route is already frozen')
            self._checkout_consent(draft,user_id,state,callback.get('id'))
            draft.update(commerce_funding='transfer',commerce_transfer_route=route.as_dict())
            self._checkout_save()
            if not draft.get('commerce_quote'):
                adapter=self._checkout_adapter()
                rate,source=asset_egp_rate(adapter.client,route.asset)
                draft['commerce_quote']=freeze_quote(user_id,state['quoted_minor'],route.asset,rate,source,
                    order_id=draft['commerce_order_id']).as_dict()
            self._checkout_save()
            quote=PaymentQuote(**draft['commerce_quote'])
            PaymentService(self._commerce()).issue(quote,route.provider,route.receiver,route_snapshot=route.snapshot)
            address=route.receiver.split(':',1)[1] if route.provider=='binance_pay' else route.receiver
            text=(f"<b>طلب تحويل محفوظ</b>\n\nالمبلغ: <b>{quote.asset_amount} {quote.asset}</b>\n"
                  f"قيمة الطلب: {egp(quote.amount_minor)} جنيه\nالمستلم: <code>{views.safe(address,256)}</code>\n"
                  f"رقم الطلب: <code>{draft['commerce_order_id']}</code>\n")
            if route.provider=='bsc':
                text+=f"الشبكة: BNB Smart Chain (BEP20)\nالعقد: <code>{route.snapshot['contract']}</code>\n"
            else:text+='الوسيلة: Binance Pay\n'
            if datetime.now(timezone.utc)>utc_time(quote.expires_at):
                text+='\nانتهت صلاحية هذا السعر. لا ترسل تحويلًا جديدًا. إذا حولت أثناء الصلاحية، أرسل المرجع للمراجعة.'
            else:
                text+='\nحوّل المبلغ المطابق خلال 15 دقيقة، ثم أرسل مرجع التحويل. سيُعتمد بعد مراجعة الإدارة لصاحب التحويل.'
            text+='\nمصدر سعر الصرف: ExchangeRate-API وCrypto Pay.'
            self._show_panel(user_id,views.Panel(text,[[views.button('إرسال مرجع التحويل',f'co:sendref:{token}')],
                [views.button('مراجعة الطلب',f'co:review:{token}')]]),message_id)
        except (CommerceUnavailable,OSError,ValueError,KeyError,TypeError):
            self._checkout_failure(token,user_id,message_id)

    def _commerce_text_input(self,user_id,text,waiting):
        if waiting.get('action')!='commerce_transfer_reference':
            return super()._commerce_text_input(user_id,text,waiting)
        return self._submit_transfer_reference(user_id,text,waiting)

    @_serial_user
    def _submit_transfer_reference(self,user_id,text,waiting):
        try:
            if waiting.get('expires',0)<time.monotonic():raise ValueError('Expired transfer prompt')
            draft=self._checkout_draft(waiting['token'],user_id)
            if draft.get('commerce_funding')!='transfer':raise ValueError('Transfer request required')
            reference=text.strip()
            if not reference or len(reference)>256:raise ValueError('Invalid transfer reference')
            if draft['commerce_transfer_route']['provider']=='bsc':
                from .payment_providers import canonical_transaction_hash
                reference=canonical_transaction_hash(reference)
            quote=PaymentQuote(**draft['commerce_quote'])
            claim=self._commerce().rpc('submit_payment_claim',{'p_quote_id':quote.id,
                'p_telegram_user_id':user_id,'p_reference':reference})
            draft['commerce_claim_id']=str(UUID(claim['claim_id']))
            self._checkout_save()
            labels={'pending':'وصل المرجع إلى قائمة مراجعة الإدارة. لم نعتبر المرجع وحده إثباتًا للدفع.',
                    'approved':'هذه الدفعة معتمدة بالفعل. افتح الطلب لمراجعة حالته.',
                    'rejected':'سبق رفض هذا المرجع. راجعه مع الإدارة قبل إرسال مرجع مختلف.'}
            self._commerce_notice(user_id,'مرجع الدفع',labels[claim['state']],waiting.get('message_id'))
        except (CommerceUnavailable,OSError,ValueError,KeyError):
            self._checkout_failure(waiting.get('token',''),user_id,waiting.get('message_id'))
        return True

    @_serial_user
    def _payment_review_callback(self,command,actor_id,message_id,callback):
        if actor_id not in self.current_authorized_ids:
            self._commerce_notice(actor_id,'غير مصرح','مراجعة المدفوعات خاصة بالإدارة.',message_id);return
        if command=='cm:list:0':
            self._show_panel(actor_id,views.Panel('<b>المدفوعات والطلبات</b>\n\nخصم المحفظة المؤكد يظهر في سجل الطلبات. التحويلات التي تحتاج اعتمادًا تظهر في قائمة المراجعة.',
                [[views.button('طلبات العملاء ومدفوعاتهم','oh:admin:0')],
                 [views.button('تحويلات تحتاج اعتمادًا','cm:claims:0')],
                 [views.button('مراجعة فودافون كاش','vc:inbox:0')],[views.button('الرئيسية','home')]]),message_id)
            return
        try:
            parts=command.split(':');action=parts[1]
            if action in {'list','claims'}:
                offset=int(parts[2]);claims=self._commerce().rpc('list_payment_claims',{'p_limit':10,'p_offset':offset})
                rows=[[views.button(f"{claim['telegram_user_id']} · {egp(claim['quote']['amount_minor'])} جنيه",
                    f"cm:review:{claim['claim_id']}")] for claim in claims]
                if offset:rows.append([views.button('السابق',f'cm:list:{max(0,offset-10)}')])
                if len(claims)==10:rows.append([views.button('التالي',f'cm:list:{offset+10}')])
                rows.append([views.button('الرئيسية','home')])
                self._show_panel(actor_id,views.Panel('<b>مراجعة المدفوعات</b>\n\n'+
                    ('اختر المرجع لمراجعة وصول التحويل وصاحبه.' if claims else 'لا توجد مراجع معلقة.'),rows),message_id)
                return
            claim_id=str(UUID(parts[2]))
            service=PaymentService(self._commerce(),admin_ids=self.current_authorized_ids)
            if action=='review':
                claim=self._commerce().rpc('payment_claim',{'p_claim_id':claim_id})
                if claim['state']!='pending':
                    self._commerce_notice(actor_id,'حالة المرجع',claim['state'],message_id);return
                request,_=service.load(claim['quote_id'],claim['telegram_user_id'])
                prepared=service.prepare_claim(claim_id,verifier_for(request),actor_id=actor_id)
                if 'evidence' not in prepared:raise ValueError('Claim is no longer pending')
                with self._ui_lock:
                    cached=getattr(self,'_commerce_payment_reviews',{})
                    self._commerce_payment_reviews={key:value for key,value in cached.items() if value['expires']>time.monotonic()}
                    if len(self._commerce_payment_reviews)>=128:raise ValueError('Review limit reached')
                    self._commerce_payment_reviews[(actor_id,claim_id)]={'prepared':prepared,'expires':time.monotonic()+180}
                evidence=prepared['evidence']
                text=(f"<b>مراجعة ربط التحويل</b>\n\nالعميل: <code>{claim['telegram_user_id']}</code>\n"
                    f"المرجع المؤكد: <code>{views.safe(evidence.reference,256)}</code>\n"
                    f"المبلغ: {evidence.amount} {evidence.asset}\nالمستلم: <code>{views.safe(evidence.receiver,256)}</code>\n"
                    f"قيمة الطلب: {egp(prepared['quote'].amount_minor)} جنيه\n\n"
                    'ثبت وصول التحويل ومطابقته للطلب. راجع هوية صاحبه ودليله؛ المرجع وحده لا يثبت أنه لهذا العميل. '
                    'الموافقة أدناه تؤكد أنك راجعت صاحب التحويل وتخصصه لهذا العميل.')
                self._show_panel(actor_id,views.Panel(text,[[views.button('أؤكد صاحب التحويل وأعتمد الدفع',f'cm:approve:{claim_id}','success')],
                    [views.button('رفض المرجع',f'cm:reject:{claim_id}')],[views.button('قائمة المراجعة','cm:list:0')]]),message_id)
            elif action=='approve':
                review=getattr(self,'_commerce_payment_reviews',{}).get((actor_id,claim_id))
                if not review or review['expires']<=time.monotonic():raise ValueError('Review before confirming payment')
                prepared=review['prepared']
                result=service.approve_claim(prepared,actor_id=actor_id)
                if prepared['quote'].order_id is None:
                    self._commerce_notice(actor_id,'تم اعتماد الشحن','حُفظ الإيصال وزاد رصيد محفظة العميل مرة واحدة.',message_id)
                    return
                queued=self._finish_assigned_payment(prepared['quote'],result['payment_id'])
                self._commerce_notice(actor_id,'تم اعتماد الدفعة',
                    'حُفظت الدفعة وأُضيف الطلب إلى الطابور.' if queued else
                    'حُفظت الدفعة مرة واحدة. إذا لم يظهر الطلب في الطابور، افتحه لاستعادة إضافته.',message_id)
            elif action=='reject':
                if not isinstance(callback.get('id'),str) or not callback['id'].strip():
                    raise ValueError('Authenticated rejection callback required')
                self._commerce().rpc('reject_payment_claim',{'p_claim_id':claim_id,'p_actor_telegram_id':actor_id,
                    'p_reference':'telegram-review:'+str(callback.get('id',''))})
                self._commerce_notice(actor_id,'رُفض المرجع','لم يُضف رصيد ولم تُخصم قيمة طلب.',message_id)
        except (CommerceUnavailable,OSError,ValueError,KeyError,TypeError):
            self._commerce_notice(actor_id,'تحتاج مراجعة','لم نتأكد من إكمال الإجراء. راجع حالة المرجع؛ لا تعتمد دفعة مختلفة لإصلاح تعذر مؤقت.',message_id)

    def _finish_assigned_payment(self,quote,payment_id):
        if quote.order_id is None:return False
        with self._ui_lock: candidates=list(self._sheet_drafts.items())
        for token,draft in candidates:
            if draft.get('commerce_order_id')==quote.order_id and draft.get('chat')==quote.user_id:
                self._checkout_draft(token,quote.user_id)
                state=self._checkout_state(draft,quote.user_id)
                if state['payment_state']=='unpaid':
                    self._checkout_reserve(draft,quote.user_id,state,payment_id=payment_id)
                    state=self._checkout_state(draft,quote.user_id)
                self._checkout_enqueue(token,draft,quote.user_id,state)
                return True
        return False

    def _commerce_command(self,text,user_id,is_admin,username='',*,message_id=None):
        parts=text.split();command=parts[0].split('@')[0].lower() if parts else ''
        if command not in {'/payment_reviews','/commerce_binance','/commerce_bsc'}:
            return super()._commerce_command(text,user_id,is_admin,username,message_id=message_id)
        if not is_admin or user_id not in self.current_authorized_ids:
            self._commerce_notice(user_id,'غير مصرح','إعداد الدفع ومراجعته خاصان بالإدارة.');return True
        try:
            if command=='/payment_reviews':
                self._payment_review_callback('cm:list:0',user_id,None,{});return True
            positive_minor(message_id)
            if command=='/commerce_binance' and len(parts) in {2,3}:
                pay=positive_minor(int(parts[1]));uid=positive_minor(int(parts[2])) if len(parts)==3 else None
                route=TransferRoute('binance','binance_pay','USDT','uid:'+str(uid) if uid else 'pay:'+str(pay),
                    {'pay_id':pay,'uid':uid,'asset':'USDT','source':'authenticated_admin_configuration'})
            elif command=='/commerce_bsc' and len(parts)==5:
                from .payment_providers import hex_data
                asset=parts[1].upper();receiver=hex_data(parts[2],20);contract=hex_data(parts[3],20);decimals=int(parts[4])
                route=TransferRoute('bscusdt' if asset=='USDT' else 'bscusdc','bsc',asset,receiver,
                    {'chain_id':56,'asset':asset,'contract':contract,'decimals':decimals,'source':'authenticated_admin_configuration'})
                from telegram_bot.crypto_pay import BSC_CHAIN
                from .payment_providers import quantity
                if quantity(BSC_CHAIN._rpc_call('eth_chainId',[]))!=56:
                    raise ValueError('BSC mainnet verification required')
                observed=int(hex_data(BSC_CHAIN._rpc_call('eth_call',[{'to':contract,'data':'0x313ce567'},'latest']),32),16)
                if observed!=decimals:raise ValueError('Configured decimals differ from contract')
            else:raise ValueError('Invalid provider configuration')
            revision=self._transfer_routes().save(route,actor_id=user_id,reference=f'telegram:{user_id}:{message_id}')
            self._commerce_notice(user_id,'حُفظ إعداد الدفع',f'تم حفظ المستلم والعملة بإقرار الإدارة. النسخة: {revision}. الطلبات السابقة تحتفظ بإعداداتها.')
        except (OSError,ValueError,KeyError,TypeError):
            self._commerce_notice(user_id,'راجع الإعداد','الاستخدام: /commerce_binance PAY_ID [UID] أو /commerce_bsc USDT|USDC ADDRESS CONTRACT DECIMALS. القيم عامة؛ لا ترسل مفاتيح سرية.')
        return True
