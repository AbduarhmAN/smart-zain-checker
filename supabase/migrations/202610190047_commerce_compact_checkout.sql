-- Compact checkout v2. Existing orders and v1 callers retain their contracts.
begin;

alter table commerce.orders
    add column check_storage_version smallint not null default 1 check(check_storage_version in (1,2)),
    add column checks_materialized boolean not null default true,
    add column account_check_count bigint check(account_check_count>=0),
    add column service_check_count bigint check(service_check_count>=0),
    add constraint order_check_counts_match check(
        (account_check_count is null and service_check_count is null)
        or (account_check_count is not null and service_check_count is not null
            and account_check_count+service_check_count=requested_checks));

-- One immutable offer snapshot per order, shared by its eventual discount rows.
create table commerce.order_discount_plans (
    order_id uuid primary key references commerce.orders(id),
    campaign_id uuid not null references commerce.campaigns(id),
    funder_kind text not null check(funder_kind in ('platform','partner')),
    rules_snapshot jsonb not null check(jsonb_typeof(rules_snapshot)='object'),
    candidate_check_limit bigint not null check(candidate_check_limit>0),
    candidate_budget_minor numeric not null check(candidate_budget_minor>0),
    reserved_checks bigint not null check(reserved_checks>0),
    reserved_discount_minor numeric not null check(reserved_discount_minor>0),
    selected_row_count bigint not null check(selected_row_count>0),
    usage_state text not null default 'reserved' check(usage_state in ('reserved','consumed','released')),
    created_at timestamptz not null default now(),
    unique(order_id,campaign_id)
);
alter table commerce.order_discount_plans enable row level security;
revoke all on commerce.order_discount_plans from public,anon,authenticated,service_role;

create function commerce.priced_compact_rows(p_order commerce.orders,p_rows jsonb)
returns table(row_number bigint,check_type text,unit_quote_minor numeric)
language sql stable security definer set search_path='' as $$
    with input as (
        select r.row_number,r.check_type,
            count(*) filter(where r.check_type='service') over(order by r.row_number) as service_position,
            max(r.row_number) over() as last_row
        from jsonb_to_recordset(p_rows) as r(row_number bigint,check_type text)
    )
    select i.row_number,i.check_type,
        case when coalesce((p_order.pricing_snapshot->>'partner_cost_sheet')::boolean,false) then
            case when i.row_number=i.last_row
                then p_order.quoted_minor-trunc(p_order.quoted_minor/p_order.requested_checks,16)*(p_order.requested_checks-1)
                else trunc(p_order.quoted_minor/p_order.requested_checks,16) end
            when i.check_type='service' and i.service_position>(p_order.pricing_snapshot->>'service_threshold')::bigint
                then (p_order.pricing_snapshot->>'service_excess_minor')::numeric
            else (p_order.pricing_snapshot->>'base_minor')::numeric end
    from input i order by i.row_number
$$;
revoke all on function commerce.priced_compact_rows(commerce.orders,jsonb) from public,anon,authenticated,service_role;

-- The exact same allocation is used for preview and materialization. No row
-- identities or per-row prices are persisted for an unpaid v2 draft.
create function commerce.compact_discount_candidate(p_order commerce.orders,p_rows jsonb,p_rules jsonb,
    p_check_limit bigint,p_budget_minor numeric)
returns jsonb language plpgsql stable security definer set search_path='' as $$
declare r record;candidate jsonb:='[]';total numeric:=0;used bigint:=0;
    remaining_count bigint:=p_check_limit;remaining_budget bigint:=p_budget_minor;
    discount bigint;percent_base numeric:=0;percent_rounded bigint:=0;next_rounded bigint;
    service_seen bigint:=0;free_offer boolean:=p_rules->>'benefit_kind'='free_checks';
