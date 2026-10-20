-- Per-partner access to flat-price sheets, independently of referral/earnings roles.
begin;
create table commerce.partner_sheet_access_settings (
    version bigint generated always as identity primary key,
    partner_id uuid not null references commerce.partners(id),
    enabled boolean not null,
    actor_telegram_id bigint not null check(actor_telegram_id>0),
    reference text not null unique check(length(btrim(reference)) between 1 and 300),
    created_at timestamptz not null default now()
);
create index partner_sheet_access_latest on commerce.partner_sheet_access_settings(partner_id,version desc);
alter table commerce.partner_sheet_access_settings enable row level security;
revoke all on commerce.partner_sheet_access_settings from public,anon,authenticated,service_role;

create function commerce.partner_sheet_access_enabled(p_partner_id uuid) returns boolean
language sql stable security definer set search_path='' as $$
    select coalesce((select enabled from commerce.partner_sheet_access_settings where partner_id=p_partner_id order by version desc limit 1),true)
$$;
revoke all on function commerce.partner_sheet_access_enabled(uuid) from public,anon,authenticated,service_role;

create function commerce_rpc.partner_sheet_access(p_actor_telegram_id bigint,p_partner_id uuid) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare result jsonb;
begin
    perform commerce.require_sheet_admin(p_actor_telegram_id);
    select jsonb_build_object('partner_id',p.id,'telegram_user_id',u.telegram_user_id,'state',p.state,
        'code',(select code from commerce.codes where partner_id=p.id and kind='referral' limit 1),
        'enabled',commerce.partner_sheet_access_enabled(p.id),
        'effective',p.state='active' and p.ended_at is null and u.registration_state='active' and commerce.partner_sheet_access_enabled(p.id))
        into result from commerce.partners p join commerce.users u on u.id=p.user_id where p.id=p_partner_id;
    if result is null then raise exception 'Unknown partner' using errcode='22023';end if;
    return result;
end $$;

create function commerce_rpc.configure_partner_sheet_access(p_actor_telegram_id bigint,p_partner_id uuid,p_enabled boolean,p_reference text) returns jsonb
language plpgsql security definer set search_path='' as $$
declare existing commerce.partner_sheet_access_settings%rowtype;
begin
    perform commerce.require_sheet_admin(p_actor_telegram_id);
    if p_enabled is null or p_reference is null or length(btrim(p_reference)) not between 1 and 300 then
        raise exception 'Reviewed partner sheet access required' using errcode='22023';end if;
    -- New quotes and sheet approvals take a share lock on the same partner.
    perform 1 from commerce.partners where id=p_partner_id for update;
    if not found then raise exception 'Unknown partner' using errcode='22023';end if;
    select * into existing from commerce.partner_sheet_access_settings where reference=p_reference;
    if found then
        if existing.partner_id<>p_partner_id or existing.enabled<>p_enabled or existing.actor_telegram_id<>p_actor_telegram_id then
            raise exception 'Conflicting partner sheet access replay' using errcode='22023';end if;
    else
        insert into commerce.partner_sheet_access_settings(partner_id,enabled,actor_telegram_id,reference)
            values(p_partner_id,p_enabled,p_actor_telegram_id,p_reference);
    end if;
    -- Replaying an old click reports today's setting, without undoing a later decision.
    return commerce_rpc.partner_sheet_access(p_actor_telegram_id,p_partner_id);
end $$;

alter function commerce_rpc.partner_sheet_context(bigint) rename to partner_sheet_context_before_access;
alter function commerce_rpc.partner_sheet_context_before_access(bigint) set schema commerce;
revoke all on function commerce.partner_sheet_context_before_access(bigint) from public,anon,authenticated,service_role;
create function commerce_rpc.partner_sheet_context(p_telegram_user_id bigint) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare result jsonb;pid uuid;enabled boolean;
begin
    result:=commerce.partner_sheet_context_before_access(p_telegram_user_id);
    select p.id into pid from commerce.partners p join commerce.users u on u.id=p.user_id where u.telegram_user_id=p_telegram_user_id;
    enabled:=commerce.partner_sheet_access_enabled(pid);
    return result||jsonb_build_object('partner_sheet_enabled',enabled,'partner_sheet_allowed',coalesce((result->>'is_partner')::boolean,false) and enabled);
end $$;

alter function commerce_rpc.list_partners(integer) rename to list_partners_before_sheet_access;
alter function commerce_rpc.list_partners_before_sheet_access(integer) set schema commerce;
revoke all on function commerce.list_partners_before_sheet_access(integer) from public,anon,authenticated,service_role;
create function commerce_rpc.list_partners(p_offset integer default 0) returns jsonb
language sql stable security definer set search_path='' as $$
    select coalesce(jsonb_agg(item||jsonb_build_object('partner_sheet_enabled',commerce.partner_sheet_access_enabled((item->>'id')::uuid)) order by position),'[]'::jsonb)
        from jsonb_array_elements(commerce.list_partners_before_sheet_access(p_offset)) with ordinality as page(item,position)
$$;

