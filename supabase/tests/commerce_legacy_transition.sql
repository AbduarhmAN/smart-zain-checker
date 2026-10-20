\set ON_ERROR_STOP on
begin;
do $$
declare manifest jsonb;body text;digest text;response jsonb;again jsonb;ord uuid;other uuid;policy bigint;payments_before bigint;
begin
    select count(*) into payments_before from commerce.payments;
    insert into commerce.support_actors(telegram_user_id,active,selection_reference) values(886900,true,'isolated transition administrator');
    manifest:=jsonb_build_object('version',1,'wallets_sha256',repeat('a',64),'payments_sha256',repeat('b',64),'queue_sha256',repeat('c',64),
        'accounts',jsonb_build_array(jsonb_build_object('telegram_user_id',886001,'legacy_account_id',442426326,
        'available_minor',38524,'service_history_requires_review',false,'held_orders',jsonb_build_array(
            jsonb_build_object('token','abcdef012345','amount_minor',3000,'job_id','legacy-job-one','receipt_verified',true,'operation_id','261014-0004',
                'rows','[{"row_number":2,"check_type":"account"},{"row_number":3,"check_type":"service"},{"row_number":4,"check_type":"account"}]'::jsonb,
                'source',jsonb_build_object('file_sha256',repeat('d',64))),
            jsonb_build_object('token','012345abcdef','amount_minor',3000,'job_id','legacy-job-two','receipt_verified',true,'operation_id','261014-0003',
                'rows','[{"row_number":2,"check_type":"account"},{"row_number":3,"check_type":"account"}]'::jsonb,
                'source',jsonb_build_object('file_sha256',repeat('d',64)))))));
    body:=manifest::text;digest:=encode(sha256(convert_to(body,'UTF8')),'hex');
    begin perform commerce_rpc.import_legacy_wallets(886999,body,digest);raise exception 'Unauthorized transition accepted';
    exception when invalid_parameter_value then null;end;
    begin perform commerce_rpc.import_legacy_wallets(886900,body,repeat('0',64));raise exception 'Modified manifest hash accepted';
    exception when invalid_parameter_value then null;end;
    response:=commerce_rpc.import_legacy_wallets(886900,body,digest);
    if (commerce_rpc.wallet_state(886001)->'customer'->>'available_minor')::numeric<>38524
       or (commerce_rpc.wallet_state(886001)->'customer'->>'reserved_minor')::numeric<>6000 then
        raise exception 'Original available balance or original charges changed';end if;
    if (select count(*) from commerce.payments)<>payments_before then raise exception 'Transition fabricated a fresh payment receipt';end if;
    ord:=(response->'orders'->'abcdef012345'->>'order_id')::uuid;
    if (select quoted_minor from commerce.orders where id=ord)<>3000
       or (select sum(unit_quote_minor) from commerce.checks where order_id=ord)<>3000
       or (select refund_consent_reference from commerce.orders where id=ord) is not null then
        raise exception 'Transition repriced existing work or fabricated customer consent';end if;
    again:=commerce_rpc.import_legacy_wallets(886900,body,digest);
    if again->>'replayed'<>'true' or (again->'orders'->'abcdef012345'->>'order_id')::uuid<>ord
       or (commerce_rpc.wallet_state(886001)->'customer'->>'reserved_minor')::numeric<>6000 then
        raise exception 'Transition replay duplicated money or orders';end if;
    begin perform commerce_rpc.request_customer_stop(ord,886001,'missing-refund-consent');raise exception 'Old consent was guessed';
    exception when invalid_parameter_value then null;end;
    select (pricing_snapshot->>'policy_version')::bigint into policy from commerce.orders where id=ord;
    perform commerce_rpc.checkout_consent(ord,886001,policy,'explicit-native-owner-refund-consent');
    perform commerce_rpc.request_customer_stop(ord,886001,'explicit-owned-stop');
    perform commerce_rpc.record_terminal_results(ord,'legacy-job-one','[{"row_number":2,"state":"succeeded","verified_result":true},{"row_number":3,"state":"cancelled","verified_result":false},{"row_number":4,"state":"cancelled","verified_result":false}]',repeat('d',64));
    perform commerce_rpc.confirm_result_delivery(ord,886001,125,'legacy-partial-result',repeat('d',64));
    perform commerce_rpc.settle_order(ord);perform commerce_rpc.settle_order(ord);
    if (commerce_rpc.wallet_state(886001)->'customer'->>'available_minor')::numeric<>40524
       or (commerce_rpc.wallet_state(886001)->'customer'->>'reserved_minor')::numeric<>3000 then
        raise exception 'Existing charge partial refund was duplicated or repriced';end if;
end $$;
rollback;