begin
    if free_offer then
        if p_order.requested_checks>3400 then
            return jsonb_build_object('rows','[]'::jsonb,'total_minor',0,'quota_checks',0,'reason','exceeds_total_limit');end if;
        if p_order.service_check_count>1500 then
            return jsonb_build_object('rows','[]'::jsonb,'total_minor',0,'quota_checks',0,'reason','exceeds_service_limit');end if;
    end if;
    for r in select * from commerce.priced_compact_rows(p_order,p_rows) loop
        if not free_offer and (remaining_count<=0 or remaining_budget<=0) then exit;end if;
        if r.unit_quote_minor=0 or not (p_rules->'eligible_check_types') ? r.check_type then continue;end if;
        if free_offer then
            if r.check_type='service' then service_seen:=service_seen+1;end if;
            discount:=case when r.check_type='account' or service_seen<=500 then r.unit_quote_minor else 0 end;
        else
            discount:=case p_rules->>'benefit_kind'
                when 'percent' then round(r.unit_quote_minor*(p_rules->>'benefit_value')::bigint/10000)::bigint
                when 'fixed' then least(r.unit_quote_minor,(p_rules->>'benefit_value')::bigint) else 0 end;
            if p_rules->>'benefit_kind'='percent' then
                percent_base:=percent_base+r.unit_quote_minor;
                next_rounded:=round(percent_base*(p_rules->>'benefit_value')::bigint/10000)::bigint;
                discount:=next_rounded-percent_rounded;percent_rounded:=next_rounded;
            end if;
        end if;
        discount:=least(discount,remaining_budget,r.unit_quote_minor);
        if discount<=0 and (free_offer or p_rules->>'benefit_kind'<>'percent') then continue;end if;
        candidate:=candidate||jsonb_build_array(jsonb_build_object('row_number',r.row_number,'discount_minor',discount));
        total:=total+discount;remaining_budget:=remaining_budget-discount;
        remaining_count:=remaining_count-1;used:=used+1;
    end loop;
    return jsonb_build_object('rows',candidate,'total_minor',total,
        'quota_checks',case when free_offer and total>0 then 1 else used end);
end $$;
revoke all on function commerce.compact_discount_candidate(commerce.orders,jsonb,jsonb,bigint,numeric) from public,anon,authenticated,service_role;

create function commerce.discount_rules(p_order_id uuid,p_campaign_id uuid,p_legacy jsonb)
returns jsonb language sql stable security definer set search_path='' as $$
    select coalesce(p_legacy->'rules',(select rules_snapshot from commerce.order_discount_plans
        where order_id=p_order_id and campaign_id=p_campaign_id))
$$;
revoke all on function commerce.discount_rules(uuid,uuid,jsonb) from public,anon,authenticated,service_role;


CREATE OR REPLACE FUNCTION commerce.create_compact_order(p_telegram_user_id bigint, p_request_key text, p_rows jsonb, p_source jsonb)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO ''
AS $function$
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
    -- The user row lock serializes retries and simultaneous uploads. Reuse only
    -- an unpaid, uninvoiced draft with the same input and selection semantics.
    select * into v_order from commerce.orders existing
        where existing.user_id=v_user and existing.check_storage_version=2
        and existing.order_state='draft' and existing.payment_state='unpaid'
        and existing.source_snapshot-'original_telegram_file_id'-'original_telegram_file_unique_id'-'original_filename'
            =v_source-'original_telegram_file_id'-'original_telegram_file_unique_id'-'original_filename'
        and exists(select 1 from commerce.partner_sheet_requests where order_id=existing.id)
            =exists(select 1 from commerce.partners p where p.user_id=v_user and p.state='active'
                and commerce.partner_sheet_access_enabled(p.id))
        and not exists(select 1 from commerce.payment_requests where order_id=existing.id)
        and not exists(select 1 from commerce.reservations where order_id=existing.id)
        order by existing.created_at,existing.id limit 1 for update;
    if found then
        return jsonb_build_object('order_id',v_order.id,'quoted_minor',v_order.quoted_minor,
            'requested_checks',v_order.requested_checks,'currency',v_order.currency,'replayed',true,'reused_draft',true);
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
        partner_terms_snapshot,referral_partner_id,source_snapshot,check_storage_version,checks_materialized,
        account_check_count,service_check_count)
    values (v_user,p_request_key,v_count,(v_quote->>'total_minor')::numeric,v_quote-'row_unit_minor',
        v_terms,v_partner_id,v_source,2,false,(v_quote->>'account_count')::bigint,
        (v_quote->>'service_count')::bigint) returning * into v_order;
    return jsonb_build_object('order_id',v_order.id,'quoted_minor',v_order.quoted_minor,
        'requested_checks',v_count,'currency',v_order.currency,'replayed',false);
end $function$;
revoke all on function commerce.create_compact_order(bigint,text,jsonb,jsonb) from public,anon,authenticated,service_role;



alter function commerce.create_order(bigint,text,jsonb,jsonb) rename to create_order_before_compact;
create function commerce.create_order(p_telegram_user_id bigint,p_request_key text,p_rows jsonb,p_source jsonb)
returns jsonb language plpgsql security definer set search_path='' as $$
begin
    if p_source->'storage_version'='2'::jsonb then
        return commerce.create_compact_order(p_telegram_user_id,p_request_key,p_rows,p_source);end if;
    return commerce.create_order_before_compact(p_telegram_user_id,p_request_key,p_rows,p_source);
end $$;
revoke all on function commerce.create_order(bigint,text,jsonb,jsonb),commerce.create_order_before_compact(bigint,text,jsonb,jsonb)
    from public,anon,authenticated,service_role;



