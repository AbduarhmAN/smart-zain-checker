"""Reuse the existing commerce SQLite file; no additional production database."""
import os
from pathlib import Path


def database_path(root):
    root=Path(root).resolve()
    path=root/'.commerce'/'outbox.sqlite'
    if not path.exists():
        marker=root/'.services'/'TEST_FIXTURE'
        if os.environ.get('ZAIN_SERVICE_TESTING')!='1' or not marker.exists() or marker.read_text(encoding='utf-8').strip()!='smart-zain-isolated-tests':
            raise RuntimeError('Existing commerce outbox database required; restore the existing application state before cutover')
        path.parent.mkdir(parents=True,exist_ok=True)
    return path
