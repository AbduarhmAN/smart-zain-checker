\set ON_ERROR_STOP on
begin;
do $$
declare a uuid;b uuid;ord uuid;balance jsonb;answer jsonb;policy bigint;rules jsonb;lot_total bigint;expired_before bigint;
begin
    perform commerce_rpc.configure_campaign_policy(898900,'{"max_percent_basis_points":10000,"max_checks_per_user":4000,"max_discount_minor_per_user":100000,"max_total_discount_minor":1000000,"max_duration_seconds":86400,"max_wallet_credit_minor":100000,"max_redemptions":10}','isolated expiry policy');
    rules:='{"benefit_kind":"wallet_credit","benefit_value":100,"eligibility":"any","check_types":["account","service"],"max_checks":1,"max_discount_minor":100,"max_total_discount_minor":1000,"duration_seconds":3600,"max_redemptions":10}';
    a:=(commerce_rpc.create_platform_campaign(898900,'expire-grant-a','EXPIRE_A',rules,'isolated review')->>'campaign_id')::uuid;
    b:=(commerce_rpc.create_platform_campaign(898900,'expire-grant-b','EXPIRE_B',rules||'{"benefit_value":200,"max_discount_minor":200,"duration_seconds":7200}','isolated review')->>'campaign_id')::uuid;
    perform commerce_rpc.activate_campaign(898001,'EXPIRE_A','customer-grant-a');
    perform commerce_rpc.activate_campaign(898001,'EXPIRE_B','customer-grant-b');
    perform commerce_rpc.confirm_receipt(898001,'expiry-paid-funding','fixture','expiry-paid-funding',1000,'fixture-proof');
    update commerce.promotional_credit_lots set expires_at=now()-interval '1 second' where campaign_id=a;
    balance:=commerce_rpc.wallet_state(898001);
    if (balance->'customer'->>'available_minor')::numeric<>1200 or (balance->'customer'->>'promotional_available_minor')::numeric<>200 then
        raise exception 'Expiry removed cash or unexpired promotional credit';end if;
    perform commerce_rpc.wallet_state(898001);perform commerce_rpc.expire_promotional_credit();
    perform commerce_rpc.activate_campaign(898001,'EXPIRE_A','repeat-after-expiry');
    if (commerce_rpc.wallet_state(898001)->'customer'->>'available_minor')::numeric<>1200
       or (select expired_minor from commerce.promotional_credit_lots where campaign_id=a)<>100 then
        raise exception 'Expiry or activation replay recreated or repeatedly removed money';end if;
    ord:=(commerce_rpc.create_order(898001,'expiry-held-order','[{"row_number":2,"check_type":"account"},{"row_number":3,"check_type":"account"}]',
        jsonb_build_object('file_sha256',repeat('b',64)))->>'order_id')::uuid;
    select (pricing_snapshot->>'policy_version')::bigint into policy from commerce.orders where id=ord;
    perform commerce_rpc.checkout_consent(ord,898001,policy,'expiry-refund-consent');
    perform commerce_rpc.reserve_order(ord,898001,'expiry-refund-consent');
    perform commerce_rpc.reserve_order(ord,898001,'expiry-refund-consent');
    update commerce.promotional_credit_lots set expires_at=now()-interval '1 second' where campaign_id=b;
    balance:=commerce_rpc.wallet_state(898001);
    if (balance->'customer'->>'available_minor')::numeric<>1000
       or (balance->'customer'->>'reserved_minor')::numeric<>2
       or (balance->'customer'->>'promotional_reserved_minor')::numeric<>2 then
        raise exception 'Expiry touched a protected held request';end if;
    perform commerce_rpc.bind_checker_job(ord,'expiry-job');
    perform commerce_rpc.record_terminal_results(ord,'expiry-job','[{"row_number":2,"state":"succeeded","verified_result":true},{"row_number":3,"state":"failed","verified_result":false}]',repeat('b',64));
    begin perform commerce_rpc.settle_order(ord);raise exception 'Settled before result delivery';
    exception when invalid_parameter_value then null;end;
    perform commerce_rpc.confirm_result_delivery(ord,898001,123,'expiry-fixture-file',repeat('b',64));
    answer:=commerce_rpc.settle_order(ord);
    if (answer->>'charged_minor')::numeric<>1 or (answer->>'refunded_minor')::numeric<>1 or (answer->>'collected_minor')::numeric<>0 then
        raise exception 'Promotional terminal settlement misclassified cash';end if;
    balance:=commerce_rpc.wallet_state(898001);
    if (balance->'customer'->>'available_minor')::numeric<>1000
       or (balance->'customer'->>'reserved_minor')::numeric<>0
       or (balance->'customer'->>'promotional_available_minor')::numeric<>0
       or (select consumed_minor from commerce.promotional_credit_lots where campaign_id=b)<>1
       or (select expired_minor from commerce.promotional_credit_lots where campaign_id=b)<>199 then
        raise exception 'Expired promotional refund became durable spending money';end if;
    perform commerce_rpc.settle_order(ord);
    if (commerce_rpc.wallet_state(898001)->'customer'->>'available_minor')::numeric<>1000 then
        raise exception 'Settlement replay duplicated expired refunds';end if;
    if exists(select 1 from commerce.users where telegram_user_id=898001 and first_paid_service_at is not null) then
        raise exception 'Promotional-only usage manufactured a first paid service';end if;
    if exists(select 1 from commerce.promotional_credit_lots where granted_minor::numeric<>available_minor::numeric+reserved_minor+consumed_minor+expired_minor) then
        raise exception 'Credit conservation failed';end if;
end $$;
rollback;