alter function commerce.create_order_with_campaigns(bigint,text,jsonb,jsonb) rename to create_order_with_campaigns_before_compact;
revoke all on function commerce.create_order_with_campaigns_before_compact(bigint,text,jsonb,jsonb) from public,anon,authenticated,service_role;
create or replace function commerce.create_order_with_campaigns(p_telegram_user_id bigint,p_request_key text,p_rows jsonb,p_source jsonb)
returns jsonb language plpgsql security definer set search_path='' as $$
declare result jsonb;o commerce.orders%rowtype;c commerce.campaigns%rowtype;usage commerce.campaign_usage%rowtype;
    row_data record;remaining_count bigint;remaining_budget bigint;global_used numeric;discount bigint;
    candidate jsonb;eligible jsonb:='[]';best jsonb:='[]';best_snapshot jsonb;best_id uuid;
    candidate_total numeric;best_total numeric:=0;best_count bigint:=0;count_used bigint;
    partner commerce.partners%rowtype;customer commerce.users%rowtype;best_kind text;terms_limit bigint;
    percent_base numeric;percent_rounded bigint;next_rounded bigint;
    v_order_total_checks bigint; v_order_service_checks bigint; v_order_account_checks bigint;
    v_free_service_seen bigint; is_free_checks_offer boolean := false;
    best_check_limit bigint;best_budget numeric;plan jsonb;
    v_free_checks_disqualified_reason text := null;
begin
    if p_source->'storage_version' is distinct from '2'::jsonb then
        return commerce.create_order_with_campaigns_before_compact(p_telegram_user_id,p_request_key,p_rows,p_source);end if;
    result:=commerce.create_order(p_telegram_user_id,p_request_key,p_rows,p_source);
    select * into o from commerce.orders where id=(result->>'order_id')::uuid for update;
    if o.campaigns_evaluated_at is not null then return result; end if;
    for c in select ca.* from commerce.campaigns ca join commerce.campaign_usage cu on cu.campaign_id=ca.id
        where cu.user_id=o.user_id and ca.state='active' and cu.expires_at>now() and ca.benefit_kind<>'wallet_credit'
        and ca.funder_kind in ('platform','partner') and not ca.allow_stacking order by ca.id for update of ca loop
        select * into usage from commerce.campaign_usage where campaign_id=c.id and user_id=o.user_id for update;
        if c.funder_kind='partner' then
            select * into partner from commerce.partners where id=c.partner_id for share;
            select * into customer from commerce.users where id=o.user_id;
            if partner.state<>'active' or c.partner_id is distinct from o.referral_partner_id
               or o.partner_terms_snapshot->>'state' is distinct from 'active'
               or c.benefit_value>partner.commission_basis_points
               or c.benefit_value>coalesce((o.partner_terms_snapshot->>'basis_points')::bigint,0)
               or (o.partner_terms_snapshot->>'mode'='duration' and customer.first_paid_service_at is not null
                   and now()>=customer.first_paid_service_at+(o.partner_terms_snapshot->>'duration_seconds')::bigint*interval '1 second') then continue;end if;
        end if;
        remaining_count:=(usage.rules_snapshot->>'max_checks_per_user')::bigint-usage.reserved_checks-usage.consumed_checks;
        if c.funder_kind='partner' and o.partner_terms_snapshot->>'mode'='checks' then
            remaining_count:=least(remaining_count,greatest((o.partner_terms_snapshot->>'checks')::bigint-customer.successful_paid_checks,0));
        end if;
        if remaining_count<=0 then continue; end if;
        select coalesce(sum(discount_minor::numeric+reserved_discount_minor),0) into global_used from commerce.campaign_usage where campaign_id=c.id;
        remaining_budget:=least((usage.rules_snapshot->>'max_discount_minor_per_user')::bigint-usage.discount_minor-usage.reserved_discount_minor,
            greatest(c.max_total_discount_minor::numeric-global_used,0))::bigint;
        if remaining_budget<=0 then continue; end if;
        plan:=commerce.compact_discount_candidate(o,p_rows,usage.rules_snapshot,remaining_count,remaining_budget);
        if plan ? 'reason' then
            v_free_checks_disqualified_reason:=plan->>'reason';continue;end if;
        candidate:=plan->'rows';candidate_total:=(plan->>'total_minor')::numeric;
        count_used:=(plan->>'quota_checks')::bigint;
        eligible:=eligible||jsonb_build_array(jsonb_build_object('campaign_id',c.id,'name',c.name,'discount_minor',candidate_total));
        if candidate_total>best_total then
            best:=candidate;best_total:=candidate_total;best_id:=c.id;best_snapshot:=usage.rules_snapshot;
            best_count:=count_used;best_kind:=c.funder_kind;best_check_limit:=remaining_count;best_budget:=remaining_budget;
            is_free_checks_offer:=usage.rules_snapshot->>'benefit_kind'='free_checks';
            if is_free_checks_offer then v_free_checks_disqualified_reason:=null;end if;
        end if;
    end loop;
    if best_total>0 then
        insert into commerce.order_discount_plans(order_id,campaign_id,funder_kind,rules_snapshot,
            candidate_check_limit,candidate_budget_minor,reserved_checks,reserved_discount_minor,selected_row_count)
        values(o.id,best_id,best_kind,best_snapshot,best_check_limit,best_budget,best_count,best_total,jsonb_array_length(best));
        update commerce.campaign_usage set reserved_checks=reserved_checks+best_count,reserved_discount_minor=reserved_discount_minor+best_total::bigint
            where campaign_id=best_id and user_id=o.user_id;
    end if;
    update commerce.orders set quoted_minor=quoted_minor-case when best_kind='partner' then 0 else best_total::bigint end,campaigns_evaluated_at=now(),
        pricing_snapshot=pricing_snapshot||jsonb_build_object('base_total_minor',quoted_minor,'discount_total_minor',best_total::bigint,
            'applied_campaign',case when best_id is null then null else jsonb_build_object('campaign_id',best_id,'name',best_snapshot->>'name','discounted_checks',best_count,'benefit_kind',best_snapshot->>'benefit_kind') end,
            'free_checks_applied',is_free_checks_offer,
            'free_checks_disqualified_reason',v_free_checks_disqualified_reason,
            'eligible_campaigns',eligible,'stacking',false,'total_minor',quoted_minor-case when best_kind='partner' then 0 else best_total::bigint end,
            'partner_future_offer',best_kind='partner','estimated_final_minor',quoted_minor-best_total::bigint,
            'discount_funding',best_kind,'cash_only',best_kind='partner','offer_settlement','verified_results_after_delivery')
        where id=o.id;
    return result||jsonb_build_object('quoted_minor',o.quoted_minor-case when best_kind='partner' then 0 else best_total::bigint end,'discount_minor',best_total::bigint);
