-- Durable owned order navigation and a wallet journal independent of local drafts.
begin;
create function commerce.customer_order_view(p_order_id uuid)
returns jsonb language sql stable set search_path='' as $$
    select jsonb_build_object('order_id',o.id,'owner_telegram_id',u.telegram_user_id,
        'request_key',o.request_key,'created_at',o.created_at,'checker_job_id',o.checker_job_id,
        'requested_checks',o.requested_checks,'quoted_minor',o.quoted_minor,
        'pricing_snapshot',o.pricing_snapshot,'source_snapshot',o.source_snapshot,
        'order_state',o.order_state,'payment_state',o.payment_state,
        'execution_state',o.execution_state,'delivery_state',o.delivery_state,
        'refund_consent_reference',o.refund_consent_reference,
        'customer_stop_reference',o.customer_stop_reference,'charged_minor',o.final_charge_minor,
        'refunded_minor',case when o.final_charge_minor is null then null else o.quoted_minor-o.final_charge_minor end,
        'verified_count',(select count(*) from commerce.checks where order_id=o.id and verified_result),
        'failed_count',(select count(*) from commerce.checks where order_id=o.id and state='failed'),
        'cancelled_count',(select count(*) from commerce.checks where order_id=o.id and state='cancelled'))
    from commerce.orders o join commerce.users u on u.id=o.user_id where o.id=p_order_id
$$;
revoke all on function commerce.customer_order_view(uuid) from public,anon,authenticated,service_role;

create function commerce_rpc.customer_order_history(p_actor_telegram_id bigint,p_offset integer default 0,
    p_limit integer default 8,p_admin boolean default false)
returns jsonb language plpgsql stable security definer set search_path='' as $$
declare uid uuid;items jsonb;total bigint;
begin
    if p_actor_telegram_id is null or p_actor_telegram_id<=0 or p_offset is null or p_offset not between 0 and 1000000
       or p_limit is null or p_limit not between 1 and 100 or p_admin is null then
        raise exception 'Valid authenticated history page required' using errcode='22023';end if;
    if p_admin then
        if not exists(select 1 from commerce.support_actors where telegram_user_id=p_actor_telegram_id and active) then
            raise exception 'Active history administrator required' using errcode='22023';end if;
    else
        select id into uid from commerce.users where telegram_user_id=p_actor_telegram_id and registration_state='active';
        if not found then raise exception 'Active registered customer required' using errcode='22023';end if;
    end if;
    select count(*) into total from commerce.orders where p_admin or user_id=uid;
    select coalesce(jsonb_agg(commerce.customer_order_view(id) order by created_at desc,id desc),'[]') into items
        from (select id,created_at from commerce.orders where p_admin or user_id=uid
              order by created_at desc,id desc limit p_limit offset p_offset) page;
    return jsonb_build_object('items',items,'total',total,'offset',p_offset,'limit',p_limit);
end $$;

create function commerce_rpc.customer_order_detail(p_actor_telegram_id bigint,p_order_id uuid,p_admin boolean default false)
returns jsonb language plpgsql stable security definer set search_path='' as $$
declare result jsonb;
begin
    if p_actor_telegram_id is null or p_actor_telegram_id<=0 or p_order_id is null or p_admin is null then
        raise exception 'Valid authenticated order required' using errcode='22023';end if;
    if p_admin then
        if not exists(select 1 from commerce.support_actors where telegram_user_id=p_actor_telegram_id and active) then
            raise exception 'Active order administrator required' using errcode='22023';end if;
    elsif not exists(select 1 from commerce.orders o join commerce.users u on u.id=o.user_id
        where o.id=p_order_id and u.telegram_user_id=p_actor_telegram_id and u.registration_state='active') then
        raise exception 'Unknown or unowned order' using errcode='22023';end if;
    result:=commerce.customer_order_view(p_order_id);
    if result is null then raise exception 'Unknown order' using errcode='22023';end if;
    return result;
end $$;

create function commerce_rpc.customer_wallet_ledger(p_telegram_user_id bigint,p_offset integer default 0,p_limit integer default 8)
returns jsonb language plpgsql stable security definer set search_path='' as $$
declare uid uuid;items jsonb;total bigint;
begin
    if p_telegram_user_id is null or p_telegram_user_id<=0 or p_offset is null or p_offset not between 0 and 1000000
       or p_limit is null or p_limit not between 1 and 100 then
        raise exception 'Valid authenticated ledger page required' using errcode='22023';end if;
    select id into uid from commerce.users where telegram_user_id=p_telegram_user_id and registration_state='active';
    if not found then raise exception 'Active registered customer required' using errcode='22023';end if;
    select count(*) into total from commerce.ledger_entries e join commerce.wallets w on w.id=e.wallet_id where w.user_id=uid;
    select coalesce(jsonb_agg(jsonb_build_object('journal_id',journal_id,'line_number',line_number,'order_id',order_id,
        'kind',kind,'wallet_kind',wallet_kind,'bucket',bucket,'delta_minor',delta_minor,'created_at',created_at)
        order by created_at desc,journal_id desc,line_number),'[]') into items
        from (select j.id as journal_id,e.line_number,j.order_id,j.kind,w.kind as wallet_kind,
              split_part(e.account_key,':',3) as bucket,e.delta_minor,j.created_at
              from commerce.ledger_entries e join commerce.wallets w on w.id=e.wallet_id join commerce.journals j on j.id=e.journal_id
              where w.user_id=uid order by j.created_at desc,j.id desc,e.line_number limit p_limit offset p_offset) page;
    return jsonb_build_object('items',items,'total',total,'offset',p_offset,'limit',p_limit);
end $$;

create or replace function commerce_rpc.wallet_state(p_telegram_user_id bigint)
returns jsonb language plpgsql security definer set search_path='' as $$
declare uid uuid;result jsonb;promo jsonb;account_id bigint;
begin
    select id,legacy_account_id into uid,account_id from commerce.users where telegram_user_id=p_telegram_user_id;
    if found then perform commerce.expire_user_promotional_credit(uid);end if;
    result:=commerce.wallet_state_before_promotion_expiry(p_telegram_user_id);
    select jsonb_build_object('expired_promotional_minor',coalesce(sum(expired_minor),0),
        'next_promotional_expiry',min(expires_at) filter(where available_minor>0 and expires_at>now())) into promo
        from commerce.promotional_credit_lots where user_id=uid;
    return result||jsonb_build_object('legacy_account_id',account_id,'customer',
        (result->'customer')||promo||jsonb_build_object('cash_available_minor',
            (result->'customer'->>'available_minor')::bigint-(result->'customer'->>'promotional_available_minor')::bigint));
end $$;

revoke all on function commerce_rpc.customer_order_history(bigint,integer,integer,boolean),
    commerce_rpc.customer_order_detail(bigint,uuid,boolean),commerce_rpc.customer_wallet_ledger(bigint,integer,integer) from public,anon,authenticated;
grant execute on function commerce_rpc.customer_order_history(bigint,integer,integer,boolean),
    commerce_rpc.customer_order_detail(bigint,uuid,boolean),commerce_rpc.customer_wallet_ledger(bigint,integer,integer) to service_role;
create or replace function commerce_rpc.health()
returns jsonb language sql stable security definer set search_path='' as $$
    select jsonb_build_object('status','ok','schema_version',25,'currency','EGP')
$$;
notify pgrst,'reload schema';
commit;
