\set ON_ERROR_STOP on
begin;
set local timezone='UTC';
do $$ declare q uuid := gen_random_uuid(); q2 uuid := gen_random_uuid(); q3 uuid := gen_random_uuid();
    snap jsonb; snap2 jsonb; ev jsonb; audit jsonb; r jsonb; p uuid; o uuid; minor bigint;
begin
    snap := jsonb_build_object('id',q,'user_id',887001,'amount_minor',3000,'asset','USDT',
        'asset_amount','0.600000','egp_per_asset','50','rate_source','fixture-only','precision',6,
        'observed_at',to_char(now()-interval '1 minute','YYYY-MM-DD"T"HH24:MI:SS')||'+00:00',
        'expires_at',to_char(now()+interval '14 minutes','YYYY-MM-DD"T"HH24:MI:SS')||'+00:00','order_id',null);
    perform commerce_rpc.create_payment_request(snap,'cryptopay','7');
    if (commerce_rpc.create_payment_request(snap,'cryptopay','7')->>'replayed')::boolean is not true then
        raise exception 'Quote replay failed'; end if;
    begin
        perform commerce_rpc.create_payment_request(snap||'{"asset_amount":"0.5"}','cryptopay','7');
        raise exception 'Underquoted asset charge accepted';
    exception when invalid_parameter_value then null; end;
    begin
        perform commerce_rpc.create_payment_request(snap,'cryptopay','9');
        raise exception 'Changed receiver on replay accepted';
    exception when invalid_parameter_value then null; end;
    begin
        perform commerce_rpc.payment_request(q,887002);
        raise exception 'Another user read the quote';
    exception when invalid_parameter_value then null; end;
    if commerce_rpc.begin_payment_invoice(q,887001)->>'state'<>'claimed'
       or commerce_rpc.begin_payment_invoice(q,887001)->>'state'<>'started' then
        raise exception 'Ambiguous invoice creation was restarted'; end if;
    perform commerce_rpc.attach_payment_invoice(q,887001,8);
    perform commerce_rpc.attach_payment_invoice(q,887001,8);
    begin
        perform commerce_rpc.attach_payment_invoice(q,887001,9);
        raise exception 'Issued invoice was replaced';
    exception when invalid_parameter_value then null; end;
    ev := jsonb_build_object('provider','cryptopay','reference','7:8','asset','USDT','amount','0.600000',
        'receiver','7','received_at',to_char(now(),'YYYY-MM-DD"T"HH24:MI:SS')||'+00:00',
        'invoice_payload','zain-commerce:'||q,'proof_json',jsonb_build_object('payload','zain-commerce:'||q)::text);
    audit := '{"kind":"invoice"}';
    begin
        perform commerce_rpc.confirm_payment_request(q,887001,ev||'{"invoice_payload":"other"}',audit);
        raise exception 'Invoice paid another customer quote';
    exception when invalid_parameter_value then null; end;
    r := commerce_rpc.confirm_payment_request(q,887001,ev,audit); p := (r->>'payment_id')::uuid;
    if (commerce_rpc.confirm_payment_request(q,887001,ev,audit)->>'payment_id')::uuid<>p then
        raise exception 'Invoice replay funded twice'; end if;
    begin
        perform commerce_rpc.confirm_payment_request(q,887001,ev||'{"amount":"1"}',audit);
        raise exception 'Changed evidence accepted after confirmation';
    exception when invalid_parameter_value then null; end;
    if (commerce_rpc.wallet_state(887001)->'customer'->>'available_minor')::numeric<>3000 then
        raise exception 'Invoice credit amount changed'; end if;

    snap2 := snap||jsonb_build_object('id',q2);
    perform commerce_rpc.create_payment_request(snap2,'binance_pay','uid:41');
    ev := ev||jsonb_build_object('provider','binance_pay','reference','fixture-transfer-1',
        'receiver','uid:41','invoice_payload',null,'proof_json','{"transaction_id":"fixture-transfer-1"}');
    begin
        perform commerce_rpc.confirm_payment_request(q2,887001,ev,'{}');
        raise exception 'Raw transaction identified the customer';
    exception when invalid_parameter_value then null; end;
    audit := '{"kind":"admin","actor_id":887900,"approval_reference":"synthetic-update:1"}';
    begin
        perform commerce_rpc.confirm_payment_request(q2,887001,ev||'{"amount":"0.59"}',audit);
        raise exception 'Amount tolerance allowed an underpayment';
    exception when invalid_parameter_value then null; end;
    begin
        perform commerce_rpc.confirm_payment_request(q2,887001,ev||'{"amount":"1"}',audit);
        raise exception 'Overpayment silently discarded';
    exception when invalid_parameter_value then null; end;
    begin
        perform commerce_rpc.confirm_payment_request(q2,887001,ev||'{"received_at":"2000-01-01T00:00:00Z"}',audit);
        raise exception 'Old transfer funded a new quote';
    exception when invalid_parameter_value then null; end;
    perform commerce_rpc.confirm_payment_request(q2,887001,ev,audit);
    perform commerce_rpc.confirm_payment_request(q2,887001,ev,audit);
    if (commerce_rpc.wallet_state(887001)->'customer'->>'available_minor')::numeric<>6000 then
        raise exception 'Assigned transfer replay credited twice'; end if;
    perform commerce_rpc.create_payment_request(snap||jsonb_build_object('id',q3,'user_id',887002),'binance_pay','uid:41');
    begin
        perform commerce_rpc.confirm_payment_request(q3,887002,ev,audit);
        raise exception 'One transfer credited another customer';
    exception when invalid_parameter_value then null; end;
    if (commerce_rpc.wallet_state(887002)->'customer'->>'available_minor')::numeric<>0 then
        raise exception 'Rejected assignment changed wallet'; end if;

    o := (commerce_rpc.create_order(887003,'quote-direct-fixture','[{"row_number":2,"check_type":"account"}]',
        jsonb_build_object('file_sha256',repeat('f',64)))->>'order_id')::uuid;
    q3 := gen_random_uuid();
    select quoted_minor into minor from commerce.orders where id=o;
    snap2 := snap||jsonb_build_object('id',q3,'user_id',887003,'order_id',o,
        'amount_minor',minor,'asset_amount',(minor::numeric/5000)::text);
    perform commerce_rpc.create_payment_request(snap2,'bsc','fixture-address',
        jsonb_build_object('chain_id',56,'asset','USDT','contract','0x'||repeat('d',40),'decimals',18));
    ev := ev||jsonb_build_object('provider','bsc','reference','56:fixture-direct','receiver','fixture-address',
        'amount',snap2->>'asset_amount');
    r := commerce_rpc.confirm_payment_request(q3,887003,ev,audit);
    perform commerce_rpc.reserve_order(o,887003,'fixture-refund-consent',(r->>'payment_id')::uuid);
    if (commerce_rpc.wallet_state(887003)->'customer'->>'available_minor')::numeric<>0
       or not exists(select 1 from commerce.reservations where order_id=o) then
        raise exception 'Direct receipt incorrectly became wallet credit'; end if;
    if exists(select 1 from commerce.journals j join commerce.ledger_entries le on le.journal_id=j.id
        group by j.id having sum(le.delta_minor)<>0) then raise exception 'Ledger is not balanced'; end if;
end $$;
rollback;
