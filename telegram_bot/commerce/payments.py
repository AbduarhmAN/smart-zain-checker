"""Durable quotes and customer binding before an atomic ledger confirmation.

Raw transfer evidence is never enough to identify the customer. Only a stored
invoice or an assignment by an authenticated, configured administrator may do so.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from .client import CommerceClient
from .finance import positive_minor
from .payment_providers import CryptoInvoiceAdapter, TransferEvidence, _text
from .payment_quotes import PaymentQuote, decimal_amount, utc_time


class PaymentService:
    def __init__(self, client=None, *, admin_ids=()):
        self.client = CommerceClient() if client is None else client
        self.admin_ids = frozenset(positive_minor(value) for value in admin_ids)

    def issue(self, quote: PaymentQuote, provider, receiver, *, route_snapshot=None):
        if not isinstance(quote, PaymentQuote):
            raise ValueError('A frozen payment quote is required')
        if provider not in {'cryptopay', 'binance_pay', 'bsc'}:
            raise ValueError('Unsupported payment provider')
        args = {
            'p_quote': quote.as_dict(), 'p_provider': provider,
            'p_receiver': _text(receiver)}
        if route_snapshot is not None:
            args['p_route_snapshot']=route_snapshot
        return self.client.rpc('create_payment_request',args)

    def load(self, quote_id, user_id):
        positive_minor(user_id)
        result = self.client.rpc('payment_request', {
            'p_quote_id': str(UUID(quote_id)), 'p_telegram_user_id': user_id})
        if not isinstance(result, dict):
            raise ValueError('Payment request is unavailable')
        quote = PaymentQuote(**result['quote'])
        if quote.user_id != user_id or quote.id != str(UUID(quote_id)):
            raise ValueError('Payment request belongs to another customer')
        return result, quote

    def create_invoice(self, quote_id, user_id, adapter: CryptoInvoiceAdapter, *, now=None):
        request, quote = self.load(quote_id, user_id)
        if request['provider'] != 'cryptopay' or request['receiver'] != str(adapter.app_id):
            raise ValueError('Invoice application does not match the stored request')
        if request.get('invoice_id') is not None:
            return {'invoice_id': request['invoice_id'], 'replayed': True}
        now = datetime.now(timezone.utc) if now is None else now
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError('Explicit invoice time required')
        seconds = int((utc_time(quote.expires_at) - now).total_seconds())
        if not 1 <= seconds <= 86400 or now < utc_time(quote.observed_at):
            raise ValueError('Payment quote is outside its validity window')
        # Mark the external operation before contacting the provider. A lost
        # response leaves an ambiguous attempt; never create a second invoice.
        claim = self.client.rpc('begin_payment_invoice', {
            'p_quote_id': quote.id, 'p_telegram_user_id': user_id})
        if claim.get('state') != 'claimed':
            raise ValueError('Invoice creation requires recovery of the existing attempt')
        invoice = adapter.client._call('createInvoice', {
            'currency_type': 'crypto', 'asset': quote.asset,
            'amount': quote.asset_amount, 'payload': quote.payload,
            'description': 'Zain Checker', 'expires_in': seconds})
        if (not isinstance(invoice, dict) or type(invoice.get('invoice_id')) is not int
                or invoice.get('currency_type') != 'crypto'
                or invoice.get('asset') != quote.asset or invoice.get('payload') != quote.payload
                or decimal_amount(invoice.get('amount')) != decimal_amount(quote.asset_amount)):
            raise ValueError('Invoice creation response is ambiguous or mismatched')
        return self.attach_invoice(quote.id, user_id, adapter, invoice['invoice_id'])

    def attach_invoice(self, quote_id, user_id, adapter: CryptoInvoiceAdapter, invoice_id):
        """Also recovers a lost createInvoice response by querying its real ID."""
        request, quote = self.load(quote_id, user_id)
        if request['provider'] != 'cryptopay' or request['receiver'] != str(adapter.app_id):
            raise ValueError('Invoice application does not match the stored request')
        positive_minor(invoice_id)
        invoice = adapter.client.get_invoice(invoice_id)
        if (not isinstance(invoice, dict) or invoice.get('invoice_id') != invoice_id
                or type(invoice.get('invoice_id')) is not int
                or invoice.get('currency_type') != 'crypto'
                or invoice.get('asset') != quote.asset or invoice.get('payload') != quote.payload
                or decimal_amount(invoice.get('amount')) != decimal_amount(quote.asset_amount)
                or not utc_time(quote.observed_at) <= utc_time(invoice.get('created_at')) <= utc_time(quote.expires_at)):
            raise ValueError('Provider invoice does not match the stored request')
        return self.client.rpc('attach_payment_invoice', {
            'p_quote_id': quote.id, 'p_telegram_user_id': user_id, 'p_invoice_id': invoice_id})

    def confirm_invoice(self, quote_id, user_id, adapter: CryptoInvoiceAdapter):
        request, quote = self.load(quote_id, user_id)
        if request['provider'] != 'cryptopay' or request['receiver'] != str(adapter.app_id):
            raise ValueError('Invoice application does not match the stored request')
        evidence = adapter.verify(quote, request.get('invoice_id'))
        return self._confirm(request, quote, evidence, {'kind': 'invoice'})

    def assign_transfer(self, quote_id, user_id, adapter, reference, *, actor_id, approval_reference):
        """actor_id must come from the authenticated Telegram update, never UI text."""
        positive_minor(actor_id)
        if actor_id not in self.admin_ids:
            raise ValueError('Authenticated administrator assignment required')
        approval_reference = _text(approval_reference, 1000)
        request, quote = self.load(quote_id, user_id)
        if request['provider'] == 'cryptopay':
            raise ValueError('Invoice payments require their issued invoice')
        params = {'minimum': quote.asset_amount, 'not_before': quote.observed_at}
        if request['provider'] == 'binance_pay':
            params['asset'] = quote.asset
        evidence = adapter.verify(reference, **params)
        return self._confirm(request, quote, evidence, {
            'kind': 'admin', 'actor_id': actor_id, 'approval_reference': approval_reference})

    def validate_evidence(self, request, quote, evidence, binding):
        if not isinstance(evidence, TransferEvidence):
            raise ValueError('Authenticated provider evidence is required')
        if (evidence.provider != request['provider'] or evidence.receiver != request['receiver']
                or evidence.asset != quote.asset
                or decimal_amount(evidence.amount) != decimal_amount(quote.asset_amount)
                or not utc_time(quote.observed_at) <= utc_time(evidence.received_at) <= utc_time(quote.expires_at)):
            raise ValueError('Transfer differs from the frozen payment request; reconciliation required')
        if binding['kind'] == 'invoice' and evidence.invoice_payload != quote.payload:
            raise ValueError('Invoice is not bound to this customer request')
        if evidence.provider=='bsc':
            import json
            proof=json.loads(evidence.proof_json)
            route=request.get('route_snapshot',{})
            if (not route or proof.get('contract')!=route.get('contract')
                    or proof.get('decimals')!=route.get('decimals') or proof.get('chain_id')!=56):
                raise ValueError('BSC transfer differs from its frozen contract')
        return evidence

    def _confirm(self, request, quote, evidence, binding):
        self.validate_evidence(request,quote,evidence,binding)
        return self.client.rpc('confirm_payment_request', {
            'p_quote_id': quote.id, 'p_telegram_user_id': quote.user_id,
            'p_evidence': evidence.as_dict(), 'p_binding': binding})

    def prepare_claim(self, claim_id, adapter, *, actor_id):
        if positive_minor(actor_id) not in self.admin_ids:
            raise ValueError('Authenticated payment reviewer required')
        claim=self.client.rpc('payment_claim',{'p_claim_id':str(UUID(claim_id))})
        if claim['state']!='pending':
            return {'claim':claim,'actor_id':actor_id}
        request,quote=self.load(claim['quote_id'],claim['telegram_user_id'])
        params={'minimum':quote.asset_amount,'not_before':quote.observed_at}
        if request['provider']=='binance_pay':params['asset']=quote.asset
        evidence=adapter.verify(claim['reference'],**params)
        self.validate_evidence(request,quote,evidence,{'kind':'admin'})
        return {'claim':claim,'quote':quote,'request':request,'evidence':evidence,'actor_id':actor_id}

    def approve_claim(self, prepared, *, actor_id):
        if positive_minor(actor_id) not in self.admin_ids or prepared.get('actor_id')!=actor_id:
            raise ValueError('Authenticated matching payment reviewer required')
        claim=prepared['claim']
        if claim['state']=='approved':
            return {'payment_id':claim['payment_id'],'telegram_user_id':claim['telegram_user_id'],'replayed':True}
        if claim['state']!='pending':raise ValueError('Pending payment review required')
        evidence=self.validate_evidence(prepared['request'],prepared['quote'],prepared['evidence'],{'kind':'admin'})
        return self.client.rpc('approve_payment_claim',{'p_claim_id':str(UUID(claim['claim_id'])),
            'p_actor_telegram_id':actor_id,'p_evidence':evidence.as_dict()})
