\set ON_ERROR_STOP on
begin;
set local timezone='UTC';
do $$
declare q uuid:=gen_random_uuid();q2 uuid:=gen_random_uuid();snap jsonb;c jsonb;other jsonb;proof jsonb;ev jsonb;paid jsonb;again jsonb;
begin
    snap:=jsonb_build_object('id',q,'user_id',888101,'order_id',null,'amount_minor',3000,'asset','USDT',
        'asset_amount','0.600000','precision',6,'egp_per_asset','50','rate_source','isolated Binance rate',
        'observed_at',to_char(now()-interval '1 minute','YYYY-MM-DD"T"HH24:MI:SS.US')||'Z',
        'expires_at',to_char(now()+interval '14 minutes','YYYY-MM-DD"T"HH24:MI:SS.US')||'Z');
    perform commerce_rpc.create_payment_request(snap,'binance_pay','uid:41');
    perform commerce_rpc.create_payment_request(snap||jsonb_build_object('id',q2,'user_id',888102),'binance_pay','uid:41');
    c:=commerce_rpc.issue_binance_note(q,888101,'999');
    if c->>'challenge' !~ '^ZC[a-f0-9]{32}$' then raise exception 'Weak or missing random note';end if;
    if commerce_rpc.issue_binance_note(q,888101,'999')->>'challenge'<>c->>'challenge' then raise exception 'Issued note changed';end if;
    begin perform commerce_rpc.binance_note(q,888102);raise exception 'Customer read another private challenge';exception when invalid_parameter_value then null;end;
    begin perform commerce_rpc.issue_binance_note(q,888102,'999');raise exception 'Customer reissued another challenge';exception when invalid_parameter_value then null;end;
    begin perform commerce_rpc.issue_binance_note(q,888101,'998');raise exception 'Sender changed after issuing challenge';exception when invalid_parameter_value then null;end;
    other:=commerce_rpc.issue_binance_note(q2,888102,'999');
    proof:=jsonb_build_object('transaction_id','fixture-note-tx','order_type','C2C','payer_uid','999',
        'payment_note',c->>'challenge','receiver','uid:41','asset','USDT','amount','0.600000');
    ev:=jsonb_build_object('provider','binance_pay','reference','fixture-note-tx','asset','USDT','amount','0.600000',
        'receiver','uid:41','received_at',c->>'issued_at','proof_json',proof::text);
    begin perform commerce_rpc.confirm_binance_note(q2,ev);raise exception 'Copied receipt credited a different quote';exception when invalid_parameter_value then null;end;
    begin perform commerce_rpc.confirm_binance_note(q,ev||jsonb_build_object('proof_json',(proof||'{"payment_note":"TX1"}')::text));
        raise exception 'Raw TXID replaced private note';exception when invalid_parameter_value then null;end;
    begin perform commerce_rpc.confirm_binance_note(q,ev||jsonb_build_object('proof_json',(proof||'{"payer_uid":"998"}')::text));
        raise exception 'Another sender paid private request';exception when invalid_parameter_value then null;end;
    begin perform commerce_rpc.confirm_binance_note(q,ev||jsonb_build_object('received_at',snap->>'observed_at'));
        raise exception 'Old transfer claimed fresh note';exception when invalid_parameter_value then null;end;
    paid:=commerce_rpc.confirm_binance_note(q,ev);again:=commerce_rpc.confirm_binance_note(q,ev);
    if paid->>'payment_id'<>again->>'payment_id' or again->>'replayed'<>'true'
       or (commerce_rpc.wallet_state(888101)->'customer'->>'available_minor')::numeric<>3000
       or (commerce_rpc.wallet_state(888102)->'customer'->>'available_minor')::numeric<>0 then
        raise exception 'Private note changed beneficiary or repeated money';end if;
    if has_function_privilege('anon','commerce_rpc.confirm_binance_note(uuid,jsonb)','EXECUTE')
       or has_function_privilege('authenticated','commerce_rpc.pending_binance_notes(uuid,integer)','EXECUTE') then
        raise exception 'Client accessed private-note backend';end if;
end $$;
rollback;
