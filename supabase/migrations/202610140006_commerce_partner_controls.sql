begin;
create table commerce.partner_terms_history (
    version uuid primary key default gen_random_uuid(),
    partner_id uuid not null references commerce.partners(id),
    actor_telegram_id bigint not null check (actor_telegram_id > 0),
    terms_snapshot jsonb not null,
    created_at timestamptz not null default now()
);
alter table commerce.partner_terms_history enable row level security;
revoke all on commerce.partner_terms_history from public,anon,authenticated,service_role;

create function commerce_rpc.configure_partner(p_partner_id uuid,p_actor_telegram_id bigint,
    p_basis_points integer,p_mode text,p_limit bigint default null,p_activate boolean default false)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare v_version uuid; v_partner commerce.partners%rowtype; v_snapshot jsonb;
begin
    if p_actor_telegram_id is null or p_actor_telegram_id <= 0
       or p_basis_points is null or p_basis_points not between 0 and 10000
       or p_activate is null or p_mode is null or p_mode not in ('checks','duration','forever')
       or (p_mode = 'forever' and p_limit is not null)
       or (p_mode <> 'forever' and (p_limit is null or p_limit <= 0)) then
        raise exception 'Invalid partner terms' using errcode = '22023';
    end if;
    select * into v_partner from commerce.partners where id = p_partner_id for update;
    if not found then raise exception 'Unknown partner' using errcode = '22023'; end if;
    v_snapshot := jsonb_build_object('basis_points',p_basis_points,'mode',p_mode,'limit',p_limit,'active',p_activate);
    -- Repeating a confirmation does not create another terms version.
    if v_partner.terms = v_snapshot and v_partner.approved_at is not null then
        return jsonb_build_object('partner_id',p_partner_id,'version',v_partner.terms_version,'state',v_partner.state);
    end if;
    insert into commerce.partner_terms_history(partner_id,actor_telegram_id,terms_snapshot)
    values (p_partner_id,p_actor_telegram_id,v_snapshot) returning version into v_version;
    update commerce.partners set commission_basis_points = p_basis_points,entitlement_mode = p_mode,
        entitlement_checks = case when p_mode = 'checks' then p_limit end,
        entitlement_duration_seconds = case when p_mode = 'duration' then p_limit end,
        terms = v_snapshot,terms_version = v_version::text,approved_at = now(),
        state = case when p_activate then 'active' else 'pending' end where id = p_partner_id;
    update commerce.codes set active = p_activate where partner_id = p_partner_id and kind = 'referral';
    return jsonb_build_object('partner_id',p_partner_id,'version',v_version,'state',case when p_activate then 'active' else 'pending' end);
end $$;

create function commerce_rpc.list_partners(p_offset integer default 0)
returns jsonb language plpgsql stable security definer set search_path = '' as $$
begin
    if p_offset is null or p_offset < 0 then raise exception 'Invalid offset' using errcode = '22023'; end if;
    return coalesce((select jsonb_agg(to_jsonb(r)) from (
        select p.id,u.telegram_user_id,p.state,p.commission_basis_points,p.entitlement_mode,
            p.entitlement_checks,p.entitlement_duration_seconds,c.code,p.terms_version
        from commerce.partners p join commerce.users u on u.id = p.user_id
        left join commerce.codes c on c.partner_id = p.id and c.kind = 'referral'
        order by p.created_at,p.id limit 10 offset p_offset) r),'[]'::jsonb);
end $$;

create function commerce_rpc.referral_profile(p_telegram_user_id bigint)
returns jsonb language plpgsql stable security definer set search_path = '' as $$
declare v_user commerce.users%rowtype; v_ref commerce.referrals%rowtype; v_code text;
begin
    select * into v_user from commerce.users where telegram_user_id = p_telegram_user_id;
    if not found then return jsonb_build_object('registered',false); end if;
    select * into v_ref from commerce.referrals where user_id = v_user.id;
    select code into v_code from commerce.codes where id = v_ref.code_id;
    return jsonb_build_object('registered',true,'user_id',v_user.id,
        'first_used_at',v_user.first_used_at,'eligible',v_user.registration_state = 'active' and v_user.first_paid_service_at is null,
        'referral_code',v_code,'locked_at',v_ref.locked_at);
end $$;

create function commerce_rpc.health()
returns jsonb language sql stable security definer set search_path = '' as $$
    select jsonb_build_object('status','ok','schema_version',6,'currency','EGP')
$$;

revoke all on function commerce_rpc.configure_partner(uuid,bigint,integer,text,bigint,boolean),
    commerce_rpc.list_partners(integer),commerce_rpc.referral_profile(bigint),commerce_rpc.health()
    from public,anon,authenticated;
grant execute on function commerce_rpc.configure_partner(uuid,bigint,integer,text,bigint,boolean),
    commerce_rpc.list_partners(integer),commerce_rpc.referral_profile(bigint),commerce_rpc.health() to service_role;
commit;
