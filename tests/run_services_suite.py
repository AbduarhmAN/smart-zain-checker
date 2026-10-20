"""Run service boundary tests from a retained isolated working directory."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
fixture=Path(tempfile.mkdtemp(prefix='service_boundaries_retained_'))
os.chdir(fixture)
patterns=['test_services_ipc.py','test_services_resilience.py','test_services_files.py','test_services_browser.py','test_services_engine.py',
          'test_recovery_and_route_isolation.py','test_minimal_sheet.py','test_results_exporter.py']
suite=unittest.TestSuite()
for pattern in patterns:suite.addTests(unittest.defaultTestLoader.discover(str(ROOT/'tests'),pattern=pattern))
started=time.perf_counter();result=unittest.TextTestRunner(verbosity=1).run(suite)
report={'passed':result.wasSuccessful(),'tests_run':result.testsRun,'failures':len(result.failures),
        'errors':len(result.errors),'skipped':len(result.skipped),'seconds':time.perf_counter()-started,
        'patterns':patterns,'isolated_cwd':str(fixture),'production_modified':False}
destination=ROOT/'docs/four_services/service_regression_results.json'
destination.write_text(json.dumps(report,indent=2),encoding='utf-8')
print(json.dumps(report,indent=2))
raise SystemExit(0 if result.wasSuccessful() else 1)
