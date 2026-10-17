"""Automatic confirmation of pre-bound invoices; user TXIDs are never inputs."""
import threading
from uuid import UUID
from .payment_providers import CryptoInvoiceAdapter
from .payments import PaymentService
from .finance import positive_minor


class InvoiceBatch:
    base_url = 'https://pay.crypt.bot/api'

    def __init__(self, app_id, invoices):
        self.app_id, self.invoices = app_id, invoices

    def get_me(self):
        return {'app_id': self.app_id}

    def get_invoice(self, invoice_id):
        return self.invoices.get(invoice_id)


class BoundInvoiceMonitor:
    def __init__(self, client, adapter_factory, *, on_confirmed=None, limit=20):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Bounded invoice monitor required')
        self.client, self.adapter_factory = client, adapter_factory
        self.on_confirmed, self.limit = on_confirmed, limit
        self.after = None
        self.lock = threading.Lock()

    def tick(self):
        # A second cycle cannot duplicate provider work while this one runs.
        if not self.lock.acquire(blocking=False):
            return {'checked': 0, 'confirmed': 0, 'busy': True}
        try:
            rows = self.client.rpc('pending_bound_invoices', {'p_after': self.after, 'p_limit': self.limit})
            if not isinstance(rows, list) or len(rows) > self.limit:
                raise ValueError('Invalid bound invoice page')
            if not rows:
                self.after = None
                return {'checked': 0, 'confirmed': 0}
            for row in rows:
                row['quote_id'] = str(UUID(row['quote_id']))
                positive_minor(row['telegram_user_id']); positive_minor(row['invoice_id'])
            adapter = self.adapter_factory()
            if not isinstance(adapter, CryptoInvoiceAdapter):
                raise ValueError('Authenticated production invoice adapter required')
            ids = {row['invoice_id'] for row in rows}
            response = adapter.client._call('getInvoices', {'invoice_ids': ','.join(str(i) for i in sorted(ids)), 'count': len(ids)})
            if not isinstance(response, dict) or not isinstance(response.get('items'), list):
                raise ValueError('Provider invoice batch unavailable')
            invoices = {}
            for invoice in response['items']:
                if not isinstance(invoice, dict) or type(invoice.get('invoice_id')) is not int or invoice['invoice_id'] not in ids:
                    raise ValueError('Unexpected provider invoice in batch')
                if invoice['invoice_id'] in invoices and invoices[invoice['invoice_id']] != invoice:
                    raise ValueError('Conflicting provider invoice in batch')
                invoices[invoice['invoice_id']] = invoice
            batch = CryptoInvoiceAdapter(InvoiceBatch(adapter.app_id, invoices), app_id=adapter.app_id)
            service = PaymentService(self.client)
            confirmed, rejected = 0, 0
            for row in rows:
                invoice = invoices.get(row['invoice_id'])
                if not invoice or invoice.get('status') != 'paid':
                    continue
                try:
                    # Reload the owner, receiver and immutable quote. The scanned
                    # invoice cannot replace the invoice attached in the database.
                    request, quote = service.load(row['quote_id'], row['telegram_user_id'])
                    if request.get('invoice_id') != row['invoice_id']:
                        raise ValueError('Invoice binding changed')
                    result = service.confirm_invoice(row['quote_id'], row['telegram_user_id'], batch)
                    confirmed += 1
                    if self.on_confirmed:
                        self.on_confirmed(quote, result)
                except (ValueError, RuntimeError, OSError, KeyError, TypeError):
                    rejected += 1
            self.after = rows[-1]['quote_id']
            return {'checked': len(rows), 'confirmed': confirmed, 'requires_review': rejected}
        finally:
            self.lock.release()