end $$;
revoke all on function commerce.create_order_with_campaigns(bigint,text,jsonb,jsonb) from public,anon,authenticated,service_role;


CREATE OR REPLACE FUNCTION commerce.settle_partner_offer(p_order_id uuid)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO ''
AS $function$
declare o commerce.orders%rowtype;u commerce.users%rowtype;w commerce.wallets%rowtype;row_data record;d record;
    snapshot jsonb;result jsonb;rate bigint;discount_rate bigint;prior_count bigint;ordinal bigint;
    gross numeric:=0;offer_gross numeric:=0;paid_count bigint;eligible_rows bigint[]:='{}';
    ceiling numeric;desired numeric;discount_total numeric:=0;last_discount numeric:=0;row_discount numeric;
    share numeric;net_share numeric;net_revenue numeric;running_revenue numeric:=0;assigned numeric:=0;amount numeric;j uuid;
begin
    select user_id into u.id from commerce.orders where id=p_order_id;
    perform 1 from commerce.users where id=u.id for update;
    select * into o from commerce.orders where id=p_order_id for update;
    if o.commission_finalized_at is not null then
        return jsonb_build_object('state',o.payment_state,'charged_minor',o.final_charge_minor,
            'refunded_minor',o.quoted_minor-o.final_charge_minor,'collected_minor',o.collected_cash_minor,
            'commission_minor',(select coalesce(sum(amount_minor),0) from commerce.commissions where order_id=o.id),'replayed',true);
    end if;
    if o.payment_state<>'reserved' or o.execution_state<>'terminal' or o.delivery_state<>'confirmed' then
        raise exception 'Settlement requires funded terminal rows, delivery and consent' using errcode='22023';end if;
    select * into u from commerce.users where id=o.user_id;
    snapshot:=o.partner_terms_snapshot;rate:=(snapshot->>'basis_points')::bigint;prior_count:=u.successful_paid_checks;ordinal:=prior_count;
    if rate not between 0 and 10000 or snapshot->>'state' is distinct from 'active'
       or snapshot->>'partner_id' is distinct from o.referral_partner_id::text then raise exception 'Invalid frozen commission terms' using errcode='22023';end if;
    if exists(select 1 from commerce.checks where order_id=o.id and promotional_payment_minor<>0) then raise exception 'Partner offer requires cash funding' using errcode='22023';end if;
    select sum(discount_minor)::numeric,max((commerce.discount_rules(order_id,campaign_id,funder_snapshot)->>'benefit_value')::bigint)
        into ceiling,discount_rate from commerce.order_discounts where order_id=o.id and usage_state='reserved';
    if discount_rate not between 1 and rate then raise exception 'Partner discount exceeds approved share' using errcode='22023';end if;
    for row_data in select * from commerce.checks where order_id=o.id and verified_result and unit_quote_minor>0 order by row_number loop
        ordinal:=ordinal+1;
        if (snapshot->>'mode'='checks' and ordinal>(snapshot->>'checks')::bigint)
           or (snapshot->>'mode'='duration' and u.first_paid_service_at is not null
               and now()>=u.first_paid_service_at+(snapshot->>'duration_seconds')::bigint*interval '1 second') then continue;end if;
        eligible_rows:=array_append(eligible_rows,row_data.row_number);gross:=gross+row_data.unit_quote_minor;
        if exists(select 1 from commerce.order_discounts where order_id=o.id and row_number=row_data.row_number and usage_state='reserved') then
            offer_gross:=offer_gross+row_data.unit_quote_minor;
            desired:=least(ceiling,case when o.pricing_snapshot->>'financial_precision'='exact_decimal' then offer_gross*discount_rate/10000 else round(offer_gross*discount_rate/10000) end);
            row_discount:=desired-last_discount;last_discount:=desired;discount_total:=desired;
            update commerce.checks set discount_minor=row_discount where order_id=o.id and row_number=row_data.row_number;
        end if;
    end loop;
    share:=case when o.pricing_snapshot->>'financial_precision'='exact_decimal' then gross*rate/10000 else round(gross*rate/10000) end;net_share:=share-discount_total;
    if net_share<0 then raise exception 'Offer exceeds current order commission';end if;
    result:=commerce.settle_order(o.id);
    select * into o from commerce.orders where id=o.id;
    select coalesce(sum(unit_quote_minor-discount_minor),0) into net_revenue from commerce.checks where order_id=o.id and row_number=any(eligible_rows);
    select wal.* into w from commerce.wallets wal join commerce.partners p on p.user_id=wal.user_id
        where p.id=o.referral_partner_id and wal.kind='partner' for update of wal;
    if not found then raise exception 'Partner earnings wallet unavailable';end if;
    for row_data in select row_number,unit_quote_minor,discount_minor,unit_quote_minor-discount_minor as revenue from commerce.checks
        where order_id=o.id and row_number=any(eligible_rows) order by row_number loop
        running_revenue:=running_revenue+row_data.revenue;
        amount:=case when o.pricing_snapshot->>'financial_precision'='exact_decimal' then row_data.unit_quote_minor*rate/10000-row_data.discount_minor when net_revenue=0 then 0 else round(running_revenue*net_share/net_revenue)-assigned end;
        assigned:=assigned+amount;j:=null;
        if amount>0 then j:=commerce.post_pair('commission:'||o.id||':'||row_data.row_number,'commission',o.id,
            'platform:revenue','wallet:'||w.id||':available',amount,null,w.id);end if;
        insert into commerce.commissions(order_id,row_number,partner_id,revenue_minor,amount_minor,terms_snapshot,state,journal_id)
            values(o.id,row_data.row_number,o.referral_partner_id,row_data.revenue,amount,snapshot||jsonb_build_object(
                'basis','original_verified_cash_gross_less_partner_discount','order_gross_minor',gross,
                'order_original_share_minor',share,'order_partner_discount_minor',discount_total,
                'rounding',case when o.pricing_snapshot->>'financial_precision'='exact_decimal' then 'exact_decimal' else 'aggregate_verified_gross_half_up' end,'prior_earnings_debited',false),'available',j);
    end loop;
    update commerce.wallets set available_minor=available_minor+net_share where id=w.id;
    select count(*) into paid_count from commerce.checks where order_id=o.id and verified_result and unit_quote_minor>discount_minor;
    if paid_count=0 then update commerce.users set first_paid_service_at=u.first_paid_service_at where id=u.id;
        update commerce.referrals set locked_at=case when u.first_paid_service_at is null then null else locked_at end where user_id=u.id;end if;
    update commerce.users set successful_paid_checks=successful_paid_checks+paid_count where id=u.id;
    update commerce.orders set collected_cash_minor=final_charge_minor,financial_completed_at=now(),commission_finalized_at=now(),
        pricing_snapshot=pricing_snapshot||jsonb_build_object('settled_discount_minor',discount_total,'settled_original_share_minor',share)
        where id=o.id;
    return result||jsonb_build_object('collected_minor',o.final_charge_minor,'commission_minor',net_share,'discount_minor',discount_total,
        'original_share_minor',share,'platform_minor',o.final_charge_minor-net_share,'eligible_paid_checks',cardinality(eligible_rows));
