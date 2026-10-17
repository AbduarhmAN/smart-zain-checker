-- A16/A23-A26/A32: settlement requires committed terminal rows and delivery.
begin;
alter table commerce.checks add column verified_result boolean not null default false;
alter table commerce.orders add column result_sha256 text check (result_sha256 ~ '^[a-f0-9]{64}$'),
    add column execution_terminal_at timestamptz,
    add column delivery_file_id text,
    add column delivery_message_id bigint check (delivery_message_id > 0);

create function commerce_rpc.bind_checker_job(p_order_id uuid,p_job_id text)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare o commerce.orders%rowtype;
begin
    if p_job_id is null or length(p_job_id) not between 1 and 120 then raise exception 'Invalid checker job' using errcode = '22023'; end if;
    select * into o from commerce.orders where id = p_order_id for update;
    if not found then raise exception 'Unknown order' using errcode = '22023'; end if;
    if o.checker_job_id = p_job_id then return jsonb_build_object('order_id',o.id,'job_id',p_job_id); end if;
    if o.payment_state <> 'reserved' then raise exception 'Order must be funded before queueing' using errcode = '22023'; end if;
    if o.checker_job_id is not null and o.checker_job_id <> p_job_id then raise exception 'Order is bound to another job' using errcode = '22023'; end if;
    if o.checker_job_id is null then
        update commerce.orders set checker_job_id = p_job_id,execution_state = 'queued' where id = o.id;
    end if;
    return jsonb_build_object('order_id',o.id,'job_id',p_job_id);
end $$;

create function commerce_rpc.record_terminal_results(p_order_id uuid,p_job_id text,p_rows jsonb,p_result_sha256 text)
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
        or coalesce(r->>'state','') not in ('succeeded','failed','review','not_found')
        or jsonb_typeof(r->'verified_result') is distinct from 'boolean') then
        raise exception 'Invalid terminal row identity/state/verification' using errcode = '22023'; end if;
    if exists (select 1 from jsonb_array_elements(p_rows) r where (r->>'row_number')::numeric > 9223372036854775807
        or (r->>'state' in ('succeeded','not_found') and not (r->>'verified_result')::boolean)
        or (r->>'state' = 'failed' and (r->>'verified_result')::boolean)) then
        raise exception 'Contradictory terminal result' using errcode = '22023'; end if;
    select * into o from commerce.orders where id = p_order_id for update;
    if not found or o.checker_job_id is null or o.checker_job_id is distinct from p_job_id or o.payment_state not in ('reserved','settled','partial_refund','refunded') then
        raise exception 'Unfunded or mismatched terminal job' using errcode = '22023'; end if;
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

create function commerce_rpc.confirm_result_delivery(p_order_id uuid,p_telegram_user_id bigint,p_message_id bigint,
    p_file_id text,p_result_sha256 text)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare o commerce.orders%rowtype; u uuid;
begin
    if p_message_id is null or p_message_id <= 0 or p_file_id is null or length(p_file_id) not between 1 and 512 then
        raise exception 'Confirmed Telegram document receipt required' using errcode = '22023'; end if;
    select id into u from commerce.users where telegram_user_id = p_telegram_user_id;
    select * into o from commerce.orders where id = p_order_id and user_id = u for update;
    if not found or o.execution_state <> 'terminal' or o.result_sha256 is distinct from p_result_sha256 then
        raise exception 'Delivery must match an owned terminal result' using errcode = '22023'; end if;
    if o.delivery_state = 'confirmed' then
        if o.delivery_message_id <> p_message_id or o.delivery_file_id <> p_file_id then
            raise exception 'Confirmed delivery receipt differs' using errcode = '22023'; end if;
        return jsonb_build_object('state','confirmed','replayed',true);
    end if;
    update commerce.orders set delivery_state = 'confirmed',delivered_at = now(),delivery_message_id = p_message_id,
        delivery_file_id = p_file_id where id = o.id;
    return jsonb_build_object('state','confirmed','replayed',false);
end $$;

create function commerce_rpc.settle_order(p_order_id uuid)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare u uuid; o commerce.orders%rowtype; r commerce.reservations%rowtype; w commerce.wallets%rowtype;
        earned numeric; refunded numeric; total numeric; source_account text; row_data record;
        j uuid; final_state text; v_ref commerce.referrals%rowtype; v_partner commerce.partners%rowtype;
