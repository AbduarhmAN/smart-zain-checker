\set ON_ERROR_STOP on
begin;
do $$ declare result jsonb;first uuid;before_count bigint;balance bigint;ref_user uuid;
begin
    result:=commerce_rpc.issue_cash_credit_code('PAID_CASH',3001,2,6002,'CASH-CODE-001',897900,'fixture-review');
    first:=(result->>'code_id')::uuid;
    if (commerce_rpc.issue_cash_credit_code('paid_cash',3001,2,6002,'cash-code-001',897901,'another-review')->>'code_id')::uuid<>first then
        raise exception 'Cash code replay created a second liability'; end if;
    if (select actor_telegram_id from commerce.cash_receipts where reference='CASH-CODE-001')<>897900 then
        raise exception 'Issuance replay changed original reviewer'; end if;
    begin perform commerce_rpc.admin_cash_topup(897001,6002,'CASH-CODE-001',897900,'reuse');
        raise exception 'Issued-code receipt also funded direct wallet'; exception when invalid_parameter_value then null; end;
    begin perform commerce_rpc.issue_cash_credit_code('OTHER_CASH',3001,2,6002,'CASH-CODE-001',897900,'reuse');
        raise exception 'Receipt also funded another code'; exception when invalid_parameter_value then null; end;
    begin perform commerce_rpc.issue_cash_credit_code('UNFUNDED',3001,2,6001,'CASH-CODE-002',897900,'underfunded');
        raise exception 'Underfunded cash code was issued'; exception when invalid_parameter_value then null; end;
    begin perform commerce_rpc.issue_cash_credit_code('OVERFLOW',9223372036854775807,2,9223372036854775807,'CASH-CODE-003',897900,'overflow');
        raise exception 'Overflow funded amount accepted'; exception when invalid_parameter_value then null; end;
    result:=commerce_rpc.apply_customer_code(897001,'PAID_CASH','fixture-redemption');
    if result->>'kind'<>'cash_credit' or result->>'state'<>'redeemed' then raise exception 'Cash input did not redeem'; end if;
    before_count:=(select count(*) from commerce.journals);
    result:=commerce_rpc.apply_customer_code(897001,'PAID_CASH','second-message');
    if result->>'state'<>'already_redeemed' or (select count(*) from commerce.journals)<>before_count then
        raise exception 'Message replay duplicated cash credit'; end if;
    perform commerce_rpc.apply_customer_code(897002,'PAID_CASH','fixture-redemption');
    if commerce_rpc.apply_customer_code(897003,'PAID_CASH','fixture-redemption')->>'state'<>'code_exhausted' then
        raise exception 'Code exceeded funded redemptions'; end if;
    balance:=(commerce_rpc.wallet_state(897001)->'customer'->>'available_minor')::numeric;
    if balance<>3001 or (commerce_rpc.wallet_state(897001)->'customer'->>'promotional_available_minor')::numeric<>0
       or (commerce_rpc.wallet_state(897001)->'partner'->>'available_minor')::numeric<>0 then
        raise exception 'Cash credit was confused with a promotion or commission'; end if;
    if (select first_paid_service_at from commerce.users where telegram_user_id=897001) is not null then
        raise exception 'Code redemption counted as a paid service'; end if;
    if (select cash_redeemed_minor from commerce.codes where id=first)<>6002 then
        raise exception 'Cash code liability did not match its redemptions'; end if;
    if (select sum(delta_minor) from commerce.ledger_entries where account_key='cash-code:'||first||':unredeemed')<>0 then
        raise exception 'Funded cash pool was not conserved'; end if;
    perform commerce_rpc.admin_cash_topup(897004,3000,'DIRECT-CASH-001',897900,'fixture-receipt');
    begin perform commerce_rpc.issue_cash_credit_code('REUSED_DIRECT',3000,1,3000,'DIRECT-CASH-001',897900,'reuse');
        raise exception 'Direct receipt was issued as a code too'; exception when invalid_parameter_value then null; end;
    perform commerce_rpc.issue_cash_credit_code('BOUND_CASH',3000,1,3000,'BOUND-CASH-001',897900,'fixture-review',897005);
    if commerce_rpc.apply_customer_code(897006,'BOUND_CASH','wrong-owner')->>'state'<>'unowned_code' then
        raise exception 'Wrong owner redeemed bound cash code'; end if;
    if commerce_rpc.apply_customer_code(897005,'BOUND_CASH','right-owner')->>'state'<>'redeemed' then
        raise exception 'Bound owner could not redeem'; end if;
    perform commerce_rpc.issue_cash_credit_code('EXPIRED_CASH',3000,1,3000,'EXPIRING-CASH-001',897900,'fixture-review',null,now()+interval '1 day');
    update commerce.codes set expires_at=now()-interval '1 second' where code='EXPIRED_CASH';
    if commerce_rpc.apply_customer_code(897007,'EXPIRED_CASH','fixture-redemption')->>'state'<>'inactive_code' then
        raise exception 'Expired code redeemed'; end if;
    if (select sum(delta_minor) from commerce.ledger_entries where account_key='cash-code:'||
        (select id from commerce.codes where code='EXPIRED_CASH')||':unredeemed')<>3000 then
        raise exception 'Expired code lost unredeemed liability'; end if;
    if commerce_rpc.apply_customer_code(897001,'UNKNOWN','fixture-redemption')->>'state'<>'invalid_code' then
        raise exception 'Unknown code accepted'; end if;
    if exists(select 1 from commerce.journals j where (select sum(delta_minor::numeric) from commerce.ledger_entries where journal_id=j.id)<>0) then
        raise exception 'Cash code created an unbalanced journal'; end if;
    raise notice 'PASS: cash funding conservation, exact redemption, canonical receipt isolation, bound owner, expiry, overflow, replay and no premature commission';
end $$;
commit;
