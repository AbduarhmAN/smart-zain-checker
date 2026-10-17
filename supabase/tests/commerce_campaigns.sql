\set ON_ERROR_STOP on
begin;
do $$ declare policy jsonb:='{"max_percent_basis_points":10000,"max_checks_per_user":50,"max_discount_minor_per_user":50000,"max_total_discount_minor":100000,"max_duration_seconds":2592000,"max_wallet_credit_minor":5000,"max_redemptions":100}';
    input jsonb:='{"benefit_kind":"percent","benefit_value":2000,"eligibility":"new","check_types":["account"],"max_checks":2,"max_discount_minor":10000,"max_total_discount_minor":50000,"duration_seconds":259200,"max_redemptions":10}';
    rows jsonb:='[{"row_number":2,"check_type":"account"},{"row_number":3,"check_type":"service"},{"row_number":4,"check_type":"account"},{"row_number":5,"check_type":"account"}]';
    source jsonb:=jsonb_build_object('file_sha256',repeat('a',64));a uuid;b uuid;c uuid;g uuid;v_order uuid;second_id uuid;p uuid;reply jsonb;expiry timestamptz;usage commerce.campaign_usage%rowtype;
begin
    begin
        perform commerce_rpc.create_platform_campaign(894900,'missing-policy','OFFER_MISSING',input,'fixture-review');
        raise exception 'Campaign ignored missing administration limits';
    exception when invalid_parameter_value then null; end;
    perform commerce_rpc.configure_campaign_policy(894900,policy,'fixture-policy');
    begin
        perform commerce_rpc.create_platform_campaign(894900,'excess-rate','OFFER_EXCESS',input||'{"benefit_value":10001}','fixture-review');
        raise exception 'Campaign ignored percentage limit';
    exception when invalid_parameter_value then null; end;
    a:=(commerce_rpc.create_platform_campaign(894900,'campaign-a','OFFER_A',input,'fixture-review')->>'campaign_id')::uuid;
    if (commerce_rpc.create_platform_campaign(894901,'campaign-a','OFFER_A',input,'repeat-review')->>'campaign_id')::uuid<>a then
        raise exception 'Campaign replay created another offer'; end if;
    if commerce_rpc.activate_campaign(894001,'OFFER_A','fixture-activate')->>'state'<>'activated' then raise exception 'Activation failed'; end if;
    select expires_at into expiry from commerce.campaign_usage where campaign_id=a;
    perform commerce_rpc.activate_campaign(894001,'OFFER_A','repeat-activate');
    if (select expires_at from commerce.campaign_usage where campaign_id=a)<>expiry
       or (select count(*) from commerce.code_redemptions where code_id=(select id from commerce.codes where campaign_id=a))<>1 then
        raise exception 'Activation reset duration or consumed code twice'; end if;
    p:=(commerce_rpc.create_partner(894100,'OFFER_REFERRAL')->>'partner_id')::uuid;
    perform commerce_rpc.configure_partner(p,894900,3000,'forever',null,true);
    perform commerce_rpc.attribute_referral(894001,'OFFER_REFERRAL','manual');
    perform commerce_rpc.confirm_receipt(894001,'campaign-topup','fixture','campaign-topup',30000,'fixture-proof');
    v_order:=(commerce_rpc.create_order(894001,'campaign-order',rows,source)->>'order_id')::uuid;
    if (select quoted_minor from commerce.orders where id=v_order)<>10800
       or (select discount_minor from commerce.checks where order_id=v_order and row_number=3)<>0
       or (select discount_minor from commerce.checks where order_id=v_order and row_number=5)<>0 then
        raise exception 'Types or partial check cap incorrect'; end if;
    second_id:=(commerce_rpc.create_order(894001,'campaign-held-order',rows,source)->>'order_id')::uuid;
    if (select quoted_minor from commerce.orders where id=second_id)<>12000 then raise exception 'Pending quote overspent offer'; end if;
    perform commerce_rpc.cancel_campaign_checkout(second_id,894001);
    perform commerce_rpc.reserve_order(v_order,894001,'fixture-consent');
    perform commerce_rpc.bind_checker_job(v_order,'campaign-job');
    perform commerce_rpc.record_terminal_results(v_order,'campaign-job',
        '[{"row_number":2,"state":"succeeded","verified_result":true},{"row_number":3,"state":"succeeded","verified_result":true},{"row_number":4,"state":"failed","verified_result":false},{"row_number":5,"state":"succeeded","verified_result":true}]',repeat('b',64));
    perform commerce_rpc.confirm_result_delivery(v_order,894001,10,'fixture-result',repeat('b',64));
    reply:=commerce_rpc.settle_order(v_order);perform commerce_rpc.settle_order(v_order);
    if (reply->>'charged_minor')::numeric<>8400 or (reply->>'refunded_minor')::numeric<>2400 or (reply->>'commission_minor')::numeric<>2520 then
        raise exception 'Discounted settlement, refund or commission basis incorrect'; end if;
    select * into usage from commerce.campaign_usage where campaign_id=a and user_id=(select id from commerce.users where telegram_user_id=894001);
    if usage.consumed_checks<>1 or usage.reserved_checks<>0 or usage.discount_minor<>600 or usage.reserved_discount_minor<>0 then
        raise exception 'Failed check did not release campaign quota or replay consumed twice'; end if;
    if (select partner_id from commerce.referrals where user_id=usage.user_id)<>p then raise exception 'Discount changed referral'; end if;
    second_id:=(commerce_rpc.create_order(894001,'campaign-remaining',rows,source)->>'order_id')::uuid;
    if (select quoted_minor from commerce.orders where id=second_id)<>11400 then raise exception 'Welcome activation did not retain remaining check after first paid service'; end if;
    perform commerce_rpc.cancel_campaign_checkout(second_id,894001);perform commerce_rpc.cancel_campaign_checkout(second_id,894001);
    if (select reserved_checks from commerce.campaign_usage where campaign_id=a and user_id=usage.user_id)<>0 then raise exception 'Cancel failed to release offer once'; end if;

    b:=(commerce_rpc.create_platform_campaign(894900,'campaign-b','OFFER_B',input||'{"benefit_kind":"fixed","benefit_value":1000,"eligibility":"any","max_checks":5,"check_types":["service"]}','fixture-review')->>'campaign_id')::uuid;
    perform commerce_rpc.activate_campaign(894001,'OFFER_B','fixture-activate');
    second_id:=(commerce_rpc.create_order(894001,'campaign-best',rows,source)->>'order_id')::uuid;
    if (select quoted_minor from commerce.orders where id=second_id)<>11000
       or (select count(distinct campaign_id) from commerce.order_discounts where order_id=second_id)<>1
       or (select campaign_id from commerce.order_discounts where order_id=second_id limit 1)<>b then
        raise exception 'Best eligible discount did not win without stacking'; end if;
    perform commerce_rpc.pause_campaign(b,894900,'pause-fixture');
    perform commerce_rpc.configure_campaign_policy(894900,policy||'{"max_percent_basis_points":1000}','strict-policy');
    if (commerce_rpc.create_order(894001,'campaign-best',rows,source)->>'quoted_minor')::numeric<>11000 then
        raise exception 'Policy or pause repriced an issued checkout'; end if;
    perform commerce_rpc.cancel_campaign_checkout(second_id,894001);
    perform commerce_rpc.configure_campaign_policy(894900,policy,'restore-fixture-policy');
    if commerce_rpc.activate_campaign(894002,'OFFER_B','fixture-activate')->>'state'<>'inactive_offer' then raise exception 'Paused offer activated'; end if;

    c:=(commerce_rpc.create_platform_campaign(894900,'campaign-free','OFFER_FREE',input||'{"benefit_kind":"free_checks","benefit_value":1,"eligibility":"any","max_checks":2,"max_discount_minor":6000,"check_types":["account","service"]}','fixture-review')->>'campaign_id')::uuid;
    perform commerce_rpc.activate_campaign(894003,'OFFER_FREE','fixture-activate');
    v_order:=(commerce_rpc.create_order(894003,'campaign-free-order','[{"row_number":2,"check_type":"account"},{"row_number":3,"check_type":"service"}]',source)->>'order_id')::uuid;
    if (select quoted_minor from commerce.orders where id=v_order)<>0 then raise exception 'Free check price nonzero'; end if;
    perform commerce_rpc.reserve_order(v_order,894003,'fixture-consent');
    perform commerce_rpc.bind_checker_job(v_order,'campaign-free-job');
    perform commerce_rpc.record_terminal_results(v_order,'campaign-free-job','[{"row_number":2,"state":"not_found","verified_result":true},{"row_number":3,"state":"failed","verified_result":false}]',repeat('c',64));
    perform commerce_rpc.confirm_result_delivery(v_order,894003,11,'fixture-free-result',repeat('c',64));
    reply:=commerce_rpc.settle_order(v_order);
    if (reply->>'charged_minor')::numeric<>0 or (reply->>'refunded_minor')::numeric<>0 or (reply->>'commission_minor')::numeric<>0
       or (select first_paid_service_at is not null or successful_paid_checks<>0 from commerce.users where telegram_user_id=894003)
       or (select consumed_checks from commerce.campaign_usage where campaign_id=c and user_id=(select id from commerce.users where telegram_user_id=894003))<>1 then
        raise exception 'Free or failed outcomes incorrectly started paid entitlement or consumed offer'; end if;

    g:=(commerce_rpc.create_platform_campaign(894900,'campaign-grant','OFFER_GRANT',input||'{"benefit_kind":"wallet_credit","benefit_value":3001,"eligibility":"any","max_discount_minor":4000,"max_total_discount_minor":4000,"max_checks":1}','fixture-review')->>'campaign_id')::uuid;
    perform commerce_rpc.activate_campaign(894004,'OFFER_GRANT','fixture-activate');perform commerce_rpc.activate_campaign(894004,'OFFER_GRANT','repeat-activate');
    if (commerce_rpc.wallet_state(894004)->'customer'->>'available_minor')::numeric<>3001
       or (select first_paid_service_at is not null from commerce.users where telegram_user_id=894004)
       or commerce_rpc.activate_campaign(894005,'OFFER_GRANT','fixture-activate')->>'state'<>'budget_exhausted' then
        raise exception 'Grant replay, paid-service distinction or global budget failed'; end if;
    if jsonb_array_length(commerce_rpc.customer_campaigns(894004))<>1 or jsonb_array_length(commerce_rpc.customer_campaigns(894005))<>0 then
        raise exception 'Customer campaigns missing or exposed'; end if;
    perform commerce_rpc.attribute_referral(894004,'OFFER_REFERRAL','manual');
    v_order:=(commerce_rpc.create_order(894004,'grant-funded-service','[{"row_number":2,"check_type":"account"}]',source)->>'order_id')::uuid;
    perform commerce_rpc.reserve_order(v_order,894004,'fixture-consent');
    if (commerce_rpc.wallet_state(894004)->'customer'->>'promotional_reserved_minor')::numeric<>3000 then raise exception 'Promotional money was not classified in the reserve'; end if;
    perform commerce_rpc.bind_checker_job(v_order,'grant-funded-job');
    perform commerce_rpc.record_terminal_results(v_order,'grant-funded-job','[{"row_number":2,"state":"succeeded","verified_result":true}]',repeat('d',64));
    perform commerce_rpc.confirm_result_delivery(v_order,894004,12,'fixture-grant-result',repeat('d',64));
    reply:=commerce_rpc.settle_order(v_order);perform commerce_rpc.settle_order(v_order);
    if (reply->>'charged_minor')::numeric<>3000 or (reply->>'collected_minor')::numeric<>0 or (reply->>'commission_minor')::numeric<>0
       or (select first_paid_service_at is not null or successful_paid_checks<>0 from commerce.users where telegram_user_id=894004)
       or (select locked_at is not null from commerce.referrals where user_id=(select id from commerce.users where telegram_user_id=894004)) then
        raise exception 'Promotional payment manufactured collected revenue, commission or first paid service'; end if;
    perform commerce_rpc.confirm_receipt(894004,'mixed-grant-fund','fixture','mixed-grant-fund',6000,'fixture-proof');
    v_order:=(commerce_rpc.create_order(894004,'mixed-grant-service','[{"row_number":2,"check_type":"account"},{"row_number":3,"check_type":"account"}]',source)->>'order_id')::uuid;
    perform commerce_rpc.reserve_order(v_order,894004,'fixture-consent');
    perform commerce_rpc.bind_checker_job(v_order,'mixed-grant-job');
    perform commerce_rpc.record_terminal_results(v_order,'mixed-grant-job','[{"row_number":2,"state":"succeeded","verified_result":true},{"row_number":3,"state":"failed","verified_result":false}]',repeat('e',64));
    perform commerce_rpc.confirm_result_delivery(v_order,894004,13,'fixture-mixed-result',repeat('e',64));
    reply:=commerce_rpc.settle_order(v_order);
    if (reply->>'collected_minor')::numeric<>2999 or (reply->>'commission_minor')::numeric<>899.7
       or (select successful_paid_checks from commerce.users where telegram_user_id=894004)<>1
       or (commerce_rpc.wallet_state(894004)->'customer'->>'available_minor')::numeric<>3001 then
        raise exception 'Mixed grant/payment commission basis or refund incorrect'; end if;
    g:=(commerce_rpc.create_platform_campaign(894900,'campaign-refund-grant','OFFER_REFUND_GRANT',input||'{"benefit_kind":"wallet_credit","benefit_value":3000,"eligibility":"any","max_checks":1}','fixture-review')->>'campaign_id')::uuid;
    perform commerce_rpc.activate_campaign(894004,'OFFER_REFUND_GRANT','fixture-activate');
    v_order:=(commerce_rpc.create_order(894004,'refund-grant-service','[{"row_number":2,"check_type":"account"}]',source)->>'order_id')::uuid;
    perform commerce_rpc.reserve_order(v_order,894004,'fixture-consent');
    perform commerce_rpc.bind_checker_job(v_order,'refund-grant-job');
    perform commerce_rpc.record_terminal_results(v_order,'refund-grant-job','[{"row_number":2,"state":"failed","verified_result":false}]',repeat('f',64));
    perform commerce_rpc.confirm_result_delivery(v_order,894004,14,'fixture-refund-grant-result',repeat('f',64));
    perform commerce_rpc.settle_order(v_order);perform commerce_rpc.settle_order(v_order);
    if (commerce_rpc.wallet_state(894004)->'customer'->>'promotional_available_minor')::numeric<>3000
       or (commerce_rpc.wallet_state(894004)->'customer'->>'promotional_reserved_minor')::numeric<>0
       or (commerce_rpc.wallet_state(894004)->'customer'->>'available_minor')::numeric<>6001 then
        raise exception 'Technical refund converted promotional credit into paid credit or replayed its value'; end if;
    update commerce.campaign_usage set expires_at=now()-interval '1 second' where campaign_id=c;
    v_order:=(commerce_rpc.create_order(894003,'campaign-expired','[{"row_number":2,"check_type":"account"}]',source)->>'order_id')::uuid;
    if (select quoted_minor from commerce.orders where id=v_order)<>3000 then raise exception 'Expired offer discounted a new quote'; end if;
    if commerce_rpc.activate_campaign(894001,'OFFER_A','late-fixture')->>'state'<>'already_activated'
       or commerce_rpc.activate_campaign(894004,'OFFER_A','late-fixture')->>'state'<>'ineligible_customer' then raise exception 'Activation eligibility incorrect'; end if;
    perform commerce_rpc.confirm_receipt(894006,'prior-fund','fixture','prior-fund',3000,'fixture-proof');
    update commerce.users set first_paid_service_at=now() where telegram_user_id=894006;
    if commerce_rpc.activate_campaign(894006,'OFFER_A','fixture-activate')->>'state'<>'ineligible_customer' then raise exception 'Prior paid client got new-client activation'; end if;
    g:=(commerce_rpc.create_platform_campaign(894900,'campaign-free-budget','OFFER_FREE_BUDGET',input||'{"benefit_kind":"free_checks","benefit_value":1,"eligibility":"any","max_checks":3,"max_discount_minor":4000,"check_types":["account","service"]}','fixture-review')->>'campaign_id')::uuid;
    perform commerce_rpc.activate_campaign(894007,'OFFER_FREE_BUDGET','fixture-activate');
    v_order:=(commerce_rpc.create_order(894007,'free-budget-order',rows,source)->>'order_id')::uuid;
    if (select quoted_minor from commerce.orders where id=v_order)<>9000
       or (select count(*) from commerce.checks where order_id=v_order and discount_minor=3000)<>1
       or exists(select 1 from commerce.checks where order_id=v_order and discount_minor between 1 and 2999) then
        raise exception 'Free offer produced a partially paid supposedly free check'; end if;
    if has_function_privilege('anon','commerce_rpc.activate_campaign(bigint,text,text)','EXECUTE')
       or has_function_privilege('authenticated','commerce_rpc.create_platform_campaign(bigint,text,text,jsonb,text)','EXECUTE')
       or has_function_privilege('service_role','commerce.create_order(bigint,text,jsonb,jsonb)','EXECUTE') then
        raise exception 'Campaign trust boundary exposed'; end if;
end $$;
rollback;
