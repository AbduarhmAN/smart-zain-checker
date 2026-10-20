"""Explicit JSON value codec. Wire data never names importable Python objects."""
import dataclasses
import datetime
import json
from decimal import Decimal
from pathlib import Path


def _default(value):
    if isinstance(value, Decimal): return {'$type': 'decimal', 'value': str(value)}
    if isinstance(value, Path): return {'$type': 'path', 'value': str(value)}
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return {'$type': type(value).__name__, 'value': value.isoformat()}
    if isinstance(value, set): return {'$type': 'set', 'value': sorted(value)}
    if dataclasses.is_dataclass(value):
        # Deliberate allowlist; do not expose an arbitrary class loader.
        from domain.models import Customer, QueueJob
        if type(value) not in (Customer, QueueJob): raise TypeError('Unsupported wire dataclass')
        return {'$type': type(value).__name__, 'value': dataclasses.asdict(value)}
    raise TypeError('Unsupported wire value')


def _object(value):
    if set(value) != {'$type', 'value'}: return value
    kind, data = value['$type'], value['value']
    if kind == 'decimal':
        number = Decimal(data)
        if not number.is_finite(): raise ValueError('Nonfinite wire money')
        return number
    if kind == 'path': return Path(data)
    if kind == 'set': return set(data)
    if kind in ('datetime', 'date', 'time'): return getattr(datetime, kind).fromisoformat(data)
    if kind in ('Customer', 'QueueJob'):
        from domain.models import Customer, QueueJob
        return {'Customer': Customer, 'QueueJob': QueueJob}[kind](**data)
    raise ValueError('Unsupported wire type')


def dumps(value):
    return json.dumps(value, default=_default, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':'))


def loads(value):
    return json.loads(value, object_hook=_object, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))