end $function$;
revoke all on function commerce.settle_partner_offer(uuid) from public,anon,authenticated,service_role;


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
    if coalesce((result->>'replayed')::boolean,false) then return result;end if;
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
revoke all on function commerce_rpc.create_order(bigint,text,jsonb,jsonb) from public,anon,authenticated;
grant execute on function commerce_rpc.create_order(bigint,text,jsonb,jsonb) to service_role;


create function commerce.validate_compact_manifest(o commerce.orders,p_rows jsonb)
returns jsonb language plpgsql stable security definer set search_path='' as $$
declare sorted jsonb;accounts bigint;services bigint;
begin
    if jsonb_typeof(p_rows) is distinct from 'array' or jsonb_array_length(p_rows)<>o.requested_checks then
        raise exception 'Exact original check manifest required' using errcode='22023';end if;
    if exists(select 1 from jsonb_array_elements(p_rows) x where jsonb_typeof(x) is distinct from 'object') then
        raise exception 'Invalid check manifest' using errcode='22023';end if;
    if exists(select 1 from jsonb_array_elements(p_rows) x where
        (select count(*) from jsonb_object_keys(x))<>2
        or jsonb_typeof(x->'row_number') is distinct from 'number'
        or coalesce(x->>'row_number','')!~'^[1-9][0-9]{0,18}$'
        or coalesce(x->>'check_type','') not in ('account','service')) then
        raise exception 'Invalid check manifest' using errcode='22023';end if;
    if exists(select 1 from jsonb_array_elements(p_rows) x where (x->>'row_number')::numeric>9223372036854775807)
        or (select count(distinct x->>'row_number') from jsonb_array_elements(p_rows) x)<>o.requested_checks then
        raise exception 'Invalid check row identity' using errcode='22023';end if;
    select jsonb_agg(x order by (x->>'row_number')::bigint),
        count(*) filter(where x->>'check_type'='account'),count(*) filter(where x->>'check_type'='service')
        into sorted,accounts,services from jsonb_array_elements(p_rows) x;
    if md5(sorted::text) is distinct from o.source_snapshot->>'rows_fingerprint'
        or accounts is distinct from o.account_check_count or services is distinct from o.service_check_count then
        raise exception 'Check manifest differs from frozen quote' using errcode='22023';end if;
    return sorted;
