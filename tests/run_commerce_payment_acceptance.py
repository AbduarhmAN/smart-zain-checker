"""Retained local PostgreSQL proof of invoice recovery and lost acknowledgments."""
from __future__ import annotations
import json
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from run_commerce_database_acceptance import DATA, sql
from telegram_bot.commerce.payment_quotes import freeze_quote
from telegram_bot.commerce.payment_providers import CryptoInvoiceAdapter
from telegram_bot.commerce.payments import PaymentService


class LocalRpc:
    def __init__(self, database):
        self.database = database
        self.lose_confirmation = True

    def rpc(self, name, arguments):
        allowed = {'create_payment_request', 'payment_request', 'begin_payment_invoice',
                   'attach_payment_invoice', 'confirm_payment_request'}
        if name not in allowed: raise AssertionError(name)
        values = []
        for key, value in arguments.items():
            if isinstance(value, dict):
                literal = "'" + json.dumps(value).replace("'", "''") + "'::jsonb"
            elif type(value) is int:
                literal = str(value)
            elif value is None:
                literal = 'null'
            else:
                literal = "'" + str(value).replace("'", "''") + "'"
            values.append(key + ' => ' + literal)
        result = json.loads(sql(self.database, statement=
            'select commerce_rpc.' + name + '(' + ','.join(values) + ')')[0])
        if name == 'confirm_payment_request' and self.lose_confirmation:
            self.lose_confirmation = False
            raise TimeoutError('Synthetic lost acknowledgment after committed payment')
        return result


def main():
    if Path(sql('postgres', statement='show data_directory')[0]).resolve() != DATA.resolve():
        raise RuntimeError('Refusing writes to an unknown PostgreSQL instance')
    database = 'commerce_payment_' + secrets.token_hex(6)
    sql('postgres', statement=f'create database {database}')
    for file in sorted((ROOT / 'supabase/migrations').glob('*commerce*.sql')):
        sql(database, file=file)
    client = LocalRpc(database)
    service = PaymentService(client)
    quote = freeze_quote(887500, 3000, 'USDT', '50.1', 'local-fixture',
                         now=datetime.now(timezone.utc))
    service.issue(quote, 'cryptopay', '7')
    provider = Mock(base_url='https://pay.crypt.bot/api')
    provider.get_me.return_value = {'app_id': 7}
    adapter = CryptoInvoiceAdapter(provider, app_id=7)
    provider._call.side_effect = TimeoutError('Synthetic lost invoice creation response')
    try:
        service.create_invoice(quote.id, quote.user_id, adapter)
    except TimeoutError:
        pass
    else:
        raise AssertionError('Lost creation response was not simulated')
    # A restarted process sees the durable attempt and cannot create another.
    restarted = PaymentService(LocalRpc(database))
    try:
        restarted.create_invoice(quote.id, quote.user_id, adapter)
    except ValueError:
        pass
    else:
        raise AssertionError('Restart created another invoice')
    if provider._call.call_count != 1:
        raise AssertionError('Duplicate external invoice creation')
    provider.get_invoice.return_value = {
        'invoice_id': 8, 'status': 'paid', 'currency_type': 'crypto', 'asset': 'USDT',
        'amount': quote.asset_amount, 'payload': quote.payload, 'created_at': quote.observed_at,
        'paid_at': datetime.now(timezone.utc).isoformat()}
    restarted.attach_invoice(quote.id, quote.user_id, adapter, 8)
    try:
        restarted.confirm_invoice(quote.id, quote.user_id, adapter)
    except TimeoutError:
        pass
    else:
        raise AssertionError('Lost ledger acknowledgment was not simulated')
    final = restarted.confirm_invoice(quote.id, quote.user_id, adapter)
    if not final['replayed']:
        raise AssertionError('Confirmation did not recover the committed receipt')
    wallet = json.loads(sql(database, statement='select commerce_rpc.wallet_state(887500)')[0])
    if wallet['customer']['available_minor'] != 3000:
        raise AssertionError('Payment recovery credited more than once')
    if sql(database, statement='select count(*) from commerce.payments')[0] != '1':
        raise AssertionError('Payment recovery duplicated its receipt')
    evidence = {'passed': True, 'at': datetime.now(timezone.utc).isoformat(), 'database': database,
                'production': False, 'external_provider': 'synthetic, no real invoices or transfers',
                'external_invoice_creations': provider._call.call_count, 'ledger_payments': 1,
                'credited_minor': 3000, 'invoice_restart_recovery': True, 'lost_confirmation_recovery': True}
    path = ROOT / 'docs/telegram_commerce' / ('acceptance_' + database + '.json')
    path.write_text(json.dumps(evidence, indent=2), encoding='utf-8')
    print(json.dumps({'passed': True, 'evidence': str(path)}))


if __name__ == '__main__':
    main()
