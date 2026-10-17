"""Exact EGP presentation; padding display decimals never discards fractions."""
from decimal import Decimal,localcontext

def egp_from_minor(value):
    amount=Decimal(str(value))
    if not amount.is_finite():raise ValueError('Nonfinite money')
    with localcontext() as context:
        context.prec=max(100,len(amount.as_tuple().digits)+abs(amount.as_tuple().exponent)+4)
        text=format(amount/100,'f')
    if '.' not in text:return text+'.00'
    integer,fraction=text.split('.',1);fraction=fraction.rstrip('0')
    return integer+'.'+fraction.ljust(2,'0')
