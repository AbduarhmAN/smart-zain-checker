\set ON_ERROR_STOP on
begin;
do $$
declare uid uuid;pid uuid;wid uuid;ord uuid;j uuid;result jsonb;before_count bigint;
begin
    insert into commerce.support_actors(telegram_user_id,active,selection_reference) values(888034,true,'isolated finance admin');
    uid:=(commerce_rpc.register_telegram_user(888035,null)->>'user_id')::uuid;
    insert into commerce.partners(user_id) values(uid) returning id into pid;
    insert into commerce.codes(code,kind,partner_id) values('FINANCE_FIXTURE','referral',pid);
    select id into wid from commerce.wallets where user_id=uid and kind='partner';
    insert into commerce.orders(user_id,request_key,requested_checks,quoted_minor,pricing_snapshot)
        values(uid,'finance-fixture',1,10000,'{}') returning id into ord;
    insert into commerce.checks(order_id,row_number,check_type,unit_quote_minor) values(ord,2,'account',10000);
    perform commerce.post_pair('finance-service','service',ord,'wallet:'||wid||':reserved','platform:revenue',10000,wid,null);
    j:=commerce.post_pair('finance-commission','commission',ord,'platform:revenue','wallet:'||wid||':available',3000.125,null,wid);
    insert into commerce.commissions(order_id,row_number,partner_id,revenue_minor,amount_minor,terms_snapshot,state,journal_id)
        values(ord,2,pid,10000,3000.125,'{}','available',j);
    update commerce.wallets set available_minor=2500.125,reserved_minor=500 where id=wid;
    perform commerce.post_pair('finance-topup','topup',null,'external:fixture','wallet:'||wid||':available',1000000,null,wid);
    select count(*) into before_count from commerce.ledger_entries;
    result:=commerce_rpc.admin_earnings_summary(888034);
    if (result->>'gross_service_minor')::numeric<>10000 or (result->>'partner_commission_minor')::numeric<>3000.125
       or (result->>'platform_revenue_minor')::numeric<>6999.875 or (result->>'today_platform_revenue_minor')::numeric<>6999.875 then
        raise exception 'Earnings included deposits, lost decimals, or subtracted commissions incorrectly';end if;
    if (result->>'partners_total')::integer<>1 or (result->'partners'->0->>'available_minor')::numeric<>2500.125
       or (result->'partners'->0->>'reserved_minor')::numeric<>500
       or (result->'partners'->0->>'earned_minor')::numeric<>3000.125 then raise exception 'Partner earnings missing';end if;
    if (select count(*) from commerce.ledger_entries)<>before_count then raise exception 'Finance view changed accounting';end if;
    begin perform commerce_rpc.admin_earnings_summary(888035);raise exception 'Customer read admin earnings';
        exception when insufficient_privilege then null;end;
    update commerce.support_actors set active=false where telegram_user_id=888034;
    begin perform commerce_rpc.admin_earnings_summary(888034);raise exception 'Revoked admin read earnings';
        exception when insufficient_privilege then null;end;
    if has_function_privilege('anon','commerce_rpc.admin_earnings_summary(bigint,integer,integer)','EXECUTE')
       or has_function_privilege('authenticated','commerce_rpc.admin_earnings_summary(bigint,integer,integer)','EXECUTE') then
        raise exception 'Public finance access enabled';end if;
end $$;
rollback;
