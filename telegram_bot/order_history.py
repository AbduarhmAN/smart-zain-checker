"""Read-only navigation over durable payment receipts and checker jobs."""
from __future__ import annotations
from domain import decimal_json as json
from domain.decimal_money import egp_from_minor
import re
import hashlib
import secrets
import time
from pathlib import Path
from decimal import Decimal
from uuid import UUID
from telegram_bot import views
from telegram_bot.authorization import user_id,admin_ids


PAYMENT_LABELS = {'ready':'بانتظار الدفع', 'pending':'بانتظار مراجعة التحويل',
                  'approved':'الدفع معتمد', 'fulfilled':'الدفع محفوظ والطلب أُضيف للطابور',
                  'rejected':'مرجع الدفع مرفوض'}
JOB_LABELS = {'pending':'بانتظار بدء الفحص', 'active':'جارٍ الفحص',
              'completed':'اكتمل الفحص', 'archived':'اكتمل الفحص',
              'paused':'الفحص متوقف مؤقتًا', 'failed':'الفحص يحتاج مراجعة',
              'cancelled':'الفحص ملغى'}


class OrderHistoryMixin:
    @staticmethod
    def _history_legacy_stop_allowed(record, actor):
        """Ownership and an accepted checkout authorize stopping, including zero-price promos."""
        job=record.get('job') or {};payment=record.get('payment') or {};mapping=job.get('column_mapping',{})
        return (str(record.get('owner'))==str(actor) and not record.get('draft',{}).get('commerce_order_id')
            and job.get('status') in {'pending','active','paused'}
            and payment.get('status') in {'approved','fulfilled'}
            and mapping.get('telegram_chat_id')==actor
            and mapping.get('payment_token')==f'telegram:{actor}:{record.get("token")}')

    @staticmethod
    def _history_legacy_stop_note(record):
        draft=record.get('draft') or {};payment=record.get('payment') or {}
        amount=draft.get('egp_amount',payment.get('amount_egp'))
        try:free=amount is not None and Decimal(str(amount)).is_finite() and Decimal(str(amount))==0
        except (ValueError,ArithmeticError):free=False
        text='ستصلك النتيجة المنجزة، ولا يتوقف طابور بقية العملاء.'
        if free:text+=' الطلب مجاني؛ لا يوجد مبلغ نقدي لاسترداده.'
        else:text+=' دفعتك القديمة محفوظة؛ تسوية أي مبلغ مسترد لهذا المسار تحتاج مراجعة الإدارة.'
        if draft.get('promo_applied') or draft.get('promo_code') or payment.get('kind')=='promo':
            text+=' عرض المرة الواحدة أو الشيت يبقى مستهلكًا بعد الإيقاف؛ لا يُعاد استخدام الكود لنفس المنفعة.'
        return text

    def _history_input_signature(self, draft, *, live=False):
        from telegram_bot.sheet_workflow import file_version
        from telegram_bot.commerce.bridge import file_sha256
        if draft.get('commerce_source'):
            source=draft['commerce_source']
        elif draft.get('payment_input_source') and not live:
            source=draft['payment_input_source']
        else:
            path=Path(draft['path']).resolve()
            if path.parent != self.project_root.resolve() or tuple(draft.get('version',())) != file_version(path):
                raise ValueError('Paid input is unavailable or changed')
            digest=file_sha256(path)
            if tuple(draft.get('version',())) != file_version(path):raise ValueError('Input changed during review')
            source={'file_sha256':digest,**{key:draft[key] for key in ('sheet_index','mapping','mode','amount_target')}}
        selected={key:source[key] for key in ('file_sha256','sheet_index','mapping','mode','amount_target')}
        return hashlib.sha256(json.dumps(selected,sort_keys=True,allow_nan=False).encode('utf-8')).hexdigest()

    def _history_capture_input(self,draft):
        from telegram_bot.commerce.bridge import file_sha256
        self._history_input_signature(draft,live=True)
        source={'file_sha256':file_sha256(Path(draft['path'])),
                **{key:draft[key] for key in ('sheet_index','mapping','mode','amount_target')}}
        draft['payment_input_source']=source

    def _history_existing_paid_input(self, token, draft, actor, message_id=None):
        """Require an explicit choice when uploaded bytes match a paid request."""
        candidates=[]
        wallets=None
        for record in self._history_orders(actor):
            if record['token']==token:continue
            paid=record['payment'].get('status') in {'approved','fulfilled'}
            if record['payment'].get('kind')=='wallet':
                if wallets is None:wallets=self._history_json('user_wallets.json')
                paid=paid or any(e.get('type')=='debit' and e.get('idempotency_key')==f'wallet:{actor}:{record["token"]}'
                                for e in wallets.get(str(actor),{}).get('history',[]))
            if record['draft'].get('commerce_order_id'):
                state=self._checkout_state(record['draft'],actor)
                paid=state.get('payment_state') in {'reserved','settled'}
            if paid:candidates.append(record)
        if not candidates:return False
        from telegram_bot.commerce.bridge import file_sha256
        signature=self._history_input_signature(draft,live=True)
        consent=getattr(self,'_duplicate_payment_reviews',{}).get((actor,token))
        if consent and consent.get('confirmed') and consent['expires']>time.monotonic() and consent['signature']==signature:return False
        digest=draft.get('commerce_source',{}).get('file_sha256')
        if digest is None:
            digest=file_sha256(Path(draft['path']))
        for record in candidates:
            try:
                self._history_input_signature(record['draft'])
                previous=(record['draft'].get('commerce_source') or record['draft'].get('payment_input_source') or {}).get('file_sha256')
                if previous is None:previous=file_sha256(Path(record['draft']['path']))
            except (OSError,ValueError,KeyError,TypeError):continue
            if previous != digest:continue
            self._duplicate_payment_reviews={key:value for key,value in getattr(self,'_duplicate_payment_reviews',{}).items()
                if value['expires']>time.monotonic()}
            self._duplicate_payment_reviews[(actor,token)]={'signature':signature,'existing':record['token'],
                'confirmed':False,'expires':time.monotonic()+180}
            # Never fund, cancel or re-enqueue the old request from this navigation.
            self._show_panel(actor,views.Panel('<b>هذا الملف له طلب مدفوع محفوظ</b>\n\n'+views.safe(self._history_title(record))+
                '\n\nتطابقت بصمة SHA-256 لمحتوى الملف، بغض النظر عن اسمه.\n<code>'+digest+'</code>'+
                '\n\nهل تريد متابعة الطلب الموجود أم إنشاء طلب مستقل مع دفع جديد؟ لم نخصم مبلغًا جديدًا.',
                [[views.button('متابعة الطلب الموجود',f'oh:open:{record["token"]}')],
                 [views.button('أريد طلبًا جديدًا مع دفع جديد',f'oh:repeat:{token}')],[views.button('طلباتي','oh:list:0')]]),message_id)
            return True
        return False

    def _history_json(self, name):
        if getattr(self,'project_root',None) is None:return {}
        path = self.project_root / name
        if not path.exists():return {}
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(value, dict):raise ValueError('Invalid payment history')
        return value

    def _history_orders(self, actor, *, admin=False):
        actor = user_id(actor)
        if actor is None or admin and actor not in admin_ids(self):
            raise ValueError('Order history access denied')
        payments = self._history_json('pending_payments.json')
        transition=self._history_json('.commerce/legacy-transition-state.json')
        jobs = {job['id']:job for job in self.queue_service.get_jobs()}
        records = {}
        for token, payment in payments.items():
            if not re.fullmatch(r'[a-f0-9]{12}', token) or not isinstance(payment, dict):continue
            owner = payment.get('user_id')
            if not str(owner).isdigit() or not admin and str(owner) != str(actor):continue
            draft = payment.get('draft_info') or {}
            adopted=transition.get('orders',{}).get(token)
            if adopted:
                if str(adopted['owner'])!=str(owner):raise ValueError('Transition order ownership differs')
                draft={**draft,'commerce_order_id':adopted['order_id'],'commerce_source':adopted['source'],
                    'commerce_rows':adopted['rows'],'commerce_input':str(self.project_root/'.commerce/inputs'/(adopted['source']['file_sha256']+'.xlsx')),
                    'chat':int(owner)}
            # A job ID alone must never disclose another customer's queue entry.
            job = jobs.get(payment.get('job_id'))
            marker = f'telegram:{owner}:{token}'
            if job and job.get('column_mapping', {}).get('payment_token') != marker:job = None
            if job is None:
                job = next((j for j in jobs.values() if j.get('column_mapping', {}).get('payment_token') == marker), None)
            records[token] = {'token':token,'owner':str(owner),'payment':payment,'draft':draft,'job':job}
        with self._ui_lock: drafts = list(self._sheet_drafts.items())
        for token, draft in drafts:
            if not draft.get('commerce_order_id') or not admin and draft.get('chat') != actor:continue
            job = next((j for j in jobs.values() if j.get('column_mapping', {}).get('commerce_order_id') == draft['commerce_order_id']
                        and j.get('column_mapping', {}).get('telegram_chat_id') == draft.get('chat')), None)
            records[token] = {'token':token,'owner':str(draft['chat']),'payment':{},'draft':draft,'job':job}
        return sorted(records.values(), key=lambda r:(r['payment'].get('created_at',''),r['token']), reverse=True)

    def _history_title(self, record):
        draft = record['draft'];job = record['job'] or {};payment = record['payment']
        reference = payment.get('operation_id') or draft.get('operation_id') or draft.get('commerce_order_id') or record['token']
        name = draft.get('sheet_name') or payment.get('sheet_name') or job.get('sheet_name') or 'ملف'
        status = JOB_LABELS.get(job.get('status'), PAYMENT_LABELS.get(payment.get('status'),'راجع حالة الطلب'))
        if job.get('column_mapping',{}).get('customer_stop'):
            status='أوقف العميل الطلب' if job.get('status')=='completed' else 'طلب الإيقاف؛ ننتظر انتهاء الأعمال الجارية'
        return f'{reference} · {name} · {status}'

    def _history_list(self, actor, offset, message_id=None, *, admin=False):
        if self._history_server_enabled(actor, admin=admin):
            return self._history_server_list(actor, offset, message_id, admin=admin)
        records = self._history_orders(actor, admin=admin)
        offset = max(0, min(offset, max(0, (len(records)-1)//8*8)))
        action = 'admin' if admin else 'list'
        rows = [[views.button(self._history_title(r)[:100],f'oh:{"inspect" if admin else "open"}:{r["token"]}')] for r in records[offset:offset+8]]
        if offset:rows.append([views.button('الأحدث',f'oh:{action}:{max(0,offset-8)}')])
        if offset+8<len(records):rows.append([views.button('الأقدم',f'oh:{action}:{offset+8}')])
        rows.append([views.button('سجل المحفظة','oh:ledger:0')])
        rows.append([views.button('تحديث الطلبات',f'oh:{action}:{offset}')])
        rows.append([views.button('الرئيسية','home')])
        text = '<b>طلبات العملاء</b>' if admin else '<b>طلباتي ومدفوعاتي</b>'
        text += '\n\nاختر طلبًا لمراجعة الدفع وحالة الفحص. عرض الحالة لا يخصم رصيدًا أو يعيد تنفيذ الطلب.' if records else '\n\nلا توجد طلبات دفع محفوظة.'
        self._show_panel(actor,views.Panel(text,rows),message_id)

    def _history_order(self, actor, token, message_id=None, *, admin=False):
        record = next((r for r in self._history_orders(actor,admin=admin) if r['token']==token),None)
        if record is None:raise ValueError('Unknown or unowned order')
        draft = record['draft'];payment = record['payment'];job = record['job'] or {}
        order = draft.get('commerce_order_id');lines=['<b>متابعة الطلب</b>',views.safe(self._history_title(record))]
        if admin:lines.append('العميل: <code>'+record['owner']+'</code>')
        if order:
            state = self._checkout_state(draft,int(record['owner']))
            lines += ['رقم الطلب: <code>'+views.safe(order)+'</code>', 'المبلغ المحفوظ: '+egp_from_minor(state['quoted_minor'])+' جنيه',
                      'حالة الدفع: '+views.safe(state['payment_state'])]
            if not state.get('refund_consent_reference'):
                lines.append('دفعتك محفوظة بسعرها الأصلي. أكّد استرداد قيمة غير المنفذ والأعطال للمحفظة قبل بدء الفحص؛ لن يُخصم مبلغ جديد.')
            if state.get('charged_minor') is not None:
                lines.append('الرسوم النهائية: '+egp_from_minor(state['charged_minor'])+' جنيه')
                lines.append('المُعاد للمحفظة: '+egp_from_minor(state.get('refunded_minor',0))+' جنيه')
        else:
            amount = draft.get('egp_amount',payment.get('amount_egp'))
            if amount is not None:lines.append('المبلغ المحفوظ: '+views.safe(amount)+' جنيه')
            lines.append('حالة الدفع: '+PAYMENT_LABELS.get(payment.get('status'),'تحتاج مراجعة'))
            if payment.get('kind')=='wallet':
                wallet = self._history_json('user_wallets.json').get(record['owner'],{})
                receipt = next((e for e in wallet.get('history',[]) if e.get('type')=='debit'
                                and e.get('idempotency_key')==f'wallet:{record["owner"]}:{token}'),None)
                lines.append('خصم المحفظة مؤكد: '+views.safe(receipt['amount'])+' جنيه' if receipt else 'إيصال خصم المحفظة يحتاج مراجعة؛ لا تدفع مرة أخرى.')
        lines.append('حالة الفحص: '+JOB_LABELS.get(job.get('status'),'لم نجد طلب الفحص المرتبط؛ تحتاج الإدارة مراجعته.'))
        if job:
            lines.append('رقم الفحص: <code>'+views.safe(job['id'])+'</code>')
            lines.append(f"السجلات: {views.count(job.get('total_records',0))}")
            lines.append('تمت معالجتها: '+views.count(max(0,job.get('completed',0)-job.get('cancelled_count',0))))
            if job.get('cancelled_count'):lines.append('لم تُفحص بطلبك: '+views.count(job['cancelled_count']))
            if job.get('status')=='pending':
                pending=[j for j in self.queue_service.get_jobs() if j.get('status') in {'pending','active'} and j.get('column_mapping',{}).get('payment_token')]
                position=next((i for i,j in enumerate(pending) if j['id']==job['id']),None)
                if position is not None:
                    if getattr(self.orchestrator,'cross_file_enabled',False) is True:
                        lines.append('ترتيب الإضافة: '+str(position+1))
                        lines.append('قد يبدأ فحص الأرقام المتاحة قبل اكتمال الملفات السابقة.')
                    else:
                        lines.append('ملفات أمامك في الطابور: '+str(position))
        if job.get('status') not in {'completed','archived'}:lines.append('ستظهر النتيجة بعد اكتمال الفحص؛ لا توجد نتيجة مؤرشفة لهذا الطلب حتى الآن.')
        rows=[]
        if order:
            rows.append([views.button('الأرشيف',f'oh:archive:{token}')])
            rows.append([views.support_button()])
            if not state.get('refund_consent_reference') and state.get('payment_state')=='reserved':
                rows.append([views.button('أوافق على الاسترداد للمحفظة وأتابع طلبي',f'oh:consent:{token}')])
            if job.get('status') in {'pending','active','paused'} and not job.get('column_mapping',{}).get('customer_stop'):
                rows.append([views.button('إيقاف طلبي واسترداد المتبقي',f'oh:stop:{token}')])
        else:
            lines.append('أُنشئ هذا الطلب بالنظام السابق. رقم عمليته محفوظ أعلاه؛ ربطه بالأرشيف الجديد يحتاج استكمالًا.')
            if self._history_legacy_stop_allowed(record,actor) and not job.get('column_mapping',{}).get('customer_stop'):
                rows.append([views.button('إيقاف فحص طلبي',f'oh:stop:{token}')])
            if job.get('column_mapping',{}).get('customer_stop'):
                lines.append(self._history_legacy_stop_note(record))
        rows.extend([[views.button('تحديث الحالة',f'oh:{"inspect" if admin else "open"}:{token}')],
                     [views.button('الطلبات',f'oh:{"admin" if admin else "list"}:0')],[views.button('الرئيسية','home')]])
        self._show_panel(actor,views.Panel('\n\n'.join(lines),rows),message_id)

    def _history_ledger(self, actor, offset, message_id=None):
        actor = user_id(actor)
        if actor is None:raise ValueError('Wallet history identity required')
        if self._history_server_enabled(actor):
            return self._history_server_ledger(actor, offset, message_id)
        wallet = self._history_json('user_wallets.json').get(str(actor),{})
        entries = list(reversed(wallet.get('history',[])));offset=max(0,offset)
        lines=['<b>سجل محفظتي</b>'];rows=[]
        for entry in entries[offset:offset+8]:
            label='إضافة' if entry.get('type')=='credit' else 'خصم'
            lines.append(f"{label}: {views.safe(entry.get('amount'))} جنيه\n{views.safe(entry.get('reason'),120)}\n{views.safe(entry.get('timestamp'))}")
        if not entries:lines.append('لا توجد حركات محفوظة في هذه المحفظة.')
        if offset:rows.append([views.button('الأحدث',f'oh:ledger:{max(0,offset-8)}')])
        if offset+8<len(entries):rows.append([views.button('الأقدم',f'oh:ledger:{offset+8}')])
        rows.append([views.button('طلباتي','oh:list:0'),views.button('المحفظة','wallet')])
        self._show_panel(actor,views.Panel('\n\n'.join(lines),rows),message_id)

    def _history_server_enabled(self, actor, *, admin=False):
        root=getattr(self,'project_root',None)
        if root is not None and (root/'.commerce/legacy-transition-state.json').exists():return True
        return getattr(self,'_uses_commerce_wallet',lambda _:False)(actor) is True

    def _history_server_list(self, actor, offset, message_id=None, *, admin=False):
        if user_id(actor) is None or admin and actor not in admin_ids(self):raise ValueError('History authority required')
        page=self._commerce().rpc('customer_order_history',{'p_actor_telegram_id':actor,'p_offset':max(0,offset),'p_limit':8,'p_admin':admin})
        rows=[]
        for item in page['items']:
            order=str(UUID(item['order_id']))
            label=views.count(item['requested_checks'])+' صف · '+str(item['payment_state'])+' · '+order[:8]
            rows.append([views.button(label,'oh:'+('cinspect' if admin else 'copen')+':'+order)])
        action='cadmin' if admin else 'clist'
        if offset:rows.append([views.button('الأحدث',f'oh:{action}:{max(0,offset-8)}')])
        if offset+8<page['total']:rows.append([views.button('الأقدم',f'oh:{action}:{offset+8}')])
        rows.extend([[views.button('تحديث الطلبات',f'oh:{action}:{offset}')],
                     [views.button('سجل محفظتي','oh:ledger:0')],[views.button('الرئيسية','home')]])
        self._show_panel(actor,views.Panel(('<b>طلبات العملاء</b>' if admin else '<b>طلباتي</b>')+
            ('\n\nاختر طلبًا لمراجعة حالته.' if rows and page['items'] else '\n\nلا توجد طلبات محفوظة.'),rows),message_id)

    def _history_server_order(self, actor, order, message_id=None, *, admin=False):
        if user_id(actor) is None or admin and actor not in admin_ids(self):raise ValueError('Order authority required')
        order=str(UUID(order))
        item=self._commerce().rpc('customer_order_detail',{'p_actor_telegram_id':actor,'p_order_id':order,'p_admin':admin})
        if item.get('order_id')!=order or not admin and item.get('owner_telegram_id')!=actor:raise ValueError('Unowned server order')
        labels={'not_started':'لم يبدأ الفحص','queued':'بانتظار الفحص','running':'جارٍ الفحص','paused':'الفحص متوقف مؤقتًا','terminal':'انتهت المعالجة'}
        payment={'unpaid':'بانتظار الدفع','reserved':'القيمة محجوزة','settled':'تمت التسوية','refunded':'أُعيدت القيمة','partial_refund':'تمت التسوية مع استرداد جزئي'}
        execution_label=labels.get(item['execution_state'],'تحتاج مراجعة')
        if item.get('customer_stop_reference') and item['payment_state']=='reserved' and item['execution_state'] in {'queued','not_started'}:
            execution_label='قيد تنفيذ الإيقاف وتسوية الحجز'
        lines=['<b>متابعة طلبي</b>','رقم الطلب: <code>'+order+'</code>',
            'عدد الصفوف: '+views.count(item['requested_checks']),
            'القيمة المحفوظة: '+egp_from_minor(item['quoted_minor'])+' جنيه',
            'الدفع: '+payment.get(item['payment_state'],'تحتاج مراجعة'),
            'الفحص: '+execution_label]
        if 'account_count' in item or 'service_count' in item:
            accounts,services=item.get('account_count'),item.get('service_count')
            if type(accounts) is not int or type(services) is not int or min(accounts,services)<0 or accounts+services!=item['requested_checks']:
                raise ValueError('Order check counts do not match its manifest')
            lines[3:3]=['عدد أرقام الحسابات: '+views.count(accounts),
                        'عدد أرقام الخدمة/المحافظ: '+views.count(services)]
        if admin:lines.append('العميل: <code>'+str(item['owner_telegram_id'])+'</code>')
        if item.get('charged_minor') is not None:
            lines.extend(['الرسوم النهائية: '+egp_from_minor(item['charged_minor'])+' جنيه',
                'المُعاد للمحفظة: '+egp_from_minor(item['refunded_minor'])+' جنيه'])
        stopped=bool(item.get('customer_stop_reference'))
        if stopped:
            if item['payment_state'] in {'refunded','partial_refund','settled'}:
                lines.append('اكتملت تسوية طلب الإيقاف. مبلغ الاسترداد أعلاه عاد إلى المحفظة.')
            elif item['execution_state'] in {'queued','not_started'}:
                lines.append('طلب الإيقاف محفوظ؛ لن يبدأ فحص صفوف جديدة. يجري تجهيز تقرير الإيقاف وتسوية الحجز تلقائيًا، حتى لو كانت متابعة الطلبات الجديدة معطّلة.')
            else:lines.append('طلب الإيقاف محفوظ؛ تُستكمل الأعمال الجارية فقط وتُسلّم النتيجة المنجزة قبل تسوية المتبقي.')
        rows=[]
        if not admin:
            rows.append([views.support_button()])
            if item['delivery_state']=='confirmed':rows.append([views.button('تحميل النتيجة',f'ar:result:{order}')])
            if item['payment_state']=='reserved':
                if not item.get('refund_consent_reference'):rows.append([views.button('أوافق على الاسترداد للمحفظة وأتابع',f'oh:cconsent:{order}')])
                if not item.get('customer_stop_reference'):rows.append([views.button('إيقاف طلبي واسترداد المتبقي',f'oh:cstop:{order}')])
        if item['delivery_state']!='confirmed':
            lines.append('ستظهر التسوية والنتيجة المنجزة بعد اكتمال الإيقاف.' if stopped else 'ستظهر النتيجة بعد اكتمال الفحص وتسليمها.')
        rows.extend([[views.button('تحديث الحالة','oh:'+('cinspect' if admin else 'copen')+':'+order)],
            [views.button('الطلبات','oh:'+('cadmin' if admin else 'clist')+':0')]])
        self._show_panel(actor,views.Panel('\n\n'.join(lines),rows),message_id)
        return item

    def _history_server_ledger(self, actor, offset, message_id):
        page=self._commerce().rpc('customer_wallet_activity',{'p_telegram_user_id':actor,'p_offset':max(0,offset),'p_limit':8})
        kinds={'topup':'شحن','reserve':'حجز مؤقت للملف','service':'رسوم الملف','refund':'استرداد','release':'فك حجز','commission':'عمولة','withdrawal':'سحب','adjustment':'تسوية أو رصيد ترويجي'}
        lines=['<b>سجل محفظتي</b>','المبالغ إجمالية لكل ملف. الحجز تحويل مؤقت بين المتاح والمحجوز.']
        def signed(value):
            amount=Decimal(str(value))
            return ('+' if amount>0 else '')+egp_from_minor(amount)+' جنيه'
        for entry in page['items']:
            parts=['<b>'+views.safe(kinds.get(entry['kind'],entry['kind']))+' · '+signed(entry['delta_minor'])+'</b>']
            if entry.get('filename'):parts.append(views.safe(entry['filename'],120))
            elif entry.get('order_id') and entry.get('owned_order'):
                parts.append('ملف · الطلب '+views.safe(str(entry['order_id'])[:8]))
            if entry.get('wallet_kind')=='partner':parts.append('محفظة الأرباح')
            buckets=entry.get('bucket_deltas',{})
            labels={'available':'المتاح','reserved':'المحجوز'}
            parts.extend(views.safe(labels.get(bucket,bucket))+': '+signed(value) for bucket,value in buckets.items())
            parts.append(views.safe(entry['created_at']))
            lines.append('\n'.join(parts))
        if not page['items']:lines.append('لا توجد حركات محفوظة.')
        rows=[]
        if offset:rows.append([views.button('الأحدث',f'oh:ledger:{max(0,offset-8)}')])
        if offset+8<page['total']:rows.append([views.button('الأقدم',f'oh:ledger:{offset+8}')])
        rows.append([views.button('طلباتي','oh:list:0'),views.button('المحفظة','wallet')])
        self._show_panel(actor,views.Panel('\n\n'.join(lines),rows),message_id)

    def _history_callback(self, command, actor, message_id=None, callback=None):
        try:
            _,action,value=command.split(':')
            if action in {'clist','cadmin'}:self._history_server_list(actor,int(value),message_id,admin=action=='cadmin')
            elif action in {'copen','cinspect'}:self._history_server_order(actor,value,message_id,admin=action=='cinspect')
            elif action=='cconsent':
                item=self._history_server_order(actor,value,message_id);receipt=(callback or {}).get('id')
                if not isinstance(receipt,str) or not receipt.strip():raise ValueError('Native owner consent required')
                self._commerce().rpc('checkout_consent',{'p_order_id':str(UUID(value)),'p_telegram_user_id':actor,
                    'p_policy_version':item['pricing_snapshot']['policy_version'],'p_reference':'owner-wallet-refund:'+str(actor)+':'+receipt})
                service=getattr(self.orchestrator,'continuous_service',None)
                if service:service.wake()
                self._history_server_order(actor,value,message_id)
            elif action=='cstop':
                item=self._history_server_order(actor,value,message_id)
                if item['payment_state']!='reserved' or not item.get('checker_job_id'):raise ValueError('Funded bound job required')
                job=self.queue_service.get_job(item['checker_job_id'])
                if not job or job.column_mapping.get('telegram_chat_id')!=actor or job.column_mapping.get('commerce_order_id')!=value:
                    raise ValueError('Owned local checker job required')
                nonce=secrets.token_hex(6)
                self._customer_stop_reviews={k:v for k,v in getattr(self,'_customer_stop_reviews',{}).items() if v['expires']>time.monotonic()}
                self._customer_stop_reviews[nonce]={'actor':actor,'token':value,'job_id':job.id,'order':value,'expires':time.monotonic()+180,'server_order':True}
                self._show_panel(actor,views.Panel('<b>تأكيد إيقاف طلبك</b>\n\nلن نبدأ صفوفًا جديدة. ننتظر الأعمال الجارية، ثم نصلك النتيجة المنجزة وتعود قيمة غير المنفذ والأعطال للمحفظة. طابور بقية العملاء يستمر.',
                    [[views.button('أؤكد إيقاف طلبي',f'oh:stopconfirm:{nonce}')],[views.button('الرجوع دون إيقاف',f'oh:copen:{value}')]]),message_id)
            elif action in {'list','admin'}:self._history_list(actor,int(value),message_id,admin=action=='admin')
            elif action in {'open','inspect'}:self._history_order(actor,value,message_id,admin=action=='inspect')
            elif action=='ledger':self._history_ledger(actor,int(value),message_id)
            elif action=='consent':
                record=next((r for r in self._history_orders(actor) if r['token']==value),None);receipt=(callback or {}).get('id')
                if record is None or not record['draft'].get('commerce_order_id') or not isinstance(receipt,str) or not receipt.strip():
                    raise ValueError('Native owned refund consent required')
                state=self._checkout_state(record['draft'],actor)
                self._commerce().rpc('checkout_consent',{'p_order_id':record['draft']['commerce_order_id'],'p_telegram_user_id':actor,
                    'p_policy_version':state['pricing_snapshot']['policy_version'],'p_reference':'legacy-wallet-refund:'+str(actor)+':'+receipt})
                service=getattr(self.orchestrator,'continuous_service',None)
                if service:service.wake()
                self._history_order(actor,value,message_id)
            elif action=='stop':
                record=next((r for r in self._history_orders(actor) if r['token']==value),None)
                if record is None or not record['job']:raise ValueError('Accepted owned request required')
                legacy=not record['draft'].get('commerce_order_id')
                if legacy and not self._history_legacy_stop_allowed(record,actor):raise ValueError('Accepted owned legacy request required')
                nonce=secrets.token_hex(6)
                self._customer_stop_reviews={key:review for key,review in getattr(self,'_customer_stop_reviews',{}).items() if review['expires']>time.monotonic()}
                self._customer_stop_reviews[nonce]={'actor':actor,'token':value,'job_id':record['job']['id'],
                    'order':record['draft'].get('commerce_order_id'),'legacy':legacy,'expires':time.monotonic()+180}
                self._show_panel(actor,views.Panel('<b>تأكيد إيقاف طلبك</b>\n\nلن نبدأ أعمالًا جديدة لهذا الطلب. الأعمال الجارية تنتظر حتى تنتهي. '+
                    (self._history_legacy_stop_note(record) if legacy else
                    'ستصلك النتيجة المنجزة، ثم تُعاد قيمة غير المنفذ والأعطال الفنية إلى محفظتك. نتائج الفحص الصحيحة تبقى مدفوعة. '+
                    'لا يتوقف طابور باقي العملاء. بتأكيد الإيقاف توافق على إعادة المتبقي للمحفظة.'),
                    [[views.button('أؤكد إيقاف طلبي',f'oh:stopconfirm:{nonce}')],[views.button('الرجوع دون إيقاف',f'oh:open:{value}')]]),message_id)
            elif action=='stopconfirm':
                review=getattr(self,'_customer_stop_reviews',{}).get(value);receipt=(callback or {}).get('id')
                if not review or review['actor']!=actor or review['expires']<=time.monotonic() or not isinstance(receipt,str) or not receipt.strip():
                    raise ValueError('Owned reviewed stop confirmation required')
                ref='telegram-customer-stop:'+str(actor)+':'+receipt
                if review.get('legacy'):
                    record=next((r for r in self._history_orders(actor) if r['token']==review['token']),None)
                    if record is None or not self._history_legacy_stop_allowed(record,actor) or record['job']['id']!=review['job_id']:
                        raise ValueError('Legacy request ownership or state changed')
                else:self._commerce().rpc('request_customer_stop',{'p_order_id':review['order'],'p_telegram_user_id':actor,'p_reference':ref})
                self.queue_service.request_customer_stop(review['job_id'],actor,ref)
                self.orchestrator.apply_customer_stop(review['job_id'])
                service=getattr(self.orchestrator,'continuous_service',None)
                if service:service.wake()
                if review.get('server_order'):self._history_server_order(actor,review['token'],message_id)
                else:self._history_order(actor,review['token'],message_id)
            elif action=='repeat':
                receipt=(callback or {}).get('id')
                if user_id(actor) is None or not isinstance(receipt,str) or not receipt.strip():raise ValueError('Authenticated duplicate confirmation required')
                review=getattr(self,'_duplicate_payment_reviews',{}).get((actor,value))
                draft=self._sheet_drafts.get(value)
                if not review or review['expires']<=time.monotonic() or not draft or draft.get('chat')!=actor:
                    raise ValueError('Expired duplicate confirmation')
                if self._history_input_signature(draft,live=True)!=review['signature']:raise ValueError('Duplicate input changed')
                review['confirmed']=True
                self._show_panel(actor,views.Panel('<b>اخترت إنشاء طلب مستقل</b>\n\nلم نخصم مبلغًا بعد. افتح الرسوم وراجع المبلغ ثم اختر طريقة الدفع. الطلب السابق يبقى محفوظًا.',
                    [[views.button('مراجعة الرسوم والدفع',f'map:{value}:pay')],[views.button('متابعة الطلب السابق',f'oh:open:{review["existing"]}')]]),message_id)
            elif action=='archive':
                record=next((r for r in self._history_orders(actor) if r['token']==value),None)
                if record is None or not record['draft'].get('commerce_order_id'):raise ValueError('Order archive unavailable')
                self._archive_panel(record['draft']['commerce_order_id'],actor,message_id)
            else:raise ValueError('Unknown history action')
        except (ValueError,OSError,KeyError,TypeError,RuntimeError):
            self._show_panel(actor,views.Panel('<b>تعذر عرض السجل</b>\n\nراجع صلاحية الوصول أو حاول تحديث الحالة لاحقًا. لم نغيّر الطلب أو الرصيد.',
                [[views.button('الرئيسية','home')]]),message_id)
