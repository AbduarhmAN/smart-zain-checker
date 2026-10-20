"""Offline regression evidence for the Vodafone release and shared bot boundaries."""
from datetime import datetime,timezone
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from offline_http import post as offline_post,get as offline_get
RUNTIME=['telegram_bot/bot.py','telegram_bot/ui.py','telegram_bot/views.py','telegram_bot/order_history.py',
         'telegram_bot/commerce/checkout.py','telegram_bot/commerce/wallets.py',
         'telegram_bot/commerce/manual_payments.py','telegram_bot/commerce/vodafone_cash.py',
         'telegram_bot/commerce/payment_media.py','telegram_bot/assets/vodafone_cash_logo.jpg',
         'telegram_bot/assets/vodafone_cash_logo.source.json']

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--focused',action='store_true')
    args=parser.parse_args()
    suite=unittest.TestSuite()
    patterns=(('test_commerce_vodafone_cash.py','test_commerce_checkout.py','test_commerce_wallets.py',
               'test_commerce_manual_payments.py','test_commerce_support.py','test_telegram_menu_navigation.py',
               'test_telegram_order_history.py','test_wallet_activity_display.py') if args.focused else
              ('test_commerce_*.py','test_telegram*.py','test_wallet_activity_display.py'))
    for pattern in patterns:
        suite.addTests(unittest.defaultTestLoader.discover(str(ROOT/'tests'),pattern=pattern))
    output=io.StringIO()
    with patch('urllib.request.urlopen',side_effect=AssertionError('Live network forbidden')), \
         patch('requests.Session.post',new=offline_post), \
         patch('requests.Session.get',new=offline_get), \
         patch.dict(os.environ,{'ENABLE_CRYPTO_PAYMENTS':'1'}):
        result=unittest.TextTestRunner(verbosity=1,stream=output).run(suite)
    suffix='focused' if args.focused else 'verification'
    (ROOT/f'scratch/vodafone_{suffix}_20261020.log').write_text(output.getvalue(),encoding='utf-8')
    report={'at':datetime.now(timezone.utc).isoformat(),'tests_run':result.testsRun,'passed':result.wasSuccessful(),
            'failures':len(result.failures),'errors':len(result.errors),'network':'blocked','production_modified':False,
            'failed_tests':[{'id':case.id(),'detail':detail} for case,detail in result.failures+result.errors],
            'runtime_sha256':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in RUNTIME},
            'migration_sha256':hashlib.sha256((ROOT/'supabase/migrations/202610200050_commerce_vodafone_cash.sql').read_bytes()).hexdigest()}
    (ROOT/f'docs/telegram_commerce/vodafone_cash_{suffix}_20261020.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({key:report[key] for key in ['tests_run','passed','failures','errors']}))
    return 0 if result.wasSuccessful() else 1

if __name__=='__main__':raise SystemExit(main())
