"""Service fees, separate from customer balances; amounts use integer piastres."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
from pathlib import Path
import threading


def money_minor(value):
    if isinstance(value, bool):
        raise ValueError("أدخل سعرًا صحيحًا غير سالب، بمنزلتين عشريتين كحد أقصى.")
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or amount > 1_000_000 or amount != amount.quantize(Decimal('0.01')):
            raise InvalidOperation
        return int(amount * 100)
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("أدخل سعرًا صحيحًا غير سالب، بمنزلتين عشريتين كحد أقصى.") from None


def normalize_settings(value):
    if not isinstance(value, dict) or value.get('mode', 'sheet') not in ('customer', 'sheet'):
        raise ValueError("اختر التسعير بالعميل أو بالشيت.")
    return {'mode': value.get('mode', 'sheet'),
            'customer_price': f"{money_minor(value.get('customer_price', '0')) / 100:.2f}",
            'sheet_price': f"{money_minor(value.get('sheet_price', '0')) / 100:.2f}",
            'currency': 'EGP', 'partner_percent': 30}


def normalize_quote(value):
    if not isinstance(value, dict) or value.get('mode') not in ('customer', 'sheet'):
        raise ValueError("اختر طريقة تسعير هذا الشيت.")
    return {'mode': value['mode'], 'unit_minor': money_minor(value.get('unit_price', '0')),
            'currency': 'EGP', 'partner_percent': 30}


def billing_summary(quote, total, completed, status='pending'):
    if not quote:
        return None  # Old jobs remain unpriced; never silently apply new defaults.
    total = max(0, int(total))
    checked = min(total, max(0, int(completed)))
    unit = int(quote['unit_minor'])
    expected = unit * total if quote['mode'] == 'customer' else (unit if total else 0)
    final = status in {'completed', 'archived'} and checked == total and total > 0
    billed = unit * checked if quote['mode'] == 'customer' else (unit if final else 0)
    partner = int((Decimal(billed) * Decimal('0.30')).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    return {**quote, 'total_rows': total, 'checked_rows': checked, 'expected_minor': expected,
            'billed_minor': billed, 'partner_minor': partner, 'owner_minor': billed - partner,
            'configured': unit > 0, 'final': final}


class PricingSettings:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.settings = normalize_settings({})
        if path.is_file():
            try:
                self.settings = normalize_settings(json.loads(path.read_text(encoding='utf-8')))
            except (ValueError, OSError):
                pass

    def get(self):
        with self.lock:
            return dict(self.settings)

    def save(self, value):
        settings = normalize_settings(value)
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding='utf-8')
            self.settings = settings
        return dict(settings)

    def quote(self, value=None):
        if value is not None:
            return normalize_quote(value)
        settings = self.get()
        return normalize_quote({'mode': settings['mode'], 'unit_price': settings[settings['mode'] + '_price']})
