\set ON_ERROR_STOP on
begin;
do $$
declare first_order uuid;second_order uuid;replayed uuid;quote jsonb;rows jsonb;old_total bigint;current_policy bigint;
begin
    insert into commerce.support_actors(telegram_user_id,active,selection_reference) values(899900,true,'isolated pricing fixture');
    quote:=commerce_rpc.customer_pricing(899001);
    if quote->>'currency'<>'EGP' or (quote->>'base_minor')::numeric<>1 or (quote->>'service_threshold')::integer<>500 then
        raise exception 'Human default pricing is not one piastre with a 500-service threshold';end if;
    first_order:=(commerce_rpc.create_order(899001,'pricing-first','[{"row_number":2,"check_type":"account"},{"row_number":3,"check_type":"service"}]',
        jsonb_build_object('file_sha256',repeat('1',64)))->>'order_id')::uuid;
    old_total:=(select quoted_minor from commerce.orders where id=first_order);
    if old_total<>2 then raise exception 'One-piastre per-customer quote wrong';end if;
    perform commerce_rpc.configure_customer_pricing(899900,0,'{"base_minor":1,"service_excess_minor":5,"service_threshold":500}','global-price-review');
    select jsonb_agg(jsonb_build_object('row_number',n,'check_type','service')) into rows from generate_series(2,503) n;
    second_order:=(commerce_rpc.create_order(899002,'pricing-tier',rows,jsonb_build_object('file_sha256',repeat('2',64)))->>'order_id')::uuid;
    if (select quoted_minor from commerce.orders where id=second_order)<>510
       or (select count(*) from commerce.checks where order_id=second_order and unit_quote_minor=5)<>2 then
        raise exception 'Only service 501 and later should use the excess price';end if;
    perform commerce_rpc.configure_customer_pricing(899900,899002,'{"base_minor":2,"service_excess_minor":7,"service_threshold":500}','customer-price-review');
    if (commerce_rpc.create_order(899002,'pricing-customer',rows,jsonb_build_object('file_sha256',repeat('3',64)))->>'quoted_minor')::numeric<>1014 then
        raise exception 'Customer-specific service tier not applied';end if;
    if (commerce_rpc.create_order(899002,'pricing-account','[{"row_number":2,"check_type":"account"},{"row_number":3,"check_type":"account"}]',
        jsonb_build_object('file_sha256',repeat('4',64)))->>'quoted_minor')::numeric<>4 then
        raise exception 'Account checks incorrectly used service tier';end if;
    replayed:=(commerce_rpc.create_order(899001,'pricing-first','[{"row_number":2,"check_type":"account"},{"row_number":3,"check_type":"service"}]',
        jsonb_build_object('file_sha256',repeat('1',64)))->>'order_id')::uuid;
    if replayed<>first_order or (select quoted_minor from commerce.orders where id=first_order)<>old_total then
        raise exception 'A frozen quote was repriced';end if;
    begin
        perform commerce_rpc.configure_customer_pricing(899003,0,'{"base_minor":1,"service_excess_minor":5,"service_threshold":500}','forged actor');
        raise exception 'Unauthorized pricing administrator accepted';
    exception when invalid_parameter_value then null;end;
    select jsonb_agg(jsonb_build_object('row_number',n,'check_type','account')) into rows from generate_series(2,4002) n;
    begin
        perform commerce_rpc.create_order(899004,'too-many-customers',rows,jsonb_build_object('file_sha256',repeat('5',64)));
        raise exception '4001-customer sheet accepted';
    exception when invalid_parameter_value then null;end;
    select jsonb_agg(jsonb_build_object('row_number',n,'check_type','account')) into rows from generate_series(2,4001) n;
    if (commerce_rpc.create_order(899004,'limit-customers',rows,jsonb_build_object('file_sha256',repeat('6',64)))->>'quoted_minor')::numeric<>4000 then
        raise exception '4000-customer boundary wrong';end if;
    perform commerce_rpc.configure_customer_pricing(899900,899002,'{"inherit_global":true}','reset-to-global-review');
    if (commerce_rpc.customer_pricing(899002)->>'base_minor')::numeric<>1 then raise exception 'Customer override reset wrong';end if;
    perform commerce_rpc.confirm_receipt(899001,'pricing-funding','fixture','pricing-funding',2,'fixture-proof');
    select (pricing_snapshot->>'policy_version')::bigint into current_policy from commerce.orders where id=first_order;
    perform commerce_rpc.checkout_consent(first_order,899001,current_policy,'fixture-explicit-wallet-refund-consent');
    perform commerce_rpc.reserve_order(first_order,899001,'fixture-explicit-wallet-refund-consent');
    perform commerce_rpc.reserve_order(first_order,899001,'fixture-explicit-wallet-refund-consent');
    if (commerce_rpc.wallet_state(899001)->'customer'->>'reserved_minor')::numeric<>2
       or (commerce_rpc.wallet_state(899001)->'customer'->>'available_minor')::numeric<>0 then
        raise exception 'Reservation was prematurely finalized or repeated';end if;
end $$;
rollback;