begin
    select user_id into u from commerce.orders where id = p_order_id;
    if not found then raise exception 'Unknown order' using errcode = '22023'; end if;
    perform 1 from commerce.users where id = u for update;
    select * into o from commerce.orders where id = p_order_id for update;
    if o.payment_state in ('settled','refunded','partial_refund') then
        return jsonb_build_object('state',o.payment_state,'charged_minor',o.final_charge_minor,
            'refunded_minor',o.quoted_minor-o.final_charge_minor,'replayed',true);
    end if;
    if o.payment_state <> 'reserved' or o.execution_state <> 'terminal' or o.delivery_state <> 'confirmed'
       or o.execution_terminal_at is null or o.result_sha256 is null or o.delivery_file_id is null
       or o.refund_consent_reference is null then
        raise exception 'Settlement requires funded terminal rows, delivery and consent' using errcode = '22023'; end if;
    if (select count(*) from commerce.checks where order_id = o.id and terminal_at is not null) <> o.requested_checks then
        raise exception 'Incomplete terminal coverage' using errcode = '22023'; end if;
    select sum(unit_quote_minor::numeric-discount_minor),
        coalesce(sum(unit_quote_minor::numeric-discount_minor) filter (where verified_result),0)
        into total,earned from commerce.checks where order_id = o.id;
    if total is distinct from o.quoted_minor::numeric then raise exception 'Settlement quote mismatch' using errcode = '22023'; end if;
    refunded := total-earned;
    select * into r from commerce.reservations where order_id = o.id for update;
    if not found or r.state <> 'held' or r.amount_minor <> total then raise exception 'Reservation is not held' using errcode = '22023'; end if;
    select * into w from commerce.wallets where user_id = u and kind = 'customer' and currency = 'EGP' for update;
    if r.wallet_id is not null then
        if r.wallet_id <> w.id or w.reserved_minor < r.amount_minor then raise exception 'Wallet reservation mismatch' using errcode = '22023'; end if;
        source_account := 'wallet:'||w.id||':reserved';
    else source_account := 'direct:'||o.id||':reserved'; end if;
    if earned > 0 then
        perform commerce.post_pair('service:'||o.id,'service',o.id,source_account,'platform:revenue',earned::bigint,r.wallet_id,null);
    end if;
    for row_data in select row_number,unit_quote_minor-discount_minor as amount from commerce.checks
        where order_id = o.id and not verified_result and unit_quote_minor > discount_minor loop
        j := commerce.post_pair('refund:'||o.id||':'||row_data.row_number,'refund',o.id,source_account,
            'wallet:'||w.id||':available',row_data.amount,r.wallet_id,w.id);
        insert into commerce.refunds(order_id,row_number,amount_minor,destination,policy_version,consent_reference,
            state,journal_id,idempotency_key)
        values (o.id,row_data.row_number,row_data.amount,'wallet',o.pricing_snapshot->>'policy_version',
            o.refund_consent_reference,'confirmed',j,'refund:'||o.id||':'||row_data.row_number);
    end loop;
    update commerce.wallets set available_minor = available_minor+refunded::bigint,
        reserved_minor = reserved_minor-case when r.wallet_id is not null then r.amount_minor else 0 end where id = w.id;
    update commerce.reservations set state = 'settled' where id = r.id;
    if r.payment_id is not null then update commerce.payments set state = 'settled' where id = r.payment_id; end if;
    final_state := case when refunded = 0 then 'settled' when earned = 0 then 'refunded' else 'partial_refund' end;
    if earned > 0 then
        update commerce.users set first_paid_service_at = coalesce(first_paid_service_at,now()) where id = u;
        update commerce.referrals set locked_at = coalesce(locked_at,now()) where user_id = u;
        -- An eligible source entered after quoting but before first paid service
        -- receives a terms snapshot when this first paid operation is finalized.
        if o.referral_partner_id is null then
            select * into v_ref from commerce.referrals where user_id = u;
            if found then
                select * into v_partner from commerce.partners where id = v_ref.partner_id;
                update commerce.orders set referral_partner_id = v_ref.partner_id,partner_terms_snapshot =
                    jsonb_build_object('partner_id',v_partner.id,'state',v_partner.state,'terms_version',v_partner.terms_version,
                        'basis_points',v_partner.commission_basis_points,'mode',v_partner.entitlement_mode,
                        'checks',v_partner.entitlement_checks,'duration_seconds',v_partner.entitlement_duration_seconds,
                        'anchor','first_paid_service','count_basis','successful_paid_checks') where id = o.id;
            end if;
        end if;
    end if;
    update commerce.orders set payment_state = final_state,order_state = 'closed',final_charge_minor = earned::bigint where id = o.id;
    return jsonb_build_object('state',final_state,'charged_minor',earned::bigint,'refunded_minor',refunded::bigint,'replayed',false);
end $$;
revoke all on function commerce_rpc.bind_checker_job(uuid,text),commerce_rpc.record_terminal_results(uuid,text,jsonb,text),
    commerce_rpc.confirm_result_delivery(uuid,bigint,bigint,text,text),commerce_rpc.settle_order(uuid) from public,anon,authenticated;
grant execute on function commerce_rpc.bind_checker_job(uuid,text),commerce_rpc.record_terminal_results(uuid,text,jsonb,text),
    commerce_rpc.confirm_result_delivery(uuid,bigint,bigint,text,text),commerce_rpc.settle_order(uuid) to service_role;
create or replace function commerce_rpc.health()
returns jsonb language sql stable security definer set search_path = '' as $$
    select jsonb_build_object('status','ok','schema_version',9,'currency','EGP')
$$;
commit;
