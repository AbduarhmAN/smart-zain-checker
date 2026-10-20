"""Owner-only access during maintenance, served by the normal bot's single poller."""
import html
import json
from pathlib import Path
import re
from . import subscription_policy as subscription
from .official_support import support_button,support_text


def active(root):
    return (Path(root)/'.maintenance/enabled').exists()


def owner_allowed(root,actor,primary,fingerprint):
    if type(actor) is not int or actor<=0 or actor!=primary:
        return False
    try:
        value=json.loads((Path(root)/'.maintenance/owner-only.json').read_text(encoding='utf-8'))
        return (value.get('version')==1 and type(value.get('owner_id')) is int
                and value['owner_id']==primary and value.get('bot')==fingerprint)
    except (OSError,ValueError,TypeError,AttributeError):
        return False


class MaintenanceAccessMixin:
    def _maintenance_gate(self,update):
        root=getattr(self,'project_root',None)
        if root is None or not active(root):
            return False
        callback=update.get('callback_query') or {}
        message=callback.get('message') or update.get('message') or update.get('edited_message') or {}
        sender=callback.get('from') or message.get('from') or {}
        actor=sender.get('id');chat=message.get('chat') or {}
        private=(type(actor) is int and actor>0 and actor==chat.get('id')
                 and chat.get('type','private')=='private' and sender.get('is_bot') is not True)
        if not private:return True
        if (owner_allowed(root,actor,getattr(self,'authorized_id',None),getattr(self,'_bot_fingerprint',None))
                and actor in self.current_authorized_ids):
            return False
        if callback.get('id'):
            self._api_call('answerCallbackQuery',{'callback_query_id':callback['id'],
                'text':'البوت متوقف مؤقتاً للصيانة.','show_alert':False})
        try:
            raw=(Path(root)/'.maintenance/announcement.txt').read_text(encoding='utf-8').strip()
            text=re.sub(r'\*\*(.+?)\*\*',r'<b>\1</b>',html.escape(raw),flags=re.S)
        except OSError:
            text='🛠 البوت متوقف مؤقتاً لأعمال الصيانة والتحديث. سنتيح الخدمات بعد انتهاء الصيانة.'
        rows=[[support_button()]]
        route=callback.get('data') or message.get('text','')
        if subscription.support_route(route):
            text=support_text()
        else:
            status=subscription.membership(self._api_call,actor)
            if status!='member':text,rows=subscription.prompt(status),subscription.rows()
        self._api_call('sendMessage',{'chat_id':actor,'text':text,'parse_mode':'HTML',
            'reply_markup':{'inline_keyboard':rows}})
        return True
