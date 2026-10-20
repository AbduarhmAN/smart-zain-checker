"""Commerce regression suite with blocked external I/O and filesystem deletion."""
import atexit
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'docs/telegram_commerce'
fixture=Path(tempfile.mkdtemp(prefix='commerce-suite-retained-'))
tempfile.tempdir=str(fixture)
sys.path.insert(0,str(ROOT));os.chdir(fixture)
import openpyxl.worksheet._writer as worksheet_writer
def retain_xml(writer):
    if writer.out in worksheet_writer.ALL_TEMP_FILES:worksheet_writer.ALL_TEMP_FILES.remove(writer.out)
worksheet_writer.WorksheetWriter.cleanup=retain_xml
atexit.unregister(worksheet_writer._openpyxl_shutdown)
with (patch('urllib.request.urlopen',side_effect=AssertionError('Live network forbidden')),
     patch('requests.Session.post',side_effect=AssertionError('Live network forbidden')),
     patch('requests.Session.get',side_effect=AssertionError('Live network forbidden')),
     patch('pathlib.Path.unlink',side_effect=AssertionError('File deletion forbidden')),
     patch('shutil.rmtree',side_effect=AssertionError('Directory deletion forbidden')),
     patch('os.remove',side_effect=AssertionError('File deletion forbidden')),
     patch('os.unlink',side_effect=AssertionError('File deletion forbidden'))):
    suite=unittest.defaultTestLoader.discover(str(ROOT/'tests'),pattern='test_commerce_*.py')
    result=unittest.TextTestRunner(verbosity=1).run(suite)
report={'tests_run':result.testsRun,'passed':result.wasSuccessful(),'failures':len(result.failures),'errors':len(result.errors),
    'fixtures_retained_at':str(fixture),'network':'blocked','filesystem_deletion':'blocked; XML fixture files retained'}
OUT.mkdir(parents=True,exist_ok=True)
(OUT/'cash_code_commerce_tests_20261014.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report));sys.exit(0 if result.wasSuccessful() else 1)
