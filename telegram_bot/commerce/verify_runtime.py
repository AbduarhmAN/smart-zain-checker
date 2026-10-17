"""Read-only runtime proof: backend health, anonymous denial, safe schema metadata."""
import json
import os
from pathlib import Path
from datetime import datetime,timezone
from .client import CommerceClient,CommerceUnavailable,PROJECT
from .management import verify_foundation
from .setup_runtime import management


def main():
    proof = {'at':datetime.now(timezone.utc).isoformat(),'project_ref':PROJECT,
             'backend_health':CommerceClient().rpc('health'),'foundation':verify_foundation()}
    try:
        CommerceClient(key_provider=lambda:os.environ['SUPABASE_KEY']).rpc('health')
    except CommerceUnavailable as exc:
        if str(exc) not in {'Commerce RPC HTTP 401','Commerce RPC HTTP 403','Commerce RPC HTTP 406'}:
            raise
        proof['anonymous_denied'] = True
        proof['anonymous_denial'] = str(exc)
    else:
        raise RuntimeError('Anonymous key unexpectedly reached backend RPC')
    config = management('postgrest')
    proof['exposed_schemas'] = [s.strip() for s in config['db_schema'].split(',')]
    proof['test_users_created'] = False
    path = Path(__file__).resolve().parents[2]/'docs/telegram_commerce'/f"runtime_verification_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(proof,ensure_ascii=False,indent=2),encoding='utf-8')
    return {'passed':True,'proof':str(path),'backend_health':proof['backend_health'],'anonymous_denied':True}


if __name__ == '__main__':
    print(json.dumps(main()))
