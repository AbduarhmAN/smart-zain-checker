"""Reproduce a real restricted-role COMMIT, with all fixtures retained."""
import json
from pathlib import Path
import secrets
from run_commerce_database_acceptance import ROOT,DATA,sql


def main():
    if Path(sql('postgres',statement='show data_directory')[0]).resolve()!=DATA.resolve():
        raise RuntimeError('Unknown PostgreSQL instance; refusing changes')
    suffix=secrets.token_hex(6);database='commerce_deferred_'+suffix;role='commerce_commit_'+suffix
    sql('postgres',statement=f'create database {database}')
    migrations=sorted((ROOT/'supabase/migrations').glob('*commerce*.sql'))
    patch=next(p for p in migrations if '150028_' in p.name)
    for path in migrations:
        if path!=patch:sql(database,file=path)
    sql(database,statement=f"create role {role} nologin nosuperuser nobypassrls; "
        f"grant usage on schema commerce_rpc to {role}; "
        f"grant execute on function commerce_rpc.issue_cash_credit_code(text,bigint,bigint,bigint,text,bigint,text,bigint,timestamptz) to {role}")
    statement=(f"begin; set local role {role}; select commerce_rpc.issue_cash_credit_code("
        "'COMMIT_CASH',2500,1,2500,'COMMIT-RECEIPT-001',9,'native-review',null,null); commit;")
    version=int(sql(database,statement="show server_version_num")[0])
    before='passed_on_postgresql_18_event_role_context'
    try:sql(database,statement=statement)
    except RuntimeError as exc:
        if 'permission denied' not in str(exc):raise
        before='permission_denied_at_commit'
        if sql(database,statement='select count(*) from commerce.codes')[0]!='0':
            raise AssertionError('Failed commit left a partially funded code')
    else:
        # PG18 intentionally changed deferred AFTER triggers to use the role
        # that queued the event. Production PG17 uses the role at commit.
        if version<180000:raise AssertionError('Expected PG17 commit failure was not reproduced')
    sql(database,file=patch)
    sql(database,statement=statement);sql(database,statement=statement)
    if sql(database,statement='select count(*) from commerce.codes')[0]!='1':raise AssertionError('Code replay duplicated issuance')
    if sql(database,statement='select count(*) from commerce.cash_receipts')[0]!='1':raise AssertionError('Receipt replay duplicated cash')
    if sql(database,statement='select count(*) from commerce.ledger_entries')[0]!='2':raise AssertionError('Journal replay duplicated lines')
    # The same role still cannot read financial storage directly.
    try:sql(database,statement=f'set role {role}; select * from commerce.ledger_entries')
    except RuntimeError as exc:
        if 'permission denied' not in str(exc):raise
    else:raise AssertionError('Private storage became accessible')
    sql(database,statement="""create function commerce_rpc.test_unbalanced_commit() returns void
        language plpgsql security definer set search_path='' as $$ declare j uuid; begin
        insert into commerce.journals(idempotency_key,kind,payload_fingerprint)
        values ('test:unbalanced','topup','test') returning id into j;
        insert into commerce.ledger_entries(journal_id,line_number,account_key,delta_minor)
        values (j,1,'test:one',-1),(j,2,'test:two',2); end $$;"""
        +f' grant execute on function commerce_rpc.test_unbalanced_commit() to {role}')
    try:sql(database,statement=f'begin; set local role {role}; select commerce_rpc.test_unbalanced_commit(); commit;')
    except RuntimeError as exc:
        if 'Journal must contain at least two balanced lines' not in str(exc):raise
    else:raise AssertionError('Unbalanced journal passed commit')
    proof={'passed':True,'production':False,'database':database,'postgres_version_num':version,
        'pre_fix_outcome':before,'production_version':'17.6','checks':[
        'Post-fix commit and replay produce one cash code, one receipt and two ledger lines',
        'Runtime role still denied direct financial storage',
        'Deferred balance validation still rejects unbalanced journals']}
    path=ROOT/'docs/telegram_commerce'/f'acceptance_{database}.json'
    path.write_text(json.dumps(proof,indent=2),encoding='utf-8')
    print(json.dumps({'passed':True,'evidence':str(path)}))


if __name__=='__main__':main()
