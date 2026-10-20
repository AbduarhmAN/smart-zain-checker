"""Offline evidence: membership gate plus subscribed payment/support/navigation fixtures."""
import hashlib,io,json,os,sys,unittest
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from offline_http import post as offline_post,get as offline_get
from run_vodafone_cash_regressions import RUNTIME

suite=unittest.TestSuite()
for pattern in ('test_telegram_subscription.py','test_official_support.py','test_telegram_maintenance.py',
    'test_commerce_vodafone_cash.py','test_commerce_checkout.py','test_commerce_wallets.py',
    'test_commerce_manual_payments.py','test_commerce_support.py','test_telegram_menu_navigation.py',
    'test_telegram_order_history.py','test_wallet_activity_display.py','test_telegram_ui.py'):
    suite.addTests(unittest.defaultTestLoader.discover(str(ROOT/'tests'),pattern=pattern))
output=io.StringIO()
with patch('urllib.request.urlopen',side_effect=AssertionError('Live network forbidden')),\
     patch('requests.Session.post',new=offline_post),patch('requests.Session.get',new=offline_get),\
     patch.dict(os.environ,{'ENABLE_CRYPTO_PAYMENTS':'1'}):
    result=unittest.TextTestRunner(stream=output,verbosity=1).run(suite)
names=RUNTIME+['telegram_bot/official_support.py','telegram_bot/commerce/support_ui.py',
    'maintenance/telegram_maintenance.py','telegram_bot/subscription.py','telegram_bot/subscription_policy.py']
proof={'at':datetime.now(timezone.utc).isoformat(),'passed':result.wasSuccessful(),'tests_run':result.testsRun,
    'failures':len(result.failures),'errors':len(result.errors),'network':'blocked','channel':'@zainchecker',
    'membership_tests':'real gate; synthetic Bot API including member/left/kicked/restricted/errors and identity mismatch',
    'commerce_and_navigation_fixtures':'explicitly subscribed actors, no global gate bypass',
    'historical_support_engine_tests':'isolated compatibility mode; production channel-only entry points tested separately',
    'source_sha256':{p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in names},
    'failed_tests':[{'id':case.id(),'detail':detail} for case,detail in result.failures+result.errors]}
(ROOT/'docs/telegram_commerce/subscription_verification_20261020.json').write_text(json.dumps(proof,indent=2),encoding='utf-8')
(ROOT/'scratch/subscription_regressions.log').write_text(output.getvalue(),encoding='utf-8')
print(json.dumps({k:proof[k] for k in ('passed','tests_run','failures','errors')}))
raise SystemExit(0 if proof['passed'] else 1)
