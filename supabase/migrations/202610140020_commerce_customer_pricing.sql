-- Latest human decision: EGP 0.01/check; immutable per-customer and service tiers.
begin;
create table commerce.customer_price_settings (
    id bigint generated always as identity primary key,
    target_telegram_id bigint not null check(target_telegram_id>=0),
    base_minor bigint not null check(base_minor between 0 and 100000000),
    service_excess_minor bigint not null check(service_excess_minor between 0 and 100000000),
    service_threshold integer not null check(service_threshold between 1 and 4000),
    inherit_global boolean not null default false,
    actor_telegram_id bigint,
    reference text not null,
    created_at timestamptz not null default now()
);
create index customer_price_latest on commerce.customer_price_settings(target_telegram_id,id desc);
alter table commerce.customer_price_settings enable row level security;
revoke all on commerce.customer_price_settings from public,anon,authenticated,service_role;
revoke all on sequence commerce.customer_price_settings_id_seq from public,anon,authenticated,service_role;
insert into commerce.customer_price_settings(target_telegram_id,base_minor,service_excess_minor,service_threshold,reference)
    values(0,1,1,500,'human_voice_20261014_EGP_001');
insert into commerce.pricing_policies(unit_price_minor,refund_destination,decision_reference)
    values(1,'wallet','human_voice_20261014_EGP_001');

create function commerce_rpc.customer_pricing(p_telegram_user_id bigint)
returns jsonb language plpgsql stable security definer set search_path='' as $$
declare cfg commerce.customer_price_settings%rowtype;
begin
    if p_telegram_user_id is null or p_telegram_user_id<0 then raise exception 'Customer identity required' using errcode='22023'; end if;
    select * into cfg from commerce.customer_price_settings where target_telegram_id=p_telegram_user_id order by id desc limit 1;
    if not found or cfg.inherit_global then
        select * into cfg from commerce.customer_price_settings where target_telegram_id=0 order by id desc limit 1;
    end if;
    return jsonb_build_object('pricing_version',cfg.id,'scope',cfg.target_telegram_id,'base_minor',cfg.base_minor,
        'service_excess_minor',cfg.service_excess_minor,'service_threshold',cfg.service_threshold,
        'max_customers',4000,'currency','EGP');
end $$;

