-- Robust partner addition and reactivation; eliminates foreign code collisions and handles ended partners seamlessly.
begin;

create or replace function commerce_rpc.add_partner_with_notice(
    p_actor_telegram_id bigint,
    p_telegram_user_id bigint,
    p_code text
) returns jsonb
language plpgsql security definer set search_path='' as $$
declare
    v_code text;
    v_user uuid;
    v_partner uuid;
    partner_rec commerce.partners%rowtype;
    event jsonb;
    notice uuid;
    was_ended boolean := false;
    was_existing boolean := false;
begin
    perform commerce.require_sheet_admin(p_actor_telegram_id);

    v_code := upper(btrim(p_code));
    if v_code is null or v_code !~ '^[A-Z0-9_-]{3,48}$' then
        raise exception 'Invalid referral code' using errcode = '22023';
    end if;

    v_user := (commerce_rpc.register_telegram_user(p_telegram_user_id,null)->>'user_id')::uuid;
    perform 1 from commerce.users where id = v_user for update;

    select * into partner_rec from commerce.partners where user_id = v_user for update;

    if partner_rec.id is not null then
        was_existing := true;
        v_partner := partner_rec.id;

        -- Check if code is taken by ANOTHER partner
        if exists (select 1 from commerce.codes where code = v_code and partner_id <> v_partner) then
            raise exception 'Code is assigned elsewhere' using errcode = '22023';
        end if;

        -- Update or insert referral code for this partner
        update commerce.codes set code = v_code where partner_id = v_partner and kind = 'referral';
        if not found then
            insert into commerce.codes(code, kind, partner_id, active) values (v_code, 'referral', v_partner, false);
        end if;

        -- If partner was ended, reactivate them directly!
        if partner_rec.ended_at is not null then
            was_ended := true;
            update commerce.partners
            set ended_at = null,
                ended_by_telegram_id = null,
                state = 'pending'
            where id = v_partner;

            event := commerce.record_partner_lifecycle(v_partner, 'reactivated', p_actor_telegram_id,
                'partner-reactivated:' || v_partner || ':' || extract(epoch from now())::bigint);
            notice := (event->>'id')::uuid;
        else
            select id into notice from commerce.partner_lifecycle_events where partner_id = v_partner and kind in ('added','reactivated') order by occurred_at desc limit 1;
        end if;

        return jsonb_build_object(
            'partner_id', v_partner,
            'code', v_code,
            'state', (select state from commerce.partners where id = v_partner),
            'reactivated', was_ended,
            'already_partner', not was_ended,
            'notice_event_id', notice
        );
    else
        -- Completely new partner
        if exists (select 1 from commerce.codes where code = v_code) then
            raise exception 'Code is assigned elsewhere' using errcode = '22023';
        end if;

        insert into commerce.partners(user_id) values (v_user) returning id into v_partner;
        insert into commerce.codes(code, kind, partner_id, active) values (v_code, 'referral', v_partner, false);

        event := commerce.record_partner_lifecycle(v_partner, 'added', p_actor_telegram_id, 'partner-added:' || v_partner);
        notice := (event->>'id')::uuid;

        return jsonb_build_object(
            'partner_id', v_partner,
            'code', v_code,
            'state', 'pending',
            'reactivated', false,
            'already_partner', false,
            'notice_event_id', notice
        );
    end if;
end $$;

revoke all on function commerce_rpc.add_partner_with_notice(bigint,bigint,text) from public,anon,authenticated;
grant execute on function commerce_rpc.add_partner_with_notice(bigint,bigint,text) to service_role;

create or replace function commerce_rpc.health() returns jsonb language sql stable security definer set search_path='' as $$
    select jsonb_build_object('status','ok','schema_version',42,'currency','EGP')
$$;

notify pgrst,'reload schema';
commit;
