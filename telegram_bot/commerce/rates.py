"""Exact, observed asset/USD and USD/EGP rates for frozen checkout quotes."""
from datetime import datetime, timezone
from decimal import Decimal
import json
from urllib.request import Request, urlopen
from .payment_quotes import composed_asset_rate, decimal_amount

FX_URL = 'https://open.er-api.com/v6/latest/USD'


def asset_egp_rate(client, asset, *, opener=urlopen, now=None):
    now = datetime.now(timezone.utc) if now is None else now
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError('Explicit rate observation time required')
    rates = client._call('getExchangeRates')
    if not isinstance(rates, list):
        raise ValueError('Asset exchange rates are unavailable')
    matched = [item for item in rates if isinstance(item, dict) and item.get('source') == asset
               and item.get('target') == 'USD' and item.get('is_valid') is True
               and item.get('is_crypto') is True]
    if len(matched) != 1:
        raise ValueError('Unique current asset/USD rate required')
    asset_usd = decimal_amount(matched[0].get('rate'))
    request = Request(FX_URL, headers={'User-Agent': 'ZainChecker/commerce'})
    with opener(request, timeout=8) as response:
        fx = json.load(response, parse_float=Decimal)
    if not isinstance(fx, dict) or fx.get('result') != 'success' or fx.get('base_code') != 'USD':
        raise ValueError('USD/EGP observation is unavailable')
    updated = fx.get('time_last_update_unix')
    if type(updated) is not int or not 0 <= now.timestamp() - updated <= 172800:
        raise ValueError('USD/EGP observation is stale or in the future')
    if not isinstance(fx.get('rates'), dict):
        raise ValueError('USD/EGP observation is missing')
    usd_egp = decimal_amount(fx['rates'].get('EGP'))
    source = ('cryptopay:getExchangeRates:' + asset + '/USD@' + now.astimezone(timezone.utc).isoformat()
              + ';' + FX_URL + '@' + datetime.fromtimestamp(updated, timezone.utc).isoformat())
    return composed_asset_rate(asset, asset_usd, usd_egp), source
