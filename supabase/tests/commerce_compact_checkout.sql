\set ON_ERROR_STOP on
begin;
do $$
declare rows jsonb;source jsonb;ord uuid;replayed uuid;state jsonb;answer jsonb;policy bigint;payment uuid;
    camp uuid;rules jsonb;uid uuid;partner uuid;price bigint;quote numeric;heap_before bigint;
begin
    rows:=(select jsonb_agg(jsonb_build_object('row_number',n,'check_type',case when n<=2500 then 'account' else 'service' end))
        from generate_series(1,3000)n);
    source:=jsonb_build_object('file_sha256',repeat('1',64),'sheet_index',0,'storage_version',2);
    ord:=(commerce_rpc.create_order(990001,'compact-3000',rows,source)->>'order_id')::uuid;
    state:=commerce_rpc.checkout_state(ord,990001);
    if state->>'account_count'<>'2500' or state->>'service_count'<>'500' or state->>'checks_materialized'<>'false'
        or (select count(*) from commerce.checks where order_id=ord)<>0
        or state->'pricing_snapshot' ? 'row_unit_minor' then
        raise exception 'Unpaid draft stored row details or lost check counts';end if;
    replayed:=(commerce_rpc.create_order(990001,'compact-3000-new-upload',rows,
        source||jsonb_build_object('original_filename','another-name.xlsx','original_telegram_file_id','another-upload'))->>'order_id')::uuid;
    if replayed<>ord then raise exception 'Identical unpaid input was duplicated';end if;
    policy:=(state->'pricing_snapshot'->>'policy_version')::bigint;
    perform commerce_rpc.checkout_consent(ord,990001,policy,'compact-consent');
    heap_before:=pg_relation_size('commerce.checks');
    answer:=commerce_rpc.reserve_order_with_rows(ord,990001,'compact-consent',rows);
    if answer->>'state'<>'insufficient_balance' or exists(select 1 from commerce.checks where order_id=ord)
        or (select checks_materialized from commerce.orders where id=ord) then
        raise exception 'Insufficient funds persisted check details';end if;
    if pg_relation_size('commerce.checks')<>heap_before then
        raise exception 'Insufficient balance allocated unnecessary heap pages';end if;
    begin
        perform commerce_rpc.reserve_order(ord,990001,'compact-consent');
        raise exception 'Legacy reservation bypassed manifest';
    exception when invalid_parameter_value then null;end;
    begin
        perform commerce_rpc.reserve_order_with_rows(ord,990002,'compact-consent',rows);
        raise exception 'Wrong owner reserved compact order';
    exception when invalid_parameter_value then null;end;
    begin
        perform commerce_rpc.reserve_order_with_rows(ord,990001,'compact-consent',
            jsonb_set(rows,'{0,check_type}','"service"'));
        raise exception 'Modified manifest was accepted';
    exception when invalid_parameter_value then null;end;
    perform commerce_rpc.confirm_receipt(990001,'compact-funds','fixture','compact-funds',4000,'isolated-proof');
    begin
        perform commerce_rpc.reserve_order_with_rows(ord,990001,'wrong-consent',rows);
        raise exception 'Wrong consent was accepted';
    exception when invalid_parameter_value then null;end;
    if exists(select 1 from commerce.checks where order_id=ord) then raise exception 'Failed consent persisted rows';end if;
    perform commerce_rpc.reserve_order_with_rows(ord,990001,'compact-consent',rows);
    perform commerce_rpc.reserve_order_with_rows(ord,990001,'compact-consent',rows);
    if (select count(*) from commerce.checks where order_id=ord)<>3000
        or (commerce_rpc.wallet_state(990001)->'customer'->>'reserved_minor')::numeric<>3000 then
        raise exception 'Funded manifest was incomplete or charged twice';end if;
    if commerce.customer_order_view(ord)->>'account_count'<>'2500' then raise exception 'History lost account count';end if;
    -- A verified payment allocated directly to an order uses the same atomic
    -- manifest boundary as wallet funding, without crediting a customer twice.
    ord:=(commerce_rpc.create_order(990008,'compact-direct-payment','[{"row_number":2,"check_type":"account"},{"row_number":4,"check_type":"service"}]',source)->>'order_id')::uuid;
    state:=commerce_rpc.checkout_state(ord,990008);
    perform commerce_rpc.checkout_consent(ord,990008,(state->'pricing_snapshot'->>'policy_version')::bigint,'compact-direct-consent');
    payment:=(commerce_rpc.confirm_receipt(990008,'compact-direct-receipt','fixture','compact-direct-receipt',2,'isolated-proof',
        p_purpose=>'direct_order',p_order_id=>ord)->>'payment_id')::uuid;
    perform commerce_rpc.reserve_order_with_rows(ord,990008,'compact-direct-consent',
        '[{"row_number":2,"check_type":"account"},{"row_number":4,"check_type":"service"}]',payment);
    if (select count(*) from commerce.checks where order_id=ord)<>2
        or (select payment_id from commerce.reservations where order_id=ord) is distinct from payment then
        raise exception 'Direct payment did not reserve and materialize exactly once';end if;
    -- V1 callers retain their issued behavior and can fund without a new manifest.
    replayed:=(commerce_rpc.create_order(990003,'legacy-preserved','[{"row_number":2,"check_type":"service"}]',
        jsonb_build_object('file_sha256',repeat('2',64)))->>'order_id')::uuid;
    if (select count(*) from commerce.checks where order_id=replayed)<>1 then raise exception 'Legacy quote changed';end if;

    perform commerce_rpc.configure_support_actors('[990900]'::jsonb,'compact-local-admin');
    perform commerce_rpc.configure_campaign_policy(990900,
        '{"max_percent_basis_points":10000,"max_checks_per_user":30000,"max_discount_minor_per_user":100000,"max_total_discount_minor":1000000,"max_duration_seconds":86400,"max_wallet_credit_minor":100000,"max_redemptions":20}',
        'compact-local-policy');
    -- Service pricing is rebuilt from the frozen quote, not today's settings.
    perform commerce_rpc.configure_customer_pricing(990900,990006,
        '{"base_minor":2,"service_excess_minor":7,"service_threshold":500}','compact-tier-pricing');
    rows:=(select jsonb_agg(jsonb_build_object('row_number',n,'check_type',case when n<=2 then 'account' else 'service' end))
        from generate_series(1,504)n);
    ord:=(commerce_rpc.create_order(990006,'compact-tier',rows,source)->>'order_id')::uuid;
    if (select quoted_minor from commerce.orders where id=ord)<>1018 then raise exception 'Compact service tier quote changed';end if;
    perform commerce_rpc.configure_customer_pricing(990900,990006,
        '{"base_minor":9,"service_excess_minor":9,"service_threshold":500}','compact-tier-later-pricing');
    perform commerce_rpc.confirm_receipt(990006,'compact-tier-funds','fixture','compact-tier-funds',2000,'isolated-proof');
    state:=commerce_rpc.checkout_state(ord,990006);
    perform commerce_rpc.checkout_consent(ord,990006,(state->'pricing_snapshot'->>'policy_version')::bigint,'compact-tier-consent');
    perform commerce_rpc.reserve_order_with_rows(ord,990006,'compact-tier-consent',rows);
    if (select count(*) from commerce.checks where order_id=ord and unit_quote_minor=7)<>2
        or (select sum(unit_quote_minor) from commerce.checks where order_id=ord)<>1018 then
        raise exception 'Funding repriced the frozen service tiers';end if;
    rows:=(select jsonb_agg(jsonb_build_object('row_number',n,'check_type',case when n<=2500 then 'account' else 'service' end))
        from generate_series(1,3000)n);
    rules:='{"benefit_kind":"free_checks","benefit_value":1,"eligibility":"any","check_types":["account","service"],"max_checks":3,"max_discount_minor":10000,"max_total_discount_minor":100000,"duration_seconds":3600,"max_redemptions":20}';
    camp:=(commerce_rpc.create_platform_campaign(990900,'compact-free','COMPACT_FREE',rules,'compact-free-review')->>'campaign_id')::uuid;
    perform commerce_rpc.activate_campaign(990004,'COMPACT_FREE','compact-free-activate');
    ord:=(commerce_rpc.create_order(990004,'compact-free-3000',rows,source)->>'order_id')::uuid;
    uid:=(select id from commerce.users where telegram_user_id=990004);
    if (select quoted_minor from commerce.orders where id=ord)<>0
        or exists(select 1 from commerce.checks where order_id=ord)
        or exists(select 1 from commerce.order_discounts where order_id=ord)
        or (select count(*) from commerce.order_discount_plans where order_id=ord)<>1
        or (select reserved_checks from commerce.campaign_usage where campaign_id=camp and user_id=uid)<>1 then
        raise exception 'Free quote expanded rows or lost run reservation';end if;
    perform commerce_rpc.cancel_campaign_checkout(ord,990004);
    perform commerce_rpc.cancel_campaign_checkout(ord,990004);
    if (select reserved_checks from commerce.campaign_usage where campaign_id=camp and user_id=uid)<>0
        or exists(select 1 from commerce.checks where order_id=ord) then
        raise exception 'Unpaid cancellation leaked quota or created rows';end if;
    rows:=(select jsonb_agg(jsonb_build_object('row_number',n,'check_type',case when n<=2500 then 'account' else 'service' end))
        from generate_series(1,3100)n);
    ord:=(commerce_rpc.create_order(990004,'compact-free-excess',rows,source)->>'order_id')::uuid;
    if (select quoted_minor from commerce.orders where id=ord)<>100
        or (select reserved_discount_minor from commerce.order_discount_plans where order_id=ord)<>3000 then
        raise exception 'Only the first 500 eligible services should be free';end if;
    perform commerce_rpc.cancel_campaign_checkout(ord,990004);
    rows:=(select jsonb_agg(jsonb_build_object('row_number',n,'check_type','account')) from generate_series(1,3401)n);
    ord:=(commerce_rpc.create_order(990004,'compact-free-total-limit',rows,source)->>'order_id')::uuid;
    if (select pricing_snapshot->>'free_checks_disqualified_reason' from commerce.orders where id=ord)<>'exceeds_total_limit'
        or exists(select 1 from commerce.order_discount_plans where order_id=ord) then
        raise exception 'Free-offer total limit changed';end if;
    perform commerce_rpc.cancel_campaign_checkout(ord,990004);
    rows:=(select jsonb_agg(jsonb_build_object('row_number',n,'check_type','service')) from generate_series(1,1501)n);
    ord:=(commerce_rpc.create_order(990004,'compact-free-service-limit',rows,source)->>'order_id')::uuid;
    if (select pricing_snapshot->>'free_checks_disqualified_reason' from commerce.orders where id=ord)<>'exceeds_service_limit' then
        raise exception 'Free-offer service limit changed';end if;
    perform commerce_rpc.cancel_campaign_checkout(ord,990004);
    ord:=(commerce_rpc.create_order(990004,'compact-free-small','[{"row_number":2,"check_type":"account"},{"row_number":3,"check_type":"service"}]',source)->>'order_id')::uuid;
    state:=commerce_rpc.checkout_state(ord,990004);
    perform commerce_rpc.checkout_consent(ord,990004,(state->'pricing_snapshot'->>'policy_version')::bigint,'compact-free-consent');
    perform commerce_rpc.reserve_order_with_rows(ord,990004,'compact-free-consent','[{"row_number":2,"check_type":"account"},{"row_number":3,"check_type":"service"}]');
    if exists(select 1 from commerce.order_discounts where order_id=ord and funder_snapshot ? 'rules')
        or (select count(*) from commerce.order_discounts where order_id=ord)<>2 then
        raise exception 'New discount rows repeated offer rules';end if;
    perform commerce_rpc.bind_checker_job(ord,'compact-free-job');
    perform commerce_rpc.record_terminal_results(ord,'compact-free-job','[{"row_number":2,"state":"not_found","verified_result":true},{"row_number":3,"state":"failed","verified_result":false}]',repeat('c',64));
    perform commerce_rpc.confirm_result_delivery(ord,990004,47,'isolated-compact-file',repeat('c',64));
    perform commerce_rpc.settle_order(ord);
    perform commerce_rpc.settle_order(ord);
    if (select reserved_checks from commerce.campaign_usage where campaign_id=camp and user_id=uid)<>0
        or (select consumed_checks from commerce.campaign_usage where campaign_id=camp and user_id=uid)<>1 then
        raise exception 'Free offer settlement consumed rows instead of one run';end if;

    partner:=(commerce_rpc.create_partner(990005,'COMPACT_PARTNER')->>'partner_id')::uuid;
    perform commerce_rpc.configure_partner(partner,990900,3000,'forever',null,true);
    price:=(commerce_rpc.configure_partner_sheet_price(990900,13.37,'compact-partner-price')->>'version')::bigint;
    ord:=(commerce_rpc.create_order(990005,'compact-partner-quote','[{"row_number":2,"check_type":"account"},{"row_number":4,"check_type":"service"},{"row_number":6,"check_type":"service"}]',source)->>'order_id')::uuid;
    replayed:=(commerce_rpc.create_order(990005,'compact-partner-repeat','[{"row_number":2,"check_type":"account"},{"row_number":4,"check_type":"service"},{"row_number":6,"check_type":"service"}]',source)->>'order_id')::uuid;
    if replayed<>ord then raise exception 'Partner draft was duplicated';end if;
    perform commerce_rpc.decide_partner_sheet(990900,ord,true,price,'compact-partner-approval');
    if exists(select 1 from commerce.checks where order_id=ord) then raise exception 'Partner approval materialized an unpaid sheet';end if;
    perform commerce_rpc.confirm_receipt(990005,'compact-partner-funds','fixture','compact-partner-funds',20,'isolated-proof');
    state:=commerce_rpc.checkout_state(ord,990005);
    perform commerce_rpc.checkout_consent(ord,990005,(state->'pricing_snapshot'->>'policy_version')::bigint,'compact-partner-consent');
    perform commerce_rpc.reserve_order_with_rows(ord,990005,'compact-partner-consent','[{"row_number":2,"check_type":"account"},{"row_number":4,"check_type":"service"},{"row_number":6,"check_type":"service"}]');
    select sum(unit_quote_minor) into quote from commerce.checks where order_id=ord;
    if quote<>13.37 or (commerce_rpc.checkout_state(ord,990005)->>'service_count')::bigint<>2 then
        raise exception 'Approved fixed-price sheet allocation or counts changed';end if;
    -- Partner-funded settlement must resolve the one shared rules snapshot.
    perform commerce_rpc.configure_customer_pricing(990900,990007,
        '{"base_minor":100,"service_excess_minor":100,"service_threshold":500}','compact-partner-customer-price');
    rules:='{"benefit_kind":"percent","benefit_value":2000,"eligibility":"any","check_types":["account","service"],"max_checks":10,"max_discount_minor":1000,"max_total_discount_minor":10000,"duration_seconds":3600,"max_redemptions":20}';
    camp:=(commerce_rpc.create_partner_campaign(990005,'compact-partner-offer','COMPACT_SHARE',rules,'compact-partner-offer-review')->>'campaign_id')::uuid;
    perform commerce_rpc.activate_campaign(990007,'COMPACT_SHARE','compact-share-activation');
    rows:='[{"row_number":2,"check_type":"account"},{"row_number":4,"check_type":"service"}]';
    ord:=(commerce_rpc.create_order(990007,'compact-partner-funded',rows,source)->>'order_id')::uuid;
    if (select quoted_minor from commerce.orders where id=ord)<>200
        or (select reserved_discount_minor from commerce.order_discount_plans where order_id=ord)<>40 then
        raise exception 'Partner-funded quote or shared snapshot changed';end if;
    perform commerce_rpc.confirm_receipt(990007,'compact-share-funds','fixture','compact-share-funds',300,'isolated-proof');
    state:=commerce_rpc.checkout_state(ord,990007);
    perform commerce_rpc.checkout_consent(ord,990007,(state->'pricing_snapshot'->>'policy_version')::bigint,'compact-share-consent');
    perform commerce_rpc.reserve_order_with_rows(ord,990007,'compact-share-consent',rows);
    perform commerce_rpc.bind_checker_job(ord,'compact-share-job');
    perform commerce_rpc.record_terminal_results(ord,'compact-share-job','[{"row_number":2,"state":"succeeded","verified_result":true},{"row_number":4,"state":"failed","verified_result":false}]',repeat('d',64));
    perform commerce_rpc.confirm_result_delivery(ord,990007,48,'isolated-share-file',repeat('d',64));
    answer:=commerce_rpc.settle_order(ord);
    if (answer->>'charged_minor')::numeric<>80 or (answer->>'refunded_minor')::numeric<>120 then
        raise exception 'Shared partner rules changed settlement: %',answer;end if;
    if has_table_privilege('service_role','commerce.order_discount_plans','SELECT')
        or has_function_privilege('anon','commerce_rpc.reserve_order_with_rows(uuid,bigint,text,jsonb,uuid)','EXECUTE')
        or has_function_privilege('service_role','commerce.materialize_compact_checks(uuid,jsonb)','EXECUTE') then
        raise exception 'Compact storage/RPC privilege boundary weakened';end if;
end $$;
rollback;
