"""JSON decimal money as numeric literals, preserving every stored decimal digit."""
import json as _json
import re
from decimal import Decimal
from uuid import uuid4

JSONDecodeError=_json.JSONDecodeError

def dumps(value,**kwargs):
    prefix='__exact_decimal_'+uuid4().hex+'_';numbers=[];fallback=kwargs.pop('default',None)
    def encode(item):
        if isinstance(item,Decimal):
            if not item.is_finite():raise ValueError('Nonfinite decimal money')
            if len(item.as_tuple().digits)>1000 or abs(item.adjusted())>1000:raise ValueError('Decimal JSON bound exceeded')
            token=prefix+str(len(numbers));numbers.append(format(item,'f'));return token
        if fallback is not None:return fallback(item)
        raise TypeError('Unsupported JSON value: '+type(item).__name__)
    result=_json.dumps(value,default=encode,**kwargs)
    return re.sub('"'+prefix+r'(\d+)"',lambda m:numbers[int(m.group(1))],result)

def dump(value,stream,**kwargs):stream.write(dumps(value,**kwargs))

def _money_hook(item):
    # Other numeric fields retain normal JSON behavior. Monetary fields never
    # pass through binary floats on durable quote/queue/recovery round trips.
    return {key:(value if key.endswith('_minor') or key=='benefit_value' else float(value))
        if isinstance(value,Decimal) else value for key,value in item.items()}

def loads(value,**kwargs):
    if 'parse_float' not in kwargs and 'object_hook' not in kwargs:
        kwargs.update(parse_float=Decimal,object_hook=_money_hook)
    return _json.loads(value,**kwargs)

def load(stream,**kwargs):return loads(stream.read(),**kwargs)

def __getattr__(name):return getattr(_json,name)
