-- Reclaim unused promotional grants only. Cash deposits and held orders remain intact.
begin;
create table commerce.promotional_campaign_cancellations (
    campaign_id uuid primary key references commerce.campaigns(id),
    actor_telegram_id bigint not null check(actor_telegram_id>0),
    reference text not null check(length(btrim(reference)) between 1 and 1000),
    cancelled_at timestamptz not null default now()
);
alter table commerce.promotional_campaign_cancellations enable row level security;
revoke all on commerce.promotional_campaign_cancellations from public,anon,authenticated,service_role;
alter table commerce.promotional_credit_lots add column reclaimed_minor numeric not null default 0 check(reclaimed_minor>=0);
alter table commerce.promotional_credit_lots drop constraint promotional_credit_lots_check;
alter table commerce.promotional_credit_lots add constraint promotional_credit_lots_check
    check(available_minor+reserved_minor+consumed_minor+expired_minor+reclaimed_minor=granted_minor);

create or replace function commerce.expire_user_promotional_credit(p_user_id uuid)
returns numeric language plpgsql security definer set search_path='' as $$
declare lot commerce.promotional_credit_lots%rowtype;w commerce.wallets%rowtype;total numeric:=0;cancelled boolean;
begin
    perform 1 from commerce.users where id=p_user_id for update;
    select * into w from commerce.wallets where user_id=p_user_id and kind='customer' for update;
    if not found then return 0;end if;
    for lot in select l.* from commerce.promotional_credit_lots l where l.user_id=p_user_id and l.available_minor>0
        and (l.expires_at<=now() or exists(select 1 from commerce.promotional_campaign_cancellations x where x.campaign_id=l.campaign_id))
        order by l.expires_at,l.id for update of l loop
        if lot.wallet_id<>w.id or lot.available_minor>w.promotional_available_minor or lot.available_minor>w.available_minor then
            raise exception 'Promotional attribution differs from wallet';end if;
        cancelled:=exists(select 1 from commerce.promotional_campaign_cancellations where campaign_id=lot.campaign_id);
        perform commerce.post_pair(
            case when cancelled then 'promotion:reclaim:'||lot.id||':'||(lot.reclaimed_minor+lot.available_minor)
                 else 'promotion:expire:'||lot.id||':'||(lot.expired_minor+lot.available_minor) end,
            'adjustment',null,'wallet:'||w.id||':available',
            case when cancelled then 'platform:promotion' else 'platform:expired_promotion' end,lot.available_minor,w.id,null);
        update commerce.wallets set available_minor=available_minor-lot.available_minor,
            promotional_available_minor=promotional_available_minor-lot.available_minor where id=w.id;
        update commerce.promotional_credit_lots set available_minor=0,
            reclaimed_minor=reclaimed_minor+case when cancelled then lot.available_minor else 0 end,
            expired_minor=expired_minor+case when cancelled then 0 else lot.available_minor end where id=lot.id;
        total:=total+lot.available_minor;
        w.available_minor:=w.available_minor-lot.available_minor;
        w.promotional_available_minor:=w.promotional_available_minor-lot.available_minor;
    end loop;
    return total;
end $$;

create or replace function commerce_rpc.expire_promotional_credit(p_limit integer default 100)
returns jsonb language plpgsql security definer set search_path='' as $$
declare uid uuid;total numeric:=0;count integer:=0;
begin
    if p_limit is null or p_limit not between 1 and 500 then raise exception 'Bounded expiry batch required' using errcode='22023';end if;
    for uid in select distinct l.user_id from commerce.promotional_credit_lots l where l.available_minor>0
        and (l.expires_at<=now() or exists(select 1 from commerce.promotional_campaign_cancellations x where x.campaign_id=l.campaign_id))
        order by l.user_id limit p_limit loop
        total:=total+commerce.expire_user_promotional_credit(uid);count:=count+1;
    end loop;
    return jsonb_build_object('users',count,'expired_minor',total);
end $$;

create or replace function commerce_rpc.wallet_state(p_telegram_user_id bigint)
returns jsonb language plpgsql security definer set search_path='' as $$
declare uid uuid;result jsonb;promo jsonb;account_id bigint;
begin
    select id,legacy_account_id into uid,account_id from commerce.users where telegram_user_id=p_telegram_user_id;
    if found then perform commerce.expire_user_promotional_credit(uid);end if;
    result:=commerce.wallet_state_before_promotion_expiry(p_telegram_user_id);
    select jsonb_build_object('expired_promotional_minor',coalesce(sum(expired_minor),0),
        'reclaimed_promotional_minor',coalesce(sum(reclaimed_minor),0),
        'next_promotional_expiry',min(expires_at) filter(where available_minor>0 and expires_at>now())) into promo
        from commerce.promotional_credit_lots where user_id=uid;
    return result||jsonb_build_object('legacy_account_id',account_id,'customer',
        (result->'customer')||promo||jsonb_build_object('cash_available_minor',
            (result->'customer'->>'available_minor')::numeric-(result->'customer'->>'promotional_available_minor')::numeric));
end $$;

