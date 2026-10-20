\set ON_ERROR_STOP on
begin;
do $$ declare o uuid; q jsonb; r jsonb; source jsonb := jsonb_build_object('file_sha256',repeat('a',64));
        rows jsonb := '[{"row_number":2,"check_type":"account"},{"row_number":3,"check_type":"service"}]';
begin
    q := commerce_rpc.quote_checks(2);
    if (q->>'total_minor')::numeric <> 6000 or q->>'unit' <> 'check' then raise exception 'Per-check quote wrong'; end if;
    r := commerce_rpc.create_order(883001,'quote-fixture',rows,source);
    o := (r->>'order_id')::uuid;
    if (select count(*) from commerce.checks where order_id = o) <> 2 then raise exception 'Checks not saved atomically'; end if;
    if (select order_state from commerce.orders where id = o) <> 'draft'
       or (select payment_state from commerce.orders where id = o) <> 'unpaid' then raise exception 'Quote prematurely paid'; end if;
    insert into commerce.pricing_policies(unit_price_minor,refund_destination,decision_reference)
    values (4000,'wallet','fixture-new-policy');
    r := commerce_rpc.create_order(883001,'quote-fixture',rows,source);
    if (r->>'order_id')::uuid <> o or (r->>'quoted_minor')::numeric <> 6000 then raise exception 'Replay repriced original order'; end if;
    begin
        perform commerce_rpc.create_order(883001,'quote-fixture','[{"row_number":8,"check_type":"account"}]',source);
        raise exception 'Changed source rows accepted on replay';
    exception when invalid_parameter_value then null; end;
    begin
        perform commerce_rpc.create_order(883002,'quote-fixture',rows,source);
        raise exception 'Request replay by different user accepted';
    exception when invalid_parameter_value then null; end;
    begin
        perform commerce_rpc.create_order(883001,'duplicate-fixture','[{"row_number":2,"check_type":"account"},{"row_number":2,"check_type":"service"}]',source);
        raise exception 'Duplicate row identities accepted';
    exception when invalid_parameter_value then null; end;
    begin
        perform commerce_rpc.create_order(883001,'missing-fixture','[{"row_number":2}]',source);
        raise exception 'Missing check type accepted';
    exception when invalid_parameter_value then null; end;
    begin
        perform commerce_rpc.quote_checks(9223372036854775807);
        raise exception 'Monetary overflow accepted';
    exception when invalid_parameter_value then null; end;
    raise notice 'PASS: per-check pricing, atomic rows, frozen quote, conflicting replay, user ownership, duplicate/missing row and overflow';
end $$;
-- New test policy is synthetic and only exists in this retained test database.
commit;
