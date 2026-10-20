-- A17-A19: quote from server policy; one immutable request binds the input rows.
begin;
alter table commerce.orders add column source_snapshot jsonb not null default '{}';

create function commerce_rpc.quote_checks(p_count bigint)
returns jsonb language plpgsql stable security definer set search_path = '' as $$
declare v_policy commerce.pricing_policies%rowtype; v_total numeric;
begin
    if p_count is null or p_count <= 0 then raise exception 'Positive check count required' using errcode = '22023'; end if;
    select * into v_policy from commerce.pricing_policies where effective_at <= now() order by version desc limit 1;
    if not found then raise exception 'Pricing policy unavailable' using errcode = '22023'; end if;
    v_total := p_count::numeric * v_policy.unit_price_minor;
    if v_total > 9223372036854775807 then raise exception 'Quote exceeds monetary range' using errcode = '22023'; end if;
    return jsonb_build_object('policy_version',v_policy.version,'unit','check','count',p_count,
        'unit_price_minor',v_policy.unit_price_minor,'total_minor',v_total::bigint,'currency',v_policy.currency,
        'refund_destination',v_policy.refund_destination,'refund_requires_consent',v_policy.refund_requires_consent,
        'billing_basis','verified_terminal_result','technical_failure_refunded',true);
end $$;

create function commerce_rpc.create_order(p_telegram_user_id bigint,p_request_key text,p_rows jsonb,p_source jsonb)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare v_user uuid; v_order commerce.orders%rowtype; v_ref commerce.referrals%rowtype;
        v_partner commerce.partners%rowtype; v_quote jsonb; v_terms jsonb := '{}';
        v_sorted jsonb; v_count bigint; v_partner_id uuid; v_source jsonb;
begin
    if p_request_key is null or length(p_request_key) not between 1 and 160
       or jsonb_typeof(p_rows) is distinct from 'array' or jsonb_array_length(p_rows) = 0
       or jsonb_typeof(p_source) is distinct from 'object'
       or coalesce(p_source->>'file_sha256','') !~ '^[a-f0-9]{64}$' then
        raise exception 'Invalid order input' using errcode = '22023';
    end if;
    if exists (select 1 from jsonb_array_elements(p_rows) r where jsonb_typeof(r) is distinct from 'object') then
        raise exception 'Check rows must be objects' using errcode = '22023';
    end if;
    if exists (select 1 from jsonb_array_elements(p_rows) r where
        (select count(*) from jsonb_object_keys(r)) <> 2
        or jsonb_typeof(r->'row_number') is distinct from 'number'
        or coalesce(r->>'row_number','') !~ '^[1-9][0-9]{0,18}$'
        or coalesce(r->>'check_type','') not in ('account','service')) then
        raise exception 'Invalid check row identity/type' using errcode = '22023';
    end if;
    if exists (select 1 from jsonb_array_elements(p_rows) r where (r->>'row_number')::numeric > 9223372036854775807) then
        raise exception 'Row exceeds bigint range' using errcode = '22023';
    end if;
    v_count := jsonb_array_length(p_rows);
    if (select count(distinct (r->>'row_number')::bigint) from jsonb_array_elements(p_rows) r) <> v_count then
        raise exception 'Duplicate input row' using errcode = '22023';
    end if;
    select jsonb_agg(r order by (r->>'row_number')::bigint) into v_sorted from jsonb_array_elements(p_rows) r;
    v_source := p_source || jsonb_build_object('rows_fingerprint',md5(v_sorted::text));
    v_user := (commerce_rpc.register_telegram_user(p_telegram_user_id,null)->>'user_id')::uuid;
    perform 1 from commerce.users where id = v_user and registration_state = 'active' for update;
    if not found then raise exception 'Account suspended' using errcode = '22023'; end if;
    select * into v_order from commerce.orders where request_key = p_request_key for update;
    if found then
        if v_order.user_id <> v_user or v_order.source_snapshot <> v_source or v_order.requested_checks <> v_count then
            raise exception 'Conflicting order request replay' using errcode = '22023';
        end if;
        return jsonb_build_object('order_id',v_order.id,'quoted_minor',v_order.quoted_minor,
            'requested_checks',v_order.requested_checks,'currency',v_order.currency,'replayed',true);
    end if;
    v_quote := commerce_rpc.quote_checks(v_count);
    select * into v_ref from commerce.referrals where user_id = v_user;
    if found then
        select * into v_partner from commerce.partners where id = v_ref.partner_id for share;
        -- Attribution remains recorded independently of discounts or partner suspension.
        v_partner_id := v_ref.partner_id;
        v_terms := jsonb_build_object('partner_id',v_partner.id,'state',v_partner.state,
            'terms_version',v_partner.terms_version,'basis_points',v_partner.commission_basis_points,
            'mode',v_partner.entitlement_mode,'checks',v_partner.entitlement_checks,
            'duration_seconds',v_partner.entitlement_duration_seconds,
            'anchor','first_paid_service','count_basis','successful_paid_checks');
    end if;
    insert into commerce.orders(user_id,request_key,requested_checks,quoted_minor,pricing_snapshot,
        partner_terms_snapshot,referral_partner_id,source_snapshot)
    values (v_user,p_request_key,v_count,(v_quote->>'total_minor')::bigint,v_quote,
        v_terms,v_partner_id,v_source) returning * into v_order;
    insert into commerce.checks(order_id,row_number,check_type,unit_quote_minor)
    select v_order.id,(r->>'row_number')::bigint,r->>'check_type',(v_quote->>'unit_price_minor')::bigint
    from jsonb_array_elements(v_sorted) r;
    return jsonb_build_object('order_id',v_order.id,'quoted_minor',v_order.quoted_minor,
        'requested_checks',v_count,'currency',v_order.currency,'replayed',false);
end $$;
revoke all on function commerce_rpc.quote_checks(bigint),commerce_rpc.create_order(bigint,text,jsonb,jsonb) from public,anon,authenticated;
grant execute on function commerce_rpc.quote_checks(bigint),commerce_rpc.create_order(bigint,text,jsonb,jsonb) to service_role;
commit;
