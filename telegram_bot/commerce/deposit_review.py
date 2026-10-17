"""Read-only evidence for legacy Binance credits, without inferring ownership."""
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import secrets


def review(root, client):
    root = Path(root).resolve()
    path = root / 'user_wallets.json'
    raw = path.read_bytes()
    wallets = json.loads(raw.decode('utf-8-sig'), parse_float=Decimal)
    if not isinstance(wallets, dict):
        raise ValueError('Legacy wallet object required')
    # Provider history is queried once. Missing history never proves a duplicate.
    history = client.get_recent_transactions(limit=100)
    if not isinstance(history, list):
        raise ValueError('Provider history unavailable')
    observations = []
    for wallet in wallets.values():
        for entry in wallet.get('history', []):
            if entry.get('type') != 'credit':
                continue
            kind, separator, reference = str(entry.get('reason', '')).partition(':')
            if kind != 'binance_pay_auto' or not separator or not reference:
                continue
            exact, payer = set(), set()
            for tx in history:
                if not isinstance(tx, dict):
                    continue
                canonical = str(tx.get('transactionId', '')).strip()
                if not canonical:
                    continue
                fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
                if reference in {canonical, str(tx.get('orderId', '')).strip()}:
                    exact.add(fingerprint)
                if reference == str((tx.get('payerInfo') or {}).get('binanceId', '')).strip():
                    payer.add(fingerprint)
            observations.append({
                'legacy_account_id': wallet['account_id'],
                'credit_egp': str(entry['amount']),
                'reference_sha256': hashlib.sha256(reference.encode()).hexdigest(),
                'exact_transfer_matches': sorted(exact),
                'payer_matches': sorted(payer),
                'classification': 'exact_reference_requires_owner_review' if len(exact) == 1 else
                    'ambiguous_transfer_reference' if len(exact) > 1 else
                    'payer_identity_is_not_a_transfer' if payer else 'no_provider_match_in_available_history',
                'ownership_confirmed': False,
            })
    if path.read_bytes() != raw:
        raise ValueError('Wallet changed during deposit review')
    report = {'at': datetime.now(timezone.utc).isoformat(), 'source_sha256': hashlib.sha256(raw).hexdigest(),
              'production_reads': True, 'financial_writes': False,
              'provider_rows': len(history), 'observations': observations}
    folder = root / '.commerce/legacy_migration'
    folder.mkdir(parents=True, exist_ok=True)
    evidence = folder / ('deposit_review_' + secrets.token_hex(8) + '.json')
    with evidence.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    return report, evidence


if __name__ == '__main__':
    from telegram_bot.crypto_pay import BINANCE_PAY
    report, evidence = review(Path(__file__).resolve().parents[2], BINANCE_PAY)
    print(json.dumps({'evidence': str(evidence), 'provider_rows': report['provider_rows'],
                      'classifications': [item['classification'] for item in report['observations']],
                      'financial_writes': False}))
