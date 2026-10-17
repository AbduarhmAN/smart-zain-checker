"""Single-attempt document transport. Unknown send outcomes are never retried.

Uses the hosted Bot API: binary upload <=50 MiB; verified download <=20 MiB.
Errors deliberately omit URLs, tokens, provider bodies and customer names.
"""
from __future__ import annotations
import hashlib
import re
import time
from pathlib import Path
import requests
from .bridge import file_sha256


UPLOAD_LIMIT=50*1024*1024
DOWNLOAD_LIMIT=20*1024*1024


class ArchiveRejected(RuntimeError):
    def __init__(self,code=400,retry_after=60):
        self.code=code;self.retry_after=max(1,min(86400,int(retry_after)))
        super().__init__('Telegram rejected archive operation')


class ArchiveAmbiguous(RuntimeError):
    pass


def document_receipt(message,chat_id,*,expected_size=None):
    if not isinstance(message,dict) or (message.get('chat') or {}).get('id')!=chat_id:
        raise ArchiveAmbiguous('Archive acknowledgement destination missing')
    doc=message.get('document')
    if type(message.get('message_id')) is not int or message['message_id']<=0 or not isinstance(doc,dict):
        raise ArchiveAmbiguous('Archive acknowledgement document missing')
    for key in ('file_id','file_unique_id'):
        if not isinstance(doc.get(key),str) or not 1<=len(doc[key])<=512:
            raise ArchiveAmbiguous('Archive acknowledgement identity missing')
    if type(doc.get('file_size')) is not int or doc['file_size']<0 or (
            expected_size is not None and doc['file_size']!=expected_size):
        raise ArchiveAmbiguous('Archive acknowledgement size differs')
    return {'chat_id':chat_id,'message_id':message['message_id'],'file_id':doc['file_id'],
            'file_unique_id':doc['file_unique_id'],'file_size':doc['file_size']}


class ArchiveTransport:
    def __init__(self,token,*,session=None):
        if not isinstance(token,str) or not token:raise ValueError('Archive bot token required')
        self._token=token;self._base='https://api.telegram.org/bot'+token
        self.session=requests.Session() if session is None else session
        self._verified=None;self._verified_until=0

    def api(self,method,data,*,files=None):
        if method not in {'getMe','getChat','getChatMember','getFile','sendDocument'}:
            raise ValueError('Unsupported archive operation')
        try:
            response=self.session.post(self._base+'/'+method,**({'data':data,'files':files} if files else {'json':data}),timeout=(10,45))
            body=response.json()
        except Exception:
            raise ArchiveAmbiguous('Archive response unavailable') from None
        if not isinstance(body,dict) or type(body.get('ok')) is not bool:
            raise ArchiveAmbiguous('Archive response malformed')
        if body['ok'] is False:
            code=body.get('error_code',response.status_code)
            delay=(body.get('parameters') or {}).get('retry_after',60)
            raise ArchiveRejected(code if type(code) is int else 400,delay if type(delay) is int else 60)
        if 'result' not in body:raise ArchiveAmbiguous('Archive acknowledgement missing')
        return body['result']

    def verify(self,config,*,force=False):
        identity=(config['chat_id'],config['bot_id'])
        if not force and identity==self._verified and time.monotonic()<self._verified_until:return
        me=self.api('getMe',{});chat=self.api('getChat',{'chat_id':config['chat_id']})
        if me.get('id')!=config['bot_id'] or me.get('is_bot') is not True:
            raise ValueError('Archive bot identity changed')
        if chat.get('id')!=config['chat_id'] or chat.get('type')!='channel' or chat.get('username'):
            raise ValueError('Archive channel identity/privacy changed')
        member=self.api('getChatMember',{'chat_id':config['chat_id'],'user_id':config['bot_id']})
        if member.get('status')!='administrator' or member.get('can_post_messages') is not True:
            raise ValueError('Archive publishing rights missing')
        self._verified=identity;self._verified_until=time.monotonic()+60

    def send(self,config,*,file_id,path,sha256,caption,filename):
        self.verify(config)
        path=Path(path)
        # Frozen local backups establish file provenance even when reusing file_id.
        if file_sha256(path)!=sha256:raise ValueError('Archive backup hash differs')
        size=path.stat().st_size
        if len(caption)>1024:raise ValueError('Archive caption too long')
        data={'chat_id':config['chat_id'],'caption':caption}
        if file_id:
            if not isinstance(file_id,str) or len(file_id)>512:raise ValueError('Invalid reusable file identity')
            message=self.api('sendDocument',{**data,'document':file_id})
        else:
            if size>UPLOAD_LIMIT:raise ValueError('Archive exceeds hosted Bot API upload limit')
            with path.open('rb') as stream:
                message=self.api('sendDocument',data,files={'document':(filename,stream,'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')})
        return document_receipt(message,config['chat_id'],expected_size=size)

    def verify_download(self,file_id,sha256):
        info=self.api('getFile',{'file_id':file_id})
        size=info.get('file_size');file_path=info.get('file_path')
        if type(size) is not int or not 0<=size<=DOWNLOAD_LIMIT:
            raise ValueError('Archive download requires local backup above hosted limit')
        if (not isinstance(file_path,str) or not re.fullmatch(r'[A-Za-z0-9_./-]+',file_path)
                or file_path.startswith('/') or '..' in file_path.split('/')):
            raise ValueError('Archive download path invalid')
        digest=hashlib.sha256();received=0
        try:
            with self.session.get('https://api.telegram.org/file/bot'+self._token+'/'+file_path,
                    stream=True,timeout=(10,30)) as response:
                if response.status_code!=200:raise ArchiveRejected(response.status_code)
                for chunk in response.iter_content(256*1024):
                    received+=len(chunk)
                    if received>DOWNLOAD_LIMIT:raise ValueError('Archive download exceeds hosted limit')
                    digest.update(chunk)
        except requests.RequestException:
            raise ArchiveAmbiguous('Archive download unavailable') from None
        if received!=size or digest.hexdigest()!=sha256:raise ValueError('Archive download hash differs')
        return {'sha256':sha256,'bytes':received,'verified':True}
