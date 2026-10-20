-- Unused promotional credit expires; paid deposits and held requests are preserved.
begin;
do $$ begin
    if exists(select 1 from commerce.wallets where promotional_available_minor>0 or promotional_reserved_minor>0) then
        raise exception 'Existing promotional balances require grant attribution before expiry migration';
    end if;
end $$;
create table commerce.promotional_credit_lots (
    id uuid primary key references commerce.journals(id),
    user_id uuid not null references commerce.users(id),
    wallet_id uuid not null references commerce.wallets(id),
    campaign_id uuid not null references commerce.campaigns(id),
    granted_minor bigint not null check(granted_minor>0),
    available_minor bigint not null check(available_minor>=0),
    reserved_minor bigint not null default 0 check(reserved_minor>=0),
    consumed_minor bigint not null default 0 check(consumed_minor>=0),
    expired_minor bigint not null default 0 check(expired_minor>=0),
    expires_at timestamptz,
    check(available_minor::numeric+reserved_minor+consumed_minor+expired_minor=granted_minor)
);
create table commerce.promotional_credit_allocations (
    order_id uuid not null,
    row_number bigint not null,
    lot_id uuid not null references commerce.promotional_credit_lots(id),
    amount_minor bigint not null check(amount_minor>0),
    state text not null default 'held' check(state in ('held','settled')),
    primary key(order_id,row_number,lot_id),
    foreign key(order_id,row_number) references commerce.checks(order_id,row_number)
);
create index promotional_expiry_due on commerce.promotional_credit_lots(expires_at,user_id) where available_minor>0;
alter table commerce.promotional_credit_lots enable row level security;
alter table commerce.promotional_credit_allocations enable row level security;
revoke all on commerce.promotional_credit_lots,commerce.promotional_credit_allocations from public,anon,authenticated,service_role;
-- The preflight proves these historical grants have no spendable or held balance.
insert into commerce.promotional_credit_lots(id,user_id,wallet_id,campaign_id,granted_minor,available_minor,consumed_minor,expires_at)
    select cu.grant_journal_id,cu.user_id,w.id,cu.campaign_id,cu.discount_minor,0,cu.discount_minor,cu.expires_at
    from commerce.campaign_usage cu join commerce.wallets w on w.user_id=cu.user_id and w.kind='customer'
    where cu.grant_journal_id is not null;

create function commerce.expire_user_promotional_credit(p_user_id uuid)
returns bigint language plpgsql security definer set search_path='' as $$
declare lot commerce.promotional_credit_lots%rowtype;w commerce.wallets%rowtype;total bigint:=0;
begin
    perform 1 from commerce.users where id=p_user_id for update;
    select * into w from commerce.wallets where user_id=p_user_id and kind='customer' for update;
    if not found then return 0;end if;
    for lot in select * from commerce.promotional_credit_lots where user_id=p_user_id
        and available_minor>0 and expires_at<=now() order by expires_at,id for update loop
        if lot.wallet_id<>w.id or lot.available_minor>w.promotional_available_minor or lot.available_minor>w.available_minor then
            raise exception 'Promotional attribution differs from wallet';end if;
        perform commerce.post_pair('promotion:expire:'||lot.id||':'||(lot.expired_minor+lot.available_minor),
            'adjustment',null,'wallet:'||w.id||':available','platform:expired_promotion',lot.available_minor,w.id,null);
        update commerce.wallets set available_minor=available_minor-lot.available_minor,
            promotional_available_minor=promotional_available_minor-lot.available_minor where id=w.id;
        update commerce.promotional_credit_lots set expired_minor=expired_minor+available_minor,available_minor=0 where id=lot.id;
        total:=total+lot.available_minor;
        w.available_minor:=w.available_minor-lot.available_minor;
        w.promotional_available_minor:=w.promotional_available_minor-lot.available_minor;
    end loop;
    return total;
end $$;
revoke all on function commerce.expire_user_promotional_credit(uuid) from public,anon,authenticated,service_role;

