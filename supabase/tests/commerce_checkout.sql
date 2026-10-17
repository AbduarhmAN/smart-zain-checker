\set ON_ERROR_STOP on
begin;
do $$ declare o uuid; policy bigint; result jsonb; state jsonb;
begin
    o := (commerce_rpc.create_order(888001,'checkout-consent-fixture',
        '[{"row_number":2,"check_type":"account"}]',jsonb_build_object('file_sha256',repeat('a',64)))->>'order_id')::uuid;
    state := commerce_rpc.checkout_state(o,888001);
    policy := (state->'pricing_snapshot'->>'policy_version')::bigint;
    begin
        perform commerce_rpc.checkout_state(o,888002);
        raise exception 'Another customer read checkout state';
    exception when invalid_parameter_value then null; end;
    begin
        perform commerce_rpc.checkout_consent(o,888001,policy+1,'callback:1');
        raise exception 'Consent accepted for another policy';
    exception when invalid_parameter_value then null; end;
    perform commerce_rpc.checkout_consent(o,888001,policy,'callback:1');
    result := commerce_rpc.checkout_consent(o,888001,policy,'callback:2');
    if result->>'reference'<>'callback:1' then raise exception 'First consent receipt was replaced'; end if;
    perform commerce_rpc.confirm_receipt(888001,'checkout-fixture','fixture','checkout-funds',100000,'fixture-proof');
    begin
        perform commerce_rpc.reserve_order(o,888001,'callback:other');
        raise exception 'Reservation ignored saved consent';
    exception when invalid_parameter_value then null; end;
    perform commerce_rpc.reserve_order(o,888001,'callback:1');
    state := commerce_rpc.checkout_state(o,888001);
    if state->>'reservation_state'<>'held' or state->>'payment_state'<>'reserved'
       or state->>'refund_consent_reference'<>'callback:1' then
        raise exception 'Checkout state does not prove its held reservation'; end if;
    if has_function_privilege('anon','commerce_rpc.checkout_state(uuid,bigint)','EXECUTE')
       or has_function_privilege('authenticated','commerce_rpc.checkout_consent(uuid,bigint,bigint,text)','EXECUTE')
       or has_function_privilege('service_role','commerce.reserve_order(uuid,bigint,text,uuid)','EXECUTE') then
        raise exception 'Checkout trust boundary is exposed'; end if;
end $$;
rollback;