end $$;
revoke all on function commerce.validate_compact_manifest(commerce.orders,jsonb) from public,anon,authenticated,service_role;

create function commerce.materialize_compact_checks(p_order_id uuid,p_rows jsonb)
returns void language plpgsql security definer set search_path='' as $$
declare o commerce.orders%rowtype;plan commerce.order_discount_plans%rowtype;
    sorted jsonb;candidate jsonb;r record;total numeric;
begin
    select * into o from commerce.orders where id=p_order_id for update;
    if not found then raise exception 'Owned checkout required' using errcode='22023';end if;
    if o.check_storage_version<>2 then return;end if;
    sorted:=commerce.validate_compact_manifest(o,p_rows);
    if o.checks_materialized then return;end if;
    if o.order_state<>'draft' or o.payment_state<>'unpaid' or exists(select 1 from commerce.checks where order_id=o.id) then
        raise exception 'Unmaterialized unpaid draft required' using errcode='22023';end if;
    perform commerce.require_approved_partner_sheet(o.id);
    insert into commerce.checks(order_id,row_number,check_type,unit_quote_minor)
        select o.id,row_number,check_type,unit_quote_minor from commerce.priced_compact_rows(o,sorted);
    select * into plan from commerce.order_discount_plans where order_id=o.id for update;
    if found then
        if plan.usage_state<>'reserved' then raise exception 'Offer reservation is no longer available' using errcode='22023';end if;
        candidate:=commerce.compact_discount_candidate(o,sorted,plan.rules_snapshot,
            plan.candidate_check_limit,plan.candidate_budget_minor);
        if (candidate->>'total_minor')::numeric<>plan.reserved_discount_minor
            or (candidate->>'quota_checks')::bigint<>plan.reserved_checks
            or jsonb_array_length(candidate->'rows')<>plan.selected_row_count then
            raise exception 'Offer allocation differs from frozen quote' using errcode='22023';end if;
        for r in select * from jsonb_to_recordset(candidate->'rows') as d(row_number bigint,discount_minor numeric) loop
            update commerce.checks set discount_minor=case when plan.funder_kind='partner' then 0 else r.discount_minor end
                where order_id=o.id and row_number=r.row_number;
            -- The shared rules live once in order_discount_plans. Retain the
            -- small per-row monetary attribution needed by existing settlement.
            insert into commerce.order_discounts(order_id,row_number,campaign_id,discount_minor,funder_snapshot)
                values(o.id,r.row_number,plan.campaign_id,r.discount_minor,jsonb_build_object(
                    'kind',plan.funder_kind,'platform_minor',case when plan.funder_kind='platform' then r.discount_minor else 0 end,
                    'partner_minor',case when plan.funder_kind='partner' then r.discount_minor else 0 end,'stacking',false));
        end loop;
    end if;
    select sum(unit_quote_minor-discount_minor) into total from commerce.checks where order_id=o.id;
    if total is distinct from o.quoted_minor then
        raise exception 'Materialized price differs from accepted quote' using errcode='22023';end if;
    update commerce.orders set checks_materialized=true where id=o.id;
