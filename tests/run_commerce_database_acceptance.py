"""Exercise the SQL against our isolated PostgreSQL instance, retaining all files.

No production connection, build, compiler, deletion or existing wallet mutation.
Every run creates a new database; evidence and synthetic records are retained.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import secrets
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
PSQL = Path(r'C:\Program Files\PostgreSQL\18\bin\psql.exe')
DATA = ROOT / 'scratch' / 'commerce_pg_20261014' / 'data'


def sql(database, *, statement=None, file=None):
    runtime_port=DATA.parent/'runtime-port.json'
    port=55439
    if runtime_port.exists():
        selected=json.loads(runtime_port.read_text(encoding='utf-8'))
        if selected.get('data_directory')!=str(DATA) or type(selected.get('port')) is not int or not 1024<=selected['port']<=65535:
            raise RuntimeError('Invalid retained test-instance port selection')
        port=selected['port']
    args = [str(PSQL), '-X', '-h', '127.0.0.1', '-p', str(port), '-U', 'commerce_test_admin',
            '-d', database, '-v', 'ON_ERROR_STOP=1', '-At']
    if statement is not None and len(statement)>12000:
        # Windows command lines cannot carry a full 4,000-row checkout JSON.
        # Retain the synthetic SQL fixture and pass its filename instead.
        retained=ROOT/'scratch'/('sql_acceptance_'+secrets.token_hex(8)+'.sql')
        with retained.open('x',encoding='utf-8') as stream:stream.write(statement)
        args+=['-f',str(retained)]
    else:
        args += ['-c', statement] if statement is not None else ['-f', str(file)]
    result = subprocess.run(args, capture_output=True, text=True, encoding='utf-8', timeout=60)
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return result.stdout.strip(), result.stderr.strip()


def main():
    live_path = Path(sql('postgres', statement='show data_directory')[0]).resolve()
    if live_path != DATA.resolve():
        raise RuntimeError('Port belongs to a different PostgreSQL instance; refusing writes')
    database = 'commerce_acceptance_' + secrets.token_hex(6)
    sql('postgres', statement=f'create database {database}')
    evidence = {'run_at': datetime.now(timezone.utc).isoformat(), 'database': database,
                'instance_directory': str(DATA), 'production': False, 'checks': [], 'files': []}
    for file in sorted((ROOT / 'supabase' / 'migrations').glob('*commerce*.sql')):
        stdout, stderr = sql(database, file=file)
        evidence['files'].append({'path': str(file.relative_to(ROOT)),
                                  'sha256': hashlib.sha256(file.read_bytes()).hexdigest(),
                                  'stdout': stdout, 'stderr': stderr})
    cent_fixtures={'commerce_customer_pricing.sql','commerce_promotional_expiry.sql','commerce_customer_stop.sql',
                   'commerce_cent_discount.sql','commerce_legacy_transition.sql','commerce_customer_history.sql','commerce_bound_invoice_monitor.sql','commerce_binance_note_binding.sql'}
    retained=ROOT/'outputs'/'commerce_sql_fixtures'/database
    retained.mkdir(parents=True,exist_ok=True)
    legacy_pricing="update commerce.customer_price_settings set base_minor=3000,service_excess_minor=3000 where target_telegram_id=0;\nupdate commerce.pricing_policies set unit_price_minor=3000 where version=(select max(version) from commerce.pricing_policies);"
    for file in sorted((ROOT / 'supabase' / 'tests').glob('commerce_*.sql')):
        fixture=file
        if file.name in cent_fixtures:
            sql(database,statement=legacy_pricing.replace('=3000','=1'))
        if file.name not in cent_fixtures:
            # Older feature scenarios were specified at EGP 30/check. Preserve
            # that explicit fixture inside their rollback, while the new rule
            # scenarios verify the actual one-piastre default independently.
            content=file.read_text(encoding='utf-8')
            if '\nbegin;' not in content:raise RuntimeError('Transactional regression fixture required')
            fixture=retained/file.name
            fixture.write_text(content.replace('\nbegin;','\nbegin;\n'+legacy_pricing,1),encoding='utf-8')
        stdout, stderr = sql(database, file=fixture)
        evidence['checks'].append({'test': file.name, 'passed': True, 'stdout': stdout, 'stderr': stderr})

    # Concurrency scenarios below also retain their original explicit EGP 30
    # amounts; these assignments affect only this newly created test database.
    sql(database,statement=legacy_pricing)

    # Independent database sessions concurrently register one returning user.
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        ids = list(executor.map(lambda _: sql(database, statement=
            "select commerce_rpc.register_telegram_user(880002,'concurrent')->>'user_id'")[0], range(16)))
    if len(set(ids)) != 1:
        raise AssertionError('Concurrent registration created multiple users')
    user = ids[0]
    wallets = sql(database, statement=f"select count(*) from commerce.wallets where user_id = '{user}'")[0]
    if wallets != '2':
        raise AssertionError('Concurrent registration duplicated wallets')
    order = sql(database, statement=f"""insert into commerce.orders
        (user_id,request_key,checker_job_id,requested_checks,quoted_minor,pricing_snapshot)
        values ('{user}','concurrency-order','concurrency-job',1,100,'{{"unit_minor":100}}')
        returning id""")[0].splitlines()[0]
    event_sql = f"""select commerce_rpc.receive_checker_event('{order}','zain_checker',
        'concurrent-event','check_outcome',
        '{{"schema_version":1,"job_id":"concurrency-job","revision":1,"row_number":2,"check_type":"account","outcome":"succeeded"}}')"""
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        event_ids = list(executor.map(lambda _: sql(database, statement=event_sql)[0], range(16)))
    if len(set(event_ids)) != 1 or sql(database, statement=
        f"select count(*) from commerce.events where order_id = '{order}'")[0] != '1':
        raise AssertionError('Concurrent replay duplicated event')
    evidence['checks'].append({'test': '16 concurrent registrations and 16 concurrent event replays',
                               'passed': True, 'users': 1, 'wallets': 2, 'events': 1})
    def competing_referral(index):
        code, source = ('PARTNER_A', 'deep_link') if index % 2 == 0 else ('PARTNER_B', 'manual')
        return sql(database, statement=f"select commerce_rpc.attribute_referral(881200,'{code}','{source}')->>'partner_id'")[0]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        partner_ids = list(executor.map(competing_referral, range(16)))
    if len(set(partner_ids)) != 1 or sql(database, statement=
        "select count(*) from commerce.referrals where user_id = (select id from commerce.users where telegram_user_id = 881200)")[0] != '1':
        raise AssertionError('Concurrent link/manual code replaced attribution')
    evidence['checks'].append({'test': '16 competing link/manual referrals preserve one first source', 'passed': True})
    order_statement = """select commerce_rpc.create_order(884100,'concurrent-quote',
        '[{"row_number":2,"check_type":"account"},{"row_number":3,"check_type":"service"}]',
        jsonb_build_object('file_sha256',repeat('c',64)))->>'order_id'"""
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        order_ids = list(executor.map(lambda _: sql(database,statement=order_statement)[0], range(16)))
    if len(set(order_ids)) != 1 or sql(database,statement=
        f"select count(*) from commerce.checks where order_id='{order_ids[0]}'")[0] != '2':
        raise AssertionError('Concurrent request replay duplicated order or input rows')
    evidence['checks'].append({'test':'16 concurrent order quotes preserve one order and two rows','passed':True})
    unit = int(sql(database,statement="select commerce_rpc.quote_checks(1)->>'unit_price_minor'")[0])
    receipt_statement = f"select commerce_rpc.confirm_receipt(886001,'concurrent-topup','fixture','concurrent-tx',{unit},'fixture-proof')->>'payment_id'"
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        payment_ids = list(executor.map(lambda _: sql(database,statement=receipt_statement)[0],range(16)))
    if len(set(payment_ids)) != 1 or int(sql(database,statement="select commerce_rpc.wallet_state(886001)->'customer'->>'available_minor'")[0]) != unit:
        raise AssertionError('Concurrent topup receipt credited more than once')
    orders = []
    for index in range(2):
        orders.append(sql(database,statement=f"""select commerce_rpc.create_order(886001,'competing-spend-{index}',
            '[{{"row_number":2,"check_type":"account"}}]',jsonb_build_object('file_sha256',repeat('1',64)))->>'order_id'""")[0])
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        reserves = list(executor.map(lambda index: json.loads(sql(database,statement=
            f"select commerce_rpc.reserve_order('{orders[index%2]}',886001,'fixture-consent')")[0]),range(16)))
    held = sql(database,statement="select order_id from commerce.reservations where order_id in ('"+orders[0]+"','"+orders[1]+"')")[0]
    if held not in orders or int(sql(database,statement="select commerce_rpc.wallet_state(886001)->'customer'->>'reserved_minor'")[0]) != unit:
        raise AssertionError('Concurrent orders overspent the wallet')
    if not any(r['state']=='insufficient_balance' for r in reserves):
        raise AssertionError('Competing order did not receive insufficient balance')
    sql(database,statement=f"select commerce_rpc.bind_checker_job('{held}','concurrent-settle-job')")
    sql(database,statement=f"""select commerce_rpc.record_terminal_results('{held}','concurrent-settle-job',
        '[{{"row_number":2,"state":"failed","verified_result":false}}]',repeat('2',64))""")
    sql(database,statement=f"select commerce_rpc.confirm_result_delivery('{held}',886001,9,'concurrent-file',repeat('2',64))")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        settlements = list(executor.map(lambda _: json.loads(sql(database,statement=f"select commerce_rpc.settle_order('{held}')")[0]),range(16)))
    wallet = json.loads(sql(database,statement="select commerce_rpc.wallet_state(886001)")[0])['customer']
    if wallet['available_minor'] != unit or wallet['reserved_minor'] != 0:
        raise AssertionError('Concurrent settlement changed wallet more than once')
    if sql(database,statement=f"select count(*) from commerce.refunds where order_id='{held}'")[0] != '1':
        raise AssertionError('Concurrent settlement duplicated refund')
    evidence['checks'].append({'test':'16 receipt replays, 16 competing reservations and 16 settlement replays',
                              'passed':True,'payments':1,'funded_orders':1,'refunds':1})
    # A payment request, its provider proof and the ledger credit commit together.
    quote_id = str(uuid4())
    observed = datetime.now(timezone.utc) - timedelta(seconds=5)
    snapshot = {'id':quote_id,'user_id':887100,'amount_minor':3000,'asset':'USDT',
                'asset_amount':'0.600000','egp_per_asset':'50','rate_source':'concurrency-fixture',
                'precision':6,'observed_at':observed.isoformat(),
                'expires_at':(observed+timedelta(minutes=15)).isoformat(),'order_id':None}
    quote_sql = json.dumps(snapshot).replace("'", "''")
    issue = f"select commerce_rpc.create_payment_request('{quote_sql}','binance_pay','uid:fixture')"
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        issued = list(executor.map(lambda _:json.loads(sql(database,statement=issue)[0]),range(16)))
    if sum(not item['replayed'] for item in issued)!=1:
        raise AssertionError('Concurrent payment quote was issued more than once')
    transfer = {'provider':'binance_pay','reference':'quote-concurrent-transfer','asset':'USDT',
                'amount':'0.600000','received_at':datetime.now(timezone.utc).isoformat(),
                'receiver':'uid:fixture','proof_json':'{"transaction_id":"quote-concurrent-transfer"}',
                'invoice_payload':None}
    binding = {'kind':'admin','actor_id':887900,'approval_reference':'synthetic-update:concurrent'}
    confirm = f"select commerce_rpc.confirm_payment_request('{quote_id}',887100,'{json.dumps(transfer)}','{json.dumps(binding)}')"
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        receipts = list(executor.map(lambda _:json.loads(sql(database,statement=confirm)[0]),range(16)))
    if len({item['payment_id'] for item in receipts})!=1 or sum(not item['replayed'] for item in receipts)!=1:
        raise AssertionError('Concurrent quote confirmation duplicated its receipt')
    if int(sql(database,statement="select commerce_rpc.wallet_state(887100)->'customer'->>'available_minor'")[0])!=3000:
        raise AssertionError('Concurrent quote credited the wallet more than once')
    evidence['checks'].append({'test':'16 quote issues and 16 bound-transfer confirmations',
                              'passed':True,'quotes':1,'payments':1,'credited_minor':3000})
    claim_quote=str(uuid4())
    claim_snapshot={**snapshot,'id':claim_quote,'user_id':887101}
    sql(database,statement=f"select commerce_rpc.create_payment_request('{json.dumps(claim_snapshot)}','binance_pay','uid:fixture')")
    submit=f"select commerce_rpc.submit_payment_claim('{claim_quote}',887101,'manual-concurrent-alias')->>'claim_id'"
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        claims=list(executor.map(lambda _:sql(database,statement=submit)[0],range(16)))
    if len(set(claims))!=1:raise AssertionError('Concurrent submissions duplicated payment review')
    claim_transfer={**transfer,'reference':'manual-concurrent-tx',
        'proof_json':'{"transaction_id":"manual-concurrent-tx","order_id":"manual-concurrent-alias"}'}
    approve=f"select commerce_rpc.approve_payment_claim('{claims[0]}',887900,'{json.dumps(claim_transfer)}')"
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        decisions=list(executor.map(lambda _:json.loads(sql(database,statement=approve)[0]),range(16)))
    if len({decision['payment_id'] for decision in decisions})!=1:
        raise AssertionError('Concurrent claim approvals duplicated receipt')
    if int(sql(database,statement="select commerce_rpc.wallet_state(887101)->'customer'->>'available_minor'")[0])!=3000:
        raise AssertionError('Concurrent claim approvals credited more than once')
    evidence['checks'].append({'test':'16 manual claim submissions and 16 approvals',
                              'passed':True,'reviews':1,'payments':1,'credited_minor':3000})
    cash_statement="select commerce_rpc.admin_cash_topup(891001,3001,'CASH-CONCURRENT-001',891900,'synthetic-cash-approval')"
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        cash_receipts=list(executor.map(lambda _:json.loads(sql(database,statement=cash_statement)[0]),range(16)))
    if len({receipt['payment_id'] for receipt in cash_receipts})!=1:
        raise AssertionError('Concurrent cash approval duplicated its receipt')
    if int(sql(database,statement="select commerce_rpc.wallet_state(891001)->'customer'->>'available_minor'")[0])!=3001:
        raise AssertionError('Concurrent cash approval credited more than once')
    evidence['checks'].append({'test':'16 authenticated cash receipt approvals',
                              'passed':True,'receipts':1,'credited_minor':3001})
    partner=sql(database,statement="select commerce_rpc.create_partner(893100,'CONCURRENT_COMMISSION')->>'partner_id'")[0]
    sql(database,statement=f"select commerce_rpc.configure_partner('{partner}',893900,3000,'forever',null,true)")
    sql(database,statement="select commerce_rpc.attribute_referral(893101,'CONCURRENT_COMMISSION','manual')")
    sql(database,statement=f"select commerce_rpc.confirm_receipt(893101,'concurrent-commission-fund','fixture','concurrent-commission-fund',{unit},'fixture-proof')")
    order=sql(database,statement="""select commerce_rpc.create_order(893101,'concurrent-commission-order',
        '[{"row_number":2,"check_type":"account"}]',jsonb_build_object('file_sha256',repeat('9',64)))->>'order_id'""")[0]
    sql(database,statement=f"""select commerce_rpc.reserve_order('{order}',893101,'fixture-consent');
        select commerce_rpc.bind_checker_job('{order}','concurrent-commission-job');
        select commerce_rpc.record_terminal_results('{order}','concurrent-commission-job',
            '[{{"row_number":2,"state":"succeeded","verified_result":true}}]',repeat('8',64));
        select commerce_rpc.confirm_result_delivery('{order}',893101,20,'fixture-result-file',repeat('8',64));""")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        settlements=list(executor.map(lambda _:json.loads(sql(database,statement=f"select commerce_rpc.settle_order('{order}')")[0]),range(16)))
    commission=(unit*3000+5000)//10000
    if any(item['commission_minor']!=commission for item in settlements) or sql(database,statement=f"select count(*) from commerce.commissions where order_id='{order}'")[0]!='1':
        raise AssertionError('Concurrent settlement duplicated commission')
    if sql(database,statement='select successful_paid_checks from commerce.users where telegram_user_id=893101')[0]!='1':
        raise AssertionError('Concurrent settlement consumed entitlement more than once')
    withdrawal_amount=commission*2//3
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        held=list(executor.map(lambda index:json.loads(sql(database,statement=f"select commerce_rpc.request_withdrawal(893100,'competing-withdrawal-{index}',{withdrawal_amount},'bank fixture destination')")[0]),range(16)))
    winners=[item for item in held if item['state']=='requested']
    if len(winners)!=1:raise AssertionError('Competing withdrawals overspent available commission')
    withdrawal=winners[0]['withdrawal_id'];sql(database,statement=f"select commerce_rpc.approve_withdrawal('{withdrawal}',893900,'fixture-approval')")
    payout=f"select commerce_rpc.complete_withdrawal('{withdrawal}',893900,'bank','CONCURRENT-TX','fixture-proof')"
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        payouts=list(executor.map(lambda _:json.loads(sql(database,statement=payout)[0]),range(16)))
    final=json.loads(sql(database,statement='select commerce_rpc.partner_finance(893100)')[0])
    if any(item['state']!='paid' for item in payouts) or (final['available_minor'],final['reserved_minor'],final['withdrawn_minor'])!=(commission-withdrawal_amount,0,withdrawal_amount):
        raise AssertionError('Concurrent payout changed earnings more than once')
    if sql(database,statement=f"select count(*) from commerce.journals where idempotency_key='withdrawal:paid:{withdrawal}'")[0]!='1':
        raise AssertionError('Concurrent payout duplicated ledger debit')
    evidence['checks'].append({'test':'16 commission settlements, 16 competing withdrawals and 16 payout replays',
        'passed':True,'commissions':1,'commission_minor':commission,'paid_check_count':1,'held_withdrawals':1,
        'paid_minor':withdrawal_amount,'ending_available_minor':commission-withdrawal_amount})
    limits={'max_percent_basis_points':10000,'max_checks_per_user':50,'max_discount_minor_per_user':50000,
        'max_total_discount_minor':100000,'max_duration_seconds':2592000,'max_wallet_credit_minor':5000,'max_redemptions':100}
    sql(database,statement=f"select commerce_rpc.configure_campaign_policy(895900,'{json.dumps(limits)}','fixture-policy')")
    campaign={'benefit_kind':'wallet_credit','benefit_value':3000,'eligibility':'any','check_types':['account','service'],
        'max_checks':1,'max_discount_minor':3000,'max_total_discount_minor':9000,'duration_seconds':259200,'max_redemptions':100}
    sql(database,statement=f"select commerce_rpc.create_platform_campaign(895900,'concurrent-grant','CONCURRENT_GRANT','{json.dumps(campaign)}','fixture-review')")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        activations=list(executor.map(lambda index:json.loads(sql(database,statement=f"select commerce_rpc.activate_campaign({895000+index},'CONCURRENT_GRANT','fixture-activate')")[0]),range(16)))
    if sum(item['state']=='activated' for item in activations)!=3:raise AssertionError('Concurrent campaign grants exceeded global budget')
    credited=sql(database,statement='select sum(promotional_available_minor) from commerce.wallets where user_id in (select id from commerce.users where telegram_user_id between 895000 and 895015)')[0]
    if credited!='9000':raise AssertionError('Concurrent promotional grants had incorrect classified credit')
    winner=next(895000+index for index,item in enumerate(activations) if item['state']=='activated')
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        repeated=list(executor.map(lambda _:json.loads(sql(database,statement=f"select commerce_rpc.activate_campaign({winner},'CONCURRENT_GRANT','repeat-activate')")[0]),range(16)))
    if any(item['state']!='already_activated' for item in repeated):raise AssertionError('Grant replay reset activation or credited twice')
    free={**campaign,'benefit_kind':'free_checks','benefit_value':1,'max_checks':2,'max_discount_minor':unit*2,'max_total_discount_minor':unit*2}
    sql(database,statement=f"select commerce_rpc.create_platform_campaign(895900,'concurrent-free','CONCURRENT_FREE','{json.dumps(free)}','fixture-review')")
    sql(database,statement="select commerce_rpc.activate_campaign(895100,'CONCURRENT_FREE','fixture-activate')")
    def free_quote(index):
        return json.loads(sql(database,statement=f"""select commerce_rpc.create_order(895100,'concurrent-free-order-{index}',
            '[{{"row_number":2,"check_type":"account"}}]',jsonb_build_object('file_sha256',repeat('7',64)))""")[0])
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:quotes=list(executor.map(free_quote,range(16)))
    if sum(item['quoted_minor']==0 for item in quotes)!=2 or any(item['quoted_minor'] not in (0,unit) for item in quotes):
        raise AssertionError('Concurrent quote reserved more free checks than allowed')
    for quote in quotes:
        if quote['quoted_minor']==0:sql(database,statement=f"select commerce_rpc.cancel_campaign_checkout('{quote['order_id']}',895100)")
    refreshed=free_quote(17)
    if refreshed['quoted_minor']!=0:raise AssertionError('Cancelled quote failed to restore promotional quota')
    shared_free={**free,'max_checks':1,'max_discount_minor':unit}
    sql(database,statement=f"select commerce_rpc.create_platform_campaign(895900,'concurrent-shared-free','CONCURRENT_SHARED_FREE','{json.dumps(shared_free)}','fixture-review')")
    for index in range(16):sql(database,statement=f"select commerce_rpc.activate_campaign({895200+index},'CONCURRENT_SHARED_FREE','fixture-activate')")
    def shared_quote(index):
        return json.loads(sql(database,statement=f"""select commerce_rpc.create_order({895200+index},'concurrent-shared-free-{index}',
            '[{{"row_number":2,"check_type":"account"}}]',jsonb_build_object('file_sha256',repeat('6',64)))""")[0])
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:shared_quotes=list(executor.map(shared_quote,range(16)))
    if sum(item['quoted_minor']==0 for item in shared_quotes)!=2 or any(item['quoted_minor'] not in (0,unit) for item in shared_quotes):
        raise AssertionError('Quotes from different customers exceeded the shared campaign budget')
    evidence['checks'].append({'test':'16 competing grants, 16 grant replays, 16 competing free quotes and cancellation reuse',
        'passed':True,'global_grants':3,'classified_promotional_minor':9000,'free_quotes':2,'released_quota_reused':True,
        'cross_customer_shared_budget_free_quotes':2})
    for role in ['anon', 'authenticated']:
        try:
            sql(database, statement=f"set role {role}; select * from commerce.users")
        except RuntimeError as exc:
            if 'permission denied' not in str(exc):
                raise
        else:
            raise AssertionError(f'{role} read private users')
    backend = sql(database, statement="set role service_role; select commerce_rpc.register_telegram_user(880003,null)->>'telegram_user_id'")[0]
    if backend.splitlines()[-1] != '880003':
        raise AssertionError('Backend RPC registration failed')
    evidence['checks'].append({'test': 'actual client role denial and service role RPC', 'passed': True})
    evidence_path = ROOT / 'docs' / 'telegram_commerce' / f'acceptance_{database}.json'
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'passed': True, 'evidence': str(evidence_path), 'database': database}))


if __name__ == '__main__':
    main()
