-- Synthetic cash cancellation fixtures. All changes roll back.
begin;
do $$
declare c uuid;exhausted uuid;unused uuid;partner_code uuid;result jsonb;original jsonb;
    before_journals bigint;before_payments bigint;before_receipts bigint;
begin
    perform commerce_rpc.configure_support_actors('[9,21]'::jsonb,'cash-cancel-admins');
    c:=(commerce_rpc.issue_cash_credit_code('CANCEL_PARTIAL',3000,6,18000,'CANCEL-PARTIAL',9,'issue')->>'code_id')::uuid;
    assert commerce_rpc.apply_customer_code(898001,'CANCEL_PARTIAL','redeem-first')->>'state'='redeemed';
    result:=commerce_rpc.cash_credit_code_record(9,c);
    assert result->>'redemptions'='1' and result->>'cash_redeemed_minor'='3000';
    assert (select active from commerce.codes where id=c),'Reading cancellation review mutated code';
    begin
        perform commerce_rpc.cancel_cash_credit_code(31,c,'forged');
        raise exception 'Unauthorized cash cancellation accepted';
    exception when insufficient_privilege then null;end;
    begin
        perform commerce_rpc.cash_credit_code_record(31,c);
        raise exception 'Unauthorized cash-code details accepted';
    exception when insufficient_privilege then null;end;
    begin
        perform commerce_rpc.cancel_cash_credit_code(9,c,' ');
        raise exception 'Missing cancellation receipt accepted';
    exception when invalid_parameter_value then null;end;
    before_journals:=(select count(*) from commerce.journals);
    before_payments:=(select count(*) from commerce.payments);
    before_receipts:=(select count(*) from commerce.cash_receipts);
    result:=commerce_rpc.cancel_cash_credit_code(9,c,'confirmed-cancellation');
    assert result->>'state'='cancelled' and result->>'unredeemed_minor'='15000';
    assert (select not active from commerce.codes where id=c);
    select to_jsonb(record) into original from commerce.codes record where id=c;
    assert commerce_rpc.cancel_cash_credit_code(21,c,'another-callback')->>'state'='already_cancelled';
    assert (select to_jsonb(record)=original from commerce.codes record where id=c),'Replay rewrote original cancellation';
    assert (select cash_cancelled_by_telegram_id=9 and cash_cancellation_reference='confirmed-cancellation' from commerce.codes where id=c);
    assert commerce_rpc.apply_customer_code(898002,'CANCEL_PARTIAL','after-cancel')->>'state'='inactive_code';
    assert commerce_rpc.apply_customer_code(898001,'CANCEL_PARTIAL','replay-redeem')->>'state'='already_redeemed';
    assert (commerce_rpc.wallet_state(898001)->'customer'->>'available_minor')::numeric=3000;
    assert (commerce_rpc.wallet_state(898002)->'customer'->>'available_minor')::numeric=0;
    assert (select cash_funded_minor=18000 and cash_redeemed_minor=3000 from commerce.codes where id=c);
    assert (select sum(delta_minor) from commerce.ledger_entries where account_key='cash-code:'||c||':unredeemed')=15000;
    assert (select count(*) from commerce.journals)=before_journals;
    assert (select count(*) from commerce.payments)=before_payments;
    assert (select count(*) from commerce.cash_receipts)=before_receipts;
    assert commerce_rpc.issue_cash_credit_code('CANCEL_PARTIAL',3000,6,18000,'CANCEL-PARTIAL',9,'retry-issue')->>'replayed'='true';
    assert (select not active from commerce.codes where id=c),'Issuance replay reactivated cancelled code';
    begin
        perform commerce_rpc.admin_cash_topup(898002,18000,'CANCEL-PARTIAL',9,'reuse');
        raise exception 'Cancelled receipt was reused';
    exception when invalid_parameter_value then null;end;
    exhausted:=(commerce_rpc.issue_cash_credit_code('CANCEL_USED',5000,1,5000,'CANCEL-USED',9,'issue')->>'code_id')::uuid;
    perform commerce_rpc.apply_customer_code(898003,'CANCEL_USED','use-once');
    result:=commerce_rpc.cancel_cash_credit_code(9,exhausted,'cancel-used');
    assert result->>'state'='cancelled' and result->>'unredeemed_minor'='0';
    assert (commerce_rpc.wallet_state(898003)->'customer'->>'available_minor')::numeric=5000;
    unused:=(commerce_rpc.issue_cash_credit_code('CANCEL_TINY',0.001,3,0.003,'CANCEL-TINY',9,'issue')->>'code_id')::uuid;
    update commerce.codes set expires_at=now()-interval '1 second' where id=unused;
    result:=commerce_rpc.cancel_cash_credit_code(9,unused,'cancel-expired');
    assert (result->>'unredeemed_minor')::numeric=0.003,'Cancellation rounded funded cash';
    perform commerce_rpc.create_partner(898004,'CANCEL_REFERRAL');
    select id into partner_code from commerce.codes where code='CANCEL_REFERRAL';
    assert commerce_rpc.cash_credit_code_record(9,partner_code) is null;
    assert commerce_rpc.cancel_cash_credit_code(9,partner_code,'wrong-kind')->>'state'='invalid_code';
    assert commerce_rpc.cancel_cash_credit_code(9,gen_random_uuid(),'missing')->>'state'='invalid_code';
    assert has_function_privilege('service_role','commerce_rpc.cancel_cash_credit_code(bigint,uuid,text)','EXECUTE');
    assert not has_function_privilege('anon','commerce_rpc.cancel_cash_credit_code(bigint,uuid,text)','EXECUTE');
    assert not has_function_privilege('authenticated','commerce_rpc.cash_credit_code_record(bigint,uuid)','EXECUTE');
    raise notice 'PASS: authorized, idempotent cancellation preserves funded cash, wallets, receipts and exact decimals';
end $$;
rollback;
