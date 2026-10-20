"""Owner-reviewed administrator changes, durable across unknown RPC replies."""
import json
import logging
import os
import time
from pathlib import Path
from uuid import UUID,uuid4
from telegram_bot import views
from telegram_bot.authorization import admin_ids,user_id
from telegram_bot.config import settings,save_admin_membership
from .bot_workflow import _serial_user
from .client import CommerceUnavailable

logger=logging.getLogger('AdminRoles')

class AdminRolesMixin:
    def _commerce_command(self,text,actor,is_admin,username='',*,message_id=None):
        if text.split() and text.split()[0].split('@')[0].lower()=='/admins':
            self._admin_roles_callback('am:list:0',actor,message_id,{})
            return True
        return super()._commerce_command(text,actor,is_admin,username,message_id=message_id)

    def _role_owner(self,actor):
        return actor in admin_ids(self) and settings()['authorized_id']==actor

    def _role_file(self):return Path(self.project_root)/'.commerce/admin-role-action.json'

    def _read_role_action(self):
        path=self._role_file()
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None

    def _save_role_action(self,record):
        path=self._role_file();path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_name(path.name+'.'+uuid4().hex+'.tmp')
        with temporary.open('x',encoding='utf-8') as stream:
            json.dump(record,stream,indent=2);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)

    def _apply_role_action(self,record):
        actor=record['actor']
        if not self._role_owner(actor) or not record.get('approval'):raise ValueError('Current owner approval required')
        if record['state']=='done':return
        if record['state']!='pending':raise ValueError('Cancelled action cannot be replayed')
        expected=set(record['expected']);desired=set(record['desired'])
        current=set(settings()['authorized_ids'])
        if current not in (expected,desired):raise ValueError('Administrator list changed; review again')
        # Revoke locally before the RPC, grant only after its acknowledgment.
        # Replaying configure_support_actors is idempotent and never moves money.
        if not record['grant']:save_admin_membership(expected,desired,actor)
        result=self._commerce().rpc('configure_support_actors',{'p_actor_ids':sorted(desired),
            'p_reference':'telegram-admin-role:'+record['token']})
        if not isinstance(result,dict) or result.get('state')!='configured' or result.get('active_count')!=len(desired):
            raise ValueError('Backend administrator acknowledgment differs from review')
        save_admin_membership(expected,desired,actor)
        record.update(state='done',completed_at=time.time());self._save_role_action(record)
        target=record['target']
        with self._ui_lock:getattr(self,'_partner_menu_ready',set()).discard(target)
        try:
            if hasattr(self,'_set_user_mode'):
                self._get_user_mode(target)
                self._set_user_mode(target,'admin' if record['grant'] else 'customer')
            if record['grant']:
                self._api_call('setMyCommands',{'scope':{'type':'chat','chat_id':target},'commands':[
                    {'command':'start','description':'لوحة الإدارة'}, {'command':'status','description':'متابعة الفحص'},
                    {'command':'wallet','description':'المحفظة'}]})
            else:
                with self._ui_lock:
                    self._waiting_user_inputs.pop(target,None)
                self._api_call('deleteMyCommands',{'scope':{'type':'chat','chat_id':target}})
        except Exception:logger.warning('Administrator role saved; native command menu refresh deferred.')

    def _recover_admin_role_action(self):
        try:
            record=self._read_role_action()
            if record and record['state']=='pending':self._apply_role_action(record)
        except Exception:logger.warning('Approved administrator change awaits synchronization; reopen administrator settings.')

    def _role_review(self,actor,target,grant,message_id):
        cfg=settings();expected=set(cfg['authorized_ids'])
        if not user_id(target) or (not grant and target==cfg['authorized_id']):raise ValueError('Primary administrator cannot be removed')
        desired=expected|{target} if grant else expected-{target}
        if desired==expected:
            self._commerce_notice(actor,'لا يوجد تغيير','الحساب مدير بالفعل.' if grant else 'الحساب ليس مديرًا.',message_id);return
        if not desired or len(desired)>30:raise ValueError('One to thirty administrators required')
        token=str(uuid4())
        record={'token':token,'actor':actor,'target':target,'grant':grant,'expected':sorted(expected),
            'desired':sorted(desired),'expires':time.time()+180,'state':'review'}
        with self._ui_lock:
            self._admin_role_reviews={token:record}
        title='إعطاء صلاحية مدير' if grant else 'سحب صلاحية المدير'
        text=('<b>مراجعة '+title+'</b>\n\nالحساب: <code>'+str(target)+'</code>\n\n'+
            ('سيتمكن من إدارة الطلبات والمدفوعات والأسعار والشركاء. إدارة المديرين تبقى للمدير الأساسي.' if grant else
             'ستُسحب أدوات الإدارة منه فورًا. لا تتغير أرصدته أو سجلاته أو حالة شراكته إن كانت نشطة.'))
        self._show_panel(actor,views.Panel(text,[[views.button('تأكيد '+title,'am:save:'+token,'success' if grant else 'danger')],
            [views.button('إلغاء','am:list:0')]]),message_id)

    @_serial_user
    def _admin_roles_callback(self,command,actor,message_id,callback):
        if not self._role_owner(actor):
            self._commerce_notice(actor,'غير مصرح','إعطاء الإدارة وسحبها خاص بالمدير الأساسي فقط.',message_id);return
        token=None
        try:
            _,action,value=command.split(':')
            pending=self._read_role_action()
            if action=='list':
                with self._ui_lock:
                    self._admin_role_reviews={};self._waiting_user_inputs.pop(actor,None)
                cfg=settings();rows=[];lines=['<b>المديرون والصلاحيات</b>','المدير الأساسي وحده يدير هذه القائمة.']
                for target in sorted(cfg['authorized_ids']):
                    primary=target==cfg['authorized_id']
                    lines.append('<code>'+str(target)+'</code>'+(' · المدير الأساسي' if primary else ' · مدير'))
                    if not primary:rows.append([views.button('سحب الإدارة من '+str(target),'am:remove:'+str(target),'danger')])
                if pending and pending['state']=='pending':
                    rows.insert(0,[views.button('استكمال التغيير السابق','am:retry:'+pending['token'])])
                    rows.insert(1,[views.button('إلغاء التغيير المعلّق','am:cancel:'+pending['token'])])
                rows.extend([[views.button('إضافة مدير','am:add:0','primary')],[views.button('الرئيسية','home')]])
                self._show_panel(actor,views.Panel('\n\n'.join(lines),rows),message_id);return
            if action=='cancel':
                if not pending or pending['token']!=str(UUID(value)) or pending['state']!='pending' or not callback.get('id'):
                    raise ValueError('Owned pending change and native click required')
                cfg=settings()
                result=self._commerce().rpc('configure_support_actors',{'p_actor_ids':sorted(cfg['authorized_ids']),
                    'p_reference':'cancel-admin-role:'+pending['token']})
                if result.get('state')!='configured' or result.get('active_count')!=len(cfg['authorized_ids']):raise ValueError('Current roles not synchronized')
                pending['state']='cancelled';self._save_role_action(pending)
                with self._ui_lock:getattr(self,'_admin_role_reviews',{}).pop(pending['token'],None)
                self._commerce_notice(actor,'أُلغي التغيير المعلّق','حُفظت صلاحيات القائمة الحالية دون استكمال التغيير السابق.',message_id);return
            if action in {'save','retry'}:
                token=str(UUID(value))
                if action=='retry':
                    if not pending or pending['token']!=token or pending['state'] not in {'pending','done'}:raise ValueError('Matching approved action required')
                    record=pending
                else:
                    record=getattr(self,'_admin_role_reviews',{}).get(token)
                    if not record or record['actor']!=actor or record['expires']<=time.time():raise ValueError('Matching active review required')
                    if pending and pending['state']=='pending' and pending['token']!=token:raise ValueError('Finish pending change first')
                    receipt=callback.get('id')
                    if not isinstance(receipt,str) or not receipt.strip():raise ValueError('Native owner confirmation required')
                    if not record.get('approval'):
                        record.update(approval=receipt,state='pending',approved_at=time.time())
                    self._save_role_action(record)
                self._apply_role_action(record)
                self._show_panel(actor,views.Panel('<b>حُفظت صلاحيات الحساب</b>\n\nالحساب: <code>'+str(record['target'])+'</code>\n'+
                    ('أصبح مديرًا.' if record['grant'] else 'سُحبت منه صلاحية الإدارة.'),[[views.button('المديرون والصلاحيات','am:list:0')]]),message_id);return
            if pending and pending['state']=='pending':raise ValueError('Finish approved pending change first')
            if action=='add':
                with self._ui_lock:self._waiting_user_inputs[actor]={'action':'commerce_admin_role_target','message_id':message_id,'expires':time.monotonic()+1800}
                self._commerce_notice(actor,'إضافة مدير','أرسل رقم مستخدم Telegram للحساب الذي تريد إعطاءه الإدارة. ستراجع التغيير قبل تأكيده.',message_id);return
            if action=='remove':self._role_review(actor,user_id(value),False,message_id);return
            raise ValueError('Unknown administrator action')
        except (CommerceUnavailable,OSError,ValueError,KeyError,TypeError):
            try:record=self._read_role_action()
            except (OSError,ValueError):record=None
            rows=[[views.button('المديرون والصلاحيات','am:list:0')]]
            if record and record.get('state')=='pending':rows.insert(0,[views.button('إعادة المزامنة بنفس التغيير','am:retry:'+record['token'])])
            self._show_panel(actor,views.Panel('<b>لم تكتمل مراجعة الصلاحيات</b>\n\n'
                'افتح القائمة للتحقق من الحالة. أي سحب حُفظ محليًا يظل نافذًا؛ استخدم إعادة المزامنة إذا ظهرت.',rows),message_id)

    def _commerce_text_input(self,actor,text,waiting):
        if waiting.get('action')!='commerce_admin_role_target':return super()._commerce_text_input(actor,text,waiting)
        try:
            if not self._role_owner(actor) or waiting.get('expires',0)<=time.monotonic():raise ValueError('Owner prompt required')
            target=user_id(text)
            if target is None:raise ValueError('Telegram user identifier required')
            self._role_review(actor,target,True,waiting.get('message_id'))
        except (ValueError,TypeError,KeyError):
            if self._role_owner(actor) and waiting.get('expires',0)>time.monotonic():
                with self._ui_lock:self._waiting_user_inputs[actor]=waiting
            self._commerce_notice(actor,'راجع رقم الحساب','أرسل رقم مستخدم Telegram صحيحًا، أو ألغِ العملية من الرئيسية.',waiting.get('message_id'))
        return True
