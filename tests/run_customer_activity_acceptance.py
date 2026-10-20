"""Exercise all migrations and read-only activity reports on retained local PostgreSQL."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import secrets
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from run_commerce_database_acceptance import DATA, sql


def main():
    if Path(sql('postgres', statement='show data_directory')[0]).resolve() != DATA.resolve():
        raise RuntimeError('Unknown PostgreSQL instance; refusing writes')
    database = 'commerce_activity_' + secrets.token_hex(6)
    sql('postgres', statement='create database ' + database)
    files = sorted((ROOT / 'supabase/migrations').glob('*commerce*.sql'))
    for path in files:
        sql(database, file=path)
    test = ROOT / 'supabase/tests/commerce_customer_activity.sql'
    sql(database, file=test)
    # Real service-role execution, independent of the owner's test session.
    sql(database, statement="select commerce_rpc.configure_support_actors('[9]'::jsonb,'activity-role-test')")
    result = json.loads(sql(database, statement="set role service_role; select commerce_rpc.admin_customer_activity(9,'free')")[0].splitlines()[-1])
    if result['items'] != []:
        raise AssertionError('Unexpected records after fixture rollback')
    proof = {'at': datetime.now(timezone.utc).isoformat(), 'production': False, 'passed': True,
             'database': database, 'migration_count': len(files),
             'migration_sha256': hashlib.sha256(files[-1].read_bytes()).hexdigest(),
             'test_sha256': hashlib.sha256(test.read_bytes()).hexdigest(),
             'fixtures': 'Synthetic financial and checker fixtures on retained local PostgreSQL; no live customer actions.',
             'checks': ['cash and promotional code credits require posted wallet grants',
                        'free usage includes zero pricing, full discounts and fully promotional payment',
                        'mixed paid/free orders counted by verified row',
                        'failed, cancelled, unpaid and partial-cash checks excluded',
                        'partner and administrator exclusion applies to lists and details',
                        'pagination and totals', 'local administrator exclusion list',
                        'RPC privileges and real service-role execution', 'administrator revocation',
                        'read-only reports preserve wallet, journal, order and check state'],
             'builds_run': False, 'files_deleted': False}
    destination = ROOT / 'docs/telegram_commerce' / ('acceptance_' + database + '.json')
    destination.write_text(json.dumps(proof, indent=2), encoding='utf-8')
    print(json.dumps({'database': database, 'passed': True, 'evidence': str(destination)}))


if __name__ == '__main__':
    main()