create or replace function commerce_rpc.create_order(p_telegram_user_id bigint,p_request_key text,p_rows jsonb,p_source jsonb) returns jsonb
language plpgsql security definer set search_path='' as $$
declare uid uuid;pid uuid;result jsonb;oid uuid;pricing jsonb;
begin
    uid:=(commerce_rpc.register_telegram_user(p_telegram_user_id,null)->>'user_id')::uuid;
    perform 1 from commerce.users where id=uid for update;
    if exists(select 1 from commerce.orders where request_key=p_request_key) then
        return commerce.create_order(p_telegram_user_id,p_request_key,p_rows,p_source);end if;
    select id into pid from commerce.partners where user_id=uid and state='active' for share;
    if not found or not commerce.partner_sheet_access_enabled(pid) then
        return commerce.create_order_with_campaigns(p_telegram_user_id,p_request_key,p_rows,p_source);end if;
    result:=commerce.create_order(p_telegram_user_id,p_request_key,p_rows,p_source);
    oid:=(result->>'order_id')::uuid;
    select jsonb_build_object('policy_version',pricing_snapshot->'policy_version','partner_cost_sheet',true,
        'fixed_scope','sheet','financial_precision','exact_decimal','total_minor',0,'discount_total_minor',0,
        'refund_destination','wallet','refund_requires_consent',true,'approval_required',true) into pricing from commerce.orders where id=oid;
    update commerce.orders set quoted_minor=0,pricing_snapshot=pricing,partner_terms_snapshot='{}',
        referral_partner_id=null,campaigns_evaluated_at=now() where id=oid;
    update commerce.checks set unit_quote_minor=0,discount_minor=0 where order_id=oid;
    insert into commerce.partner_sheet_requests(order_id,partner_id) values(oid,pid);
    insert into commerce.partner_sheet_notifications(event_key,recipient_telegram_id,payload)
        select 'request:'||oid,telegram_user_id,jsonb_build_object('kind','request','order_id',oid,'partner_telegram_id',p_telegram_user_id)
        from commerce.support_actors where active;
    return result||jsonb_build_object('quoted_minor',0,'partner_sheet_state','pending');
end $$;

create function commerce.partner_sheet_access_blocked(p_order_id uuid) returns boolean
language sql stable security definer set search_path='' as $$
    select exists(select 1 from commerce.partner_sheet_requests r where r.order_id=p_order_id
        and not commerce.partner_sheet_access_enabled(r.partner_id)
        and not exists(select 1 from commerce.reservations where order_id=p_order_id)
        and not exists(select 1 from commerce.payment_requests where order_id=p_order_id))
$$;
revoke all on function commerce.partner_sheet_access_blocked(uuid) from public,anon,authenticated,service_role;

create or replace function commerce.require_approved_partner_sheet(p_order_id uuid) returns void
language plpgsql stable security definer set search_path='' as $$
begin
    if commerce.partner_sheet_access_blocked(p_order_id) then
        raise exception 'Partner-priced sheets disabled' using errcode='42501';end if;
    if exists(select 1 from commerce.partner_sheet_requests where order_id=p_order_id) and not exists(
        select 1 from commerce.partner_sheet_requests r join commerce.orders o on o.id=r.order_id
            join commerce.partners p on p.id=r.partner_id join commerce.users u on u.id=p.user_id
        where r.order_id=p_order_id and r.state='approved' and r.amount_minor=o.quoted_minor
            and p.state='active' and u.registration_state='active') then
        raise exception 'Owner approval required for each partner sheet' using errcode='42501';end if;
end $$;

create function commerce.guard_partner_sheet_access_approval() returns trigger
language plpgsql security definer set search_path='' as $$
begin
    if new.state='approved' and old.state='pending' and not commerce.partner_sheet_access_enabled(new.partner_id) then
        raise exception 'Partner-priced sheets disabled' using errcode='42501';end if;
    return new;
end $$;
revoke all on function commerce.guard_partner_sheet_access_approval() from public,anon,authenticated,service_role;
create trigger guard_partner_sheet_access_approval before update of state on commerce.partner_sheet_requests
    for each row execute function commerce.guard_partner_sheet_access_approval();

create function commerce.filter_partner_sheet_price_notice() returns trigger
language plpgsql security definer set search_path='' as $$
begin
    if new.payload->>'kind'='price' and exists(select 1 from commerce.partners p join commerce.users u on u.id=p.user_id
        where u.telegram_user_id=new.recipient_telegram_id and not commerce.partner_sheet_access_enabled(p.id)) then
        return null;end if;
    return new;
end $$;
revoke all on function commerce.filter_partner_sheet_price_notice() from public,anon,authenticated,service_role;
create trigger filter_partner_sheet_price_notice before insert on commerce.partner_sheet_notifications
    for each row execute function commerce.filter_partner_sheet_price_notice();

alter function commerce_rpc.checkout_state(uuid,bigint) rename to checkout_state_before_sheet_access;
alter function commerce_rpc.checkout_state_before_sheet_access(uuid,bigint) set schema commerce;
revoke all on function commerce.checkout_state_before_sheet_access(uuid,bigint) from public,anon,authenticated,service_role;
create function commerce_rpc.checkout_state(p_order_id uuid,p_telegram_user_id bigint) returns jsonb
language plpgsql security definer set search_path='' as $$
begin
    return commerce.checkout_state_before_sheet_access(p_order_id,p_telegram_user_id)
        ||jsonb_build_object('partner_sheet_access_blocked',commerce.partner_sheet_access_blocked(p_order_id));
end $$;

revoke all on function commerce_rpc.partner_sheet_access(bigint,uuid),commerce_rpc.configure_partner_sheet_access(bigint,uuid,boolean,text),
    commerce_rpc.partner_sheet_context(bigint),commerce_rpc.list_partners(integer),commerce_rpc.checkout_state(uuid,bigint) from public,anon,authenticated;
grant execute on function commerce_rpc.partner_sheet_access(bigint,uuid),commerce_rpc.configure_partner_sheet_access(bigint,uuid,boolean,text),
    commerce_rpc.partner_sheet_context(bigint),commerce_rpc.list_partners(integer),commerce_rpc.checkout_state(uuid,bigint) to service_role;
notify pgrst,'reload schema';
commit;