create function commerce_rpc.expire_promotional_credit(p_limit integer default 100)
returns jsonb language plpgsql security definer set search_path='' as $$
declare uid uuid;total numeric:=0;count integer:=0;
begin
    if p_limit is null or p_limit not between 1 and 500 then raise exception 'Bounded expiry batch required' using errcode='22023';end if;
    for uid in select distinct user_id from commerce.promotional_credit_lots where available_minor>0 and expires_at<=now()
        order by user_id limit p_limit loop
        total:=total+commerce.expire_user_promotional_credit(uid);count:=count+1;
    end loop;
    return jsonb_build_object('users',count,'expired_minor',total);
end $$;

alter function commerce_rpc.activate_campaign(bigint,text,text) rename to activate_campaign_without_expiry_lots;
alter function commerce_rpc.activate_campaign_without_expiry_lots(bigint,text,text) set schema commerce;
revoke all on function commerce.activate_campaign_without_expiry_lots(bigint,text,text) from public,anon,authenticated,service_role;
create function commerce_rpc.activate_campaign(p_telegram_user_id bigint,p_code text,p_reference text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare result jsonb;uid uuid;
begin
    select id into uid from commerce.users where telegram_user_id=p_telegram_user_id;
    if found then perform commerce.expire_user_promotional_credit(uid);end if;
    result:=commerce.activate_campaign_without_expiry_lots(p_telegram_user_id,p_code,p_reference);
    if result->>'state'='activated' and (result->>'wallet_credit_minor')::bigint>0 then
        insert into commerce.promotional_credit_lots(id,user_id,wallet_id,campaign_id,granted_minor,available_minor,expires_at)
            select cu.grant_journal_id,cu.user_id,w.id,cu.campaign_id,cu.discount_minor,cu.discount_minor,cu.expires_at
            from commerce.campaign_usage cu join commerce.users u on u.id=cu.user_id
                join commerce.wallets w on w.user_id=u.id and w.kind='customer'
            where cu.campaign_id=(result->>'campaign_id')::uuid and u.telegram_user_id=p_telegram_user_id;
    end if;
    return result;
end $$;

alter function commerce_rpc.wallet_state(bigint) rename to wallet_state_before_promotion_expiry;
alter function commerce_rpc.wallet_state_before_promotion_expiry(bigint) set schema commerce;
revoke all on function commerce.wallet_state_before_promotion_expiry(bigint) from public,anon,authenticated,service_role;
create function commerce_rpc.wallet_state(p_telegram_user_id bigint)
returns jsonb language plpgsql security definer set search_path='' as $$
declare uid uuid;
begin
    select id into uid from commerce.users where telegram_user_id=p_telegram_user_id;
    if found then perform commerce.expire_user_promotional_credit(uid);end if;
    return commerce.wallet_state_before_promotion_expiry(p_telegram_user_id);
end $$;

alter function commerce_rpc.reserve_order(uuid,bigint,text,uuid) rename to reserve_order_before_expiry_lots;
alter function commerce_rpc.reserve_order_before_expiry_lots(uuid,bigint,text,uuid) set schema commerce;
revoke all on function commerce.reserve_order_before_expiry_lots(uuid,bigint,text,uuid) from public,anon,authenticated,service_role;
create function commerce_rpc.reserve_order(p_order_id uuid,p_telegram_user_id bigint,p_consent_reference text,p_payment_id uuid default null)
returns jsonb language plpgsql security definer set search_path='' as $$
declare uid uuid;result jsonb;remaining bigint;allocation bigint;item record;lot commerce.promotional_credit_lots%rowtype;
    already_reserved boolean;w commerce.wallets%rowtype;attributed numeric;
begin
    select id into uid from commerce.users where telegram_user_id=p_telegram_user_id for update;
    if not found then raise exception 'Registered customer required' using errcode='22023';end if;
    perform commerce.expire_user_promotional_credit(uid);
    select exists(select 1 from commerce.reservations where order_id=p_order_id) into already_reserved;
    result:=commerce.reserve_order_before_expiry_lots(p_order_id,p_telegram_user_id,p_consent_reference,p_payment_id);
    if result->>'state'<>'held' or already_reserved then return result;end if;
    for item in select row_number,promotional_payment_minor from commerce.checks
        where order_id=p_order_id and promotional_payment_minor>0 order by row_number loop
        remaining:=item.promotional_payment_minor;
        for lot in select * from commerce.promotional_credit_lots where user_id=uid and available_minor>0
            and (expires_at is null or expires_at>now()) order by expires_at nulls last,id for update loop
            if remaining=0 then exit;end if;
            allocation:=least(remaining,lot.available_minor);
            update commerce.promotional_credit_lots set available_minor=available_minor-allocation,reserved_minor=reserved_minor+allocation where id=lot.id;
            insert into commerce.promotional_credit_allocations(order_id,row_number,lot_id,amount_minor)
                values(p_order_id,item.row_number,lot.id,allocation);
            remaining:=remaining-allocation;
        end loop;
        if remaining<>0 then raise exception 'Promotional reserve is not fully attributed';end if;
    end loop;
    select * into w from commerce.wallets where user_id=uid and kind='customer';
    select coalesce(sum(available_minor::numeric),0) into attributed from commerce.promotional_credit_lots where user_id=uid;
    if attributed<>w.promotional_available_minor then raise exception 'Promotional available balance differs from grants';end if;
    return result;
end $$;

alter function commerce_rpc.settle_order(uuid) rename to settle_order_before_expiry_lots;
alter function commerce_rpc.settle_order_before_expiry_lots(uuid) set schema commerce;
revoke all on function commerce.settle_order_before_expiry_lots(uuid) from public,anon,authenticated,service_role;
create function commerce_rpc.settle_order(p_order_id uuid)
returns jsonb language plpgsql security definer set search_path='' as $$
declare result jsonb;uid uuid;item record;expired bigint;
begin
    select u.id into uid from commerce.users u join commerce.orders o on o.user_id=u.id where o.id=p_order_id for update of u;
    result:=commerce.settle_order_before_expiry_lots(p_order_id);
    for item in select a.lot_id,sum(a.amount_minor)::bigint as held,
        coalesce(sum(a.amount_minor) filter(where c.verified_result),0)::bigint as consumed,
        coalesce(sum(a.amount_minor) filter(where not c.verified_result),0)::bigint as refunded
        from commerce.promotional_credit_allocations a join commerce.checks c on c.order_id=a.order_id and c.row_number=a.row_number
        where a.order_id=p_order_id and a.state='held' group by a.lot_id order by a.lot_id loop
        update commerce.promotional_credit_lots set reserved_minor=reserved_minor-item.held,
            consumed_minor=consumed_minor+item.consumed,available_minor=available_minor+item.refunded where id=item.lot_id;
        update commerce.promotional_credit_allocations set state='settled' where order_id=p_order_id and lot_id=item.lot_id and state='held';
    end loop;
    expired:=commerce.expire_user_promotional_credit(uid);
    return result||jsonb_build_object('expired_promotional_minor',expired);
end $$;

revoke all on function commerce_rpc.expire_promotional_credit(integer),commerce_rpc.activate_campaign(bigint,text,text),
    commerce_rpc.wallet_state(bigint),commerce_rpc.reserve_order(uuid,bigint,text,uuid),commerce_rpc.settle_order(uuid) from public,anon,authenticated;
grant execute on function commerce_rpc.expire_promotional_credit(integer),commerce_rpc.activate_campaign(bigint,text,text),
    commerce_rpc.wallet_state(bigint),commerce_rpc.reserve_order(uuid,bigint,text,uuid),commerce_rpc.settle_order(uuid) to service_role;
create or replace function commerce_rpc.health()
returns jsonb language sql stable security definer set search_path='' as $$
    select jsonb_build_object('status','ok','schema_version',21,'currency','EGP')
$$;
notify pgrst,'reload schema';
commit;
