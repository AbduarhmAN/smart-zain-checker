"""Focused tests with retained fixtures: no builds, deletion or external network."""
import functools
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'tests'))
from retained_runtime import install
retained_cleanup = install()

patterns = sys.argv[1:] or [
    'test_cross_file_scheduler.py', 'test_customer_commerce_runtime.py',
    'test_restart_and_queue_flow.py', 'test_queue_resilience.py',
    'test_recovery_and_route_isolation.py', 'test_service2_regressions.py',
    'test_target_closed_healing.py', 'test_telegram_queue_durability.py',
    'test_telegram_order_history.py', 'test_services_engine.py',
    'test_telegram_service_boundary.py', 'test_commerce_checkout.py',
]
suite = unittest.TestSuite()
for pattern in patterns:
    suite.addTests(unittest.defaultTestLoader.discover(str(ROOT/'tests'), pattern=pattern))
result = unittest.TextTestRunner(verbosity=2).run(suite)

evidence = {'tests_run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
    'passed': result.wasSuccessful(), 'patterns': patterns,
    'cleanup_calls_retained': len(retained_cleanup), 'files_deleted': False, 'builds_run': False,
    'external_network_checks': False}
folder = ROOT/'retained-test-evidence'
folder.mkdir(exist_ok=True)
(folder/'scheduler_regressions.json').write_text(json.dumps(evidence, indent=2), encoding='utf-8')
print(json.dumps(evidence))
raise SystemExit(0 if result.wasSuccessful() else 1)
