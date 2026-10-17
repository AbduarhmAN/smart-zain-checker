"""Run Telegram regressions with external I/O blocked and local HTTP fixtures."""
import atexit
import json
import os
import shutil
import sys
import tempfile
import unittest
from offline_http import post as offline_post,get as offline_get
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'outputs/telegram_audit_20261007'
OUT.mkdir(parents=True,exist_ok=True)
fixture=Path(tempfile.mkdtemp(prefix='isolated_run_',dir=OUT))
tempfile.tempdir=str(fixture)
sys.path.insert(0,str(ROOT));os.chdir(fixture)

# XLSX fixture serialization normally removes its temporary XML file. Retain it.
import openpyxl.worksheet._writer as worksheet_writer
def retain_xml(writer):
    if writer.out in worksheet_writer.ALL_TEMP_FILES:worksheet_writer.ALL_TEMP_FILES.remove(writer.out)
worksheet_writer.WorksheetWriter.cleanup=retain_xml
atexit.unregister(worksheet_writer._openpyxl_shutdown)

patterns=['test_access_boundaries.py','test_customer_commerce_runtime.py','test_telegram_order_history.py','test_telegram_transport_reliability.py','test_telegram_queue_durability.py',
          'test_telegram_workflow_reliability.py','test_telegram_payment_reliability.py',
          'test_telegram_notifier_reliability.py','test_telegram_ui.py',
          'test_sheet_workflow.py','test_dashboard_controls.py','test_pricing.py']
suite=unittest.TestSuite()
for pattern in patterns:suite.addTests(unittest.defaultTestLoader.discover(str(ROOT/'tests'),pattern=pattern))

with patch('urllib.request.urlopen',side_effect=AssertionError('Live network forbidden by offline suite')),\
     patch('telegram_bot.commerce.client.urlopen',side_effect=AssertionError('Live commerce network forbidden by offline suite')),\
     patch('requests.Session.post',new=offline_post),\
     patch('requests.Session.get',new=offline_get),\
     patch('pathlib.Path.unlink',side_effect=AssertionError('File deletion forbidden')),\
     patch('shutil.rmtree',side_effect=AssertionError('Directory deletion forbidden')),\
     patch('os.remove',side_effect=AssertionError('File deletion forbidden')),\
     patch('os.unlink',side_effect=AssertionError('File deletion forbidden')):
    result=unittest.TextTestRunner(verbosity=1).run(suite)
report={'tests_run':result.testsRun,'passed':result.wasSuccessful(),'failures':len(result.failures),
        'errors':len(result.errors),'skipped':len(result.skipped),'fixture':str(fixture),
        'patterns':patterns,'real_network':'external I/O blocked; isolated loopback HTTP fixtures allowed',
        'filesystem_deletion':'blocked; fixture XML retained'}
(OUT/'combined_test_results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report,ensure_ascii=False));sys.exit(0 if result.wasSuccessful() else 1)
