begin;
alter table commerce.wallets add column promotional_available_minor bigint not null default 0 check(promotional_available_minor>=0 and promotional_available_minor<=available_minor);
alter table commerce.wallets add column promotional_reserved_minor bigint not null default 0 check(promotional_reserved_minor>=0 and promotional_reserved_minor<=reserved_minor);
alter table commerce.reservations add column promotional_minor bigint not null default 0 check(promotional_minor>=0 and promotional_minor<=amount_minor);
alter table commerce.checks add column promotional_payment_minor bigint not null default 0 check(promotional_payment_minor>=0 and promotional_payment_minor<=unit_quote_minor-discount_minor);
alter table commerce.orders add column collected_cash_minor bigint check(collected_cash_minor>=0 and collected_cash_minor<=final_charge_minor);
create table commerce.campaign_policies(
    version bigint generated always as identity primary key,
    rules jsonb not null,
    actor_telegram_id bigint not null check(actor_telegram_id>0),
    approval_reference text not null,
    created_at timestamptz not null default now()
);
alter table commerce.campaign_policies enable row level security;
revoke all on commerce.campaign_policies from public,anon,authenticated,service_role;
alter table commerce.campaigns add column max_total_discount_minor bigint check(max_total_discount_minor>0);
alter table commerce.campaigns add column actor_telegram_id bigint check(actor_telegram_id>0);
alter table commerce.campaigns add column approval_reference text;
alter table commerce.campaign_usage add column reserved_discount_minor bigint not null default 0 check(reserved_discount_minor>=0);
alter table commerce.campaign_usage add column rules_snapshot jsonb not null default '{}';
alter table commerce.campaign_usage add column expires_at timestamptz;
alter table commerce.campaign_usage add column grant_journal_id uuid unique references commerce.journals(id);
alter table commerce.order_discounts add column usage_state text not null default 'reserved' check(usage_state in ('reserved','consumed','released'));
alter table commerce.orders add column campaigns_evaluated_at timestamptz;
alter table commerce.orders add column campaign_usage_finalized_at timestamptz;
-- Prices already issued before this deployment remain immutable.
update commerce.orders set campaigns_evaluated_at=created_at;

