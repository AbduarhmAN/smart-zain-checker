"""Owned retrieval and authenticated administrative archive reconciliation."""
from __future__ import annotations
import time
from uuid import UUID
from telegram_bot import views
from .archive_transport import ArchiveTransport,ArchiveRejected,ArchiveAmbiguous,UPLOAD_LIMIT,document_receipt
from .bridge import file_sha256
from .bot_workflow import _serial_user
from .client import CommerceUnavailable


class ArchiveUiMixin:
    def _archive_prompt(self,user_id,message_id=None):
        with self._ui_lock:
            self._waiting_user_inputs[user_id]={'action':'commerce_archive_lookup','expires':time.monotonic()+600,'message_id':message_id}
        self._commerce_notice(user_id,'استرجاع طلب','أرسل رقم الطلب الكامل. ستظهر ملفاتك المؤرشفة وأزرار استرجاعها.',message_id)

    def _commerce_text_input(self,user_id,text,waiting):
        if waiting.get('action')!='commerce_archive_lookup':return super()._commerce_text_input(user_id,text,waiting)
        try:
            if waiting.get('expires',0)<=time.monotonic():raise ValueError('Expired archive prompt')
            order=str(UUID(text.strip()))
            self._archive_panel(order,user_id,waiting.get('message_id'),admin=user_id in self.current_authorized_ids)
        except (ValueError,CommerceUnavailable,KeyError,TypeError):
            if waiting.get('expires',0)>time.monotonic():
                with self._ui_lock:self._waiting_user_inputs[user_id]=waiting
            self._commerce_notice(user_id,'راجع رقم الطلب','أرسل رقم الطلب الصحيح لملف يخص حسابك، أو عد إلى الرئيسية.',waiting.get('message_id'))
        return True

    def _archive_transport(self):
        if not hasattr(self,'_archive_api'):self._archive_api=ArchiveTransport(self.bot_token)
        return self._archive_api

    def _commerce_command(self,text,user_id,is_admin,username='',*,message_id=None):
        parts=text.split();command=parts[0].split('@')[0].lower() if parts else ''
        if command not in {'/archive','/archive_admin','/archive_reconcile'}:
            return super()._commerce_command(text,user_id,is_admin,username,message_id=message_id)
        if command!='/archive' and user_id not in self.current_authorized_ids:
            self._commerce_notice(user_id,'غير مصرح','الأصل وفهرس القناة متاحان للإدارة فقط.',message_id);return True
        if command=='/archive' and len(parts)==1:self._archive_prompt(user_id,message_id);return True
        try:
            order=str(UUID(parts[1]))
            if command=='/archive_reconcile':
                if len(parts)!=3 or parts[2] not in {'original','result'}:raise ValueError('Archive kind required')
                dispatcher=getattr(self,'archive_dispatcher',None)
                row=dispatcher.store.lookup(order,parts[2]) if dispatcher else None
                if not row or row['state']!='ambiguous':raise ValueError('No ambiguous archive send')
                with self._ui_lock:
                    self._waiting_user_inputs[user_id]={'action':'commerce_archive_reconcile','order':order,
                        'kind':parts[2],'expires':time.monotonic()+600,'message_id':message_id}
                self._commerce_notice(user_id,'تأكيد رسالة الأرشيف','حوّل رسالة الملف الموجودة في قناة الأرشيف إلى هنا خلال 10 دقائق. سنفحص بصمتها قبل تثبيت الفهرس، دون إرسال نسخة جديدة.',message_id)
            else:self._archive_panel(order,user_id,message_id,admin=command=='/archive_admin')
        except (ValueError,IndexError):
            self._commerce_notice(user_id,'البحث برقم الطلب','اكتب /archive ثم رقم الطلب الكامل. للإدارة: /archive_admin رقم_الطلب أو /archive_reconcile رقم_الطلب original أو result.',message_id)
        except CommerceUnavailable:self._commerce_failed(user_id,message_id)
        return True

    def _archive_panel(self,order,user_id,message_id,*,admin=False):
        if admin and user_id not in self.current_authorized_ids:raise ValueError('Archive administrative access denied')
        result=self._commerce().rpc('admin_archive_index' if admin else 'customer_archive_index',
            {'p_order_id':order} if admin else {'p_order_id':order,'p_telegram_user_id':user_id})
        files=result.get('files',[]);lines=['<b>أرشيف الطلب</b>',f'<code>{order}</code>'];rows=[]
        for file in files:
            kind=file['kind'];label='الأصل' if kind=='original' else 'النتيجة'
            lines.append(f"\n{label} · نهاية مدة الاحتفاظ المسجلة: {views.safe(file['retention_until'])}\nSHA-256: <code>{file['sha256']}</code>")
            rows.append([views.button('استرجاع '+label,f'ar:{kind}:{order}')])
            if admin:
                channel=str(-file['telegram_chat_id'])
                if channel.startswith('100'):
                    rows.append([{'text':'فتح رسالة '+label,'url':f"https://t.me/c/{channel[3:]}/{file['telegram_message_id']}"}])
        if not files:lines.append('لم تُؤكد الأرشفة بعد. تُحفظ الملفات بعد اكتمال الفحص والتسليم وتسوية المبلغ.')
        if admin and getattr(self,'archive_dispatcher',None):
            labels={'original':'الأصل','result':'النتيجة'}
            for kind in ('original','result'):
                local=self.archive_dispatcher.store.lookup(order,kind)
                if local and local['state']=='blocked':
                    lines.append(f"\nأرشفة {labels[kind]} تحتاج مراجعة الإعدادات أو النسخة المحلية.")
                    rows.append([views.button('أعد المحاولة بعد الإصلاح · '+labels[kind],f'ar:retry:{kind}:{order}')])
                elif local and local['state']=='ambiguous':
                    lines.append(f"\nلم يُؤكد إرسال {labels[kind]}. راجع القناة ثم استخدم /archive_reconcile {order} {kind}")
        rows.append([views.button('الرئيسية','home')]);self._show_panel(user_id,views.Panel('\n'.join(lines),rows),message_id)

    @_serial_user
    def _archive_callback(self,command,user_id,message_id):
        try:
            if command.startswith('ar:retry:'):
                _,_,kind,raw=command.split(':');order=str(UUID(raw))
                if user_id not in self.current_authorized_ids:raise ValueError('Only an administrator may retry blocked archive')
                dispatcher=getattr(self,'archive_dispatcher',None)
                if dispatcher is None:raise ValueError('Archive worker unavailable')
                dispatcher.store.retry_blocked(order,kind);dispatcher._wake.set()
                self._commerce_notice(user_id,'أُعيدت المحاولة','سنتحقق من الإعدادات والبصمة قبل الإرسال. الحالات مجهولة الإرسال تتطلب مطابقة الرسالة الفعلية.',message_id)
                return
            _,kind,raw=command.split(':');order=str(UUID(raw))
            admin=user_id in self.current_authorized_ids
            if kind not in {'original','result'} or (kind=='original' and not admin):raise ValueError('Unowned archive kind')
            index=self._commerce().rpc('admin_archive_index' if admin else 'customer_archive_index',
                {'p_order_id':order} if admin else {'p_order_id':order,'p_telegram_user_id':user_id})
            record=next((v for v in index.get('files',[]) if v['kind']==kind),None)
            if record is None:raise ValueError('Archive is not confirmed')
            caption=f"استرجاع {('الأصل' if kind=='original' else 'النتيجة')}\nOrder ID: {order}\nSHA-256: {record['sha256']}"
            api=self._archive_transport()
            try:message=api.api('sendDocument',{'chat_id':user_id,'document':record['telegram_file_id'],'caption':caption})
            except ArchiveRejected as exc:
                if exc.code!=400:raise
                path=self.project_root/'.commerce'/('inputs/'+record['sha256']+'.xlsx' if kind=='original' else 'result_'+order+'.xlsx')
                if file_sha256(path)!=record['sha256'] or path.stat().st_size>UPLOAD_LIMIT:raise ValueError('Verified backup unavailable')
                with path.open('rb') as stream:message=api.api('sendDocument',{'chat_id':user_id,'caption':caption},
                    files={'document':(order+'_'+kind+'.xlsx',stream,'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')})
            document_receipt(message,user_id,expected_size=record['file_size'])
            self._commerce_notice(user_id,'استرجاع الملف','أُرسل الملف المحفوظ لهذا الطلب.',message_id)
        except ArchiveAmbiguous:
            self._commerce_notice(user_id,'تأكد من المحادثة','لم يصل تأكيد الإرسال. تحقق من وصول الملف قبل طلب استرجاعه مرة أخرى.',message_id)
        except (CommerceUnavailable,ArchiveRejected,OSError,ValueError,KeyError,TypeError):
            self._commerce_notice(user_id,'تعذر الاسترجاع','تحقق من رقم الطلب وصلاحية الوصول. عند استمرار التعذر، تطلب الإدارة النسخة المحلية وتتحقق من بصمتها.',message_id)

    def _archive_forward(self,message,user_id):
        waiting=self._waiting_user_inputs.get(user_id,{})
        if waiting.get('action')!='commerce_archive_reconcile':return False
        try:
            if user_id not in self.current_authorized_ids or waiting['expires']<=time.monotonic():raise ValueError('Expired or unauthorized reconciliation')
            origin=message.get('forward_origin') or {}
            if origin.get('type')!='channel' or type(origin.get('message_id')) is not int:raise ValueError('Actual channel forward required')
            dispatcher=getattr(self,'archive_dispatcher',None)
            if dispatcher is None:raise ValueError('Archive worker unavailable')
            receipt={'chat':origin.get('chat'),'message_id':origin['message_id'],'document':message.get('document')}
            dispatcher.reconcile(waiting['order'],waiting['kind'],receipt)
            self._waiting_user_inputs.pop(user_id,None)
            self._commerce_notice(user_id,'تم التحقق','تطابقت بصمة الملف. حُفظ تأكيد الرسالة لاستكمال الفهرس دون رفع نسخة جديدة.')
        except (ValueError,KeyError,TypeError,OSError,ArchiveRejected,ArchiveAmbiguous):
            self._commerce_notice(user_id,'راجع الرسالة','حوّل رسالة الملف من قناة الأرشيف نفسها. الملفات الأكبر من حد تنزيل البوت تحتاج مراجعة النسخة المحلية بواسطة الإدارة.')
        return True
