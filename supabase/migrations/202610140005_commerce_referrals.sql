-- A08-A15: one attribution path for deep links and manually entered codes.
-- Partners start pending; activating terms is an explicit administrative action.
begin;
create unique index one_referral_code_per_partner on commerce.codes(partner_id) where kind = 'referral';

create function commerce_rpc.create_partner(p_telegram_user_id bigint, p_code text)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare v_user uuid; v_partner uuid; v_code text;
begin
    v_code := upper(btrim(p_code));
    if v_code is null or v_code !~ '^[A-Z0-9_-]{3,48}$' then
        raise exception 'Invalid referral code' using errcode = '22023';
    end if;
    v_user := (commerce_rpc.register_telegram_user(p_telegram_user_id,null)->>'user_id')::uuid;
    -- Lock the user before creating/checking partner + code across competing calls.
    perform 1 from commerce.users where id = v_user for update;
    insert into commerce.partners(user_id) values (v_user)
    on conflict (user_id) do nothing;
    select id into v_partner from commerce.partners where user_id = v_user;
    if exists (select 1 from commerce.codes where partner_id = v_partner and kind = 'referral' and code <> v_code) then
        raise exception 'Partner already has another referral code' using errcode = '22023';
    end if;
    if exists (select 1 from commerce.codes where code = v_code and
        (kind <> 'referral' or partner_id is distinct from v_partner)) then
        raise exception 'Code is assigned elsewhere' using errcode = '22023';
    end if;
    insert into commerce.codes(code,kind,partner_id) values (v_code,'referral',v_partner)
    on conflict (code) do nothing;
    -- A competing partner claiming this code must be rejected, not silently reused.
    if not exists (select 1 from commerce.codes where code = v_code and partner_id = v_partner and kind = 'referral') then
        raise exception 'Code is assigned elsewhere' using errcode = '22023';
    end if;
    return jsonb_build_object('partner_id',v_partner,'code',v_code,'state',
        (select state from commerce.partners where id = v_partner));
end $$;

create function commerce_rpc.attribute_referral(p_telegram_user_id bigint, p_code text, p_source text)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare v_user uuid; v_paid timestamptz; v_status text; v_code commerce.codes%rowtype;
        v_partner commerce.partners%rowtype; v_existing commerce.referrals%rowtype;
begin
    if p_source is null or p_source not in ('deep_link','manual') then
        raise exception 'Invalid referral source' using errcode = '22023';
    end if;
    v_user := (commerce_rpc.register_telegram_user(p_telegram_user_id,null)->>'user_id')::uuid;
    select first_paid_service_at,registration_state into v_paid,v_status
    from commerce.users where id = v_user for update;
    if v_status <> 'active' then
        return jsonb_build_object('status','user_suspended');
    end if;
    select * into v_code from commerce.codes
    where code = upper(btrim(p_code)) and kind = 'referral' and active
        and (expires_at is null or expires_at > now());
    if not found then return jsonb_build_object('status','invalid_code'); end if;
    select * into v_partner from commerce.partners where id = v_code.partner_id;
    if v_partner.state <> 'active' then return jsonb_build_object('status','inactive_partner'); end if;
    if v_partner.user_id = v_user then return jsonb_build_object('status','self_referral'); end if;
    select * into v_existing from commerce.referrals where user_id = v_user;
    if found then
        return jsonb_build_object('status',case when v_existing.partner_id = v_partner.id then 'already_attributed' else 'first_source_preserved' end,
            'partner_id',v_existing.partner_id,'locked_at',v_existing.locked_at);
    end if;
    if v_paid is not null then return jsonb_build_object('status','prior_paid_service'); end if;
    if v_code.max_redemptions is not null then
        -- Serialize global code cap; user lock was acquired first consistently.
        perform 1 from commerce.codes where id = v_code.id for update;
        if (select count(*) from commerce.referrals where code_id = v_code.id) >= v_code.max_redemptions then
            return jsonb_build_object('status','code_exhausted');
        end if;
    end if;
    insert into commerce.referrals(user_id,partner_id,code_id,source,policy_version)
    values (v_user,v_partner.id,v_code.id,p_source,'first_valid_source_v1');
    return jsonb_build_object('status','attributed','partner_id',v_partner.id,'locked_at',null);
end $$;
revoke all on function commerce_rpc.create_partner(bigint,text), commerce_rpc.attribute_referral(bigint,text,text) from public,anon,authenticated;
grant execute on function commerce_rpc.create_partner(bigint,text), commerce_rpc.attribute_referral(bigint,text,text) to service_role;
commit;
