\set ON_ERROR_STOP on
begin;
do $$ declare u uuid; o uuid; d uuid; p uuid; q bigint; r jsonb; w jsonb; n bigint;
    source jsonb := jsonb_build_object('file_sha256',repeat('d',64));
    rows jsonb := '[{"row_number":2,"check_type":"account"},{"row_number":3,"check_type":"service"},{"row_number":4,"check_type":"account"},{"row_number":5,"check_type":"service"}]';
    final_rows jsonb := '[{"row_number":2,"state":"succeeded","verified_result":true},{"row_number":3,"state":"failed","verified_result":false},{"row_number":4,"state":"review","verified_result":true},{"row_number":5,"state":"not_found","verified_result":true}]';
begin
    q := (commerce_rpc.quote_checks(1)->>'unit_price_minor')::numeric;
    r := commerce_rpc.confirm_receipt(885001,'wallet-fixture-topup','fixture','topup-tx',100000,'provider-proof');
    p := (r->>'payment_id')::uuid;
    if (commerce_rpc.confirm_receipt(885001,'wallet-fixture-topup','fixture','topup-tx',100000,'provider-proof')->>'payment_id')::uuid <> p then
        raise exception 'Topup replay duplicated payment'; end if;
    w := commerce_rpc.wallet_state(885001);
    u := (w->>'user_id')::uuid;
    if (w->'customer'->>'available_minor')::numeric <> 100000
       or (w->'partner'->>'available_minor')::numeric <> 0 then raise exception 'Topup wallet isolation failed'; end if;
    if (select first_paid_service_at from commerce.users where id=u) is not null then raise exception 'Topup counted as paid service'; end if;
    begin
        perform commerce_rpc.confirm_receipt(885001,'changed-key','fixture','topup-tx',100000,'provider-proof');
        raise exception 'Provider transfer credited twice';
    exception when invalid_parameter_value then null; end;
    begin
        perform commerce_rpc.confirm_receipt(885001,'wallet-fixture-topup','fixture','topup-tx',90000,'provider-proof');
        raise exception 'Changed amount accepted on replay';
    exception when invalid_parameter_value then null; end;
    o := (commerce_rpc.create_order(885001,'wallet-settlement-order',rows,source)->>'order_id')::uuid;
    begin
        perform commerce_rpc.reserve_order(o,885001,null);
        raise exception 'Missing refund consent accepted';
    exception when invalid_parameter_value then null; end;
    perform commerce_rpc.reserve_order(o,885001,'telegram-consent-fixture');
    perform commerce_rpc.reserve_order(o,885001,'telegram-consent-fixture');
    w := commerce_rpc.wallet_state(885001);
    if (w->'customer'->>'available_minor')::numeric <> 100000-4*q
       or (w->'customer'->>'reserved_minor')::numeric <> 4*q then raise exception 'Reservation replay debited twice'; end if;
    begin
        perform commerce_rpc.settle_order(o);
        raise exception 'Settlement before execution accepted';
    exception when invalid_parameter_value then null; end;
    perform commerce_rpc.bind_checker_job(o,'wallet-fixture-job');
    begin
        perform commerce_rpc.record_terminal_results(o,'wallet-fixture-job','[{"row_number":2,"state":"succeeded","verified_result":true}]',repeat('e',64));
        raise exception 'Partial terminal rows accepted';
    exception when invalid_parameter_value then null; end;
    perform commerce_rpc.record_terminal_results(o,'wallet-fixture-job',final_rows,repeat('e',64));
    begin
        perform commerce_rpc.settle_order(o);
        raise exception 'Settlement before delivery accepted';
    exception when invalid_parameter_value then null; end;
    begin
        perform commerce_rpc.confirm_result_delivery(o,885002,1,'fixture-file',repeat('e',64));
        raise exception 'Delivery to another user accepted';
    exception when invalid_parameter_value then null; end;
    perform commerce_rpc.confirm_result_delivery(o,885001,1,'fixture-file',repeat('e',64));
    r := commerce_rpc.settle_order(o);
    if r->>'state' <> 'partial_refund' or (r->>'charged_minor')::numeric <> 3*q or (r->>'refunded_minor')::numeric <> q then
        raise exception 'Verified review/not_found or failed refund classification wrong'; end if;
    n := (select count(*) from commerce.journals where order_id=o);
    perform commerce_rpc.settle_order(o);
    if (select count(*) from commerce.journals where order_id=o) <> n then raise exception 'Settlement replay posted twice'; end if;
    w := commerce_rpc.wallet_state(885001);
    if (w->'customer'->>'available_minor')::numeric <> 100000-3*q or (w->'customer'->>'reserved_minor')::numeric <> 0 then
        raise exception 'Settlement balances wrong'; end if;
    if (select count(*) from commerce.refunds where order_id=o) <> 1 then raise exception 'Wrong refund count'; end if;
    if (select first_paid_service_at from commerce.users where id=u) is null then raise exception 'Paid service not recorded'; end if;
    -- Direct order payment must never credit customer wallet as a topup.
    d := (commerce_rpc.create_order(885003,'direct-settlement-order','[{"row_number":2,"check_type":"account"}]',source)->>'order_id')::uuid;
    p := (commerce_rpc.confirm_receipt(885003,'direct-payment','fixture','direct-tx',q,'proof','direct_order',d)->>'payment_id')::uuid;
    if (commerce_rpc.wallet_state(885003)->'customer'->>'available_minor')::numeric <> 0 then raise exception 'Direct payment treated as topup'; end if;
    begin
        perform commerce_rpc.reserve_order(d,885003,'consent');
        raise exception 'Direct funding also debited wallet';
    exception when invalid_parameter_value then null; end;
    perform commerce_rpc.reserve_order(d,885003,'consent',p);
    perform commerce_rpc.bind_checker_job(d,'direct-fixture-job');
    perform commerce_rpc.record_terminal_results(d,'direct-fixture-job','[{"row_number":2,"state":"failed","verified_result":false}]',repeat('f',64));
    perform commerce_rpc.confirm_result_delivery(d,885003,2,'direct-fixture-file',repeat('f',64));
    r := commerce_rpc.settle_order(d);
    if r->>'state' <> 'refunded' or (commerce_rpc.wallet_state(885003)->'customer'->>'available_minor')::numeric <> q then
        raise exception 'Direct payment failure was not refunded to wallet'; end if;
    if (select first_paid_service_at from commerce.users where telegram_user_id=885003) is not null then
        raise exception 'Fully refunded order consumed new-customer eligibility'; end if;
    if exists (select 1 from commerce.journals j where
        (select sum(delta_minor::numeric) from commerce.ledger_entries l where l.journal_id=j.id) <> 0) then
        raise exception 'Financial conservation failed'; end if;
    raise notice 'PASS: funding isolation, reservation, consent, delivery gate, valid results charged, failed refund, full direct refund, replay and balanced journals';
end $$;
commit;
