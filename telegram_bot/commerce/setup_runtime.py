"""Provision the previously authorized project backend connection; no test users."""
from __future__ import annotations
import json
import os
import time
from urllib.error import HTTPError,URLError
from urllib.request import Request,urlopen
from .client import PROJECT,CommerceClient,CommerceUnavailable
from .secret_store import save_service_key


def management(path,method='GET',body=None):
    token = os.environ.get('SUPABASE_ACCESS_TOKEN')
    if not token:
        raise RuntimeError('Management token unavailable')
    request = Request(f'https://api.supabase.com/v1/projects/{PROJECT}/{path}',
        headers={'Authorization':f'Bearer {token}','Content-Type':'application/json'},method=method,
        data=None if body is None else json.dumps(body).encode('utf-8'))
    try:
        with urlopen(request,timeout=30) as response:
            return json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f'Supabase configuration HTTP {exc.code}') from None
    except (URLError,TimeoutError):
        raise RuntimeError('Supabase configuration response unavailable; inspect before retrying') from None


def main():
    keys = management('api-keys?reveal=true')
    # Prefer the existing service-role credential; never create or rotate keys.
    candidates = [r.get('api_key') for r in keys if r.get('name') == 'service_role' and r.get('api_key')]
    if not candidates:
        candidates = [r.get('api_key') for r in keys if r.get('type') == 'secret' and r.get('api_key')]
    if not candidates:
        raise RuntimeError('No existing backend service credential available')
    save_service_key(candidates[0])
    del candidates,keys
    config = management('postgrest')
    schemas = [s.strip() for s in config['db_schema'].split(',') if s.strip()]
    before = list(schemas)
    if 'commerce_rpc' not in schemas:
        schemas.append('commerce_rpc')
        management('postgrest','PATCH',{'db_schema':','.join(schemas)})
    after = [s.strip() for s in management('postgrest')['db_schema'].split(',') if s.strip()]
    if any(s not in after for s in before) or 'commerce_rpc' not in after:
        raise RuntimeError('Exposed schema verification failed')
    # PostgREST configuration propagation may lag its management response.
    # Retry only this read-only health RPC, never the configuration write.
    client = CommerceClient()
    for attempt in range(3):
        try:
            health = client.rpc('health')
            break
        except CommerceUnavailable:
            if attempt == 2:
                raise
            time.sleep(attempt+1)
    return {'project_ref':PROJECT,'credential_encrypted':True,'schemas_before':before,'schemas_after':after,
            'health':health}


if __name__ == '__main__':
    try:
        print(json.dumps(main()))
    except RuntimeError as exc:
        print(str(exc))
        raise SystemExit(1)