end $$;
revoke all on function commerce.materialize_compact_checks(uuid,jsonb) from public,anon,authenticated,service_role;

alter function commerce_rpc.reserve_order(uuid,bigint,text,uuid) rename to reserve_order_before_compact;
alter function commerce_rpc.reserve_order_before_compact(uuid,bigint,text,uuid) set schema commerce;
revoke all on function commerce.reserve_order_before_compact(uuid,bigint,text,uuid) from public,anon,authenticated,service_role;
create function commerce_rpc.reserve_order(p_order_id uuid,p_telegram_user_id bigint,p_consent_reference text,p_payment_id uuid default null)
returns jsonb language plpgsql security definer set search_path='' as $$
begin
    if exists(select 1 from commerce.orders where id=p_order_id and check_storage_version=2 and not checks_materialized) then
        raise exception 'Frozen check manifest required for reservation' using errcode='22023';end if;
    return commerce.reserve_order_before_compact(p_order_id,p_telegram_user_id,p_consent_reference,p_payment_id);
end $$;

create function commerce_rpc.reserve_order_with_rows(p_order_id uuid,p_telegram_user_id bigint,p_consent_reference text,
    p_rows jsonb,p_payment_id uuid default null)
returns jsonb language plpgsql security definer set search_path='' as $$
declare result jsonb;o commerce.orders%rowtype;w commerce.wallets%rowtype;p commerce.payments%rowtype;
    available numeric;cash_only boolean;
begin
    perform 1 from commerce.users where telegram_user_id=p_telegram_user_id and registration_state='active' for update;
    select ord.* into o from commerce.orders ord join commerce.users u on u.id=ord.user_id
        where ord.id=p_order_id and u.telegram_user_id=p_telegram_user_id and u.registration_state='active' for update of ord;
    if not found then raise exception 'Owned checkout required' using errcode='22023';end if;
    if o.check_storage_version=2 and not o.checks_materialized then
        perform commerce.validate_compact_manifest(o,p_rows);
        perform commerce.require_approved_partner_sheet(o.id);
        if p_consent_reference is null or length(btrim(p_consent_reference)) not between 1 and 1000
            or o.refund_consent_reference is null or o.refund_consent_reference is distinct from p_consent_reference
            or o.order_state<>'draft' or o.payment_state<>'unpaid' then
            raise exception 'Accepted unpaid checkout consent required' using errcode='22023';end if;
        -- Avoid heap/index growth even from rolled-back insufficient-funds
        -- attempts. The existing financial implementation rechecks these locks.
        if p_payment_id is null then
            perform commerce.expire_user_promotional_credit(o.user_id);
            select * into w from commerce.wallets where user_id=o.user_id and kind='customer' for update;
            if not found then raise exception 'Customer wallet required' using errcode='22023';end if;
            cash_only:=coalesce((o.pricing_snapshot->>'partner_future_offer')::boolean,false)
                or exists(select 1 from commerce.partner_sheet_requests where order_id=o.id);
            available:=w.available_minor-case when cash_only then w.promotional_available_minor else 0 end;
            if available<o.quoted_minor then
                return jsonb_build_object('state','insufficient_balance','available_minor',available,'cash_only',cash_only);end if;
        else
            select * into p from commerce.payments where id=p_payment_id for update;
            if not found or p.user_id<>o.user_id or p.order_id is distinct from o.id
                or p.purpose<>'direct_order' or p.state<>'verified' or p.amount_minor<>o.quoted_minor then
                raise exception 'Exact verified order payment required' using errcode='22023';end if;
        end if;
    end if;
    -- A subtransaction rolls back every inserted detail when the established
    -- monetary implementation returns insufficient balance rather than raising.
    begin
        perform commerce.materialize_compact_checks(o.id,p_rows);
        result:=commerce_rpc.reserve_order(o.id,p_telegram_user_id,p_consent_reference,p_payment_id);
        if result->>'state' is distinct from 'held' then
            raise exception 'Reservation not held' using errcode='PZ047';end if;
    exception when sqlstate 'PZ047' then
        return result;
    end;
    return result;
end $$;
revoke all on function commerce_rpc.reserve_order(uuid,bigint,text,uuid),
    commerce_rpc.reserve_order_with_rows(uuid,bigint,text,jsonb,uuid) from public,anon,authenticated;
grant execute on function commerce_rpc.reserve_order(uuid,bigint,text,uuid),
    commerce_rpc.reserve_order_with_rows(uuid,bigint,text,jsonb,uuid) to service_role;

