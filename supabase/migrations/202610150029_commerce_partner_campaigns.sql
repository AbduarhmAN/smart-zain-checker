-- Partner discounts are donated from this order's original commission.
-- No prior earnings are held, debited or required. Existing quotes stay frozen.
begin;


create function commerce_rpc.partner_campaign_context(p_telegram_user_id bigint)
returns jsonb language plpgsql stable security definer set search_path='' as $$
declare p commerce.partners%rowtype;
begin
    select pa.* into p from commerce.partners pa join commerce.users u on u.id=pa.user_id
        where u.telegram_user_id=p_telegram_user_id and u.registration_state='active';
    if not found then return jsonb_build_object('is_partner',false);end if;
    return jsonb_build_object('is_partner',true,'state',p.state,'basis_points',p.commission_basis_points,
        'terms_version',p.terms_version,'policy',commerce_rpc.campaign_policy(),'funding','current_order_original_share');
end $$;

create function commerce_rpc.create_partner_campaign(p_telegram_user_id bigint,p_key text,p_code text,p_rules jsonb,p_reference text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare p commerce.partners%rowtype;c commerce.campaigns%rowtype;result jsonb;key text;
begin
    select pa.* into p from commerce.partners pa join commerce.users u on u.id=pa.user_id
        where u.telegram_user_id=p_telegram_user_id and u.registration_state='active' for update of pa;
    if not found then raise exception 'Active partner required' using errcode='22023';end if;
    if p_key is null or length(p_key) not between 1 and 90 then raise exception 'Partner campaign key required' using errcode='22023';end if;
    key:='partner:'||p.id||':'||p_key;
    select * into c from commerce.campaigns where rules->>'request_key'=key;
    if found then
        if c.partner_id is distinct from p.id or c.rules->'input' is distinct from p_rules or c.rules->>'code' is distinct from upper(btrim(p_code)) then
            raise exception 'Conflicting campaign replay' using errcode='22023';end if;
        return jsonb_build_object('campaign_id',c.id,'code',c.rules->>'code','replayed',true);
    end if;
    if p.state<>'active' or p_rules->>'benefit_kind' is distinct from 'percent'
       or jsonb_typeof(p_rules->'benefit_value') is distinct from 'number'
       or coalesce(p_rules->>'benefit_value','') !~ '^[1-9][0-9]{0,4}$'
       or (p_rules->>'benefit_value')::bigint>p.commission_basis_points then
        raise exception 'Partner discount exceeds approved share' using errcode='22023';end if;
    -- Reuse the administration's validated limits and atomic idempotent issuance.
    result:=commerce_rpc.create_platform_campaign(p_telegram_user_id,key,p_code,p_rules,p_reference);
    update commerce.campaigns set funder_kind='partner',partner_id=p.id,
        rules=rules||jsonb_build_object('funder','partner','funding','current_order_original_share',
            'approved_partner_terms',commerce.partner_snapshot(p.id),'rounding','aggregate_verified_gross_half_up',
            'prior_earnings_required',false) where id=(result->>'campaign_id')::uuid;
    return result;
end $$;

create function commerce_rpc.list_partner_campaigns(p_telegram_user_id bigint,p_offset integer default 0)
returns jsonb language plpgsql stable security definer set search_path='' as $$
declare pid uuid;
begin
    if p_offset is null or p_offset not between 0 and 1000000 then raise exception 'Invalid campaign page' using errcode='22023';end if;
    select p.id into pid from commerce.partners p join commerce.users u on u.id=p.user_id where u.telegram_user_id=p_telegram_user_id;
    if not found then raise exception 'Active partner required' using errcode='22023';end if;
    return coalesce((select jsonb_agg(to_jsonb(c)) from (select ca.id,ca.name,ca.state,ca.benefit_value,ca.rules->>'code' as code,
        ca.max_checks_per_user,ca.max_discount_minor_per_user,ca.max_total_discount_minor,ca.activation_duration_seconds,
        (select count(*) from commerce.campaign_usage where campaign_id=ca.id) as activations,
        (select coalesce(sum(discount_minor),0) from commerce.campaign_usage where campaign_id=ca.id) as used_discount_minor,
        (select coalesce(sum(reserved_discount_minor),0) from commerce.campaign_usage where campaign_id=ca.id) as held_discount_minor
        from commerce.campaigns ca where ca.partner_id=pid and ca.funder_kind='partner'
        order by ca.created_at desc,ca.id desc limit 10 offset p_offset) c),'[]'::jsonb);
end $$;

create function commerce_rpc.pause_partner_campaign(p_telegram_user_id bigint,p_campaign_id uuid,p_reference text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare pid uuid;
begin
    select p.id into pid from commerce.partners p join commerce.users u on u.id=p.user_id
        where u.telegram_user_id=p_telegram_user_id and u.registration_state='active';
    if not found or p_reference is null or length(btrim(p_reference)) not between 1 and 1000 then
        raise exception 'Owned partner campaign required' using errcode='22023';end if;
    perform 1 from commerce.campaigns where id=p_campaign_id and partner_id=pid and funder_kind='partner' for update;
    if not found then raise exception 'Owned partner campaign required' using errcode='22023';end if;
    return commerce_rpc.pause_campaign(p_campaign_id,p_telegram_user_id,p_reference);
end $$;


create or replace function commerce.activate_campaign_without_expiry_lots(p_telegram_user_id bigint,p_code text,p_reference text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare u commerce.users%rowtype;c commerce.campaigns%rowtype;code commerce.codes%rowtype;usage commerce.campaign_usage%rowtype;
    ref commerce.referrals%rowtype;partner commerce.partners%rowtype;ref_code text;attribution jsonb;
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
       or c.funder_kind not in ('platform','partner') or c.rules->>'stacking' is distinct from 'false'
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
    if c.funder_kind='partner' then
        select * into partner from commerce.partners where id=c.partner_id for share;
        if not found or partner.state<>'active' or c.benefit_kind<>'percent' or c.benefit_value>partner.commission_basis_points then
            return jsonb_build_object('state','inactive_offer');end if;
        select * into ref from commerce.referrals where user_id=u.id;
        if not found then
            select co.code into ref_code from commerce.codes co where co.partner_id=partner.id and co.kind='referral' and co.active;
            if ref_code is null then return jsonb_build_object('state','ineligible_customer');end if;
            attribution:=commerce_rpc.attribute_referral(p_telegram_user_id,ref_code,'manual');
            select * into ref from commerce.referrals where user_id=u.id;
        end if;
        if ref.partner_id is distinct from partner.id or ref.terms_snapshot->>'state' is distinct from 'active'
           or c.benefit_value>coalesce((ref.terms_snapshot->>'basis_points')::bigint,0)
           or (ref.terms_snapshot->>'mode'='checks' and u.successful_paid_checks>=(ref.terms_snapshot->>'checks')::bigint)
           or (ref.terms_snapshot->>'mode'='duration' and u.first_paid_service_at is not null
               and now()>=u.first_paid_service_at+(ref.terms_snapshot->>'duration_seconds')::bigint*interval '1 second') then
            return jsonb_build_object('state','ineligible_customer');end if;
    end if;
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

create or replace function commerce_rpc.create_order(p_telegram_user_id bigint,p_request_key text,p_rows jsonb,p_source jsonb)
returns jsonb language plpgsql security definer set search_path='' as $$
declare result jsonb;o commerce.orders%rowtype;c commerce.campaigns%rowtype;usage commerce.campaign_usage%rowtype;
    row_data record;remaining_count bigint;remaining_budget bigint;global_used numeric;discount bigint;
    candidate jsonb;eligible jsonb:='[]';best jsonb:='[]';best_snapshot jsonb;best_id uuid;
    candidate_total numeric;best_total numeric:=0;best_count bigint:=0;count_used bigint;
    partner commerce.partners%rowtype;customer commerce.users%rowtype;best_kind text;terms_limit bigint;
    percent_base numeric;percent_rounded bigint;next_rounded bigint;
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
            best:=candidate;best_total:=candidate_total;best_id:=c.id;best_snapshot:=usage.rules_snapshot;best_count:=count_used;best_kind:=c.funder_kind;
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
            'applied_campaign',case when best_id is null then null else jsonb_build_object('campaign_id',best_id,'name',best_snapshot->>'name','discounted_checks',best_count) end,
            'eligible_campaigns',eligible,'stacking',false,'total_minor',quoted_minor-case when best_kind='partner' then 0 else best_total::bigint end,
            'partner_future_offer',best_kind='partner','estimated_final_minor',quoted_minor-best_total::bigint,
            'discount_funding',best_kind,'cash_only',best_kind='partner','offer_settlement','verified_results_after_delivery')
        where id=o.id;
    return result||jsonb_build_object('quoted_minor',o.quoted_minor-case when best_kind='partner' then 0 else best_total::bigint end,'discount_minor',best_total::bigint);
end $$;

create or replace function commerce.reserve_order_before_expiry_lots(p_order_id uuid,p_telegram_user_id bigint,p_consent_reference text,p_payment_id uuid default null)
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
        if coalesce((o.pricing_snapshot->>'partner_future_offer')::boolean,false) then
            if w.available_minor-w.promotional_available_minor<o.quoted_minor then
                return jsonb_build_object('state','insufficient_balance','available_minor',w.available_minor-w.promotional_available_minor,'cash_only',true);end if;
        elsif w.available_minor>=o.quoted_minor then
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

create or replace function commerce.settle_order(p_order_id uuid)
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
    if not coalesce((o.pricing_snapshot->>'partner_future_offer')::boolean,false) and total is distinct from o.quoted_minor::numeric then raise exception 'Settlement quote mismatch' using errcode = '22023'; end if;
    if coalesce((o.pricing_snapshot->>'partner_future_offer')::boolean,false) then
        select sum(unit_quote_minor::numeric) into total from commerce.checks where order_id=o.id;
    end if;
    if total is distinct from o.quoted_minor::numeric then raise exception 'Settlement ceiling mismatch' using errcode='22023';end if;
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
    for row_data in select row_number,case when coalesce((o.pricing_snapshot->>'partner_future_offer')::boolean,false)
            then case when verified_result then discount_minor else unit_quote_minor end
            else case when not verified_result then unit_quote_minor-discount_minor else 0 end end as amount
        from commerce.checks where order_id=o.id and
            case when coalesce((o.pricing_snapshot->>'partner_future_offer')::boolean,false)
                then (not verified_result and unit_quote_minor>0) or (verified_result and discount_minor>0)
                else not verified_result and unit_quote_minor>discount_minor end loop
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

create or replace function commerce.finalize_campaign_usage(p_order_id uuid,p_cancel boolean default false)
returns void language plpgsql set search_path='' as $$
declare o commerce.orders%rowtype; item record;used_count bigint;used_discount numeric;reserved_count bigint;reserved_discount numeric;
begin
    select * into o from commerce.orders where id=p_order_id for update;
    if o.campaign_usage_finalized_at is not null then return; end if;
    for item in select distinct campaign_id from commerce.order_discounts where order_id=o.id order by campaign_id loop
        perform 1 from commerce.campaigns where id=item.campaign_id for update;
        select count(*),coalesce(sum(d.discount_minor::numeric),0),
            count(*) filter(where c.verified_result and not p_cancel),coalesce(sum(case when d.funder_snapshot->>'kind'='partner' then c.discount_minor::numeric else d.discount_minor::numeric end) filter(where c.verified_result and not p_cancel),0)
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


create function commerce.settle_partner_offer(p_order_id uuid)
returns jsonb language plpgsql security definer set search_path='' as $$
declare o commerce.orders%rowtype;u commerce.users%rowtype;w commerce.wallets%rowtype;row_data record;d record;
    snapshot jsonb;result jsonb;rate bigint;discount_rate bigint;prior_count bigint;ordinal bigint;
    gross numeric:=0;offer_gross numeric:=0;paid_count bigint;eligible_rows bigint[]:='{}';
    ceiling bigint;desired bigint;discount_total bigint:=0;last_discount bigint:=0;row_discount bigint;
    share bigint;net_share bigint;net_revenue numeric;running_revenue numeric:=0;assigned bigint:=0;amount bigint;j uuid;
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
    select sum(discount_minor)::bigint,max((funder_snapshot->'rules'->>'benefit_value')::bigint)
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
            desired:=least(ceiling,round(offer_gross*discount_rate/10000)::bigint);
            row_discount:=desired-last_discount;last_discount:=desired;discount_total:=desired;
            update commerce.checks set discount_minor=row_discount where order_id=o.id and row_number=row_data.row_number;
        end if;
    end loop;
    share:=round(gross*rate/10000)::bigint;net_share:=share-discount_total;
    if net_share<0 then raise exception 'Offer exceeds current order commission';end if;
    result:=commerce.settle_order(o.id);
    select * into o from commerce.orders where id=o.id;
    select coalesce(sum(unit_quote_minor-discount_minor),0) into net_revenue from commerce.checks where order_id=o.id and row_number=any(eligible_rows);
    select wal.* into w from commerce.wallets wal join commerce.partners p on p.user_id=wal.user_id
        where p.id=o.referral_partner_id and wal.kind='partner' for update of wal;
    if not found then raise exception 'Partner earnings wallet unavailable';end if;
    for row_data in select row_number,unit_quote_minor-discount_minor as revenue from commerce.checks
        where order_id=o.id and row_number=any(eligible_rows) order by row_number loop
        running_revenue:=running_revenue+row_data.revenue;
        amount:=case when net_revenue=0 then 0 else round(running_revenue*net_share/net_revenue)::bigint-assigned end;
        assigned:=assigned+amount;j:=null;
        if amount>0 then j:=commerce.post_pair('commission:'||o.id||':'||row_data.row_number,'commission',o.id,
            'platform:revenue','wallet:'||w.id||':available',amount,null,w.id);end if;
        insert into commerce.commissions(order_id,row_number,partner_id,revenue_minor,amount_minor,terms_snapshot,state,journal_id)
            values(o.id,row_data.row_number,o.referral_partner_id,row_data.revenue,amount,snapshot||jsonb_build_object(
                'basis','original_verified_cash_gross_less_partner_discount','order_gross_minor',gross,
                'order_original_share_minor',share,'order_partner_discount_minor',discount_total,
                'rounding','aggregate_verified_gross_half_up','prior_earnings_debited',false),'available',j);
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
end $$;
revoke all on function commerce.settle_partner_offer(uuid) from public,anon,authenticated,service_role;


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
    if coalesce((o.pricing_snapshot->>'partner_future_offer')::boolean,false) then return commerce.settle_partner_offer(p_order_id);end if;
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


revoke all on function commerce_rpc.partner_campaign_context(bigint),commerce_rpc.create_partner_campaign(bigint,text,text,jsonb,text),
    commerce_rpc.list_partner_campaigns(bigint,integer),commerce_rpc.pause_partner_campaign(bigint,uuid,text) from public,anon,authenticated;
grant execute on function commerce_rpc.partner_campaign_context(bigint),commerce_rpc.create_partner_campaign(bigint,text,text,jsonb,text),
    commerce_rpc.list_partner_campaigns(bigint,integer),commerce_rpc.pause_partner_campaign(bigint,uuid,text) to service_role;
create or replace function commerce_rpc.health()
returns jsonb language sql stable security definer set search_path='' as $$
    select jsonb_build_object('status','ok','schema_version',29,'currency','EGP')
$$;
notify pgrst,'reload schema';
commit;
