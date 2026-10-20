"""Approved public receiver/token settings; credentials stay in their existing store."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
import threading
from functools import wraps
from uuid import uuid4
from .finance import positive_minor
from .payment_providers import BscTransferAdapter,BinanceTransferAdapter,TokenSpec,hex_data


@dataclass(frozen=True)
class TransferRoute:
    name: str
    provider: str
    asset: str
    receiver: str
    snapshot: dict

    def __post_init__(self):
        if self.asset not in {'USDT','USDC'} or not isinstance(self.snapshot,dict):
            raise ValueError('Approved transfer asset required')
        if self.provider=='bsc':
            if self.name!=('bscusdt' if self.asset=='USDT' else 'bscusdc') or self.snapshot.get('chain_id')!=56:
                raise ValueError('BSC route and chain must be explicit')
            if self.receiver!=hex_data(self.receiver,20):raise ValueError('Canonical receiver required')
            TokenSpec(self.asset,self.snapshot['contract'],self.snapshot['decimals'])
            if self.snapshot.get('asset')!=self.asset:raise ValueError('Token snapshot differs from route')
        elif self.provider=='binance_pay':
            if self.name!='binance' or self.asset!='USDT':raise ValueError('Unsupported exchange route')
            uid,pay=self.snapshot.get('uid'),self.snapshot.get('pay_id')
            if uid is None and pay is None:raise ValueError('Explicit exchange receiver required')
            for value in (uid,pay):
                if value is not None:positive_minor(value)
            expected='uid:'+str(uid) if uid is not None else 'pay:'+str(pay)
            if self.receiver!=expected:raise ValueError('Receiver identity domain differs')
        else:raise ValueError('Unsupported transfer provider')

    def as_dict(self):
        return {'name':self.name,'provider':self.provider,'asset':self.asset,'receiver':self.receiver,'snapshot':self.snapshot}


def _locked(method):
    @wraps(method)
    def wrapped(self,*args,**kwargs):
        with self._lock:return method(self,*args,**kwargs)
    return wrapped


class ProviderRoutes:
    _guard=threading.Lock()
    _locks={}
    def __init__(self,root):
        self.root=Path(root).resolve()
        self.path=self.root/'.commerce'/'provider_settings.json'
        with self._guard:
            self._lock=self._locks.setdefault(str(self.path),threading.RLock())

    @_locked
    def all(self):
        if self.path.exists():
            content=json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(content,dict) or content.get('version')!=1 or not isinstance(content.get('routes'),list):
                raise ValueError('Invalid approved provider settings')
            routes=[TransferRoute(**item) for item in content['routes']]
        else:
            # Import only an explicitly saved receiver, never PaymentManager's
            # hardcoded/default receiver. No guessed BSC contract is imported.
            path=self.root/'payment_config.json'
            content=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
            pay=content.get('binance_pay_id') if isinstance(content,dict) else None
            if isinstance(pay,bool) or pay is None or not str(pay).isdigit() or int(pay)<=0:return []
            routes=[TransferRoute('binance','binance_pay','USDT','pay:'+str(int(pay)),
                {'pay_id':int(pay),'uid':None,'asset':'USDT','source':'existing_saved_payment_config'})]
        if len({route.name for route in routes})!=len(routes):raise ValueError('Duplicate payment route')
        return routes

    def get(self,name):
        route=next((route for route in self.all() if route.name==name),None)
        if route is None:raise ValueError('Payment route has not been approved')
        return route

    @_locked
    def save(self,route:TransferRoute,*,actor_id,reference):
        positive_minor(actor_id)
        if not isinstance(reference,str) or not reference.strip() or len(reference)>500:
            raise ValueError('Provider settings approval receipt required')
        routes=[old.as_dict() for old in self.all() if old.name!=route.name]+[route.as_dict()]
        payload={'version':1,'revision':str(uuid4()),'actor_id':actor_id,'reference':reference,'routes':routes}
        self.path.parent.mkdir(parents=True,exist_ok=True)
        history=self.path.with_name('provider_settings_'+payload['revision']+'.json')
        with history.open('x',encoding='utf-8') as stream:
            json.dump(payload,stream,ensure_ascii=False);stream.flush();os.fsync(stream.fileno())
        temporary=self.path.with_name(self.path.name+'.'+secrets.token_hex(6)+'.pending')
        with temporary.open('x',encoding='utf-8') as stream:
            json.dump(payload,stream,ensure_ascii=False);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,self.path)
        return payload['revision']


def verifier_for(request,*,binance_client=None,bsc_rpc=None):
    provider=request['provider']; receiver=request['receiver']; snapshot=request.get('route_snapshot',{})
    if provider=='binance_pay':
        from telegram_bot.crypto_pay import BINANCE_PAY
        uid,pay=snapshot.get('uid'),snapshot.get('pay_id')
        if not snapshot:
            domain,value=receiver.split(':',1)
            uid,pay=(int(value),None) if domain=='uid' else (None,int(value)) if domain=='pay' else (None,None)
        return BinanceTransferAdapter(BINANCE_PAY if binance_client is None else binance_client,
                                      receiver_uid=uid,receiver_pay_id=pay)
    if provider=='bsc':
        from telegram_bot.crypto_pay import BSC_CHAIN
        if snapshot.get('chain_id')!=56:raise ValueError('Frozen BSC token settings required')
        return BscTransferAdapter(BSC_CHAIN._rpc_call if bsc_rpc is None else bsc_rpc,receiver=receiver,
            token=TokenSpec(snapshot['asset'],snapshot['contract'],snapshot['decimals']))
    raise ValueError('Provider does not support manual transfer assignment')
