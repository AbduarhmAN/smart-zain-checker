-- Avoid rounding a 50% discount on each one-piastre row into 100% on all rows.
begin;
create or replace function commerce_rpc.create_order(p_telegram_user_id bigint,p_request_key text,p_rows jsonb,p_source jsonb)
returns jsonb language plpgsql security definer set search_path='' as $$
declare result jsonb;o commerce.orders%rowtype;c commerce.campaigns%rowtype;usage commerce.campaign_usage%rowtype;
    row_data record;remaining_count bigint;remaining_budget bigint;global_used numeric;discount bigint;
    candidate jsonb;eligible jsonb:='[]';best jsonb:='[]';best_snapshot jsonb;best_id uuid;
    candidate_total numeric;best_total numeric:=0;best_count bigint:=0;count_used bigint;
    percent_base numeric;percent_rounded bigint;next_rounded bigint;
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
        candidate:='[]';candidate_total:=0;count_used:=0;percent_base:=0;percent_rounded:=0;
        for row_data in select * from commerce.checks where order_id=o.id order by row_number loop
            if remaining_count<=0 or remaining_budget<=0 then exit; end if;
            if not (usage.rules_snapshot->'eligible_check_types') ? row_data.check_type then continue; end if;
            if row_data.unit_quote_minor=0 then continue;end if;
            discount:=case usage.rules_snapshot->>'benefit_kind'
                when 'percent' then round(row_data.unit_quote_minor::numeric*(usage.rules_snapshot->>'benefit_value')::bigint/10000)::bigint
                when 'fixed' then least(row_data.unit_quote_minor,(usage.rules_snapshot->>'benefit_value')::bigint)
                when 'free_checks' then row_data.unit_quote_minor else 0 end;
            if usage.rules_snapshot->>'benefit_kind'='percent' then
                percent_base:=percent_base+row_data.unit_quote_minor;
                next_rounded:=round(percent_base*(usage.rules_snapshot->>'benefit_value')::bigint/10000)::bigint;
                discount:=next_rounded-percent_rounded;percent_rounded:=next_rounded;
            end if;
            if usage.rules_snapshot->>'benefit_kind'='free_checks' and discount>remaining_budget then continue; end if;
            discount:=least(discount,remaining_budget,row_data.unit_quote_minor);
            if discount<=0 and usage.rules_snapshot->>'benefit_kind'<>'percent' then continue; end if;
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
create or replace function commerce_rpc.health()
returns jsonb language sql stable security definer set search_path='' as $$
    select jsonb_build_object('status','ok','schema_version',23,'currency','EGP')
$$;
notify pgrst,'reload schema';
commit;
