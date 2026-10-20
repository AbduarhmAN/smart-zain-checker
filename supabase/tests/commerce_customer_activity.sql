-- Synthetic, retained isolated test database only. Fixture changes roll back.
\set ON_ERROR_STOP on
begin;
do $$
declare uid uuid;ord uuid;camp uuid;cod uuid;wallet uuid;j uuid;page jsonb;detail jsonb;
    before_state jsonb;after_state jsonb;tg bigint;
begin
    perform commerce_rpc.configure_support_actors('[9]'::jsonb,'activity-test-admin');
    perform commerce_rpc.issue_cash_credit_code('ACTIVITY_CASH',1000,5,5000,'ACTIVITY-CASH',9,'issue');
    foreach tg in array array[991001,991002,991003,991004,9]::bigint[] loop
        perform commerce_rpc.apply_customer_code(tg,'ACTIVITY_CASH','activity-redeem-'||tg);
        select id into uid from commerce.users where telegram_user_id=tg;
        if tg=991004 then insert into commerce.partners(user_id) values(uid);end if;
        insert into commerce.orders(user_id,request_key,requested_checks,quoted_minor,pricing_snapshot,
            payment_state,execution_state,delivery_state,delivered_at,final_charge_minor,financial_completed_at)
            values(uid,'activity-order-'||tg,1,0,'{}','refunded','terminal','confirmed',now(),0,now()) returning id into ord;
        insert into commerce.checks(order_id,row_number,check_type,state,verified_result,unit_quote_minor,terminal_at)
            values(ord,1,'service','succeeded',true,0,now());
    end loop;
    select id into uid from commerce.users where telegram_user_id=991001;
    update commerce.users set telegram_username='activity_customer' where id=uid;
    insert into commerce.orders(user_id,request_key,requested_checks,quoted_minor,pricing_snapshot,
        payment_state,execution_state,delivery_state,delivered_at,final_charge_minor,financial_completed_at)
        values(uid,'activity-mixed',6,200,'{}','settled','terminal','confirmed',now(),200,now()) returning id into ord;
    insert into commerce.checks(order_id,row_number,check_type,state,verified_result,unit_quote_minor,
        discount_minor,promotional_payment_minor,terminal_at) values
        (ord,1,'account','succeeded',true,0,0,0,now()),
        (ord,2,'account','succeeded',true,100,100,0,now()),
        (ord,3,'service','not_found',true,100,0,100,now()),
        (ord,4,'service','succeeded',true,100,0,50,now()),
        (ord,5,'service','failed',false,0,0,0,now()),
        (ord,6,'service','cancelled',false,0,0,0,now());
    -- Unsettled zero-price drafts must not become evidence of free usage.
    insert into commerce.orders(user_id,request_key,requested_checks,quoted_minor,pricing_snapshot)
        values(uid,'activity-unpaid',1,0,'{}') returning id into ord;
    insert into commerce.checks(order_id,row_number,check_type,state,verified_result,unit_quote_minor)
        values(ord,1,'service','pending',false,0);
    -- Code activation without a wallet grant is not a top-up.
    insert into commerce.campaigns(name,benefit_kind,benefit_value,funder_kind,eligible_check_types,rules_version)
        values('activity offer','wallet_credit',250,'platform',array['account'],'activity-test') returning id into camp;
    insert into commerce.codes(code,kind,campaign_id) values('ACTIVITY_PROMO','discount',camp) returning id into cod;
    select id into wallet from commerce.wallets where user_id=uid and kind='customer';
    j:=commerce.post_pair('activity-promo-grant','adjustment',null,'platform:promotion','wallet:'||wallet||':available',250,null,wallet);
    insert into commerce.code_redemptions(code_id,user_id,idempotency_key,journal_id) values(cod,uid,'activity-promo',j);
    update commerce.wallets set available_minor=available_minor+250,promotional_available_minor=promotional_available_minor+250 where id=wallet;
    insert into commerce.codes(code,kind,campaign_id) values('ACTIVITY_NO_GRANT','discount',camp) returning id into cod;
    insert into commerce.code_redemptions(code_id,user_id,idempotency_key) values(cod,uid,'activity-no-grant');

    select jsonb_build_object('wallets',(select md5(string_agg(w::text,'' order by id)) from commerce.wallets w),
        'orders',(select md5(string_agg(o::text,'' order by id)) from commerce.orders o),
        'ledger',(select md5(string_agg(e::text,'' order by journal_id,line_number)) from commerce.ledger_entries e),
        'checks',(select md5(string_agg(c::text,'' order by order_id,row_number)) from commerce.checks c)) into before_state;
    page:=commerce_rpc.admin_customer_activity(9,'codes',0,6,null,array[991003]::bigint[]);
    assert page->>'customer_count'='2' and page->>'total'='2','Code report did not exclude administrator/partner';
    assert (page->>'cash_credit_minor')::numeric=2000 and (page->>'promotional_credit_minor')::numeric=250,'Code funding duplicated or omitted';
    detail:=commerce_rpc.admin_customer_activity(9,'codes',0,6,991001,array[991003]::bigint[]);
    assert detail->>'total'='2','Activation without a wallet credit counted as a top-up';
    assert detail->'items'->0->>'telegram_user_id'='991001','Wrong customer in details';
    page:=commerce_rpc.admin_customer_activity(9,'free',0,6,null,array[991003]::bigint[]);
    assert page->>'customer_count'='2','Free report role filtering failed';
    assert (page->>'free_account_checks')::numeric=2 and (page->>'free_service_checks')::numeric=3,
        'Failed, cancelled, partially cash funded or unpaid checks counted as free';
    detail:=commerce_rpc.admin_customer_activity(9,'free',0,6,991001,array[991003]::bigint[]);
    assert detail->>'total'='2' and (detail->>'free_account_checks')::numeric=2 and (detail->>'free_service_checks')::numeric=2,
        'Customer grouping duplicated mixed-order counts';
    assert (select sum((e->>'zero_price_checks')::bigint) from jsonb_array_elements(detail->'items')e)=2;
    assert (select sum((e->>'full_discount_checks')::bigint) from jsonb_array_elements(detail->'items')e)=1;
    assert (select sum((e->>'promotional_checks')::bigint) from jsonb_array_elements(detail->'items')e)=1;
    assert commerce_rpc.admin_customer_activity(9,'free',0,6,991004)->>'total'='0','Partner detail bypassed exclusion';
    assert commerce_rpc.admin_customer_activity(9,'free',0,6,9)->>'total'='0','Administrator detail bypassed exclusion';
    page:=commerce_rpc.admin_customer_activity(9,'free',0,1,null,array[991003]::bigint[]);
    detail:=commerce_rpc.admin_customer_activity(9,'free',1,1,null,array[991003]::bigint[]);
    assert jsonb_array_length(page->'items')=1 and page->'items'->0->>'telegram_user_id'<>detail->'items'->0->>'telegram_user_id','Pagination repeated a customer';
    assert page->>'free_service_checks'=detail->>'free_service_checks','Totals depend on the visible page';
    assert commerce_rpc.admin_customer_activity(9,'codes',600)->'items'='[]'::jsonb;
    begin perform commerce_rpc.admin_customer_activity(991001,'codes');raise exception 'Customer read admin report';
    exception when insufficient_privilege then null;end;
    begin perform commerce_rpc.admin_customer_activity(9,'invalid');raise exception 'Invalid report accepted';
    exception when invalid_parameter_value then null;end;
    begin perform commerce_rpc.admin_customer_activity(9,'codes',-1);raise exception 'Invalid page accepted';
    exception when invalid_parameter_value then null;end;
    begin perform commerce_rpc.admin_customer_activity(9,'codes',0,6,null,array[null]::bigint[]);raise exception 'Invalid exclusions accepted';
    exception when invalid_parameter_value then null;end;
    assert not has_function_privilege('anon','commerce_rpc.admin_customer_activity(bigint,text,integer,integer,bigint,bigint[])','execute');
    assert not has_function_privilege('authenticated','commerce_rpc.admin_customer_activity(bigint,text,integer,integer,bigint,bigint[])','execute');
    assert has_function_privilege('service_role','commerce_rpc.admin_customer_activity(bigint,text,integer,integer,bigint,bigint[])','execute');
    assert not has_function_privilege('service_role','commerce.customer_activity_events(text)','execute');
    select jsonb_build_object('wallets',(select md5(string_agg(w::text,'' order by id)) from commerce.wallets w),
        'orders',(select md5(string_agg(o::text,'' order by id)) from commerce.orders o),
        'ledger',(select md5(string_agg(e::text,'' order by journal_id,line_number)) from commerce.ledger_entries e),
        'checks',(select md5(string_agg(c::text,'' order by order_id,row_number)) from commerce.checks c)) into after_state;
    assert before_state=after_state,'Reading the reports changed financial or checker state';
    update commerce.support_actors set active=false where telegram_user_id=9;
    begin perform commerce_rpc.admin_customer_activity(9,'codes');raise exception 'Revoked admin read report';
    exception when insufficient_privilege then null;end;
end $$;
rollback;
