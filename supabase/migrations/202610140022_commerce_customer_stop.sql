-- Stop belongs to one paying customer; unsettled rows are refunded after delivery.
begin;
alter table commerce.orders add column customer_stop_requested_at timestamptz;
alter table commerce.orders add column customer_stop_reference text;
alter table commerce.checks drop constraint checks_state_check;
alter table commerce.checks add constraint checks_state_check
    check(state in ('pending','running','deferred','succeeded','failed','review','not_found','cancelled'));
alter table commerce.checks drop constraint checks_check1;
alter table commerce.checks add constraint checks_terminal_timestamp_check
    check((state in ('succeeded','failed','review','not_found','cancelled'))=(terminal_at is not null));

create function commerce_rpc.request_customer_stop(p_order_id uuid,p_telegram_user_id bigint,p_reference text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare o commerce.orders%rowtype;
begin
    perform 1 from commerce.users where telegram_user_id=p_telegram_user_id and registration_state='active' for update;
    if not found then raise exception 'Active registered customer required' using errcode='22023';end if;
    select ord.* into o from commerce.orders ord join commerce.users u on u.id=ord.user_id
        where ord.id=p_order_id and u.telegram_user_id=p_telegram_user_id for update of ord;
    if not found or p_reference is null or length(btrim(p_reference)) not between 1 and 1000 then
        raise exception 'Owned stop request with an authenticated confirmation required' using errcode='22023';end if;
    if o.order_state='closed' then return jsonb_build_object('state','closed');end if;
    if o.payment_state<>'reserved' or o.refund_consent_reference is null then
        raise exception 'Stop requires a funded request with wallet-refund consent' using errcode='22023';end if;
    if o.customer_stop_requested_at is null then
        update commerce.orders set customer_stop_requested_at=now(),customer_stop_reference=p_reference where id=o.id;
    end if;
    return jsonb_build_object('state','draining','order_id',o.id,'job_id',o.checker_job_id);
end $$;

create or replace function commerce_rpc.record_terminal_results(p_order_id uuid,p_job_id text,p_rows jsonb,p_result_sha256 text)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare o commerce.orders%rowtype; canonical jsonb; existing jsonb;
begin
    if p_job_id is null or length(p_job_id) not between 1 and 120
       or p_result_sha256 is null or p_result_sha256 !~ '^[a-f0-9]{64}$'
       or jsonb_typeof(p_rows) is distinct from 'array' then raise exception 'Invalid terminal payload' using errcode = '22023'; end if;
    if exists (select 1 from jsonb_array_elements(p_rows) r where jsonb_typeof(r) is distinct from 'object') then
        raise exception 'Terminal rows must be objects' using errcode = '22023'; end if;
    if exists (select 1 from jsonb_array_elements(p_rows) r where
        (select count(*) from jsonb_object_keys(r)) <> 3
        or jsonb_typeof(r->'row_number') is distinct from 'number' or coalesce(r->>'row_number','') !~ '^[1-9][0-9]{0,18}$'
        or coalesce(r->>'state','') not in ('succeeded','failed','review','not_found','cancelled')
        or jsonb_typeof(r->'verified_result') is distinct from 'boolean') then
        raise exception 'Invalid terminal row identity/state/verification' using errcode = '22023'; end if;
    if exists (select 1 from jsonb_array_elements(p_rows) r where (r->>'row_number')::numeric > 9223372036854775807
        or (r->>'state' in ('succeeded','not_found') and not (r->>'verified_result')::boolean)
        or (r->>'state' in ('failed','cancelled') and (r->>'verified_result')::boolean)) then
        raise exception 'Contradictory terminal result' using errcode = '22023'; end if;
    select * into o from commerce.orders where id = p_order_id for update;
    if not found or o.checker_job_id is null or o.checker_job_id is distinct from p_job_id or o.payment_state not in ('reserved','settled','partial_refund','refunded') then
        raise exception 'Unfunded or mismatched terminal job' using errcode = '22023'; end if;
    if o.customer_stop_requested_at is null and exists(select 1 from jsonb_array_elements(p_rows) r where r->>'state'='cancelled') then
        raise exception 'Cancelled rows require an owned customer stop receipt' using errcode='22023';end if;
    if jsonb_array_length(p_rows) <> o.requested_checks
       or (select count(distinct (r->>'row_number')::bigint) from jsonb_array_elements(p_rows) r) <> o.requested_checks
       or exists (select 1 from jsonb_array_elements(p_rows) r where not exists
           (select 1 from commerce.checks c where c.order_id = o.id and c.row_number = (r->>'row_number')::bigint)) then
        raise exception 'Terminal rows must cover the exact order input' using errcode = '22023'; end if;
    select jsonb_agg(r order by (r->>'row_number')::bigint) into canonical from jsonb_array_elements(p_rows) r;
    if o.execution_state = 'terminal' then
        select jsonb_agg(jsonb_build_object('row_number',row_number,'state',state,'verified_result',verified_result)
            order by row_number) into existing from commerce.checks where order_id = o.id;
        if existing <> canonical or o.result_sha256 <> p_result_sha256 then
            raise exception 'Conflicting terminal replay' using errcode = '22023'; end if;
        return jsonb_build_object('state','terminal','replayed',true);
    end if;
    update commerce.checks c set state = r->>'state',verified_result = (r->>'verified_result')::boolean,
        terminal_at = now(),outcome_version = 1 from jsonb_array_elements(canonical) r
        where c.order_id = o.id and c.row_number = (r->>'row_number')::bigint;
    update commerce.orders set execution_state = 'terminal',execution_terminal_at = now(),result_sha256 = p_result_sha256,
        delivery_state = 'ready' where id = o.id;
    return jsonb_build_object('state','terminal','replayed',false);
end $$;

create or replace function commerce_rpc.checkout_state(p_order_id uuid,p_telegram_user_id bigint)
returns jsonb language plpgsql stable security definer set search_path='' as $$
declare o commerce.orders%rowtype;r commerce.reservations%rowtype;
begin
    select ord.* into o from commerce.orders ord join commerce.users u on u.id=ord.user_id
        where ord.id=p_order_id and u.telegram_user_id=p_telegram_user_id;
    if not found then raise exception 'Unknown or unowned checkout' using errcode='22023';end if;
    select * into r from commerce.reservations where order_id=o.id;
    return jsonb_build_object('order_id',o.id,'quoted_minor',o.quoted_minor,'requested_checks',o.requested_checks,
        'pricing_snapshot',o.pricing_snapshot,'source_snapshot',o.source_snapshot,'order_state',o.order_state,
        'payment_state',o.payment_state,'checker_job_id',o.checker_job_id,'reservation_state',r.state,
        'payment_id',r.payment_id,'refund_consent_reference',o.refund_consent_reference,
        'execution_state',o.execution_state,'delivery_state',o.delivery_state,
        'customer_stop_requested_at',o.customer_stop_requested_at,'customer_stop_reference',o.customer_stop_reference,
        'charged_minor',o.final_charge_minor,'refunded_minor',case when o.final_charge_minor is null then null else o.quoted_minor-o.final_charge_minor end,
        'cancelled_count',(select count(*) from commerce.checks where order_id=o.id and state='cancelled'));
end $$;

revoke all on function commerce_rpc.request_customer_stop(uuid,bigint,text) from public,anon,authenticated;
grant execute on function commerce_rpc.request_customer_stop(uuid,bigint,text) to service_role;
create or replace function commerce_rpc.health()
returns jsonb language sql stable security definer set search_path='' as $$
    select jsonb_build_object('status','ok','schema_version',22,'currency','EGP')
$$;
notify pgrst,'reload schema';
commit;
