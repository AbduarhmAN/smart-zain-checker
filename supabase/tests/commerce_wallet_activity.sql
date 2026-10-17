\set ON_ERROR_STOP on
begin;
do $$
declare owner_id uuid;other_id uuid;wallet_id uuid;other_wallet uuid;ord uuid;other_ord uuid;
        page jsonb;item jsonb;before_count bigint;n integer;
begin
    owner_id:=(commerce_rpc.register_telegram_user(888032,null)->>'user_id')::uuid;
    other_id:=(commerce_rpc.register_telegram_user(888033,null)->>'user_id')::uuid;
    select id into wallet_id from commerce.wallets where user_id=owner_id and kind='customer';
    select id into other_wallet from commerce.wallets where user_id=other_id and kind='customer';
    insert into commerce.orders(user_id,request_key,requested_checks,quoted_minor,pricing_snapshot,source_snapshot)
        values(owner_id,'summary-file',2368,3369,'{}','{"original_filename":"file.xlsx"}') returning id into ord;
    insert into commerce.orders(user_id,request_key,requested_checks,quoted_minor,pricing_snapshot,source_snapshot)
        values(owner_id,'other-file',1,3,'{}','{"original_filename":"other.xlsx"}') returning id into other_ord;
    for n in 1..2368 loop
        perform commerce.post_pair('summary-refund-'||n,'refund',ord,
            'wallet:'||wallet_id||':reserved','wallet:'||wallet_id||':available',1,wallet_id,wallet_id);
    end loop;
    perform commerce.post_pair('summary-reserve','reserve',ord,
        'wallet:'||wallet_id||':available','wallet:'||wallet_id||':reserved',3369,wallet_id,wallet_id);
    perform commerce.post_pair('summary-charge','service',ord,
        'wallet:'||wallet_id||':reserved','platform:revenue',1001,wallet_id,null);
    perform commerce.post_pair('summary-other-refund','refund',other_ord,
        'wallet:'||wallet_id||':reserved','wallet:'||wallet_id||':available',3,wallet_id,wallet_id);
    perform commerce.post_pair('summary-topup-one','topup',null,'platform:cash','wallet:'||wallet_id||':available',100,null,wallet_id);
    perform commerce.post_pair('summary-topup-two','topup',null,'platform:cash','wallet:'||wallet_id||':available',200,null,wallet_id);
    perform commerce.post_pair('summary-other-owner','topup',null,'platform:cash','wallet:'||other_wallet||':available',999,null,other_wallet);
    select count(*) into before_count from commerce.ledger_entries;
    page:=commerce_rpc.customer_wallet_activity(888032,0,100);
    if (page->>'total')::integer<>6 or jsonb_array_length(page->'items')<>6 then
        raise exception 'Raw customer rows leaked or distinct file/topup operations were merged';end if;
    select value into item from jsonb_array_elements(page->'items') where value->>'kind'='refund' and value->>'order_id'=ord::text;
    if (item->>'delta_minor')::numeric<>2368 or (item->'bucket_deltas'->>'available')::numeric<>2368
       or (item->'bucket_deltas'->>'reserved')::numeric<>-2368 or (item->>'entry_count')::integer<>4736
       or item->>'filename'<>'file.xlsx' then raise exception 'Whole-file refund total or bucket details lost';end if;
    select value into item from jsonb_array_elements(page->'items') where value->>'kind'='service';
    if (item->>'delta_minor')::numeric<>-1001 then raise exception 'File charge sign changed';end if;
    select value into item from jsonb_array_elements(page->'items') where value->>'kind'='reserve';
    if (item->>'delta_minor')::numeric<>-3369 or (item->'bucket_deltas'->>'reserved')::numeric<>3369 then
        raise exception 'Reservation was cancelled out by internal transfer';end if;
    page:=commerce_rpc.customer_wallet_activity(888032,0,1);
    if jsonb_array_length(page->'items')<>1 or (page->>'total')::integer<>6 then
        raise exception 'Pagination happened before grouping';end if;
    page:=commerce_rpc.customer_wallet_activity(888033);
    if (page->>'total')::integer<>1 or (page->'items'->0->>'delta_minor')::numeric<>999 then
        raise exception 'Wallet activity crossed customer ownership';end if;
    if (select count(*) from commerce.ledger_entries)<>before_count then raise exception 'Display changed accounting records';end if;
    begin perform commerce_rpc.customer_wallet_activity(888032,0,101);raise exception 'Unbounded activity page accepted';
        exception when invalid_parameter_value then null;end;
    update commerce.users set registration_state='suspended' where id=owner_id;
    begin perform commerce_rpc.customer_wallet_activity(888032);raise exception 'Suspended user read activity';
        exception when invalid_parameter_value then null;end;
    if has_function_privilege('anon','commerce_rpc.customer_wallet_activity(bigint,integer,integer)','EXECUTE')
       or has_function_privilege('authenticated','commerce_rpc.customer_wallet_activity(bigint,integer,integer)','EXECUTE') then
        raise exception 'Public activity access enabled';end if;
end $$;
rollback;
