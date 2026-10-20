-- Migration: 202610180046_commerce_free_checks_tiered_policy.sql
-- Tiered Free Checks Policy: 3400 max customers, 1500 max service numbers, first 500 service numbers free, 501-1500 excess paid at standard rate.
begin;

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
    v_free_checks_disqualified_reason text := null;
begin
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

        -- SPECIAL TIERED HANDLING FOR FREE CHECKS OFFER (3400 MAX TOTAL, 1500 MAX SERVICES, 500 FREE SERVICES)
        if usage.rules_snapshot->>'benefit_kind' = 'free_checks' then
            select count(*),
                   count(*) filter (where check_type = 'service'),
                   count(*) filter (where check_type = 'account')
            into v_order_total_checks, v_order_service_checks, v_order_account_checks
            from commerce.checks where order_id = o.id;

            -- Disqualify if file exceeds 3400 total customers or 1500 service/wallet numbers
            if v_order_total_checks > 3400 then
                v_free_checks_disqualified_reason := 'exceeds_total_limit';
                continue;
            end if;
            if v_order_service_checks > 1500 then
                v_free_checks_disqualified_reason := 'exceeds_service_limit';
                continue;
            end if;

            candidate := '[]'; candidate_total := 0; v_free_service_seen := 0;
            for row_data in select * from commerce.checks where order_id = o.id order by row_number loop
                if row_data.unit_quote_minor = 0 then continue; end if;
                if not (usage.rules_snapshot->'eligible_check_types') ? row_data.check_type then continue; end if;

                if row_data.check_type = 'account' then
                    discount := row_data.unit_quote_minor;
                elsif row_data.check_type = 'service' then
                    v_free_service_seen := v_free_service_seen + 1;
                    if v_free_service_seen <= 500 then
                        discount := row_data.unit_quote_minor;
                    else
                        discount := 0; -- excess services from 501 to 1500 are not discounted
                    end if;
                else
                    discount := 0;
                end if;

                if discount > 0 then
                    discount := least(discount, remaining_budget, row_data.unit_quote_minor);
                    if discount > 0 then
                        candidate := candidate || jsonb_build_array(jsonb_build_object('row_number', row_data.row_number, 'discount_minor', discount));
                        candidate_total := candidate_total + discount;
                        remaining_budget := remaining_budget - discount;
                    end if;
                end if;
            end loop;

            count_used := 1; -- 1 check run consumed
            eligible := eligible || jsonb_build_array(jsonb_build_object('campaign_id', c.id, 'name', c.name, 'discount_minor', candidate_total));
            if candidate_total > best_total then
                best := candidate; best_total := candidate_total; best_id := c.id;
                best_snapshot := usage.rules_snapshot; best_count := 1; best_kind := c.funder_kind;
                is_free_checks_offer := true;
                v_free_checks_disqualified_reason := null;
            end if;
            continue;
        end if;

        -- STANDARD HANDLING FOR OTHER OFFERS (percent, fixed)
        candidate:='[]';candidate_total:=0;count_used:=0;percent_base:=0;percent_rounded:=0;
        for row_data in select * from commerce.checks where order_id=o.id order by row_number loop
            if remaining_count<=0 or remaining_budget<=0 then exit; end if;
            if not (usage.rules_snapshot->'eligible_check_types') ? row_data.check_type then continue; end if;
            if row_data.unit_quote_minor=0 then continue;end if;
            discount:=case usage.rules_snapshot->>'benefit_kind'
                when 'percent' then round(row_data.unit_quote_minor::numeric*(usage.rules_snapshot->>'benefit_value')::bigint/10000)::bigint
                when 'fixed' then least(row_data.unit_quote_minor,(usage.rules_snapshot->>'benefit_value')::bigint)
                else 0 end;
            if usage.rules_snapshot->>'benefit_kind'='percent' then
                percent_base:=percent_base+row_data.unit_quote_minor;
                next_rounded:=round(percent_base*(usage.rules_snapshot->>'benefit_value')::bigint/10000)::bigint;
                discount:=next_rounded-percent_rounded;percent_rounded:=next_rounded;
            end if;
            discount:=least(discount,remaining_budget,row_data.unit_quote_minor);
            if discount<=0 and usage.rules_snapshot->>'benefit_kind'<>'percent' then continue; end if;
            candidate:=candidate||jsonb_build_array(jsonb_build_object('row_number',row_data.row_number,'discount_minor',discount));
            candidate_total:=candidate_total+discount;remaining_count:=remaining_count-1;
            remaining_budget:=remaining_budget-discount;count_used:=count_used+1;
        end loop;
        eligible:=eligible||jsonb_build_array(jsonb_build_object('campaign_id',c.id,'name',c.name,'discount_minor',candidate_total));
        if candidate_total>best_total then
            best:=candidate;best_total:=candidate_total;best_id:=c.id;best_snapshot:=usage.rules_snapshot;best_count:=count_used;best_kind:=c.funder_kind;
            is_free_checks_offer := false;
        end if;
    end loop;
    if best_total>0 then
        for row_data in select * from jsonb_to_recordset(best) as r(row_number bigint,discount_minor bigint) loop
            update commerce.checks set discount_minor=case when best_kind='partner' then 0 else row_data.discount_minor end where order_id=o.id and row_number=row_data.row_number;
            insert into commerce.order_discounts(order_id,row_number,campaign_id,discount_minor,funder_snapshot)
                values(o.id,row_data.row_number,best_id,row_data.discount_minor,jsonb_build_object('kind',best_kind,'platform_minor',case when best_kind='platform' then row_data.discount_minor else 0 end,
                    'partner_minor',case when best_kind='partner' then row_data.discount_minor else 0 end,'rules',best_snapshot,'stacking',false));
        end loop;
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

update commerce.campaigns set
    rules = jsonb_set(jsonb_set(jsonb_set(rules, '{eligibility}', '"new"'), '{input,eligibility}', '"new"'), '{input,max_discount_minor}', '3000'),
    max_discount_minor_per_user = 3000,
    new_customer_only = true
where name = 'FREEZAIN';

update commerce.campaign_usage set
    rules_snapshot = jsonb_set(jsonb_set(jsonb_set(rules_snapshot, '{rules,eligibility}', '"new"'), '{eligibility}', '"new"'), '{max_discount_minor_per_user}', '3000')
where campaign_id in (select id from commerce.campaigns where name = 'FREEZAIN');

notify pgrst,'reload schema';
commit;
