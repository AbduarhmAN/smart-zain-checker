"""Owner-authorized management operations for the selected Supabase project.

Reads the token from environment. Never logs credentials or response headers.
Runtime commerce must use restricted RPCs, not this administrative interface.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PROJECT = 'avuzwbmiiavbuaxmnitp'
ROOT = Path(__file__).resolve().parents[2]


def query(statement: str, *, read_only: bool = True):
    token = os.environ.get('SUPABASE_ACCESS_TOKEN', '')
    if not token:
        raise RuntimeError('SUPABASE_ACCESS_TOKEN is missing')
    request = Request(f'https://api.supabase.com/v1/projects/{PROJECT}/database/query',
        data=json.dumps({'query': statement, 'read_only': read_only}).encode('utf-8'),
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}, method='POST')
    for attempt in range(6):
        try:
            with urlopen(request, timeout=60) as response:
                return json.load(response)
        except HTTPError as exc:
            if exc.code == 429 and attempt < 5:
                time.sleep(2 * (attempt + 1))
                continue
            body = exc.read().decode('utf-8', errors='replace')
            try:
                diagnostic = json.loads(body).get('message', 'No SQL diagnostic')
            except (ValueError, AttributeError):
                diagnostic = 'Response body withheld'
            raise RuntimeError(f'Supabase SQL HTTP {exc.code}: {diagnostic}') from None
        except URLError:
            if attempt < 5:
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError('Supabase management network failure; inspect state before retrying a write') from None


def preflight():
    return query("""select current_database() as database_name,
        current_setting('server_version') as postgres_version,
        exists(select 1 from pg_namespace where nspname='commerce') as commerce_exists,
        exists(select 1 from pg_namespace where nspname='commerce_rpc') as rpc_exists,
        exists(select 1 from pg_roles where rolname='anon') as anon_exists,
        exists(select 1 from pg_roles where rolname='authenticated') as authenticated_exists,
        exists(select 1 from pg_roles where rolname='service_role') as service_role_exists,
        (select count(*) from pg_tables where schemaname='public') as existing_public_tables""")


def verify_foundation():
    metadata = query("""select
        (select count(*) from pg_tables where schemaname='commerce') as commerce_tables,
        (select bool_and(rowsecurity) from pg_tables where schemaname='commerce') as all_rls,
        has_schema_privilege('anon','commerce','USAGE') as anon_storage_access,
        has_schema_privilege('authenticated','commerce','USAGE') as client_storage_access,
        (select count(*) from pg_proc p join pg_namespace n on n.oid=p.pronamespace
          where n.nspname='commerce_rpc') as rpc_functions,
        (select bool_and(not has_function_privilege('anon',p.oid,'EXECUTE')
                        and not has_function_privilege('authenticated',p.oid,'EXECUTE')
                        and has_function_privilege('service_role',p.oid,'EXECUTE'))
          from pg_proc p join pg_namespace n on n.oid=p.pronamespace
          where n.nspname='commerce_rpc') as rpc_grants_correct,
        (select count(*) from pg_tables where schemaname='public') as public_tables""")
    if len(metadata) != 1:
        raise RuntimeError('Invalid verification response')
    facts = metadata[0]
    if facts['commerce_tables'] != 46 or not facts['all_rls'] or facts['anon_storage_access'] or facts['client_storage_access']:
        raise RuntimeError('Remote schema/RLS verification failed')
    if facts['rpc_functions'] != 105 or not facts['rpc_grants_correct']:
        raise RuntimeError('Remote RPC grants verification failed')
    hashes = {r['version']: r['sha256'] for r in query('select version,sha256 from commerce.schema_migrations')}
    for path in (ROOT/'supabase/migrations').glob('*commerce*.sql'):
        if hashes.get(path.stem) != hashlib.sha256(path.read_bytes()).hexdigest():
            raise RuntimeError(f'Remote migration hash mismatch: {path.stem}')
    pricing = query('select unit,unit_price_minor,currency,refund_destination,refund_requires_consent from commerce.pricing_policies order by version desc limit 1')
    # The management API represents PostgreSQL NUMERIC as strings. Compare
    # its exact value without changing configured prices or truncating it.
    compared=[{**row,'unit_price_minor':Decimal(str(row['unit_price_minor']))} for row in pricing]
    if compared != [{'unit':'check','unit_price_minor':1,'currency':'EGP',
                   'refund_destination':'wallet','refund_requires_consent':True}]:
        raise RuntimeError('Remote agreed policy verification failed')
    return {'project_ref':PROJECT,'metadata':metadata,'pricing':pricing,'migration_hashes_match':True}


def migration_body(text):
    # Files must have one transaction wrapper. Keep all internal SQL intact.
    stripped, count = re.subn(r'^begin;\s*$', '', text, count=1, flags=re.MULTILINE | re.IGNORECASE)
    if count != 1 or not stripped.rstrip().lower().endswith('commit;'):
        raise ValueError('Migration needs exactly one outer begin/commit wrapper')
    return stripped.rstrip()[:-len('commit;')]


def apply_foundation():
    initial = preflight()
    if not isinstance(initial, list) or len(initial) != 1:
        raise RuntimeError('Unexpected preflight shape')
    state = initial[0]
    if not all(state.get(key) for key in ('anon_exists','authenticated_exists','service_role_exists')):
        raise RuntimeError('Required Supabase roles unavailable')
    existing = {}
    if state['commerce_exists']:
        tracked = query("select to_regclass('commerce.schema_migrations') is not null as tracked")
        if not tracked[0]['tracked']:
            raise RuntimeError('Existing untracked commerce schema; refusing to overwrite')
        existing = {row['version']: row['sha256'] for row in query('select version,sha256 from commerce.schema_migrations')}
    elif state['rpc_exists']:
        raise RuntimeError('Existing untracked commerce_rpc schema; refusing changes')
    applied = []
    migrations = sorted((ROOT / 'supabase/migrations').glob('*commerce*.sql'))
    if not migrations:
        raise RuntimeError('No commerce migrations found; refusing an empty deployment')
    for path in migrations:
        version = path.stem
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if version in existing:
            if existing[version] != digest:
                raise RuntimeError(f'Applied migration differs from file: {version}')
            applied.append({'version': version, 'sha256': digest, 'status': 'already_applied'})
            continue
        sql = 'begin; select pg_advisory_xact_lock(610140001);\n' + migration_body(path.read_text(encoding='utf-8'))
        if not state['commerce_exists'] and not applied:
            sql += """
                create table commerce.schema_migrations (
                    version text primary key, sha256 text not null,
                    applied_at timestamptz not null default now());
                alter table commerce.schema_migrations enable row level security;
                revoke all on commerce.schema_migrations from public,anon,authenticated,service_role;
            """
        # Names come only from repository migration filenames; no user SQL interpolation.
        if not re.fullmatch(r'[a-z0-9_]+', version):
            raise ValueError('Unsafe migration filename')
        sql += f"\ninsert into commerce.schema_migrations(version,sha256) values ('{version}','{digest}'); commit;"
        query(sql, read_only=False)
        applied.append({'version': version, 'sha256': digest, 'status': 'applied'})
    result = {'at': datetime.now(timezone.utc).isoformat(), 'project_ref': PROJECT,
              'preflight': initial, 'migrations': applied,
              'verification': query("""select schemaname,count(*) as tables,
                bool_and(rowsecurity) as all_tables_have_rls from pg_tables
                where schemaname='commerce' group by schemaname""")}
    proof = ROOT / 'docs/telegram_commerce' / ('supabase_foundation_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '.json')
    proof.parent.mkdir(parents=True, exist_ok=True)
    proof.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'evidence': str(proof), **result}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['preflight','apply-foundation','verify-foundation'])
    args = parser.parse_args()
    try:
        action = {'preflight':preflight,'apply-foundation':apply_foundation,'verify-foundation':verify_foundation}[args.action]
        print(json.dumps(action(), ensure_ascii=False))
    except (RuntimeError, ValueError) as exc:
        print(str(exc))
        raise SystemExit(1)
