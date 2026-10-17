\set ON_ERROR_STOP on
begin;
do $$
declare campaign uuid;ord uuid;rows jsonb;rules jsonb;quote jsonb;
begin
    perform commerce_rpc.configure_campaign_policy(887900,'{"max_percent_basis_points":10000,"max_checks_per_user":30000,"max_discount_minor_per_user":100000,"max_total_discount_minor":1000000,"max_duration_seconds":86400,"max_wallet_credit_minor":100000,"max_redemptions":10}','isolated one-piastre discount policy');
    rules:='{"benefit_kind":"percent","benefit_value":5000,"eligibility":"any","check_types":["account","service"],"max_checks":4000,"max_discount_minor":100000,"max_total_discount_minor":1000000,"duration_seconds":3600,"max_redemptions":10}';
    campaign:=(commerce_rpc.create_platform_campaign(887900,'half-price-cent','CENT_HALF',rules,'isolated review')->>'campaign_id')::uuid;
    perform commerce_rpc.activate_campaign(887001,'CENT_HALF','cent-half-activation');
    select jsonb_agg(jsonb_build_object('row_number',n,'check_type','account')) into rows from generate_series(2,4001) n;
    ord:=(commerce_rpc.create_order(887001,'cent-half-quote',rows,jsonb_build_object('file_sha256',repeat('f',64)))->>'order_id')::uuid;
    select pricing_snapshot into quote from commerce.orders where id=ord;
    if (quote->>'base_total_minor')::numeric<>4000 or (quote->>'discount_total_minor')::numeric<>2000
       or (quote->>'total_minor')::numeric<>2000 then
        raise exception 'Per-row rounding turned 50 percent of EGP 40 into a different discount';end if;
    if (select count(*) from commerce.order_discounts where order_id=ord)<>4000
       or (select reserved_checks from commerce.campaign_usage where user_id=(select id from commerce.users where telegram_user_id=887001) and campaign_id=campaign)<>4000 then
        raise exception 'Cent rounding did not preserve eligible usage limits';end if;
    perform commerce_rpc.cancel_campaign_checkout(ord,887001);
    if (select reserved_checks from commerce.campaign_usage where user_id=(select id from commerce.users where telegram_user_id=887001) and campaign_id=campaign)<>0 then
        raise exception 'Cancelled cent-priced quote leaked an offer reservation';end if;
end $$;
rollback;
