"""Deterministic crash point for isolated subprocess tests only."""
import os
from pathlib import Path
import time


def install_publish_gate(root):
    root=Path(root).resolve();marker=root/'.services/TEST_FIXTURE'
    if os.environ.get('ZAIN_SERVICE_TESTING')!='1' or root==Path(__file__).resolve().parents[1] or not marker.is_file() or marker.read_text().strip()!='smart-zain-isolated-tests':
        raise RuntimeError('Isolated file fault-injection fixture required')
    original=os.replace
    target=(root/'report.xlsx').resolve()
    def replace(source,destination,*args,**kwargs):
        if Path(destination).resolve()==target:
            (root/'.services'/'publish-ready').write_text('complete temporary workbook; replacement not committed',encoding='utf-8')
            deadline=time.monotonic()+120
            while not (root/'.services'/'publish-release').exists():
                if time.monotonic()>deadline:raise TimeoutError('Isolated publish gate not released')
                time.sleep(.01)
        return original(source,destination,*args,**kwargs)
    os.replace=replace
