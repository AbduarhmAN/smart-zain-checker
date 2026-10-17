"""Frozen EGP/asset quotes. No assumed USD peg, floats, or fallback FX rate."""
from __future__ import annotations
from dataclasses import asdict,dataclass
from datetime import datetime,timedelta,timezone
from decimal import Decimal,InvalidOperation,ROUND_CEILING,localcontext
from uuid import UUID,uuid4
from .finance import positive_minor,positive_money


def decimal_amount(value,*,allow_zero=False):
    if isinstance(value,bool) or not isinstance(value,(str,int,Decimal)):
        raise ValueError('An exact decimal amount is required')
    try: result=Decimal(value)
    except (InvalidOperation,ValueError):raise ValueError('Invalid decimal amount') from None
    if not result.is_finite() or result<0 or (result==0 and not allow_zero) or len(result.as_tuple().digits)>80 or abs(result.adjusted())>80:
        raise ValueError('Decimal amount is outside supported bounds')
    return result


def utc_time(value):
    if not isinstance(value,str):raise ValueError('An explicit UTC timestamp is required')
    try:result=datetime.fromisoformat(value.replace('Z','+00:00'))
    except ValueError:raise ValueError('Invalid timestamp') from None
    if result.tzinfo is None:raise ValueError('Timestamp timezone is required')
    return result.astimezone(timezone.utc)


@dataclass(frozen=True)
class PaymentQuote:
    id: str
    user_id: int
    amount_minor: int
    asset: str
    asset_amount: str
    egp_per_asset: str
    observed_at: str
    expires_at: str
    rate_source: str
    precision: int = 6
    order_id: str | None = None

    def __post_init__(self):
        if str(UUID(self.id))!=self.id:raise ValueError('Canonical quote ID required')
        positive_minor(self.user_id);positive_money(self.amount_minor)
        if self.asset not in {'USDT','USDC'}:raise ValueError('Unsupported quote asset')
        if type(self.precision) is not int or not 0<=self.precision<=8:raise ValueError('Unsupported asset precision')
        rate=decimal_amount(self.egp_per_asset)
        amount=decimal_amount(self.asset_amount)
        start,end=utc_time(self.observed_at),utc_time(self.expires_at)
        if not start<end<=start+timedelta(days=1):raise ValueError('Invalid quote validity window')
        if not isinstance(self.rate_source,str) or not self.rate_source.strip() or len(self.rate_source)>500:
            raise ValueError('FX source is required')
        if self.order_id is not None and str(UUID(self.order_id))!=self.order_id:raise ValueError('Canonical order ID required')
        with localcontext() as context:
            context.prec=100
            expected=(Decimal(self.amount_minor)/100/rate).quantize(Decimal(1).scaleb(-self.precision),rounding=ROUND_CEILING)
        if amount!=expected:raise ValueError('Asset quote differs from the frozen EGP charge')

    @property
    def payload(self):return 'zain-commerce:'+self.id

    def as_dict(self):return asdict(self)


def freeze_quote(user_id,amount_minor,asset,egp_per_asset,rate_source,*,now=None,
                 validity_seconds=900,precision=6,order_id=None,quote_id=None):
    positive_minor(user_id);positive_money(amount_minor)
    if type(validity_seconds) is not int or not 1<=validity_seconds<=86400:
        raise ValueError('Invalid quote duration')
    if type(precision) is not int or not 0<=precision<=8:raise ValueError('Invalid precision')
    now=datetime.now(timezone.utc) if now is None else now
    if not isinstance(now,datetime) or now.tzinfo is None:raise ValueError('Explicit quote time required')
    rate=decimal_amount(egp_per_asset)
    with localcontext() as context:
        context.prec=100
        amount=(Decimal(amount_minor)/100/rate).quantize(Decimal(1).scaleb(-precision),rounding=ROUND_CEILING)
    return PaymentQuote(str(UUID(quote_id)) if quote_id else str(uuid4()),user_id,amount_minor,asset,
        format(amount,'f'),format(rate,'f'),now.astimezone(timezone.utc).isoformat(),
        (now+timedelta(seconds=validity_seconds)).astimezone(timezone.utc).isoformat(),rate_source,
        precision,str(UUID(order_id)) if order_id else None)


def composed_asset_rate(asset,asset_usd,usd_egp):
    """Caller must obtain both observations from verified sources before freezing."""
    if asset not in {'USDT','USDC'}:raise ValueError('Unsupported asset')
    with localcontext() as context:
        context.prec=100
        return format(decimal_amount(asset_usd)*decimal_amount(usd_egp),'f')