create function commerce_rpc.promotion_cancellation_record(p_actor_telegram_id bigint,p_campaign_id uuid)
returns jsonb language plpgsql stable security definer set search_path='' as $$
declare c commerce.campaigns%rowtype;result jsonb;
begin
    perform commerce.require_sheet_admin(p_actor_telegram_id);
    select * into c from commerce.campaigns where id=p_campaign_id and funder_kind='platform' and benefit_kind='wallet_credit';
    if not found then raise exception 'Platform promotional credit campaign required' using errcode='22023';end if;
    select jsonb_build_object('available_minor',coalesce(sum(available_minor),0),
        'reserved_minor',coalesce(sum(reserved_minor),0),'reclaimed_minor',coalesce(sum(reclaimed_minor),0)) into result
        from commerce.promotional_credit_lots where campaign_id=c.id;
    return result||jsonb_build_object('campaign_id',c.id,'code',c.rules->>'code','state',c.state,
        'cancelled',exists(select 1 from commerce.promotional_campaign_cancellations where campaign_id=c.id));
end $$;

create function commerce_rpc.cancel_promotional_campaign(p_actor_telegram_id bigint,p_campaign_id uuid,p_reference text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare c commerce.campaigns%rowtype;replayed boolean;
begin
    perform commerce.require_sheet_admin(p_actor_telegram_id);
    if p_reference is null or length(btrim(p_reference)) not between 1 and 1000 then
        raise exception 'Reviewed promotional cancellation required' using errcode='22023';end if;
    select * into c from commerce.campaigns where id=p_campaign_id and funder_kind='platform' and benefit_kind='wallet_credit' for update;
    if not found then raise exception 'Platform promotional credit campaign required' using errcode='22023';end if;
    replayed:=exists(select 1 from commerce.promotional_campaign_cancellations where campaign_id=c.id);
    if not replayed then
        if c.state not in ('active','paused') then raise exception 'Active or paused promotion required' using errcode='22023';end if;
        insert into commerce.promotional_campaign_cancellations(campaign_id,actor_telegram_id,reference)
            values(c.id,p_actor_telegram_id,p_reference);
        update commerce.campaigns set state='ended' where id=c.id;
        update commerce.codes set active=false where campaign_id=c.id and kind='discount';
    end if;
    -- Commit this decision before reclaiming wallets: activation locks user then
    -- campaign, while expiry/reclamation locks user then wallet then grant.
    return commerce_rpc.promotion_cancellation_record(p_actor_telegram_id,c.id)||jsonb_build_object('replayed',replayed);
end $$;

create function commerce_rpc.reclaim_cancelled_promotion(p_actor_telegram_id bigint,p_campaign_id uuid,p_limit integer default 100)
returns jsonb language plpgsql security definer set search_path='' as $$
declare uid uuid;
begin
    perform commerce.require_sheet_admin(p_actor_telegram_id);
    if p_limit is null or p_limit not between 1 and 500 then raise exception 'Bounded reclaim batch required' using errcode='22023';end if;
    if not exists(select 1 from commerce.promotional_campaign_cancellations where campaign_id=p_campaign_id) then
        raise exception 'Cancelled promotion required' using errcode='22023';end if;
    for uid in select distinct user_id from commerce.promotional_credit_lots where campaign_id=p_campaign_id and available_minor>0
        order by user_id limit p_limit loop
        perform commerce.expire_user_promotional_credit(uid);
    end loop;
    return commerce_rpc.promotion_cancellation_record(p_actor_telegram_id,p_campaign_id);
end $$;

create or replace function commerce_rpc.list_campaigns(p_offset integer default 0)
returns jsonb language plpgsql stable security definer set search_path='' as $$
begin
    if p_offset is null or p_offset not between 0 and 1000000 then raise exception 'Invalid campaign page' using errcode='22023';end if;
    return coalesce((select jsonb_agg(to_jsonb(c)) from (select ca.*,x.cancelled_at as promotion_cancelled_at,
        (select co.code from commerce.codes co where co.campaign_id=ca.id limit 1) as code,
        (select count(*) from commerce.campaign_usage where campaign_id=ca.id) as activations,
        (select coalesce(sum(discount_minor),0) from commerce.campaign_usage where campaign_id=ca.id) as used_discount_minor,
        (select coalesce(sum(reserved_discount_minor),0) from commerce.campaign_usage where campaign_id=ca.id) as held_discount_minor,
        (select coalesce(sum(reclaimed_minor),0) from commerce.promotional_credit_lots where campaign_id=ca.id) as reclaimed_promotional_minor
        from commerce.campaigns ca left join commerce.promotional_campaign_cancellations x on x.campaign_id=ca.id
        order by ca.created_at desc,ca.id desc limit 10 offset p_offset) c),'[]'::jsonb);
end $$;

revoke all on function commerce_rpc.promotion_cancellation_record(bigint,uuid),
    commerce_rpc.cancel_promotional_campaign(bigint,uuid,text),commerce_rpc.reclaim_cancelled_promotion(bigint,uuid,integer) from public,anon,authenticated;
grant execute on function commerce_rpc.promotion_cancellation_record(bigint,uuid),
    commerce_rpc.cancel_promotional_campaign(bigint,uuid,text),commerce_rpc.reclaim_cancelled_promotion(bigint,uuid,integer) to service_role;
notify pgrst,'reload schema';
commit;
