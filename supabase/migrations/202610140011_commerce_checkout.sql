begin;
create function commerce_rpc.checkout_state(p_order_id uuid,p_telegram_user_id bigint)
returns jsonb language plpgsql stable security definer set search_path = '' as $$
declare o commerce.orders%rowtype; r commerce.reservations%rowtype;
begin
    select ord.* into o from commerce.orders ord join commerce.users u on u.id=ord.user_id
        where ord.id=p_order_id and u.telegram_user_id=p_telegram_user_id and u.registration_state='active';
    if not found then raise exception 'Unknown or unowned checkout' using errcode='22023'; end if;
    select * into r from commerce.reservations where order_id=o.id;
    return jsonb_build_object('order_id',o.id,'quoted_minor',o.quoted_minor,'requested_checks',o.requested_checks,
        'pricing_snapshot',o.pricing_snapshot,'source_snapshot',o.source_snapshot,'order_state',o.order_state,
        'payment_state',o.payment_state,'checker_job_id',o.checker_job_id,'reservation_state',r.state,
        'payment_id',r.payment_id,'refund_consent_reference',o.refund_consent_reference);
end $$;

create function commerce_rpc.checkout_consent(p_order_id uuid,p_telegram_user_id bigint,
    p_policy_version bigint,p_reference text)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare o commerce.orders%rowtype;
begin
    perform 1 from commerce.users where telegram_user_id=p_telegram_user_id and registration_state='active' for update;
    if not found then raise exception 'Unknown or suspended user' using errcode='22023'; end if;
    select ord.* into o from commerce.orders ord join commerce.users u on u.id=ord.user_id
        where ord.id=p_order_id and u.telegram_user_id=p_telegram_user_id for update of ord;
    if not found or p_reference is null or length(btrim(p_reference)) not between 1 and 1000
       or p_policy_version is null or p_policy_version is distinct from (o.pricing_snapshot->>'policy_version')::bigint
       or o.pricing_snapshot->>'refund_destination' is distinct from 'wallet'
       or o.pricing_snapshot->>'refund_requires_consent' is distinct from 'true' then
        raise exception 'Owned order and exact refund policy consent required' using errcode='22023'; end if;
    if o.refund_consent_reference is not null then
        return jsonb_build_object('reference',o.refund_consent_reference,'replayed',true);
    end if;
    if o.order_state<>'draft' or o.payment_state<>'unpaid' then
        raise exception 'Checkout is no longer awaiting consent' using errcode='22023'; end if;
    update commerce.orders set refund_consent_reference=p_reference where id=o.id;
    return jsonb_build_object('reference',p_reference,'replayed',false);
end $$;

-- Keep the established monetary implementation; enforce the saved consent at
-- its public backend boundary before allowing either funding path.
alter function commerce_rpc.reserve_order(uuid,bigint,text,uuid) set schema commerce;
revoke all on function commerce.reserve_order(uuid,bigint,text,uuid) from public,anon,authenticated,service_role;
create function commerce_rpc.reserve_order(p_order_id uuid,p_telegram_user_id bigint,
    p_consent_reference text,p_payment_id uuid default null)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare o commerce.orders%rowtype;
begin
    perform 1 from commerce.users where telegram_user_id=p_telegram_user_id and registration_state='active' for update;
    select ord.* into o from commerce.orders ord join commerce.users u on u.id=ord.user_id
        where ord.id=p_order_id and u.telegram_user_id=p_telegram_user_id for update of ord;
    if found and o.refund_consent_reference is not null
       and o.refund_consent_reference is distinct from p_consent_reference then
        raise exception 'Reservation consent differs from saved checkout consent' using errcode='22023'; end if;
    return commerce.reserve_order(p_order_id,p_telegram_user_id,p_consent_reference,p_payment_id);
end $$;
create or replace function commerce_rpc.health()
returns jsonb language sql stable security definer set search_path = '' as $$
    select jsonb_build_object('status','ok','schema_version',11,'currency','EGP')
$$;
revoke all on function commerce_rpc.checkout_state(uuid,bigint),
    commerce_rpc.checkout_consent(uuid,bigint,bigint,text),commerce_rpc.reserve_order(uuid,bigint,text,uuid)
    from public,anon,authenticated;
grant execute on function commerce_rpc.checkout_state(uuid,bigint),
    commerce_rpc.checkout_consent(uuid,bigint,bigint,text),commerce_rpc.reserve_order(uuid,bigint,text,uuid)
    to service_role;
notify pgrst,'reload schema';
commit;
