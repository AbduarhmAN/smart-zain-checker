"""Reviewed administration of immutable customer quote defaults and tiers."""
import secrets
import time
from telegram_bot import views
from telegram_bot.authorization import admin_ids,user_id
from .bot_workflow import _serial_user
from .client import CommerceUnavailable
from .legacy_wallets import minor
from .checkout import egp


class PricingUiMixin:
    def _commerce_command(self,text,actor,is_admin,username='',*,message_id=None):
        command=text.split()[0].split('@')[0].lower() if text.split() else ''
        if command!='/prices':return super()._commerce_command(text,actor,is_admin,username,message_id=message_id)
        self._pricing_callback('pr:home:0',actor,message_id);return True

    def _pricing_show(self,actor,target,message_id=None):
        cfg=self._commerce().rpc('customer_pricing',{'p_telegram_user_id':target})
        text=('<b>أسعار العملاء</b>\n\n'+('الإعداد العام' if target==0 else 'إعداد العميل: <code>'+str(target)+'</code>')+
              '\nسعر العميل: '+egp(cfg['base_minor'])+' جنيه\nأول '+str(cfg['service_threshold'])+' رقم خدمة بهذا السعر.'+
              '\nكل رقم خدمة إضافي: '+egp(cfg['service_excess_minor'])+' جنيه\nأرقام الحسابات تُحسب بالسعر الأساسي مهما كان عددها.'+
              '\nالحد الأقصى لشيت العميل: 4000.\nتسري التغييرات على الأسعار الجديدة؛ الطلبات التي عُرض سعرها تبقى بسعرها المحفوظ.')
        rows=[[views.button('تعديل الأسعار',f'pr:edit:{target}')],
              [views.button('سعر شيت الشريك وموافقات الفحص','ps:settings:0')]]
        if target:rows.append([views.button('إعادة العميل للسعر العام',f'pr:reset:{target}')])
        rows.extend([[views.button('سعر خاص لعميل','pr:customer:0')],[views.button('السعر العام','pr:home:0')],[views.button('الرئيسية','home')]])
        self._show_panel(actor,views.Panel(text,rows),message_id)

    def _price_review_start(self,actor,target,message_id,*,reset=False):
        token=secrets.token_hex(6)
        if not hasattr(self,'_price_reviews'):self._price_reviews={}
        self._price_reviews={key:value for key,value in self._price_reviews.items() if value['expires']>time.monotonic()}
        if len(self._price_reviews)>=128:raise ValueError('Price review capacity reached')
        review={'actor':actor,'target':target,'expires':time.monotonic()+1800,'step':0,'values':{},'ready':False}
        self._price_reviews[token]=review
        if reset:
            review['values']={'inherit_global':True};self._price_review_panel(actor,token,message_id)
        else:
            with self._ui_lock:self._waiting_user_inputs[actor]={'action':'customer_price_value','token':token,'message_id':message_id,'expires':review['expires']}
            self._commerce_notice(actor,'السعر الأساسي لكل عميل','أرسل السعر بالجنيه، بدقة القرش. السعر الافتراضي 0.01 جنيه. لا يتغير رصيد العميل بهذه الخطوة.',message_id)

    def _price_review_panel(self,actor,token,message_id):
        review=self._price_reviews[token];review['ready']=True;review['expires']=time.monotonic()+180
        text='<b>مراجعة تعديل السعر</b>\n\n'+('كل العملاء' if review['target']==0 else 'العميل: <code>'+str(review['target'])+'</code>')
        if review['values'].get('inherit_global'):text+='\nإلغاء السعر الخاص والعودة إلى الإعداد العام.'
        else:
            text+='\nالسعر الأساسي: '+egp(review['values']['base_minor'])+' جنيه'
            text+='\nأول 500 رقم خدمة بالسعر الأساسي؛ كل رقم خدمة إضافي: '+egp(review['values']['service_excess_minor'])+' جنيه.'
        text+='\n\nلن تتغير أسعار الطلبات المحفوظة أو أرصدة العملاء.'
        self._show_panel(actor,views.Panel(text,[[views.button('أؤكد حفظ السعر',f'pr:save:{token}','success')],
            [views.button('إلغاء','pr:home:0')]]),message_id)

    @_serial_user
    def _pricing_callback(self,command,actor,message_id=None,callback=None):
        if actor not in admin_ids(self):
            self._commerce_notice(actor,'غير مصرح','تعديل الأسعار خاص بالإدارة.',message_id);return
        try:
            _,action,value=command.split(':')
            if action=='home':
                self._price_reviews={k:v for k,v in getattr(self,'_price_reviews',{}).items() if v['actor']!=actor}
                with self._ui_lock:self._waiting_user_inputs.pop(actor,None)
                self._pricing_show(actor,0,message_id)
            elif action=='customer':
                with self._ui_lock:self._waiting_user_inputs[actor]={'action':'customer_price_target','expires':time.monotonic()+600,'message_id':message_id}
                self._commerce_notice(actor,'رقم حساب العميل','أرسل رقم Telegram أو رقم الحساب الظاهر في محفظته.',message_id)
            elif action in {'edit','reset'}:
                target=int(value)
                if target<0 or target and user_id(target) is None:raise ValueError('Valid price target required')
                if action=='reset' and target==0:raise ValueError('Global prices cannot reset themselves')
                self._price_review_start(actor,target,message_id,reset=action=='reset')
            elif action=='save':
                review=getattr(self,'_price_reviews',{}).get(value);receipt=(callback or {}).get('id')
                if (not review or review['actor']!=actor or not review['ready'] or review['expires']<=time.monotonic()
                    or not isinstance(receipt,str) or not receipt.strip()):raise ValueError('Authenticated reviewed price confirmation required')
                self._commerce().rpc('configure_customer_pricing',{'p_actor_telegram_id':actor,'p_target_telegram_id':review['target'],
                    'p_rules':review['values'],'p_reference':'telegram-price:'+str(actor)+':'+receipt})
                self._price_reviews.pop(value,None);self._pricing_show(actor,review['target'],message_id)
            else:raise ValueError('Unknown pricing action')
        except (CommerceUnavailable,OSError,ValueError,KeyError,TypeError):
            self._commerce_notice(actor,'تعذر عرض أو تعديل السعر','راجع إعداد الأسعار قبل تكرار الحفظ. لم نغيّر أسعار طلبات العملاء المحفوظة.',message_id)

    def _commerce_text_input(self,actor,text,waiting):
        if waiting.get('action') not in {'customer_price_target','customer_price_value'}:
            return super()._commerce_text_input(actor,text,waiting)
        try:
            if actor not in admin_ids(self) or waiting.get('expires',0)<=time.monotonic():raise ValueError('Valid administrator input required')
            if waiting['action']=='customer_price_target':
                target=user_id(text.strip())
                if target is None:raise ValueError('Customer identity required')
                from telegram_bot.payment_manager import PAYMENT_MGR
                found=PAYMENT_MGR.find_user_by_account_id(target)
                if found:target=int(found[0])
                self._pricing_show(actor,target,waiting.get('message_id'));return True
            review=self._price_reviews.get(waiting['token'])
            if not review or review['actor']!=actor or review['expires']<=time.monotonic():raise ValueError('Valid price review required')
            amount=minor(text.strip())
            if amount>100000000:raise ValueError('Price exceeds supported range')
            review['values']['base_minor' if review['step']==0 else 'service_excess_minor']=amount
            review['step']+=1
            if review['step']==1:
                with self._ui_lock:self._waiting_user_inputs[actor]=waiting
                self._commerce_notice(actor,'سعر الخدمة بعد أول 500','أرسل سعر كل رقم خدمة يبدأ من رقم الخدمة 501 في الشيت، بالجنيه. أرقام الحسابات تبقى بالسعر الأساسي.',waiting.get('message_id'))
            else:
                review['values']['service_threshold']=500
                self._price_review_panel(actor,waiting['token'],waiting.get('message_id'))
        except (CommerceUnavailable,OSError,ValueError,KeyError,TypeError):
            if actor in admin_ids(self) and waiting.get('expires',0)>time.monotonic():
                with self._ui_lock:self._waiting_user_inputs[actor]=waiting
            self._commerce_notice(actor,'راجع بيانات السعر','أدخل رقم حساب صحيحًا أو سعرًا بالجنيه بمنزلتين عشريتين كحد أقصى.',waiting.get('message_id'))
        return True
