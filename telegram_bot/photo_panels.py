"""Native photo, caption and buttons, edited in place with the image above the text."""
from html.parser import HTMLParser
import json
import secrets
import time
from .commerce.payment_media import VODAFONE_LOGO


def units(text):return len(text.encode('utf-16-le'))//2


class CaptionParser(HTMLParser):
    TYPES={'b':'bold','strong':'bold','i':'italic','em':'italic','code':'code','pre':'pre',
           'u':'underline','ins':'underline','s':'strikethrough','del':'strikethrough'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text='';self.entities=[];self.stack=[]

    def handle_data(self,data):self.text+=data

    def handle_starttag(self,tag,attrs):
        kind=self.TYPES.get(tag)
        if kind:self.stack.append((tag,kind,units(self.text)))

    def handle_endtag(self,tag):
        if self.stack and self.stack[-1][0]==tag:
            _,kind,start=self.stack.pop();length=units(self.text)-start
            if length:self.entities.append({'type':kind,'offset':start,'length':length})


def caption_pages(html):
    """Keep complete text and copyable fields within Telegram's caption limit."""
    parser=CaptionParser();parser.feed(html);parser.close()
    pages=[];start=0;offset=0
    while start<len(parser.text):
        end=start;length=0
        while end<len(parser.text) and length+units(parser.text[end])<=900:
            length+=units(parser.text[end]);end+=1
        if end<len(parser.text):
            boundary=max(parser.text.rfind('\n',start,end),parser.text.rfind(' ',start,end))
            if boundary>start+(end-start)//2:end=boundary+1
        text=parser.text[start:end];length=units(text);entities=[]
        for item in parser.entities:
            left=max(offset,item['offset']);right=min(offset+length,item['offset']+item['length'])
            if right>left:entities.append({**item,'offset':left-offset,'length':right-left})
        pages.append({'caption':text,'caption_entities':entities});offset+=length;start=end
    return pages or [{'caption':'','caption_entities':[]}]


def remember_media(bot,chat,message):
    mid=message.get('message_id')
    if type(mid) is not int:return
    if any(message.get(key) for key in ('photo','document','video','animation','audio')):
        with bot._ui_lock:
            known=getattr(bot,'_photo_panel_messages',{})
            known[(chat,mid)]=True
            while len(known)>512:known.pop(next(iter(known)))
            bot._photo_panel_messages=known


def is_media_message(bot,chat,mid):
    with bot._ui_lock:return (chat,mid) in getattr(bot,'_photo_panel_messages',{})


def retire_keyboard(bot,chat,mid,*,discard_pages=True):
    # Preserve payment details and original receipt images in the conversation.
    bot._api_call('editMessageReplyMarkup',{'chat_id':chat,'message_id':mid,
        'reply_markup':{'inline_keyboard':[]}})
    if discard_pages:
        with bot._ui_lock:
            bot._photo_panel_pages={k:v for k,v in getattr(bot,'_photo_panel_pages',{}).items()
                if v['chat']!=chat or v['message_id']!=mid}


def _success(response):
    return isinstance(response,dict) and (response.get('ok') is True or
        'message is not modified' in str(response.get('description','')).lower())


def _definite_rejection(response):
    return (isinstance(response,dict) and response.get('ok') is False
            and type(response.get('error_code')) is int and 400<=response['error_code']<500)


def _request(bot,method,payload,*,upload=False):
    if not upload:
        try:return bot._api_call(method,payload)
        except Exception:return None
    session=getattr(bot,'_session',None)
    api_url=getattr(bot,'api_url',None)
    if session is None or not isinstance(api_url,str):
        return {'ok':False,'error_code':400,'description':'Local branding unavailable'}
    try:stream=VODAFONE_LOGO.open('rb')
    except OSError:return {'ok':False,'error_code':400,'description':'Local branding unavailable'}
    try:
        fields={key:json.dumps(value,ensure_ascii=False) if isinstance(value,(dict,list,bool)) else value
                for key,value in payload.items()}
        with stream:
            response=session.post(api_url+'/'+method,data=fields,
                files={'photo':('vodafone_cash_logo.jpg',stream,'image/jpeg')},timeout=(3,10))
            result=response.json()
        if isinstance(result,dict) and result.get('ok') is False and type(response.status_code) is int:
            result.setdefault('error_code',response.status_code)
        return result
    except Exception:
        # An ambiguous upload/edit is not retried as a new message.
        return None


def _render(bot,chat,page,rows,photo,mid=None):
    cached=getattr(bot,'_vodafone_logo_file_id',None)
    media={'type':'photo','media':cached or 'attach://photo',**page,'show_caption_above_media':False}
    if mid:
        payload={'chat_id':chat,'message_id':mid,'media':media,'reply_markup':{'inline_keyboard':rows}}
        method='editMessageMedia'
    else:
        payload={'chat_id':chat,**page,'show_caption_above_media':False,'reply_markup':{'inline_keyboard':rows}}
        if cached:payload['photo']=cached
        method='sendPhoto'
    response=_request(bot,method,payload,upload=not cached)
    if _success(response):
        result=response.get('result')
        if isinstance(result,dict):
            pictures=result.get('photo') or []
            if pictures and isinstance(pictures[-1].get('file_id'),str):bot._vodafone_logo_file_id=pictures[-1]['file_id']
            remember_media(bot,chat,{**result,'photo':pictures or [True]})
        if mid:remember_media(bot,chat,{'message_id':mid,'photo':[True]})
        return True,response
    if not _definite_rejection(response) or response.get('error_code')==429:return True,response
    description=str(response.get('description','')).lower()
    if cached and any(text in description for text in ('file identifier','file_id','file reference')):
        bot._vodafone_logo_file_id=None
        return _render(bot,chat,page,rows,photo,mid)
    if mid and response['error_code']==400 and any(text in description for text in
            ('message to edit not found',"message can't be edited",'message_id_invalid','no media in the message')):
        handled,result=_render(bot,chat,page,rows,photo)
        if _success(result):retire_keyboard(bot,chat,mid,discard_pages=False)
        return handled,result
    return False,response


def show_photo_panel(bot,chat,text,rows,photo,mid=None):
    if photo!='vodafone_cash':return False,None
    pages=caption_pages(text)
    with bot._ui_lock:
        # An old page button must not restore a previous state of this panel.
        bot._photo_panel_pages={k:v for k,v in getattr(bot,'_photo_panel_pages',{}).items()
            if v['expires']>time.monotonic() and (not mid or v['chat']!=chat or v['message_id']!=mid)}
    if len(pages)>1:
        with bot._ui_lock:
            cache={k:v for k,v in getattr(bot,'_photo_panel_pages',{}).items() if v['expires']>time.monotonic()}
            while len(cache)>=128:cache.pop(next(iter(cache)))
            token=secrets.token_urlsafe(9)
            cache[token]={'chat':chat,'pages':pages,'rows':rows,'photo':photo,'expires':time.monotonic()+1800,
                          'message_id':mid}
            bot._photo_panel_pages=cache
        page_rows=_page_rows(rows,token,0,len(pages))
    else:page_rows=rows
    handled,response=_render(bot,chat,pages[0],page_rows,photo,mid)
    if len(pages)>1 and _success(response):
        result=response.get('result') or {}
        if isinstance(result,dict) and type(result.get('message_id')) is int:
            with bot._ui_lock:cache[token]['message_id']=result['message_id']
    return handled,response


def _page_rows(rows,token,index,total):
    nav=[]
    if index:nav.append({'text':'السابق','callback_data':f'zc:panel:{token}:{index-1}'})
    if index+1<total:nav.append({'text':f'المزيد ({index+1}/{total})','callback_data':f'zc:panel:{token}:{index+1}'})
    return rows+[nav] if nav else rows


def show_photo_page(bot,chat,callback):
    try:
        _,_,token,number=str(callback.get('data','')).split(':');index=int(number)
        with bot._ui_lock:entry=getattr(bot,'_photo_panel_pages',{}).get(token)
        mid=callback.get('message',{}).get('message_id')
        if (not entry or entry['chat']!=chat or entry['expires']<=time.monotonic()
                or entry['message_id']!=mid or not 0<=index<len(entry['pages'])):raise ValueError
    except (ValueError,TypeError):
        bot._api_call('answerCallbackQuery',{'callback_query_id':callback.get('id'),
            'text':'انتهت صلاحية العرض. افتح الطلب مجدداً لعرض التفاصيل.','show_alert':True})
        return
    bot._api_call('answerCallbackQuery',{'callback_query_id':callback.get('id')})
    _,response=_render(bot,chat,entry['pages'][index],_page_rows(entry['rows'],token,index,len(entry['pages'])),entry['photo'],mid)
    if _success(response) and isinstance(response.get('result'),dict):
        new_mid=response['result'].get('message_id')
        if type(new_mid) is int:
            with bot._ui_lock:entry['message_id']=new_mid
