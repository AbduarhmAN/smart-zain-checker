\set ON_ERROR_STOP on
begin;
do $$
declare q uuid:=gen_random_uuid();uid uuid;quote jsonb;page jsonb;proof jsonb;evidence jsonb;first jsonb;again jsonb;
begin
    uid:=(commerce_rpc.register_telegram_user(887101,null)->>'user_id')::uuid;
    perform commerce_rpc.register_telegram_user(887102,null);
    quote:=jsonb_build_object('id',q,'user_id',887101,'order_id',null,'amount_minor',3000,'asset','USDT',
        'asset_amount','0.600000','precision',6,'egp_per_asset','50','rate_source','isolated provider rate',
        'observed_at',to_char(now() at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
        'expires_at',to_char((now()+interval '15 minutes') at time zone 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'));
    perform commerce_rpc.create_payment_request(quote,'cryptopay','7');
    if jsonb_array_length(commerce_rpc.pending_bound_invoices())<>0 then raise exception 'Unattached invoice monitored';end if;
    perform commerce_rpc.begin_payment_invoice(q,887101);
    perform commerce_rpc.attach_payment_invoice(q,887101,810001);
    page:=commerce_rpc.pending_bound_invoices();
    if jsonb_array_length(page)<>1 or (page->0->>'telegram_user_id')::bigint<>887101 or (page->0->>'invoice_id')::bigint<>810001 then
        raise exception 'Monitor lost immutable invoice owner';end if;
    begin perform commerce_rpc.payment_request(q,887102);raise exception 'Copied quote was readable';exception when invalid_parameter_value then null;end;
    proof:=jsonb_build_object('payload','zain-commerce:'||q,'invoice_id',810001);
    evidence:=jsonb_build_object('provider','cryptopay','reference','7:810001','receiver','7','asset','USDT','amount','0.600000',
        'received_at',quote->>'observed_at','proof_json',proof::text,'invoice_payload','zain-commerce:'||q);
    begin perform commerce_rpc.confirm_payment_request(q,887102,evidence,'{"kind":"invoice"}');raise exception 'Stolen invoice credited another user';exception when invalid_parameter_value then null;end;
    begin perform commerce_rpc.confirm_payment_request(q,887101,evidence||'{"invoice_payload":"copied-other-request"}','{"kind":"invoice"}');
        raise exception 'Wrong invoice payload accepted';exception when invalid_parameter_value then null;end;
    first:=commerce_rpc.confirm_payment_request(q,887101,evidence,'{"kind":"invoice"}');
    again:=commerce_rpc.confirm_payment_request(q,887101,evidence,'{"kind":"invoice"}');
    if first->>'payment_id'<>again->>'payment_id' or again->>'replayed'<>'true' then raise exception 'Repeated monitor credited again';end if;
    if jsonb_array_length(commerce_rpc.pending_bound_invoices())<>0 then raise exception 'Confirmed invoice still scanned';end if;
    if (commerce_rpc.wallet_state(887101)->'customer'->>'available_minor')::numeric<>3000
       or (commerce_rpc.wallet_state(887102)->'customer'->>'available_minor')::numeric<>0 then
        raise exception 'Copied invoice or retry changed recipient or money';end if;
    if has_function_privilege('anon','commerce_rpc.pending_bound_invoices(uuid,integer)','EXECUTE')
       or has_function_privilege('authenticated','commerce_rpc.pending_bound_invoices(uuid,integer)','EXECUTE') then
        raise exception 'Client accessed global payment monitoring';end if;
end $$;
rollback;