alter function commerce.finalize_campaign_usage(uuid,boolean) rename to finalize_campaign_usage_before_compact;
revoke all on function commerce.finalize_campaign_usage_before_compact(uuid,boolean) from public,anon,authenticated,service_role;
create function commerce.finalize_campaign_usage(p_order_id uuid,p_cancel boolean default false)
returns void language plpgsql security definer set search_path='' as $$
declare o commerce.orders%rowtype;plan commerce.order_discount_plans%rowtype;used_count bigint;used_discount numeric;
begin
    select * into o from commerce.orders where id=p_order_id for update;
    select * into plan from commerce.order_discount_plans where order_id=p_order_id for update;
    if not found then
        perform commerce.finalize_campaign_usage_before_compact(p_order_id,p_cancel);return;end if;
    if o.campaign_usage_finalized_at is not null or plan.usage_state<>'reserved' then return;end if;
    perform 1 from commerce.campaigns where id=plan.campaign_id for update;
    perform 1 from commerce.campaign_usage where campaign_id=plan.campaign_id and user_id=o.user_id for update;
    select count(*) filter(where c.verified_result and not p_cancel),
        coalesce(sum(case when plan.funder_kind='partner' then c.discount_minor else d.discount_minor end)
            filter(where c.verified_result and not p_cancel),0)
        into used_count,used_discount
        from commerce.order_discounts d join commerce.checks c on c.order_id=d.order_id and c.row_number=d.row_number
        where d.order_id=o.id and d.campaign_id=plan.campaign_id and d.usage_state='reserved';
    -- FREEZAIN reserves one run, even when it covers thousands of check rows.
    if plan.rules_snapshot->>'benefit_kind'='free_checks' then used_count:=case when used_count>0 then 1 else 0 end;end if;
    update commerce.campaign_usage set reserved_checks=reserved_checks-plan.reserved_checks,
        reserved_discount_minor=reserved_discount_minor-plan.reserved_discount_minor,
        consumed_checks=consumed_checks+used_count,discount_minor=discount_minor+used_discount
        where campaign_id=plan.campaign_id and user_id=o.user_id;
    update commerce.order_discounts d set applied=not p_cancel and c.verified_result,
        usage_state=case when not p_cancel and c.verified_result then 'consumed' else 'released' end
        from commerce.checks c where d.order_id=o.id and c.order_id=d.order_id and c.row_number=d.row_number;
    update commerce.order_discount_plans set usage_state=case when used_count>0 then 'consumed' else 'released' end where order_id=o.id;
    update commerce.orders set campaign_usage_finalized_at=now() where id=o.id;
end $$;
revoke all on function commerce.finalize_campaign_usage(uuid,boolean) from public,anon,authenticated,service_role;

create function commerce.order_check_summary(p_order_id uuid)
returns jsonb language sql stable security definer set search_path='' as $$
    select jsonb_build_object('account_count',coalesce(o.account_check_count,
        (select count(*) from commerce.checks where order_id=o.id and check_type='account')),
        'service_count',coalesce(o.service_check_count,
        (select count(*) from commerce.checks where order_id=o.id and check_type='service')),
        'check_storage_version',o.check_storage_version,'checks_materialized',o.checks_materialized)
    from commerce.orders o where o.id=p_order_id
$$;
revoke all on function commerce.order_check_summary(uuid) from public,anon,authenticated,service_role;

alter function commerce_rpc.checkout_state(uuid,bigint) rename to checkout_state_before_compact;
alter function commerce_rpc.checkout_state_before_compact(uuid,bigint) set schema commerce;
revoke all on function commerce.checkout_state_before_compact(uuid,bigint) from public,anon,authenticated,service_role;
create function commerce_rpc.checkout_state(p_order_id uuid,p_telegram_user_id bigint)
returns jsonb language plpgsql security definer set search_path='' as $$
declare result jsonb;
begin
    result:=commerce.checkout_state_before_compact(p_order_id,p_telegram_user_id);
    return result||commerce.order_check_summary(p_order_id);
end $$;
revoke all on function commerce_rpc.checkout_state(uuid,bigint) from public,anon,authenticated;
grant execute on function commerce_rpc.checkout_state(uuid,bigint) to service_role;

alter function commerce.customer_order_view(uuid) rename to customer_order_view_before_compact;
revoke all on function commerce.customer_order_view_before_compact(uuid) from public,anon,authenticated,service_role;
create function commerce.customer_order_view(p_order_id uuid)
returns jsonb language sql stable set search_path='' as $$
    select commerce.customer_order_view_before_compact(p_order_id)||commerce.order_check_summary(p_order_id)
$$;
revoke all on function commerce.customer_order_view(uuid) from public,anon,authenticated,service_role;

notify pgrst,'reload schema';
commit;
