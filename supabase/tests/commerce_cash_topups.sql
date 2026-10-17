\set ON_ERROR_STOP on
begin;
do $$ declare r jsonb; p uuid; u uuid;
begin
    r:=commerce_rpc.admin_cash_topup(890001,3001,'cash-receipt-001',890900,'telegram:review:1');
    p:=(r->>'payment_id')::uuid;
    if (commerce_rpc.admin_cash_topup(890001,3001,'CASH-RECEIPT-001',890901,'telegram:review:2')->>'payment_id')::uuid<>p then
        raise exception 'Cash receipt case or review replay credited twice'; end if;
    if (commerce_rpc.wallet_state(890001)->'customer'->>'available_minor')::numeric<>3001
       or (commerce_rpc.wallet_state(890001)->'partner'->>'available_minor')::numeric<>0 then
        raise exception 'Cash topup changed the wrong wallet or amount'; end if;
    select id into u from commerce.users where telegram_user_id=890001;
    if (select first_paid_service_at from commerce.users where id=u) is not null then
        raise exception 'Cash topup counted as first paid service'; end if;
    if (select fx_snapshot->>'actor_telegram_id' from commerce.payments where id=p)<>'890900' then
        raise exception 'Cash receipt first approval audit was replaced'; end if;
    begin
        perform commerce_rpc.admin_cash_topup(890002,3001,'CASH-RECEIPT-001',890900,'telegram:review:3');
        raise exception 'Cash receipt reassigned to another customer';
    exception when invalid_parameter_value then null; end;
    begin
        perform commerce_rpc.admin_cash_topup(890001,3002,'CASH-RECEIPT-001',890900,'telegram:review:4');
        raise exception 'Cash receipt amount changed';
    exception when invalid_parameter_value then null; end;
    begin
        perform commerce_rpc.admin_cash_topup(890001,3001,'NEW-RECEIPT',null,'telegram:review:5');
        raise exception 'Cash receipt accepted without an approval actor';
    exception when invalid_parameter_value then null; end;
    if has_function_privilege('anon','commerce_rpc.admin_cash_topup(bigint,numeric,text,bigint,text)','EXECUTE')
       or has_function_privilege('authenticated','commerce_rpc.admin_cash_topup(bigint,numeric,text,bigint,text)','EXECUTE') then
        raise exception 'Customer could call cash funding RPC'; end if;
end $$;
rollback;