create function commerce_rpc.configure_campaign_policy(p_actor_telegram_id bigint,p_rules jsonb,p_reference text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare v bigint; key text;
begin
    if p_actor_telegram_id is null or p_actor_telegram_id<=0 or p_reference is null
       or length(btrim(p_reference)) not between 1 and 1000 or jsonb_typeof(p_rules) is distinct from 'object'
       or (select count(*) from jsonb_object_keys(p_rules))<>7 then
        raise exception 'Reviewed campaign limits required' using errcode='22023'; end if;
    foreach key in array array['max_percent_basis_points','max_checks_per_user','max_discount_minor_per_user',
        'max_total_discount_minor','max_duration_seconds','max_wallet_credit_minor','max_redemptions'] loop
        if jsonb_typeof(p_rules->key) is distinct from 'number' or coalesce(p_rules->>key,'') !~ '^[1-9][0-9]{0,18}$'
           or (p_rules->>key)::numeric>9223372036854775807 then
            raise exception 'Explicit positive integer campaign limits required' using errcode='22023'; end if;
    end loop;
    if (p_rules->>'max_percent_basis_points')::bigint>10000 then raise exception 'Percentage exceeds 100%%' using errcode='22023'; end if;
    insert into commerce.campaign_policies(rules,actor_telegram_id,approval_reference)
        values(p_rules,p_actor_telegram_id,p_reference) returning version into v;
    return jsonb_build_object('version',v,'rules',p_rules);
end $$;
create function commerce_rpc.campaign_policy()
returns jsonb language sql stable security definer set search_path='' as $$
    select jsonb_build_object('version',version,'rules',rules) from commerce.campaign_policies order by version desc limit 1
$$;

create function commerce_rpc.create_platform_campaign(p_actor_telegram_id bigint,p_key text,p_code text,p_rules jsonb,p_reference text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare policy commerce.campaign_policies%rowtype; c commerce.campaigns%rowtype; code text;
    kind text; eligibility text; types text[]; value bigint; count_limit bigint; budget bigint; global_budget bigint; duration bigint; redemptions bigint;
begin
    code:=upper(btrim(p_code));kind:=p_rules->>'benefit_kind';eligibility:=p_rules->>'eligibility';
    if p_actor_telegram_id is null or p_actor_telegram_id<=0 or p_key is null or length(p_key) not between 1 and 160
       or code is null or code !~ '^[A-Z0-9_-]{3,48}$' or p_reference is null or length(btrim(p_reference)) not between 1 and 1000
       or jsonb_typeof(p_rules) is distinct from 'object' or (select count(*) from jsonb_object_keys(p_rules))<>9
       or kind is null or kind not in ('percent','fixed','free_checks','wallet_credit')
       or eligibility is null or eligibility not in ('new','existing','any')
       or jsonb_typeof(p_rules->'check_types') is distinct from 'array'
       or jsonb_array_length(p_rules->'check_types') not between 1 and 2 then
        raise exception 'Reviewed platform campaign rules required' using errcode='22023'; end if;
    select array_agg(distinct t order by t) into types from jsonb_array_elements_text(p_rules->'check_types') t;
    if not types<@array['account','service'] or cardinality(types)<>jsonb_array_length(p_rules->'check_types') then
        raise exception 'Invalid eligible check types' using errcode='22023'; end if;
    for code in select unnest(array['benefit_value','max_checks','max_discount_minor','max_total_discount_minor','duration_seconds','max_redemptions']) loop
        if jsonb_typeof(p_rules->code) is distinct from 'number' or coalesce(p_rules->>code,'') !~ '^[1-9][0-9]{0,18}$'
           or (p_rules->>code)::numeric>9223372036854775807 then
            raise exception 'Explicit bounded campaign values required' using errcode='22023'; end if;
    end loop;
    code:=upper(btrim(p_code));
    value:=(p_rules->>'benefit_value')::bigint;count_limit:=(p_rules->>'max_checks')::bigint;
    budget:=(p_rules->>'max_discount_minor')::bigint;global_budget:=(p_rules->>'max_total_discount_minor')::bigint;
    duration:=(p_rules->>'duration_seconds')::bigint;redemptions:=(p_rules->>'max_redemptions')::bigint;
    select * into c from commerce.campaigns where rules->>'request_key'=p_key;
    if found then
        if c.rules->'input' is distinct from p_rules or c.rules->>'code' is distinct from code then
            raise exception 'Conflicting campaign replay' using errcode='22023'; end if;
        return jsonb_build_object('campaign_id',c.id,'code',code,'replayed',true);
    end if;
    select * into policy from commerce.campaign_policies order by version desc limit 1 for share;
    if not found then raise exception 'Set explicit administration limits before creating offers' using errcode='22023'; end if;
    if count_limit>(policy.rules->>'max_checks_per_user')::bigint or budget>(policy.rules->>'max_discount_minor_per_user')::bigint
       or global_budget>(policy.rules->>'max_total_discount_minor')::bigint or duration>(policy.rules->>'max_duration_seconds')::bigint
       or redemptions>(policy.rules->>'max_redemptions')::bigint or budget>global_budget
       or (kind='percent' and value>(policy.rules->>'max_percent_basis_points')::bigint)
       or (kind='free_checks' and value<>1)
       or (kind in ('fixed','wallet_credit') and value>budget)
       or (kind='wallet_credit' and value>(policy.rules->>'max_wallet_credit_minor')::bigint) then
        raise exception 'Campaign exceeds reviewed administration limits' using errcode='22023'; end if;
    perform pg_advisory_xact_lock(hashtextextended('campaign:'||p_key,0));
    select * into c from commerce.campaigns where rules->>'request_key'=p_key;
    if found then
        if c.rules->'input' is distinct from p_rules or c.rules->>'code' is distinct from code then raise exception 'Conflicting campaign replay' using errcode='22023'; end if;
        return jsonb_build_object('campaign_id',c.id,'code',code,'replayed',true);
    end if;
    insert into commerce.campaigns(name,state,benefit_kind,benefit_value,funder_kind,eligible_check_types,new_customer_only,
        activation_duration_seconds,max_checks_per_user,max_discount_minor_per_user,max_total_discount_minor,
        allow_stacking,rules_version,rules,approved_at,actor_telegram_id,approval_reference)
    values(code,'active',kind,value,'platform',types,eligibility='new',duration,count_limit,budget,global_budget,false,
        gen_random_uuid()::text,jsonb_build_object('input',p_rules,'policy_version',policy.version,'request_key',p_key,
        'code',code,'eligibility',eligibility,'funder','platform','stacking',false,'consumption','verified_discounted_checks',
        'rounding','nearest_piastre_half_up_per_check','fixed_scope','per_check'),now(),p_actor_telegram_id,p_reference)
    returning * into c;
    insert into commerce.codes(code,kind,campaign_id,active,max_redemptions) values(code,'discount',c.id,true,redemptions);
    return jsonb_build_object('campaign_id',c.id,'code',code,'replayed',false);
end $$;
create unique index campaigns_request_key on commerce.campaigns((rules->>'request_key')) where rules ? 'request_key';

create function commerce_rpc.activate_campaign(p_telegram_user_id bigint,p_code text,p_reference text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare u commerce.users%rowtype;c commerce.campaigns%rowtype;code commerce.codes%rowtype;usage commerce.campaign_usage%rowtype;
    snapshot jsonb;expiry timestamptz;w commerce.wallets%rowtype;j uuid;total numeric;
begin
    if p_reference is null or length(btrim(p_reference)) not between 1 and 1000 then raise exception 'Activation receipt required' using errcode='22023'; end if;
    perform commerce_rpc.register_telegram_user(p_telegram_user_id,null);
    select * into u from commerce.users where telegram_user_id=p_telegram_user_id and registration_state='active' for update;
    if not found then raise exception 'Active customer required' using errcode='22023'; end if;
    select ca.* into c from commerce.campaigns ca join commerce.codes co on co.campaign_id=ca.id
        where co.code=upper(btrim(p_code)) and co.kind='discount' for update of ca;
    if not found then return jsonb_build_object('state','invalid_code'); end if;
    select * into usage from commerce.campaign_usage where campaign_id=c.id and user_id=u.id;
    if found then return jsonb_build_object('state','already_activated','campaign_id',c.id,'expires_at',usage.expires_at,'wallet_credit_minor',case when usage.grant_journal_id is null then 0 else usage.discount_minor end); end if;
    select co.* into code from commerce.codes co where co.code=upper(btrim(p_code)) for update;
    if not code.active or (code.expires_at is not null and code.expires_at<=now()) or c.state<>'active'
       or c.funder_kind<>'platform' or c.rules->>'stacking' is distinct from 'false'
       or c.max_total_discount_minor is null or c.max_checks_per_user is null or c.activation_duration_seconds is null
       or (c.starts_at is not null and c.starts_at>now()) or (c.ends_at is not null and c.ends_at<=now()) then
        return jsonb_build_object('state','inactive_offer'); end if;
    if (c.rules->>'eligibility'='new' and u.first_paid_service_at is not null)
       or (c.rules->>'eligibility'='existing' and u.first_paid_service_at is null) then
        return jsonb_build_object('state','ineligible_customer'); end if;
    if (select count(*) from commerce.code_redemptions where code_id=code.id)>=code.max_redemptions then
        return jsonb_build_object('state','code_exhausted'); end if;
    expiry:=now()+c.activation_duration_seconds*interval '1 second';
    if c.ends_at is not null then expiry:=least(expiry,c.ends_at); end if;
    snapshot:=to_jsonb(c)||jsonb_build_object('activation_reference',p_reference);
    select coalesce(sum(discount_minor::numeric+reserved_discount_minor),0) into total from commerce.campaign_usage where campaign_id=c.id;
    if total>=c.max_total_discount_minor then return jsonb_build_object('state','budget_exhausted'); end if;
    if c.benefit_kind='wallet_credit' then
        if total+c.benefit_value>c.max_total_discount_minor then return jsonb_build_object('state','budget_exhausted'); end if;
        select * into w from commerce.wallets where user_id=u.id and kind='customer' for update;
        j:=commerce.post_pair('campaign:grant:'||c.id||':'||u.id,'adjustment',null,'platform:promotion',
            'wallet:'||w.id||':available',c.benefit_value,null,w.id);
        update commerce.wallets set available_minor=available_minor+c.benefit_value,promotional_available_minor=promotional_available_minor+c.benefit_value where id=w.id;
    end if;
    insert into commerce.campaign_usage(campaign_id,user_id,rules_snapshot,expires_at,discount_minor,grant_journal_id)
        values(c.id,u.id,snapshot,expiry,case when j is null then 0 else c.benefit_value end,j);
    insert into commerce.code_redemptions(code_id,user_id,idempotency_key,journal_id)
        values(code.id,u.id,'campaign:activate:'||c.id||':'||u.id,j);
    return jsonb_build_object('state','activated','campaign_id',c.id,'expires_at',expiry,'wallet_credit_minor',case when j is null then 0 else c.benefit_value end);
end $$;

create function commerce_rpc.customer_campaigns(p_telegram_user_id bigint,p_offset integer default 0)
returns jsonb language plpgsql stable security definer set search_path='' as $$
begin
    if p_offset is null or p_offset not between 0 and 1000000 then raise exception 'Invalid offer page' using errcode='22023'; end if;
    return (select coalesce(jsonb_agg(jsonb_build_object('campaign_id',cu.campaign_id,'name',cu.rules_snapshot->>'name',
        'benefit_kind',cu.rules_snapshot->>'benefit_kind','benefit_value',cu.rules_snapshot->'benefit_value',
        'check_types',cu.rules_snapshot->'eligible_check_types','activated_at',cu.activated_at,'expires_at',cu.expires_at,
        'max_checks',cu.rules_snapshot->'max_checks_per_user','max_discount_minor',cu.rules_snapshot->'max_discount_minor_per_user',
        'reserved_checks',cu.reserved_checks,'consumed_checks',cu.consumed_checks,'discount_minor',cu.discount_minor,
        'reserved_discount_minor',cu.reserved_discount_minor,'state',cu.campaign_state,'funder','platform',
        'global_available_discount_minor',greatest(cu.global_budget-(select coalesce(sum(discount_minor::numeric+reserved_discount_minor),0)
            from commerce.campaign_usage where campaign_id=cu.campaign_id),0)) order by cu.activated_at desc),'[]'::jsonb)
    from (select usage.*,c.state as campaign_state,c.max_total_discount_minor as global_budget from commerce.campaign_usage usage join commerce.users u on u.id=usage.user_id
        join commerce.campaigns c on c.id=usage.campaign_id where u.telegram_user_id=p_telegram_user_id
        order by usage.activated_at desc,usage.campaign_id limit 5 offset p_offset) cu);
end
$$;
create function commerce_rpc.list_campaigns(p_offset integer default 0)
returns jsonb language plpgsql stable security definer set search_path='' as $$
begin
    if p_offset is null or p_offset not between 0 and 1000000 then raise exception 'Invalid campaign page' using errcode='22023'; end if;
    return coalesce((select jsonb_agg(to_jsonb(c)) from (select ca.*,
        (select co.code from commerce.codes co where co.campaign_id=ca.id limit 1) as code,
        (select count(*) from commerce.campaign_usage where campaign_id=ca.id) as activations,
        (select coalesce(sum(discount_minor),0) from commerce.campaign_usage where campaign_id=ca.id) as used_discount_minor,
        (select coalesce(sum(reserved_discount_minor),0) from commerce.campaign_usage where campaign_id=ca.id) as held_discount_minor
        from commerce.campaigns ca order by created_at desc,id desc limit 10 offset p_offset) c),'[]'::jsonb);
end $$;
create function commerce_rpc.pause_campaign(p_campaign_id uuid,p_actor_telegram_id bigint,p_reference text)
returns jsonb language plpgsql security definer set search_path='' as $$
begin
    if p_actor_telegram_id is null or p_actor_telegram_id<=0 or p_reference is null or length(btrim(p_reference)) not between 1 and 1000 then
        raise exception 'Authenticated campaign decision required' using errcode='22023'; end if;
    update commerce.campaigns set state='paused',rules=rules||jsonb_build_object('paused_by',p_actor_telegram_id,'pause_reference',p_reference) where id=p_campaign_id and state='active';
    if not found and not exists(select 1 from commerce.campaigns where id=p_campaign_id and state='paused') then raise exception 'Active campaign required' using errcode='22023'; end if;
    return jsonb_build_object('state','paused');
end $$;

alter function commerce_rpc.create_order(bigint,text,jsonb,jsonb) set schema commerce;
revoke all on function commerce.create_order(bigint,text,jsonb,jsonb) from public,anon,authenticated,service_role;
create function commerce_rpc.create_order(p_telegram_user_id bigint,p_request_key text,p_rows jsonb,p_source jsonb)
returns jsonb language plpgsql security definer set search_path='' as $$
declare result jsonb;o commerce.orders%rowtype;c commerce.campaigns%rowtype;usage commerce.campaign_usage%rowtype;
    row_data record;remaining_count bigint;remaining_budget bigint;global_used numeric;discount bigint;
    candidate jsonb;eligible jsonb:='[]';best jsonb:='[]';best_snapshot jsonb;best_id uuid;
    candidate_total numeric;best_total numeric:=0;best_count bigint:=0;count_used bigint;
begin
    result:=commerce.create_order(p_telegram_user_id,p_request_key,p_rows,p_source);
    select * into o from commerce.orders where id=(result->>'order_id')::uuid for update;
    if o.campaigns_evaluated_at is not null then return result; end if;
    for c in select ca.* from commerce.campaigns ca join commerce.campaign_usage cu on cu.campaign_id=ca.id
        where cu.user_id=o.user_id and ca.state='active' and cu.expires_at>now() and ca.benefit_kind<>'wallet_credit'
        and ca.funder_kind='platform' and not ca.allow_stacking order by ca.id for update of ca loop
        select * into usage from commerce.campaign_usage where campaign_id=c.id and user_id=o.user_id for update;
        remaining_count:=(usage.rules_snapshot->>'max_checks_per_user')::bigint-usage.reserved_checks-usage.consumed_checks;
        select coalesce(sum(discount_minor::numeric+reserved_discount_minor),0) into global_used from commerce.campaign_usage where campaign_id=c.id;
        remaining_budget:=least((usage.rules_snapshot->>'max_discount_minor_per_user')::bigint-usage.discount_minor-usage.reserved_discount_minor,
            greatest(c.max_total_discount_minor::numeric-global_used,0))::bigint;
        candidate:='[]';candidate_total:=0;count_used:=0;
        for row_data in select * from commerce.checks where order_id=o.id order by row_number loop
            if remaining_count<=0 or remaining_budget<=0 then exit; end if;
            if not (usage.rules_snapshot->'eligible_check_types') ? row_data.check_type then continue; end if;
            discount:=case usage.rules_snapshot->>'benefit_kind'
                when 'percent' then round(row_data.unit_quote_minor::numeric*(usage.rules_snapshot->>'benefit_value')::bigint/10000)::bigint
                when 'fixed' then least(row_data.unit_quote_minor,(usage.rules_snapshot->>'benefit_value')::bigint)
                when 'free_checks' then row_data.unit_quote_minor else 0 end;
            if usage.rules_snapshot->>'benefit_kind'='free_checks' and discount>remaining_budget then continue; end if;
            discount:=least(discount,remaining_budget,row_data.unit_quote_minor);
            if discount<=0 then continue; end if;
            candidate:=candidate||jsonb_build_array(jsonb_build_object('row_number',row_data.row_number,'discount_minor',discount));
            candidate_total:=candidate_total+discount;remaining_count:=remaining_count-1;
            remaining_budget:=remaining_budget-discount;count_used:=count_used+1;
        end loop;
        eligible:=eligible||jsonb_build_array(jsonb_build_object('campaign_id',c.id,'name',c.name,'discount_minor',candidate_total));
        if candidate_total>best_total then
            best:=candidate;best_total:=candidate_total;best_id:=c.id;best_snapshot:=usage.rules_snapshot;best_count:=count_used;
        end if;
    end loop;
    if best_total>0 then
        for row_data in select * from jsonb_to_recordset(best) as r(row_number bigint,discount_minor bigint) loop
            update commerce.checks set discount_minor=row_data.discount_minor where order_id=o.id and row_number=row_data.row_number;
            insert into commerce.order_discounts(order_id,row_number,campaign_id,discount_minor,funder_snapshot)
                values(o.id,row_data.row_number,best_id,row_data.discount_minor,jsonb_build_object('kind','platform','platform_minor',row_data.discount_minor,
                    'partner_minor',0,'rules',best_snapshot,'stacking',false));
        end loop;
        update commerce.campaign_usage set reserved_checks=reserved_checks+best_count,reserved_discount_minor=reserved_discount_minor+best_total::bigint
            where campaign_id=best_id and user_id=o.user_id;
    end if;
    update commerce.orders set quoted_minor=quoted_minor-best_total::bigint,campaigns_evaluated_at=now(),
        pricing_snapshot=pricing_snapshot||jsonb_build_object('base_total_minor',quoted_minor,'discount_total_minor',best_total::bigint,
            'applied_campaign',case when best_id is null then null else jsonb_build_object('campaign_id',best_id,'name',best_snapshot->>'name','discounted_checks',best_count) end,
            'eligible_campaigns',eligible,'stacking',false,'total_minor',quoted_minor-best_total::bigint)
        where id=o.id;
    return result||jsonb_build_object('quoted_minor',o.quoted_minor-best_total::bigint,'discount_minor',best_total::bigint);
end $$;

create function commerce.finalize_campaign_usage(p_order_id uuid,p_cancel boolean default false)
returns void language plpgsql set search_path='' as $$
declare o commerce.orders%rowtype; item record;used_count bigint;used_discount numeric;reserved_count bigint;reserved_discount numeric;
begin
    select * into o from commerce.orders where id=p_order_id for update;
    if o.campaign_usage_finalized_at is not null then return; end if;
    for item in select distinct campaign_id from commerce.order_discounts where order_id=o.id order by campaign_id loop
        perform 1 from commerce.campaigns where id=item.campaign_id for update;
        select count(*),coalesce(sum(d.discount_minor::numeric),0),
            count(*) filter(where c.verified_result and not p_cancel),coalesce(sum(d.discount_minor::numeric) filter(where c.verified_result and not p_cancel),0)
            into reserved_count,reserved_discount,used_count,used_discount
            from commerce.order_discounts d join commerce.checks c on c.order_id=d.order_id and c.row_number=d.row_number
            where d.order_id=o.id and d.campaign_id=item.campaign_id and d.usage_state='reserved';
        update commerce.campaign_usage set reserved_checks=reserved_checks-reserved_count,
            reserved_discount_minor=reserved_discount_minor-reserved_discount::bigint,
            consumed_checks=consumed_checks+used_count,discount_minor=discount_minor+used_discount::bigint
            where campaign_id=item.campaign_id and user_id=o.user_id;
        update commerce.order_discounts d set applied=not p_cancel and c.verified_result,
            usage_state=case when not p_cancel and c.verified_result then 'consumed' else 'released' end
            from commerce.checks c where d.order_id=o.id and d.campaign_id=item.campaign_id and c.order_id=d.order_id and c.row_number=d.row_number;
    end loop;
    update commerce.orders set campaign_usage_finalized_at=now() where id=o.id;
end $$;
revoke all on function commerce.finalize_campaign_usage(uuid,boolean) from public,anon,authenticated,service_role;
alter function commerce_rpc.reserve_order(uuid,bigint,text,uuid) rename to reserve_order_consent;
alter function commerce_rpc.reserve_order_consent(uuid,bigint,text,uuid) set schema commerce;
revoke all on function commerce.reserve_order_consent(uuid,bigint,text,uuid) from public,anon,authenticated,service_role;
create function commerce_rpc.reserve_order(p_order_id uuid,p_telegram_user_id bigint,p_consent_reference text,p_payment_id uuid default null)
returns jsonb language plpgsql security definer set search_path='' as $$
declare o commerce.orders%rowtype;w commerce.wallets%rowtype;held bigint:=0;remaining bigint;row_data record;result jsonb;
begin
    perform 1 from commerce.users where telegram_user_id=p_telegram_user_id and registration_state='active' for update;
    select ord.* into o from commerce.orders ord join commerce.users u on u.id=ord.user_id
        where ord.id=p_order_id and u.telegram_user_id=p_telegram_user_id for update of ord;
    if not found then raise exception 'Owned checkout required' using errcode='22023'; end if;
    if exists(select 1 from commerce.reservations where order_id=o.id) then
        return commerce.reserve_order_consent(p_order_id,p_telegram_user_id,p_consent_reference,p_payment_id); end if;
    if p_payment_id is null then
        select * into w from commerce.wallets where user_id=o.user_id and kind='customer' for update;
        if w.available_minor>=o.quoted_minor then
            held:=least(w.promotional_available_minor,o.quoted_minor);remaining:=held;
            -- Spend promotional credit first, preserving the paid-money portion.
            for row_data in select * from commerce.checks where order_id=o.id order by row_number loop
                update commerce.checks set promotional_payment_minor=least(remaining,row_data.unit_quote_minor-row_data.discount_minor)
                    where order_id=o.id and row_number=row_data.row_number;
                remaining:=greatest(remaining-(row_data.unit_quote_minor-row_data.discount_minor),0);
            end loop;
            update commerce.wallets set promotional_available_minor=promotional_available_minor-held where id=w.id;
        end if;
    end if;
    result:=commerce.reserve_order_consent(p_order_id,p_telegram_user_id,p_consent_reference,p_payment_id);
    if result->>'state'='held' and held>0 then
        update commerce.reservations set promotional_minor=held where order_id=o.id;
        update commerce.wallets set promotional_reserved_minor=promotional_reserved_minor+held where id=w.id;
    end if;
    return result;
end $$;
revoke all on function commerce_rpc.reserve_order(uuid,bigint,text,uuid) from public,anon,authenticated;
grant execute on function commerce_rpc.reserve_order(uuid,bigint,text,uuid) to service_role;
alter function commerce_rpc.settle_order(uuid) rename to settle_order_commissions;
alter function commerce_rpc.settle_order_commissions(uuid) set schema commerce;
revoke all on function commerce.settle_order_commissions(uuid) from public,anon,authenticated,service_role;
create or replace function commerce.settle_order_commissions(p_order_id uuid)
returns jsonb language plpgsql security definer set search_path='' as $$
declare o commerce.orders%rowtype; u commerce.users%rowtype; w commerce.wallets%rowtype;
    ref commerce.referrals%rowtype; result jsonb; row_data record; snapshot jsonb;
    paid_count bigint; eligible_count bigint:=0; prior_count bigint; amount bigint; total numeric:=0;
    prior_anchor timestamptz; prior_locked timestamptz; promo_held bigint; promo_refund bigint;
    ordinal bigint; rate integer; eligible boolean; mode text; journal uuid; pid uuid;
begin
    select usr.* into u from commerce.users usr join commerce.orders ord on ord.user_id=usr.id
        where ord.id=p_order_id for update of usr;
    if not found then raise exception 'Unknown order' using errcode='22023'; end if;
    select * into o from commerce.orders where id=p_order_id for update;
    if o.commission_finalized_at is not null then
        select coalesce(sum(amount_minor),0),count(*) into total,eligible_count from commerce.commissions where order_id=o.id;
        return jsonb_build_object('state',o.payment_state,'charged_minor',o.final_charge_minor,
            'collected_minor',o.collected_cash_minor,
            'refunded_minor',o.quoted_minor-o.final_charge_minor,'commission_minor',total::bigint,
            'eligible_paid_checks',eligible_count,'replayed',true);
    end if;
    if o.referral_partner_id is null then
        select * into ref from commerce.referrals where user_id=u.id;
        if found then
            if ref.terms_snapshot='{}' then raise exception 'Referral terms require historical review' using errcode='22023'; end if;
            update commerce.orders set referral_partner_id=ref.partner_id,partner_terms_snapshot=ref.terms_snapshot where id=o.id;
        end if;
    end if;
    prior_anchor:=u.first_paid_service_at;
    select locked_at into prior_locked from commerce.referrals where user_id=u.id;
    select promotional_minor into promo_held from commerce.reservations where order_id=o.id;
    select coalesce(sum(promotional_payment_minor),0) into promo_refund from commerce.checks where order_id=o.id and not verified_result;
    if coalesce(promo_held,0)>0 then
        update commerce.wallets set promotional_reserved_minor=promotional_reserved_minor-promo_held where user_id=u.id and kind='customer';
    end if;
    result:=commerce.settle_order(o.id);
    if promo_refund>0 then
        update commerce.wallets set promotional_available_minor=promotional_available_minor+promo_refund where user_id=u.id and kind='customer';
    end if;
    if not exists(select 1 from commerce.checks where order_id=o.id and verified_result and unit_quote_minor>discount_minor+promotional_payment_minor) then
        update commerce.users set first_paid_service_at=prior_anchor where id=u.id;
        update commerce.referrals set locked_at=prior_locked where user_id=u.id;
    end if;
    update commerce.orders set collected_cash_minor=final_charge_minor-(select coalesce(sum(promotional_payment_minor),0) from commerce.checks where order_id=o.id and verified_result) where id=o.id;
    select * into o from commerce.orders where id=o.id;
    select * into u from commerce.users where id=o.user_id;
    prior_count:=u.successful_paid_checks;
    select count(*) into paid_count from commerce.checks where order_id=o.id and verified_result and unit_quote_minor>discount_minor+promotional_payment_minor;
    snapshot:=o.partner_terms_snapshot;pid:=o.referral_partner_id;
    if pid is not null and paid_count>0 then
        if snapshot->>'partner_id' is distinct from pid::text or snapshot->>'state' is distinct from 'active'
           or snapshot->>'basis_points' is null or snapshot->>'mode' is null then
            -- Pending/inactive quoted terms earn no commission; keep attribution.
            eligible:=false;
        else eligible:=true; end if;
        if eligible then
            rate:=(snapshot->>'basis_points')::integer;mode:=snapshot->>'mode';
            if rate not between 0 and 10000 or mode not in ('checks','duration','forever')
               or (mode='checks' and coalesce((snapshot->>'checks')::bigint,0)<=0)
               or (mode='duration' and coalesce((snapshot->>'duration_seconds')::bigint,0)<=0) then
                raise exception 'Invalid frozen commission terms' using errcode='22023'; end if;
            select wal.* into w from commerce.wallets wal join commerce.partners p on p.user_id=wal.user_id
                where p.id=pid and wal.kind='partner' and wal.currency='EGP' for update of wal;
            if not found then raise exception 'Partner earnings wallet unavailable' using errcode='22023'; end if;
            ordinal:=prior_count;
            for row_data in select row_number,unit_quote_minor-discount_minor-promotional_payment_minor as revenue from commerce.checks
                where order_id=o.id and verified_result and unit_quote_minor>discount_minor+promotional_payment_minor order by row_number loop
                ordinal:=ordinal+1;
                if (mode='checks' and ordinal>(snapshot->>'checks')::bigint)
                   or (mode='duration' and extract(epoch from (now()-u.first_paid_service_at))>=(snapshot->>'duration_seconds')::bigint) then
                    continue;
                end if;
                amount:=round(row_data.revenue::numeric*rate/10000)::bigint;journal:=null;
                if amount>0 then
                    journal:=commerce.post_pair('commission:'||o.id||':'||row_data.row_number,'commission',o.id,
                        'platform:revenue','wallet:'||w.id||':available',amount,null,w.id);
                end if;
                insert into commerce.commissions(order_id,row_number,partner_id,revenue_minor,amount_minor,terms_snapshot,state,journal_id)
                values(o.id,row_data.row_number,pid,row_data.revenue,amount,snapshot||jsonb_build_object(
                    'paid_check_ordinal',ordinal,'basis','collected_after_discounts_and_refunds','expenses_deducted',false,
                    'promotional_payment_excluded',true,
                    'rounding','nearest_piastre_half_up_per_check','duration_measurement','paid_service_settled_at'),'available',journal);
                total:=total+amount;eligible_count:=eligible_count+1;
            end loop;
            update commerce.wallets set available_minor=available_minor+total::bigint where id=w.id;
        end if;
    end if;
    update commerce.users set successful_paid_checks=successful_paid_checks+paid_count where id=u.id;
    update commerce.orders set financial_completed_at=now(),commission_finalized_at=now() where id=o.id;
    return result||jsonb_build_object('collected_minor',o.collected_cash_minor,'commission_minor',total::bigint,'eligible_paid_checks',eligible_count);
end $$;

create or replace function commerce_rpc.partner_finance(p_telegram_user_id bigint)
returns jsonb language plpgsql stable security definer set search_path='' as $$
declare pid uuid; uid uuid; w commerce.wallets%rowtype;
begin
    select p.id,p.user_id into pid,uid from commerce.partners p join commerce.users u on u.id=p.user_id
        where u.telegram_user_id=p_telegram_user_id;
    if not found then return jsonb_build_object('is_partner',false); end if;
    select * into w from commerce.wallets where user_id=uid and kind='partner';
    return jsonb_build_object('is_partner',true,'partner_id',pid,'available_minor',w.available_minor,
        'referral_code',(select code from commerce.codes where partner_id=pid and kind='referral' limit 1),
        'reserved_minor',w.reserved_minor,'withdrawn_minor',w.withdrawn_minor,
        'pending_commission_minor',(select coalesce(sum(amount_minor),0) from commerce.commissions where partner_id=pid and state='pending'),
        'today_earned_minor',(select coalesce(sum(amount_minor),0) from commerce.commissions where partner_id=pid
            and created_at>=date_trunc('day',now() at time zone 'Asia/Baghdad') at time zone 'Asia/Baghdad' and state<>'reversed'),
        'clients',(select count(*) from commerce.referrals where partner_id=pid),
        'paid_checks',(select count(*) from commerce.checks c join commerce.orders o on o.id=c.order_id
            where o.referral_partner_id=pid and o.commission_finalized_at is not null and c.verified_result and c.unit_quote_minor>c.discount_minor+c.promotional_payment_minor),
        'collected_minor',(select coalesce(sum(coalesce(collected_cash_minor,final_charge_minor)),0) from commerce.orders where referral_partner_id=pid and commission_finalized_at is not null),
        'awaiting_settlement_orders',(select count(*) from commerce.orders where referral_partner_id=pid and payment_state='reserved'));
end $$;

create function commerce_rpc.settle_order(p_order_id uuid)
returns jsonb language plpgsql security definer set search_path='' as $$
declare result jsonb;
begin
    result:=commerce.settle_order_commissions(p_order_id);
    perform commerce.finalize_campaign_usage(p_order_id);
    return result;
end $$;
create function commerce_rpc.cancel_campaign_checkout(p_order_id uuid,p_telegram_user_id bigint)
returns jsonb language plpgsql security definer set search_path='' as $$
declare o commerce.orders%rowtype;
begin
    perform 1 from commerce.users where telegram_user_id=p_telegram_user_id and registration_state='active' for update;
    select ord.* into o from commerce.orders ord join commerce.users u on u.id=ord.user_id
        where ord.id=p_order_id and u.telegram_user_id=p_telegram_user_id for update of ord;
    if not found then raise exception 'Owned checkout required' using errcode='22023'; end if;
    if o.order_state='cancelled' then return jsonb_build_object('state','cancelled','replayed',true); end if;
    if o.order_state<>'draft' or o.payment_state<>'unpaid'
       or exists(select 1 from commerce.payments where order_id=o.id)
       or exists(select 1 from commerce.payment_requests where order_id=o.id)
       or exists(select 1 from commerce.reservations where order_id=o.id) then
        raise exception 'Checkout with a funding request must retain its quote' using errcode='22023'; end if;
    perform commerce.finalize_campaign_usage(o.id,true);
    update commerce.orders set order_state='cancelled' where id=o.id;
    return jsonb_build_object('state','cancelled','replayed',false);
end $$;
create or replace function commerce_rpc.wallet_state(p_telegram_user_id bigint)
returns jsonb language sql stable security definer set search_path='' as $$
    select jsonb_build_object('user_id',u.id,'currency','EGP','is_partner',exists(select 1 from commerce.partners where user_id=u.id),
        'customer',coalesce((select jsonb_build_object('available_minor',available_minor,'reserved_minor',reserved_minor,
            'promotional_available_minor',promotional_available_minor,'promotional_reserved_minor',promotional_reserved_minor)
            from commerce.wallets where user_id=u.id and kind='customer'),'{}'::jsonb),
        'partner',coalesce((select jsonb_build_object('available_minor',available_minor,'reserved_minor',reserved_minor,'withdrawn_minor',withdrawn_minor)
            from commerce.wallets where user_id=u.id and kind='partner'),'{}'::jsonb)) from commerce.users u where u.telegram_user_id=p_telegram_user_id
$$;
revoke all on function commerce_rpc.configure_campaign_policy(bigint,jsonb,text),commerce_rpc.campaign_policy(),
    commerce_rpc.create_platform_campaign(bigint,text,text,jsonb,text),commerce_rpc.activate_campaign(bigint,text,text),
    commerce_rpc.customer_campaigns(bigint,integer),commerce_rpc.list_campaigns(integer),commerce_rpc.pause_campaign(uuid,bigint,text),
    commerce_rpc.create_order(bigint,text,jsonb,jsonb),commerce_rpc.settle_order(uuid),commerce_rpc.cancel_campaign_checkout(uuid,bigint)
    from public,anon,authenticated;
grant execute on function commerce_rpc.configure_campaign_policy(bigint,jsonb,text),commerce_rpc.campaign_policy(),
    commerce_rpc.create_platform_campaign(bigint,text,text,jsonb,text),commerce_rpc.activate_campaign(bigint,text,text),
    commerce_rpc.customer_campaigns(bigint,integer),commerce_rpc.list_campaigns(integer),commerce_rpc.pause_campaign(uuid,bigint,text),
    commerce_rpc.create_order(bigint,text,jsonb,jsonb),commerce_rpc.settle_order(uuid),commerce_rpc.cancel_campaign_checkout(uuid,bigint)
    to service_role;
create or replace function commerce_rpc.health()
returns jsonb language sql stable security definer set search_path='' as $$
    select jsonb_build_object('status','ok','schema_version',15,'currency','EGP')
$$;
notify pgrst,'reload schema';
commit;
