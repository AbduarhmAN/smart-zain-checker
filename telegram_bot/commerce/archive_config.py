"""Verified private archive destination; uploading waits for an explicit policy."""
import json
import os
from pathlib import Path
import secrets
from datetime import datetime,timezone
from uuid import uuid4


class ArchiveConfig:
    def __init__(self,root):
        self.path=Path(root).resolve()/'.commerce'/'archive_settings.json'

    def load(self):
        if not self.path.is_file():return None
        data=json.loads(self.path.read_text(encoding='utf-8'))
        if not isinstance(data,dict) or data.get('version')!=1:raise ValueError('Invalid archive configuration')
        self._validate_chat(data['chat_id'])
        if type(data.get('bot_id')) is not int or data['bot_id']<=0:raise ValueError('Invalid archive bot')
        if type(data.get('enabled')) is not bool:raise ValueError('Explicit archive enable state required')
        if data['enabled']:
            policy=data.get('policy')
            if (not isinstance(policy,dict) or policy.get('original_timing')!='after_completion'
                    or policy.get('protection')!='plaintext_sha256' or policy.get('hash_algorithm')!='sha256'
                    or policy.get('automatic_deletion') is not False
                    or any(type(policy.get(key)) is not int or not 1<=policy[key]<=9999
                        for key in ('original_retention_days','result_retention_days'))
                    or not policy.get('decision_reference') or not data.get('verification_reference')):
                raise ValueError('Unsupported or unverified runtime archive policy')
        return data

    @staticmethod
    def _validate_chat(chat_id):
        if type(chat_id) is not int or not -2**52<chat_id<0:raise ValueError('A negative Telegram archive chat ID is required')

    def bind(self,api,chat_id,*,source_reference):
        self._validate_chat(chat_id)
        if not isinstance(source_reference,str) or not source_reference.strip():raise ValueError('Archive selection receipt required')
        me=api('getMe',{})
        chat=api('getChat',{'chat_id':chat_id})
        if not isinstance(me,dict) or not me.get('is_bot') or type(me.get('id')) is not int or me['id']<=0:
            raise ValueError('Confirmed Telegram bot identity required')
        if not isinstance(chat,dict) or chat.get('id')!=chat_id or chat.get('type')!='channel' or chat.get('username'):
            raise ValueError('Archive destination must be the selected channel')
        member=api('getChatMember',{'chat_id':chat_id,'user_id':me['id']})
        if not isinstance(member,dict) or member.get('status')!='administrator' or member.get('can_post_messages') is not True:
            raise ValueError('Archive bot requires channel publishing rights')
        old=self.load()
        if old is not None and (old['chat_id']!=chat_id or old['bot_id']!=me['id']):
            raise ValueError('Changing an archive destination requires a separate migration of its index')
        data=old or {'version':1,'chat_id':chat_id,'bot_id':me['id'],'enabled':False,'policy':None}
        data={**data,'title':chat.get('title',''),'selection_reference':source_reference,
            'verified_at':datetime.now(timezone.utc).isoformat(),'can_post_messages':True}
        self._save(data,'archive_binding')
        return data

    def configure_policy(self,*,original_timing,protection,original_days,result_days,reference):
        if original_timing!='after_completion' or protection not in {'plaintext_sha256','encrypted'}:
            raise ValueError('Explicit archive timing/protection required')
        if any(type(value) is not int or not 1<=value<=9999 for value in (original_days,result_days)):
            raise ValueError('Original and result retention must be at least one day')
        if not isinstance(reference,str) or not reference.strip():raise ValueError('Archive policy decision receipt required')
        data=self.load()
        if data is None:raise ValueError('Bind the verified destination first')
        data={**data,'policy':{'original_timing':original_timing,'protection':protection,'hash_algorithm':'sha256',
            'original_retention_days':original_days,'result_retention_days':result_days,
            'automatic_deletion':False,'decision_reference':reference},'enabled':False}
        self._save(data,'archive_policy')
        return data

    def _save(self,data,prefix):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        history=self.path.with_name(prefix+'_'+str(uuid4())+'.json')
        encoded=json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False)
        with history.open('x',encoding='utf-8') as stream:
            stream.write(encoded);stream.flush();os.fsync(stream.fileno())
        pending=self.path.with_name(self.path.name+'.'+secrets.token_hex(6)+'.pending')
        with pending.open('x',encoding='utf-8') as stream:
            stream.write(encoded);stream.flush();os.fsync(stream.fileno())
        os.replace(pending,self.path)

    def enable(self,*,verification_reference):
        data=self.load()
        policy=data.get('policy') if data else None
        if (not policy or policy.get('protection')!='plaintext_sha256'
                or policy.get('original_timing')!='after_completion' or policy.get('automatic_deletion') is not False):
            raise ValueError('Implemented and explicitly approved archive policy required')
        if not isinstance(verification_reference,str) or not verification_reference.strip():
            raise ValueError('Archive acceptance evidence required')
        data={**data,'enabled':True,'verification_reference':verification_reference}
        self._save(data,'archive_enable')
        return data
