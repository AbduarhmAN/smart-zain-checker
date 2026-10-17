"""Private payment-note UI; enable only after a controlled API echo test."""
import time
from uuid import UUID
from telegram_bot import views
from telegram_bot.authorization import admin_ids
from .binance_notes import BinanceNoteService,enabled
from .provider_routes import verifier_for
from .payments import PaymentService
from .client import CommerceUnavailable
from .bot_workflow import _serial_user


class BinanceNoteUiMixin:
    def _binance_note_service(self):return BinanceNoteService(self._commerce(),verifier_for)

    @_serial_user
    def _binance_note_callback(self,command,actor,message_id=None,callback=None):
        try:
            _,action,value=command.split(':');quote_id=str(UUID(value))
            if not enabled(self.project_root) and actor not in admin_ids(self):raise ValueError('Payment note echo test is not verified')
            request,quote=PaymentService(self._commerce()).load(quote_id,actor)
            if request['provider']!='binance_pay':raise ValueError('Binance request required')
            if action=='issue':
                self._waiting_user_inputs[actor]={'action':'binance_note_payer','quote_id':quote_id,'message_id':message_id,'expires':time.monotonic()+600}
                self._commerce_notice(actor,'حساب Binance المرسل','أرسل Binance UID للحساب الذي ستدفع منه. لا نعد إدخال الرقم إثباتًا للملكية؛ يجب أن يصل التحويل من هذا الحساب مع الرمز الخاص الذي ستستلمه.',message_id)
            elif action=='open':
                challenge=self._commerce().rpc('binance_note',{'p_quote_id':quote_id,'p_telegram_user_id':actor})
                rows=[[views.button('التحقق من وصول التحويل','bn:check:'+quote_id)],[views.button('المحفظة','wallet')]]
                text=('<b>طلب Binance Pay خاص بك</b>\n\nالمبلغ المطابق: <b>'+quote.asset_amount+' '+quote.asset+'</b>'+
                    '\nالمستلم: <code>'+views.safe(request['receiver'])+'</code>\nالحساب المرسل UID: <code>'+views.safe(challenge['payer_uid'])+'</code>'+
                    '\n\nانسخ الرمز التالي في <b>ملاحظة التحويل</b> داخل Binance قبل تأكيد الدفع:\n<code>'+views.safe(challenge['challenge'])+'</code>'+
                    '\n\nالرمز مرتبط بطلبك وحده. لا تشاركه قبل الدفع. لا ترسل PIN أو كلمة مرور أو مفتاح API.'+
                    '\nالصلاحية: '+views.safe(challenge['expires_at'])+'\nبدون الملاحظة المطابقة لا يزيد الرصيد تلقائيًا؛ راجع الإدارة إذا دفعت بالفعل.')
                if not enabled(self.project_root):text+='\n\n<b>اختبار للإدارة:</b> المراقبة العامة معطلة حتى نثبت وصول الملاحظة عبر API. التحقق هنا يسجل دفعة حقيقية إن تم دفعها.'
                self._show_panel(actor,views.Panel(text,rows),message_id)
            elif action=='check':
                result,_=self._binance_note_service().confirm(quote_id,actor)
                if quote.order_id:self._finish_assigned_payment(quote,result['payment_id'])
                self._commerce_notice(actor,'تم اعتماد التحويل لطلبك','طابقنا رمز الملاحظة والمستلم والحساب المرسل والمبلغ والعملة والتوقيت من سجل Binance. حُفظ الدفع مرة واحدة.',message_id)
            else:raise ValueError('Unknown private note action')
        except (ValueError,RuntimeError,OSError,KeyError,TypeError):
            self._commerce_notice(actor,'لم نعتمد التحويل','لم نجد تحويلًا واحدًا مطابقًا للطلب ورمز ملاحظته. لا تدفع مرة أخرى؛ إذا دفعت بالفعل اطلب مراجعة التحويل.',message_id)

    def _commerce_text_input(self,actor,text,waiting):
        if waiting.get('action')!='binance_note_payer':return super()._commerce_text_input(actor,text,waiting)
        try:
            if waiting.get('expires',0)<=time.monotonic():raise ValueError('Expired note setup')
            if not enabled(self.project_root) and actor not in admin_ids(self):raise ValueError('Note echo test is not verified')
            self._binance_note_service().issue(waiting['quote_id'],actor,text.strip())
            self._binance_note_callback('bn:open:'+waiting['quote_id'],actor,waiting.get('message_id'))
        except (ValueError,RuntimeError,OSError,KeyError,TypeError):
            self._commerce_notice(actor,'راجع حساب الدفع','أرسل UID صحيحًا وابدأ من طلب شحن محفوظ، قبل التحويل وخلال صلاحية السعر.',waiting.get('message_id'))
        return True

    def _commerce_command(self,text,actor,is_admin,username='',*,message_id=None):
        parts=text.split()
        if not parts or parts[0].split('@')[0].lower()!='/binance_note_test':
            return super()._commerce_command(text,actor,is_admin,username,message_id=message_id)
        if not is_admin or actor not in admin_ids(self) or len(parts)!=2:
            self._commerce_notice(actor,'اختبار الملاحظة','للإدارة فقط: /binance_note_test ثم UUID لطلب شحن Binance يخص حسابك. لا تُرسل مفاتيح سرية.',message_id)
            return True
        self._binance_note_callback('bn:issue:'+parts[1],actor,message_id)
        return True