create function commerce_rpc.configure_customer_pricing(p_actor_telegram_id bigint,p_target_telegram_id bigint,p_rules jsonb,p_reference text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare setting bigint; key text; inherit boolean; base bigint; excess bigint; threshold integer;
begin
    if not exists(select 1 from commerce.support_actors where telegram_user_id=p_actor_telegram_id and active)
       or p_target_telegram_id is null or p_target_telegram_id<0
       or p_reference is null or length(btrim(p_reference)) not between 1 and 1000
       or jsonb_typeof(p_rules) is distinct from 'object' then
        raise exception 'Authorized price administrator and review receipt required' using errcode='22023'; end if;
    for key in select jsonb_object_keys(p_rules) loop
        if key not in ('base_minor','service_excess_minor','service_threshold','inherit_global') then
            raise exception 'Unknown price field' using errcode='22023'; end if;
    end loop;
    if p_rules ? 'inherit_global' and jsonb_typeof(p_rules->'inherit_global') is distinct from 'boolean' then
        raise exception 'Boolean inheritance setting required' using errcode='22023'; end if;
    inherit:=coalesce((p_rules->>'inherit_global')::boolean,false);
    if inherit and p_target_telegram_id=0 then raise exception 'Global prices cannot inherit themselves' using errcode='22023'; end if;
    if inherit then base:=1;excess:=1;threshold:=500;
    else
        for key in select unnest(array['base_minor','service_excess_minor','service_threshold']) loop
            if jsonb_typeof(p_rules->key) is distinct from 'number' or p_rules->>key !~ '^[0-9]{1,9}$' then
                raise exception 'Integer price fields required' using errcode='22023'; end if;
        end loop;
        base:=(p_rules->>'base_minor')::bigint;excess:=(p_rules->>'service_excess_minor')::bigint;
        threshold:=(p_rules->>'service_threshold')::integer;
    end if;
    insert into commerce.customer_price_settings(target_telegram_id,base_minor,service_excess_minor,service_threshold,
        inherit_global,actor_telegram_id,reference)
        values(p_target_telegram_id,base,excess,threshold,inherit,p_actor_telegram_id,p_reference) returning id into setting;
    return jsonb_build_object('state','configured','pricing_version',setting);
end $$;

create or replace function commerce_rpc.quote_checks(p_count bigint)
returns jsonb language plpgsql stable security definer set search_path='' as $$
declare policy commerce.pricing_policies%rowtype;unit bigint;total numeric;
begin
    if p_count is null or p_count<=0 then raise exception 'Positive check count required' using errcode='22023'; end if;
    select * into policy from commerce.pricing_policies where effective_at<=now() order by version desc limit 1;
    select base_minor into unit from commerce.customer_price_settings where target_telegram_id=0 order by id desc limit 1;
    total:=p_count::numeric*unit;
    if total>9223372036854775807 then raise exception 'Quote exceeds monetary range' using errcode='22023'; end if;
    return jsonb_build_object('policy_version',policy.version,'unit','check','count',p_count,'unit_price_minor',unit,
        'total_minor',total::bigint,'currency','EGP','refund_destination','wallet','refund_requires_consent',true,
        'billing_basis','verified_terminal_result','technical_failure_refunded',true);
end $$;

create function commerce.customer_quote(p_telegram_user_id bigint,p_rows jsonb)
returns jsonb language plpgsql stable security definer set search_path='' as $$
declare cfg jsonb;quote jsonb;row_data jsonb;prices jsonb:='{}';services integer:=0;accounts integer:=0;
    unit bigint;total numeric:=0;count integer:=jsonb_array_length(p_rows);
begin
    if count>4000 and not exists(select 1 from commerce.support_actors where telegram_user_id=p_telegram_user_id and active) then
        raise exception 'Customer sheets cannot exceed 4000 checks' using errcode='22023'; end if;
    cfg:=commerce_rpc.customer_pricing(p_telegram_user_id);quote:=commerce_rpc.quote_checks(count);
    for row_data in select value from jsonb_array_elements(p_rows) order by (value->>'row_number')::bigint loop
        if row_data->>'check_type'='service' then services:=services+1;else accounts:=accounts+1;end if;
        unit:=case when row_data->>'check_type'='service' and services>(cfg->>'service_threshold')::integer
                   then (cfg->>'service_excess_minor')::bigint else (cfg->>'base_minor')::bigint end;
        total:=total+unit;prices:=prices||jsonb_build_object(row_data->>'row_number',unit);
    end loop;
    if total>9223372036854775807 then raise exception 'Quote exceeds monetary range' using errcode='22023'; end if;
    return quote||cfg||jsonb_build_object('unit_price_minor',(cfg->>'base_minor')::bigint,'total_minor',total::bigint,
        'row_unit_minor',prices,'account_count',accounts,'service_count',services,
        'service_standard_count',least(services,(cfg->>'service_threshold')::integer),
        'service_excess_count',greatest(services-(cfg->>'service_threshold')::integer,0));
end $$;
revoke all on function commerce.customer_quote(bigint,jsonb) from public,anon,authenticated,service_role;
revoke all on function commerce_rpc.customer_pricing(bigint),commerce_rpc.configure_customer_pricing(bigint,bigint,jsonb,text) from public,anon,authenticated;
grant execute on function commerce_rpc.customer_pricing(bigint),commerce_rpc.configure_customer_pricing(bigint,bigint,jsonb,text) to service_role;

create or replace function commerce.create_order(p_telegram_user_id bigint,p_request_key text,p_rows jsonb,p_source jsonb)
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
    v_quote := commerce.customer_quote(p_telegram_user_id,v_sorted);
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
    select v_order.id,(r->>'row_number')::bigint,r->>'check_type',((v_quote->'row_unit_minor')->>(r->>'row_number'))::bigint
    from jsonb_array_elements(v_sorted) r;
    return jsonb_build_object('order_id',v_order.id,'quoted_minor',v_order.quoted_minor,
        'requested_checks',v_count,'currency',v_order.currency,'replayed',false);
end $$;

create or replace function commerce_rpc.health()
returns jsonb language sql stable security definer set search_path='' as $$
    select jsonb_build_object('status','ok','schema_version',20,'currency','EGP')
$$;
notify pgrst,'reload schema';
commit;
