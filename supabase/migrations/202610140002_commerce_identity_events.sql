-- A02: identity and both wallets are created in one transaction.
-- A05-A06: durable event inbox rejects conflicting replay; no money is settled.
begin;
create function commerce_rpc.register_telegram_user(p_telegram_user_id bigint, p_username text default null)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare v_user commerce.users%rowtype;
begin
    if p_telegram_user_id is null or p_telegram_user_id <= 0 then
        raise exception 'Positive Telegram user ID required' using errcode = '22023';
    end if;
    if length(p_username) > 64 then
        raise exception 'Telegram username too long' using errcode = '22023';
    end if;
    insert into commerce.users(telegram_user_id,telegram_username)
    values (p_telegram_user_id,p_username)
    on conflict (telegram_user_id) do update
        set telegram_username = coalesce(excluded.telegram_username,commerce.users.telegram_username)
    returning * into v_user;
    insert into commerce.wallets(user_id,kind)
    values (v_user.id,'customer'),(v_user.id,'partner')
    on conflict (user_id,kind,currency) do nothing;
    return jsonb_build_object('user_id',v_user.id,'telegram_user_id',v_user.telegram_user_id,
        'first_used_at',v_user.first_used_at,'registration_state',v_user.registration_state,
        'first_paid_service_at',v_user.first_paid_service_at);
end $$;

create function commerce_rpc.receive_checker_event(
    p_order_id uuid, p_source text, p_event_key text, p_kind text, p_payload jsonb
) returns uuid language plpgsql security definer set search_path = '' as $$
declare v_event commerce.events%rowtype; v_job text;
begin
    if p_source is distinct from 'zain_checker' or p_kind not in ('check_outcome','execution_terminal','result_delivery')
       or p_kind is null or p_event_key is null or length(p_event_key) not between 1 and 160
       or jsonb_typeof(p_payload) is distinct from 'object' then
        raise exception 'Invalid checker event envelope' using errcode = '22023';
    end if;
    select checker_job_id into v_job from commerce.orders where id = p_order_id;
    if not found then raise exception 'Unknown order' using errcode = '22023'; end if;
    if v_job is null or (p_payload->>'job_id') is distinct from v_job
       or (p_payload->>'schema_version') is distinct from '1' then
        raise exception 'Order/job mismatch or unsupported event version' using errcode = '22023';
    end if;
    insert into commerce.events(source,event_key,order_id,kind,payload)
    values (p_source,p_event_key,p_order_id,p_kind,p_payload)
    on conflict (source,event_key) do nothing
    returning * into v_event;
    if not found then
        select * into v_event from commerce.events where source = p_source and event_key = p_event_key;
        if v_event.order_id is distinct from p_order_id or v_event.kind is distinct from p_kind
           or v_event.payload is distinct from p_payload then
            raise exception 'Conflicting event replay' using errcode = '22023';
        end if;
    end if;
    return v_event.id;
end $$;

revoke all on function commerce_rpc.register_telegram_user(bigint,text) from public,anon,authenticated;
revoke all on function commerce_rpc.receive_checker_event(uuid,text,text,text,jsonb) from public,anon,authenticated;
grant execute on function commerce_rpc.register_telegram_user(bigint,text) to service_role;
grant execute on function commerce_rpc.receive_checker_event(uuid,text,text,text,jsonb) to service_role;
commit;
