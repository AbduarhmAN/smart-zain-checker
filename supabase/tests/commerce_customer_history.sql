\set ON_ERROR_STOP on
begin;
do $$
declare owner_id uuid;other_id uuid;wallet_id uuid;ord uuid;page jsonb;item jsonb;
begin
    owner_id:=(commerce_rpc.register_telegram_user(887001,null)->>'user_id')::uuid;
    other_id:=(commerce_rpc.register_telegram_user(887002,null)->>'user_id')::uuid;
    insert into commerce.support_actors(telegram_user_id,active,selection_reference) values(887900,true,'isolated history admin');
    insert into commerce.orders(user_id,request_key,requested_checks,quoted_minor,pricing_snapshot,source_snapshot)
        values(owner_id,'owned-history',2,2,'{"policy_version":1}',jsonb_build_object('file_sha256',repeat('a',64))) returning id into ord;
    page:=commerce_rpc.customer_order_history(887001);
    if (page->>'total')::bigint<>1 or page->'items'->0->>'order_id'<>ord::text then raise exception 'Owned server order missing';end if;
    if (commerce_rpc.customer_order_history(887002)->>'total')::bigint<>0 then raise exception 'Another customer order disclosed';end if;
    begin perform commerce_rpc.customer_order_detail(887002,ord);raise exception 'Copied order ID disclosed';exception when invalid_parameter_value then null;end;
    begin perform commerce_rpc.customer_order_history(887002,0,8,true);raise exception 'Customer became history admin';exception when invalid_parameter_value then null;end;
    item:=commerce_rpc.customer_order_detail(887900,ord,true);
    if (item->>'owner_telegram_id')::bigint<>887001 then raise exception 'Admin order owner changed';end if;
    update commerce.support_actors set active=false where telegram_user_id=887900;
    begin perform commerce_rpc.customer_order_detail(887900,ord,true);raise exception 'Revoked admin read order';exception when invalid_parameter_value then null;end;
    select id into wallet_id from commerce.wallets where user_id=owner_id and kind='customer';
    perform commerce.post_pair('history-opening','topup',null,'platform:cash','wallet:'||wallet_id||':available',1000,null,wallet_id);
    update commerce.wallets set available_minor=1000 where id=wallet_id;
    page:=commerce_rpc.customer_wallet_ledger(887001);
    if (page->>'total')::bigint<>1 or (page->'items'->0->>'delta_minor')::numeric<>1000
       or page->'items'->0->>'bucket'<>'available' then raise exception 'Owned ledger missing or wrong amount';end if;
    if (commerce_rpc.customer_wallet_ledger(887002)->>'total')::bigint<>0 then raise exception 'Ledger crossed wallets';end if;
    if (commerce_rpc.wallet_state(887001)->'customer'->>'cash_available_minor')::numeric<>1000 then raise exception 'Paid cash was hidden';end if;
    begin perform commerce_rpc.customer_wallet_ledger(887001,0,1000);raise exception 'Unbounded ledger accepted';exception when invalid_parameter_value then null;end;
    update commerce.users set registration_state='suspended' where id=owner_id;
    begin perform commerce_rpc.customer_order_detail(887001,ord);raise exception 'Suspended owner read order';exception when invalid_parameter_value then null;end;
    if has_function_privilege('anon','commerce_rpc.customer_order_history(bigint,integer,integer,boolean)','EXECUTE')
       or has_function_privilege('authenticated','commerce_rpc.customer_wallet_ledger(bigint,integer,integer)','EXECUTE') then
        raise exception 'Client role reached backend history';end if;
end $$;
rollback;
