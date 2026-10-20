-- Exact decimal piastres preserve sub-cent earnings, discounts and refunds.
-- Existing monetary values are converted without scaling or recalculation.
-- Existing issued quotes retain their frozen rounding policy.
begin;
do $$ declare col record;begin
    for col in select table_name,column_name from information_schema.columns
        where table_schema='commerce' and data_type='bigint' and column_name like '%\_minor' escape '\' loop
        execute format('alter table commerce.%I alter column %I type numeric using %I::numeric',col.table_name,col.column_name,col.column_name);
    end loop;
    alter table commerce.campaigns alter column benefit_value type numeric using benefit_value::numeric;
end $$;


CREATE OR REPLACE FUNCTION commerce.create_order(p_telegram_user_id bigint, p_request_key text, p_rows jsonb, p_source jsonb)
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
    values (v_user,p_request_key,v_count,(v_quote->>'total_minor')::numeric,v_quote,
        v_terms,v_partner_id,v_source) returning * into v_order;
    insert into commerce.checks(order_id,row_number,check_type,unit_quote_minor)
    select v_order.id,(r->>'row_number')::bigint,r->>'check_type',((v_quote->'row_unit_minor')->>(r->>'row_number'))::numeric
    from jsonb_array_elements(v_sorted) r;
    return jsonb_build_object('order_id',v_order.id,'quoted_minor',v_order.quoted_minor,
        'requested_checks',v_count,'currency',v_order.currency,'replayed',false);
end $function$;

revoke all on function commerce.create_order(p_telegram_user_id bigint, p_request_key text, p_rows jsonb, p_source jsonb) from public,anon,authenticated,service_role;

CREATE OR REPLACE FUNCTION commerce.settle_order_commissions(p_order_id uuid)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO ''
AS $function$
declare o commerce.orders%rowtype; u commerce.users%rowtype; w commerce.wallets%rowtype;
    ref commerce.referrals%rowtype; result jsonb; row_data record; snapshot jsonb;
    paid_count bigint; eligible_count bigint:=0; prior_count bigint; amount numeric; total numeric:=0;
    prior_anchor timestamptz; prior_locked timestamptz; promo_held numeric; promo_refund numeric;
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
            'refunded_minor',o.quoted_minor-o.final_charge_minor,'commission_minor',total::numeric,
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
                amount:=case when o.pricing_snapshot->>'financial_precision'='exact_decimal' then row_data.revenue::numeric*rate/10000 else round(row_data.revenue::numeric*rate/10000) end;journal:=null;
                if amount>0 then
                    journal:=commerce.post_pair('commission:'||o.id||':'||row_data.row_number,'commission',o.id,
                        'platform:revenue','wallet:'||w.id||':available',amount,null,w.id);
                end if;
                insert into commerce.commissions(order_id,row_number,partner_id,revenue_minor,amount_minor,terms_snapshot,state,journal_id)
                values(o.id,row_data.row_number,pid,row_data.revenue,amount,snapshot||jsonb_build_object(
                    'paid_check_ordinal',ordinal,'basis','collected_after_discounts_and_refunds','expenses_deducted',false,
                    'promotional_payment_excluded',true,
                    'rounding',case when o.pricing_snapshot->>'financial_precision'='exact_decimal' then 'exact_decimal' else 'nearest_piastre_half_up_per_check' end,'duration_measurement','paid_service_settled_at'),'available',journal);
                total:=total+amount;eligible_count:=eligible_count+1;
            end loop;
            update commerce.wallets set available_minor=available_minor+total::numeric where id=w.id;
        end if;
    end if;
    update commerce.users set successful_paid_checks=successful_paid_checks+paid_count where id=u.id;
    update commerce.orders set financial_completed_at=now(),commission_finalized_at=now() where id=o.id;
    return result||jsonb_build_object('collected_minor',o.collected_cash_minor,'commission_minor',total::numeric,'eligible_paid_checks',eligible_count);
end $function$;

revoke all on function commerce.settle_order_commissions(p_order_id uuid) from public,anon,authenticated,service_role;

drop function commerce.post_pair(p_key text, p_kind text, p_order uuid, p_from text, p_to text, p_amount bigint, p_from_wallet uuid, p_to_wallet uuid);

CREATE OR REPLACE FUNCTION commerce.post_pair(p_key text, p_kind text, p_order uuid, p_from text, p_to text, p_amount numeric, p_from_wallet uuid DEFAULT NULL::uuid, p_to_wallet uuid DEFAULT NULL::uuid)
 RETURNS uuid
 LANGUAGE plpgsql
 SET search_path TO ''
AS $function$
declare j uuid; fingerprint text; old_fingerprint text;
begin
    if p_amount is null or p_amount <= 0 then raise exception 'Positive journal amount required' using errcode = '22023'; end if;
    fingerprint := md5(jsonb_build_object('kind',p_kind,'order',p_order,'from',p_from,'to',p_to,
        'amount',trim_scale(p_amount),'from_wallet',p_from_wallet,'to_wallet',p_to_wallet)::text);
    insert into commerce.journals(idempotency_key,kind,order_id,payload_fingerprint)
    values (p_key,p_kind,p_order,fingerprint) on conflict (idempotency_key) do nothing returning id into j;
    if not found then
        select id,payload_fingerprint into j,old_fingerprint from commerce.journals where idempotency_key = p_key;
        if old_fingerprint <> fingerprint then raise exception 'Conflicting journal replay' using errcode = '22023'; end if;
        return j;
    end if;
    insert into commerce.ledger_entries(journal_id,line_number,account_key,wallet_id,delta_minor)
    values (j,1,p_from,p_from_wallet,-p_amount),(j,2,p_to,p_to_wallet,p_amount);
    return j;
end $function$;

revoke all on function commerce.post_pair(p_key text, p_kind text, p_order uuid, p_from text, p_to text, p_amount numeric, p_from_wallet uuid, p_to_wallet uuid) from public,anon,authenticated,service_role;

CREATE OR REPLACE FUNCTION commerce_rpc.quote_checks(p_count bigint)
 RETURNS jsonb
 LANGUAGE plpgsql
 STABLE SECURITY DEFINER
 SET search_path TO ''
AS $function$
declare policy commerce.pricing_policies%rowtype;unit numeric;total numeric;
begin
    if p_count is null or p_count<=0 then raise exception 'Positive check count required' using errcode='22023'; end if;
    select * into policy from commerce.pricing_policies where effective_at<=now() order by version desc limit 1;
    select base_minor into unit from commerce.customer_price_settings where target_telegram_id=0 order by id desc limit 1;
    total:=p_count::numeric*unit;
    if total>9223372036854775807 then raise exception 'Quote exceeds monetary range' using errcode='22023'; end if;
    return jsonb_build_object('policy_version',policy.version,'unit','check','count',p_count,'unit_price_minor',unit,
        'total_minor',total::numeric,'currency','EGP','refund_destination','wallet','refund_requires_consent',true,
        'billing_basis','verified_terminal_result','technical_failure_refunded',true);
end $function$;

revoke all on function commerce_rpc.quote_checks(p_count bigint) from public,anon,authenticated;

grant execute on function commerce_rpc.quote_checks(p_count bigint) to service_role;

drop function commerce_rpc.confirm_receipt(p_telegram_user_id bigint, p_key text, p_provider text, p_reference text, p_amount_minor bigint, p_proof text, p_purpose text, p_order_id uuid, p_fx_snapshot jsonb);

CREATE OR REPLACE FUNCTION commerce_rpc.confirm_receipt(p_telegram_user_id bigint, p_key text, p_provider text, p_reference text, p_amount_minor numeric, p_proof text, p_purpose text DEFAULT 'topup'::text, p_order_id uuid DEFAULT NULL::uuid, p_fx_snapshot jsonb DEFAULT '{}'::jsonb)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO ''
AS $function$
declare u uuid; p commerce.payments%rowtype; w commerce.wallets%rowtype; o commerce.orders%rowtype;
begin
    if p_key is null or length(p_key) not between 1 and 160 or p_provider is null
       or length(p_provider) not between 1 and 80 or p_reference is null or length(p_reference) not between 1 and 256
       or p_amount_minor is null or p_amount_minor <= 0 or p_proof is null or length(p_proof) not between 1 and 1000
       or p_purpose is null or p_purpose not in ('topup','direct_order')
       or ((p_purpose = 'direct_order') <> (p_order_id is not null))
       or jsonb_typeof(p_fx_snapshot) is distinct from 'object' then
        raise exception 'Invalid verified payment receipt' using errcode = '22023';
    end if;
    u := (commerce_rpc.register_telegram_user(p_telegram_user_id,null)->>'user_id')::uuid;
    perform 1 from commerce.users where id = u and registration_state = 'active' for update;
    if not found then raise exception 'Account suspended' using errcode = '22023'; end if;
    select * into p from commerce.payments where idempotency_key = p_key;
    if found then
        if p.user_id <> u or p.provider <> p_provider or p.provider_reference <> p_reference
           or p.amount_minor <> p_amount_minor or p.purpose <> p_purpose
           or p.order_id is distinct from p_order_id or p.fx_snapshot is distinct from p_fx_snapshot
           or p.proof_reference <> p_proof then
            raise exception 'Conflicting payment replay' using errcode = '22023';
        end if;
        return jsonb_build_object('payment_id',p.id,'state',p.state,'replayed',true);
    end if;
    if p_purpose = 'direct_order' then
        select * into o from commerce.orders where id = p_order_id and user_id = u for update;
        if not found or o.order_state <> 'draft' or o.payment_state <> 'unpaid' or o.quoted_minor <> p_amount_minor then
            raise exception 'Direct payment must match an unpaid owned order' using errcode = '22023';
        end if;
        if exists (select 1 from commerce.payments where order_id = p_order_id and state <> 'rejected') then
            raise exception 'Order already has a direct payment' using errcode = '22023';
        end if;
    end if;
    if exists (select 1 from commerce.payments where provider = p_provider and provider_reference = p_reference) then
        raise exception 'Provider receipt already assigned' using errcode = '22023';
    end if;
    insert into commerce.payments(user_id,order_id,purpose,provider,provider_reference,idempotency_key,
        amount_minor,state,proof_reference,verified_at,fx_snapshot)
    values (u,p_order_id,p_purpose,p_provider,p_reference,p_key,p_amount_minor,'verified',p_proof,now(),p_fx_snapshot)
    returning * into p;
    if p_purpose = 'topup' then
        select * into w from commerce.wallets where user_id = u and kind = 'customer' and currency = 'EGP' for update;
        perform commerce.post_pair('payment:'||p.id,'topup',null,'external:'||p_provider,
            'wallet:'||w.id||':available',p_amount_minor,null,w.id);
        update commerce.wallets set available_minor = available_minor + p_amount_minor where id = w.id;
    else
        perform commerce.post_pair('payment:'||p.id,'reserve',p_order_id,'external:'||p_provider,
            'direct:'||p_order_id||':received',p_amount_minor);
    end if;
    return jsonb_build_object('payment_id',p.id,'state',p.state,'replayed',false);
end $function$;

revoke all on function commerce_rpc.confirm_receipt(p_telegram_user_id bigint, p_key text, p_provider text, p_reference text, p_amount_minor numeric, p_proof text, p_purpose text, p_order_id uuid, p_fx_snapshot jsonb) from public,anon,authenticated;

grant execute on function commerce_rpc.confirm_receipt(p_telegram_user_id bigint, p_key text, p_provider text, p_reference text, p_amount_minor numeric, p_proof text, p_purpose text, p_order_id uuid, p_fx_snapshot jsonb) to service_role;

CREATE OR REPLACE FUNCTION commerce.settle_order(p_order_id uuid)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO ''
AS $function$
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
        perform commerce.post_pair('service:'||o.id,'service',o.id,source_account,'platform:revenue',earned::numeric,r.wallet_id,null);
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
    update commerce.wallets set available_minor = available_minor+refunded::numeric,
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
    update commerce.orders set payment_state = final_state,order_state = 'closed',final_charge_minor = earned::numeric where id = o.id;
    return jsonb_build_object('state',final_state,'charged_minor',earned::numeric,'refunded_minor',refunded::numeric,'replayed',false);
end $function$;

revoke all on function commerce.settle_order(p_order_id uuid) from public,anon,authenticated,service_role;

CREATE OR REPLACE FUNCTION commerce.create_payment_request(p_quote jsonb, p_provider text, p_receiver text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO ''
AS $function$
declare u uuid; q uuid; o uuid; minor numeric; rate numeric; amount numeric; prec integer;
    started timestamptz; expires timestamptz; r commerce.payment_requests%rowtype;
begin
    if jsonb_typeof(p_quote) is distinct from 'object'
       or p_provider is null or p_provider not in ('cryptopay','binance_pay','bsc')
       or p_receiver is null or length(btrim(p_receiver)) not between 1 and 256
       or jsonb_typeof(p_quote->'user_id') is distinct from 'number'
       or (p_quote->>'user_id') !~ '^[1-9][0-9]*$'
       or jsonb_typeof(p_quote->'amount_minor') is distinct from 'number'
       or (p_quote->>'amount_minor') !~ '^[0-9]+([.][0-9]+)?$'
       or jsonb_typeof(p_quote->'precision') is distinct from 'number'
       or (p_quote->>'precision') !~ '^[0-8]$'
       or coalesce(p_quote->>'asset','') not in ('USDT','USDC')
       or coalesce(p_quote->>'egp_per_asset','') !~ '^[0-9]{1,40}(\.[0-9]{1,40})?$'
       or coalesce(p_quote->>'asset_amount','') !~ '^[0-9]{1,40}(\.[0-9]{1,40})?$'
       or length(coalesce(btrim(p_quote->>'rate_source'),'')) not between 1 and 500
       or coalesce(p_quote->>'observed_at','') !~ '(Z|[+]00:00)$'
       or coalesce(p_quote->>'expires_at','') !~ '(Z|[+]00:00)$' then
        raise exception 'Invalid frozen payment quote' using errcode='22023';
    end if;
    q := (p_quote->>'id')::uuid;
    if q is null or q::text <> p_quote->>'id' then
        raise exception 'Canonical quote ID required' using errcode='22023'; end if;
    o := (p_quote->>'order_id')::uuid;
    minor := (p_quote->>'amount_minor')::numeric;
    rate := (p_quote->>'egp_per_asset')::numeric;
    amount := (p_quote->>'asset_amount')::numeric;
    prec := (p_quote->>'precision')::integer;
    started := (p_quote->>'observed_at')::timestamptz;
    expires := (p_quote->>'expires_at')::timestamptz;
    if rate <= 0 or amount <= 0 or started >= expires or expires > started + interval '1 day'
       or amount <> ceil(minor::numeric / 100 / rate * power(10::numeric,prec)) / power(10::numeric,prec) then
        raise exception 'Quote does not match its frozen EGP charge' using errcode='22023'; end if;
    u := (commerce_rpc.register_telegram_user((p_quote->>'user_id')::bigint,null)->>'user_id')::uuid;
    perform 1 from commerce.users where id=u and registration_state='active' for update;
    if not found then raise exception 'Account suspended' using errcode='22023'; end if;
    select * into r from commerce.payment_requests where id=q;
    if found then
        if r.user_id<>u or r.quote<>p_quote or r.provider<>p_provider or r.receiver<>p_receiver then
            raise exception 'Conflicting payment quote replay' using errcode='22023'; end if;
        return jsonb_build_object('quote_id',q,'state','issued','replayed',true);
    end if;
    if started > now() + interval '30 seconds' or expires <= now() then
        raise exception 'New payment quote is outside its validity window' using errcode='22023'; end if;
    if o is not null then
        perform 1 from commerce.orders where id=o and user_id=u and quoted_minor=minor
            and order_state='draft' and payment_state='unpaid' for update;
        if not found then raise exception 'Quote must match an unpaid owned order' using errcode='22023'; end if;
    end if;
    insert into commerce.payment_requests(id,user_id,order_id,provider,receiver,quote)
    values(q,u,o,p_provider,p_receiver,p_quote);
    return jsonb_build_object('quote_id',q,'state','issued','replayed',false);
end $function$;

revoke all on function commerce.create_payment_request(p_quote jsonb, p_provider text, p_receiver text) from public,anon,authenticated,service_role;

CREATE OR REPLACE FUNCTION commerce_rpc.confirm_payment_request(p_quote_id uuid, p_telegram_user_id bigint, p_evidence jsonb, p_binding jsonb)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO ''
AS $function$
declare u uuid; r commerce.payment_requests%rowtype; proof jsonb; received timestamptz; result jsonb;
begin
    select id into u from commerce.users where telegram_user_id=p_telegram_user_id and registration_state='active' for update;
    if not found then raise exception 'Unknown or suspended user' using errcode='22023'; end if;
    select * into r from commerce.payment_requests where id=p_quote_id and user_id=u for update;
    if not found or jsonb_typeof(p_evidence) is distinct from 'object' or jsonb_typeof(p_binding) is distinct from 'object' then
        raise exception 'Unknown or invalid payment confirmation' using errcode='22023'; end if;
    if r.payment_id is not null then
        if r.evidence<>p_evidence or r.binding<>p_binding then
            raise exception 'Conflicting payment confirmation replay' using errcode='22023'; end if;
        return jsonb_build_object('payment_id',r.payment_id,'state','verified','replayed',true);
    end if;
    if coalesce(p_evidence->>'provider','')<>r.provider or coalesce(p_evidence->>'receiver','')<>r.receiver
       or coalesce(p_evidence->>'asset','')<>r.quote->>'asset'
       or coalesce(p_evidence->>'amount','') !~ '^[0-9]{1,40}(\.[0-9]{1,40})?$'
       or length(coalesce(btrim(p_evidence->>'reference'),'')) not between 1 and 256
       or coalesce(p_evidence->>'received_at','') !~ '(Z|[+]00:00)$' then
        raise exception 'Provider evidence differs from frozen request' using errcode='22023'; end if;
    received := (p_evidence->>'received_at')::timestamptz;
    if (p_evidence->>'amount')::numeric<>(r.quote->>'asset_amount')::numeric
       or received < (r.quote->>'observed_at')::timestamptz or received > (r.quote->>'expires_at')::timestamptz
       or received > now()+interval '30 seconds' then
        raise exception 'Amount or payment time requires reconciliation' using errcode='22023'; end if;
    proof := (p_evidence->>'proof_json')::jsonb;
    if jsonb_typeof(proof) is distinct from 'object' then
        raise exception 'Provider proof is missing' using errcode='22023'; end if;
    if r.provider='cryptopay' then
        if coalesce(p_binding->>'kind','')<>'invoice' or r.invoice_id is null
           or coalesce(p_evidence->>'reference','')<>r.receiver||':'||r.invoice_id
           or coalesce(p_evidence->>'invoice_payload','')<>'zain-commerce:'||r.id
           or coalesce(proof->>'payload','')<>'zain-commerce:'||r.id then
            raise exception 'Issued invoice binding required' using errcode='22023'; end if;
    else
        -- Backend checks the actor against its configured authenticated admins.
        -- service_role is the trust boundary; these RPCs have no client grants.
        if coalesce(p_binding->>'kind','')<>'admin' or coalesce(p_binding->>'actor_id','') !~ '^[1-9][0-9]*$'
           or length(coalesce(btrim(p_binding->>'approval_reference'),'')) not between 1 and 1000 then
            raise exception 'Authenticated customer assignment required' using errcode='22023'; end if;
    end if;
    result := commerce_rpc.confirm_receipt(p_telegram_user_id,'quote:'||r.id,r.provider,p_evidence->>'reference',
        (r.quote->>'amount_minor')::numeric,'payment-request:'||r.id,
        case when r.order_id is null then 'topup' else 'direct_order' end,r.order_id,
        jsonb_build_object('quote',r.quote,'evidence',p_evidence,'binding',p_binding));
    update commerce.payment_requests set payment_id=(result->>'payment_id')::uuid,
        evidence=p_evidence,binding=p_binding,confirmed_at=now() where id=r.id;
    return result;
end $function$;

revoke all on function commerce_rpc.confirm_payment_request(p_quote_id uuid, p_telegram_user_id bigint, p_evidence jsonb, p_binding jsonb) from public,anon,authenticated;

grant execute on function commerce_rpc.confirm_payment_request(p_quote_id uuid, p_telegram_user_id bigint, p_evidence jsonb, p_binding jsonb) to service_role;

drop function commerce_rpc.admin_cash_topup(p_telegram_user_id bigint, p_amount_minor bigint, p_receipt_reference text, p_actor_telegram_id bigint, p_approval_reference text);

CREATE OR REPLACE FUNCTION commerce_rpc.admin_cash_topup(p_telegram_user_id bigint, p_amount_minor numeric, p_receipt_reference text, p_actor_telegram_id bigint, p_approval_reference text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO ''
AS $function$
declare u uuid;ref text;existing commerce.cash_receipts%rowtype;result jsonb;
begin
    if p_actor_telegram_id is null or p_actor_telegram_id<=0 or p_amount_minor is null or p_amount_minor<=0
       or p_receipt_reference is null or upper(btrim(p_receipt_reference)) !~ '^[A-Z0-9][A-Z0-9_.:-]{2,127}$'
       or p_approval_reference is null or length(btrim(p_approval_reference)) not between 1 and 1000 then
        raise exception 'Authenticated cash receipt required' using errcode='22023'; end if;
    ref:=upper(btrim(p_receipt_reference));
    perform pg_advisory_xact_lock(hashtextextended('cash-receipt:'||ref,0));
    u:=(commerce_rpc.register_telegram_user(p_telegram_user_id,null)->>'user_id')::uuid;
    perform 1 from commerce.users where id=u and registration_state='active' for update;
    if not found then raise exception 'Customer suspended' using errcode='22023'; end if;
    select * into existing from commerce.cash_receipts where reference=ref for update;
    if found then
        if existing.allocation_kind<>'direct_topup' or existing.user_id<>u or existing.amount_minor<>p_amount_minor then
            raise exception 'Cash receipt already assigned' using errcode='22023'; end if;
        return jsonb_build_object('payment_id',existing.payment_id,'state','verified','replayed',true);
    end if;
    result:=commerce_rpc.confirm_receipt(p_telegram_user_id,'manual-cash:'||ref,'manual_cash',ref,p_amount_minor,
        'admin-cash-receipt:'||ref,'topup',null,jsonb_build_object('kind','authenticated_cash_receipt',
        'actor_telegram_id',p_actor_telegram_id,'approval_reference',p_approval_reference));
    insert into commerce.cash_receipts(reference,allocation_kind,amount_minor,actor_telegram_id,approval_reference,user_id,payment_id)
        values(ref,'direct_topup',p_amount_minor,p_actor_telegram_id,p_approval_reference,u,(result->>'payment_id')::uuid);
    return result;
end $function$;

revoke all on function commerce_rpc.admin_cash_topup(p_telegram_user_id bigint, p_amount_minor numeric, p_receipt_reference text, p_actor_telegram_id bigint, p_approval_reference text) from public,anon,authenticated;

grant execute on function commerce_rpc.admin_cash_topup(p_telegram_user_id bigint, p_amount_minor numeric, p_receipt_reference text, p_actor_telegram_id bigint, p_approval_reference text) to service_role;

drop function commerce_rpc.request_withdrawal(p_telegram_user_id bigint, p_key text, p_amount_minor bigint, p_destination text);

CREATE OR REPLACE FUNCTION commerce_rpc.request_withdrawal(p_telegram_user_id bigint, p_key text, p_amount_minor numeric, p_destination text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO ''
AS $function$
declare uid uuid; pid uuid; w commerce.wallets%rowtype; request commerce.withdrawals%rowtype; j uuid;
begin
    if p_key is null or length(p_key) not between 1 and 160 or p_amount_minor is null or p_amount_minor<=0
       or p_destination is null or length(btrim(p_destination)) not between 3 and 500 then
        raise exception 'Withdrawal identity, amount and destination required' using errcode='22023'; end if;
    select id into uid from commerce.users where telegram_user_id=p_telegram_user_id and registration_state='active' for update;
    select id into pid from commerce.partners where user_id=uid and state='active' for share;
    if not found then raise exception 'Active partner required' using errcode='22023'; end if;
    select * into request from commerce.withdrawals where idempotency_key=p_key;
    if found then
        if request.partner_id<>pid or request.amount_minor<>p_amount_minor or request.destination<>btrim(p_destination) then
            raise exception 'Conflicting withdrawal replay' using errcode='22023'; end if;
        return jsonb_build_object('withdrawal_id',request.id,'state',request.state,'replayed',true);
    end if;
    select * into w from commerce.wallets where user_id=uid and kind='partner' for update;
    if w.available_minor<p_amount_minor then
        return jsonb_build_object('state','insufficient_balance','available_minor',w.available_minor); end if;
    insert into commerce.withdrawals(partner_id,amount_minor,idempotency_key,destination)
        values(pid,p_amount_minor,p_key,btrim(p_destination)) returning * into request;
    j:=commerce.post_pair('withdrawal:hold:'||request.id,'reserve',null,'wallet:'||w.id||':available',
        'wallet:'||w.id||':reserved',p_amount_minor,w.id,w.id);
    update commerce.wallets set available_minor=available_minor-p_amount_minor,reserved_minor=reserved_minor+p_amount_minor where id=w.id;
    update commerce.withdrawals set hold_journal_id=j where id=request.id;
    return jsonb_build_object('withdrawal_id',request.id,'state','requested','replayed',false);
end $function$;

revoke all on function commerce_rpc.request_withdrawal(p_telegram_user_id bigint, p_key text, p_amount_minor numeric, p_destination text) from public,anon,authenticated;

grant execute on function commerce_rpc.request_withdrawal(p_telegram_user_id bigint, p_key text, p_amount_minor numeric, p_destination text) to service_role;

CREATE OR REPLACE FUNCTION commerce.finalize_campaign_usage(p_order_id uuid, p_cancel boolean DEFAULT false)
 RETURNS void
 LANGUAGE plpgsql
 SET search_path TO ''
AS $function$
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
            reserved_discount_minor=reserved_discount_minor-reserved_discount::numeric,
            consumed_checks=consumed_checks+used_count,discount_minor=discount_minor+used_discount::numeric
            where campaign_id=item.campaign_id and user_id=o.user_id;
        update commerce.order_discounts d set applied=not p_cancel and c.verified_result,
            usage_state=case when not p_cancel and c.verified_result then 'consumed' else 'released' end
            from commerce.checks c where d.order_id=o.id and d.campaign_id=item.campaign_id and c.order_id=d.order_id and c.row_number=d.row_number;
    end loop;
    update commerce.orders set campaign_usage_finalized_at=now() where id=o.id;
end $function$;

revoke all on function commerce.finalize_campaign_usage(p_order_id uuid, p_cancel boolean) from public,anon,authenticated,service_role;

CREATE OR REPLACE FUNCTION commerce_rpc.create_platform_campaign(p_actor_telegram_id bigint, p_key text, p_code text, p_rules jsonb, p_reference text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO ''
AS $function$
declare policy commerce.campaign_policies%rowtype; c commerce.campaigns%rowtype; code text;
    kind text; eligibility text; types text[]; value numeric; count_limit bigint; budget numeric; global_budget numeric; duration bigint; redemptions bigint;
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
    budget:=(p_rules->>'max_discount_minor')::numeric;global_budget:=(p_rules->>'max_total_discount_minor')::numeric;
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
       or global_budget>(policy.rules->>'max_total_discount_minor')::numeric or duration>(policy.rules->>'max_duration_seconds')::bigint
       or redemptions>(policy.rules->>'max_redemptions')::bigint or budget>global_budget
       or (kind='percent' and value>(policy.rules->>'max_percent_basis_points')::bigint)
       or (kind='free_checks' and value<>1)
       or (kind in ('fixed','wallet_credit') and value>budget)
       or (kind='wallet_credit' and value>(policy.rules->>'max_wallet_credit_minor')::numeric) then
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
end $function$;

revoke all on function commerce_rpc.create_platform_campaign(p_actor_telegram_id bigint, p_key text, p_code text, p_rules jsonb, p_reference text) from public,anon,authenticated;

grant execute on function commerce_rpc.create_platform_campaign(p_actor_telegram_id bigint, p_key text, p_code text, p_rules jsonb, p_reference text) to service_role;

CREATE OR REPLACE FUNCTION commerce.reserve_order_before_expiry_lots(p_order_id uuid, p_telegram_user_id bigint, p_consent_reference text, p_payment_id uuid DEFAULT NULL::uuid)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO ''
AS $function$
declare o commerce.orders%rowtype;w commerce.wallets%rowtype;held numeric:=0;remaining numeric;row_data record;result jsonb;
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
end $function$;

revoke all on function commerce.reserve_order_before_expiry_lots(p_order_id uuid, p_telegram_user_id bigint, p_consent_reference text, p_payment_id uuid) from public,anon,authenticated,service_role;

CREATE OR REPLACE FUNCTION commerce_rpc.create_order(p_telegram_user_id bigint, p_request_key text, p_rows jsonb, p_source jsonb)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO ''
AS $function$
declare result jsonb;o commerce.orders%rowtype;c commerce.campaigns%rowtype;usage commerce.campaign_usage%rowtype;
    row_data record;remaining_count bigint;remaining_budget numeric;global_used numeric;discount numeric;
    candidate jsonb;eligible jsonb:='[]';best jsonb:='[]';best_snapshot jsonb;best_id uuid;
    candidate_total numeric;best_total numeric:=0;best_count bigint:=0;count_used bigint;
    partner commerce.partners%rowtype;customer commerce.users%rowtype;best_kind text;terms_limit bigint;
    percent_base numeric;percent_rounded numeric;next_rounded numeric;
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
            greatest(c.max_total_discount_minor::numeric-global_used,0))::numeric;
        candidate:='[]';candidate_total:=0;count_used:=0;percent_base:=0;percent_rounded:=0;
        for row_data in select * from commerce.checks where order_id=o.id order by row_number loop
            if remaining_count<=0 or remaining_budget<=0 then exit; end if;
            if not (usage.rules_snapshot->'eligible_check_types') ? row_data.check_type then continue; end if;
            if row_data.unit_quote_minor=0 then continue;end if;
            discount:=case usage.rules_snapshot->>'benefit_kind'
                when 'percent' then row_data.unit_quote_minor::numeric*(usage.rules_snapshot->>'benefit_value')::numeric/10000
                when 'fixed' then least(row_data.unit_quote_minor,(usage.rules_snapshot->>'benefit_value')::bigint)
                when 'free_checks' then row_data.unit_quote_minor else 0 end;
            if usage.rules_snapshot->>'benefit_kind'='percent' then
                percent_base:=percent_base+row_data.unit_quote_minor;
                next_rounded:=percent_base*(usage.rules_snapshot->>'benefit_value')::numeric/10000;
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
        for row_data in select * from jsonb_to_recordset(best) as r(row_number bigint,discount_minor numeric) loop
            update commerce.checks set discount_minor=case when best_kind='partner' then 0 else row_data.discount_minor end where order_id=o.id and row_number=row_data.row_number;
            insert into commerce.order_discounts(order_id,row_number,campaign_id,discount_minor,funder_snapshot)
                values(o.id,row_data.row_number,best_id,row_data.discount_minor,jsonb_build_object('kind',best_kind,'platform_minor',case when best_kind='platform' then row_data.discount_minor else 0 end,
                    'partner_minor',case when best_kind='partner' then row_data.discount_minor else 0 end,'rules',best_snapshot,'stacking',false));
        end loop;
        update commerce.campaign_usage set reserved_checks=reserved_checks+best_count,reserved_discount_minor=reserved_discount_minor+best_total::numeric
            where campaign_id=best_id and user_id=o.user_id;
    end if;
    update commerce.orders set quoted_minor=quoted_minor-case when best_kind='partner' then 0 else best_total::numeric end,campaigns_evaluated_at=now(),
        pricing_snapshot=pricing_snapshot||jsonb_build_object('base_total_minor',quoted_minor,'discount_total_minor',best_total::numeric,
            'applied_campaign',case when best_id is null then null else jsonb_build_object('campaign_id',best_id,'name',best_snapshot->>'name','discounted_checks',best_count) end,
            'eligible_campaigns',eligible,'stacking',false,'total_minor',quoted_minor-case when best_kind='partner' then 0 else best_total::numeric end,
            'partner_future_offer',best_kind='partner','estimated_final_minor',quoted_minor-best_total::numeric,
            'discount_funding',best_kind,'cash_only',best_kind='partner','offer_settlement','verified_results_after_delivery','financial_precision','exact_decimal')
        where id=o.id;
    return result||jsonb_build_object('quoted_minor',o.quoted_minor-case when best_kind='partner' then 0 else best_total::numeric end,'discount_minor',best_total::numeric);
end $function$;

revoke all on function commerce_rpc.create_order(p_telegram_user_id bigint, p_request_key text, p_rows jsonb, p_source jsonb) from public,anon,authenticated;

grant execute on function commerce_rpc.create_order(p_telegram_user_id bigint, p_request_key text, p_rows jsonb, p_source jsonb) to service_role;

drop function commerce_rpc.issue_cash_credit_code(p_code text, p_credit_minor bigint, p_max_redemptions bigint, p_funded_minor bigint, p_receipt_reference text, p_actor_telegram_id bigint, p_approval_reference text, p_bound_telegram_id bigint, p_expires_at timestamp with time zone);

CREATE OR REPLACE FUNCTION commerce_rpc.issue_cash_credit_code(p_code text, p_credit_minor numeric, p_max_redemptions bigint, p_funded_minor numeric, p_receipt_reference text, p_actor_telegram_id bigint, p_approval_reference text, p_bound_telegram_id bigint DEFAULT NULL::bigint, p_expires_at timestamp with time zone DEFAULT NULL::timestamp with time zone)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO ''
AS $function$
declare normalized text;ref text;bound uuid;c commerce.codes%rowtype;receipt commerce.cash_receipts%rowtype;j uuid;
begin
    normalized:=upper(btrim(p_code));ref:=upper(btrim(p_receipt_reference));
    if normalized is null or normalized !~ '^[A-Z0-9_-]{3,48}$' or ref is null or ref !~ '^[A-Z0-9][A-Z0-9_.:-]{2,127}$'
       or p_credit_minor is null or p_credit_minor<=0 or p_max_redemptions is null or p_max_redemptions<=0
       or p_funded_minor is null or p_funded_minor<=0
       or p_credit_minor::numeric*p_max_redemptions::numeric<>p_funded_minor::numeric
       or p_actor_telegram_id is null or p_actor_telegram_id<=0
       or p_approval_reference is null or length(btrim(p_approval_reference)) not between 1 and 1000
       or (p_bound_telegram_id is not null and (p_bound_telegram_id<=0 or p_max_redemptions<>1)) then
        raise exception 'Reviewed fully funded cash code required' using errcode='22023'; end if;
    perform pg_advisory_xact_lock(hashtextextended('cash-receipt:'||ref,0));
    perform pg_advisory_xact_lock(hashtextextended('cash-code:'||normalized,0));
    if p_bound_telegram_id is not null then
        bound:=(commerce_rpc.register_telegram_user(p_bound_telegram_id,null)->>'user_id')::uuid;
    end if;
    select * into receipt from commerce.cash_receipts where reference=ref for update;
    if found then
        select * into c from commerce.codes where id=receipt.code_id;
        if receipt.allocation_kind<>'cash_credit_code' or c.code<>normalized or c.credit_minor<>p_credit_minor
           or c.max_redemptions<>p_max_redemptions or c.cash_funded_minor<>p_funded_minor
           or c.cash_bound_user_id is distinct from bound or c.expires_at is distinct from p_expires_at then
            raise exception 'Conflicting cash receipt or code replay' using errcode='22023'; end if;
        return jsonb_build_object('code_id',c.id,'code',c.code,'funded_minor',c.cash_funded_minor,
            'credit_minor',c.credit_minor,'max_redemptions',c.max_redemptions,'replayed',true);
    end if;
    if exists(select 1 from commerce.payments where provider='manual_cash' and provider_reference=ref)
       or exists(select 1 from commerce.codes where code=normalized)
       or (p_expires_at is not null and p_expires_at<=now()) then
        raise exception 'Receipt or code already assigned, or new code expired' using errcode='22023'; end if;
    insert into commerce.codes(code,kind,credit_minor,active,max_redemptions,expires_at,proof_reference,
        cash_bound_user_id,cash_funded_minor)
        values(normalized,'cash_credit',p_credit_minor,true,p_max_redemptions,p_expires_at,'cash-receipt:'||ref,bound,p_funded_minor)
        returning * into c;
    j:=commerce.post_pair('cash-code:fund:'||c.id,'topup',null,'external:manual_cash','cash-code:'||c.id||':unredeemed',p_funded_minor);
    insert into commerce.cash_receipts(reference,allocation_kind,amount_minor,actor_telegram_id,approval_reference,code_id,journal_id)
        values(ref,'cash_credit_code',p_funded_minor,p_actor_telegram_id,p_approval_reference,c.id,j);
    return jsonb_build_object('code_id',c.id,'code',c.code,'funded_minor',c.cash_funded_minor,
        'credit_minor',c.credit_minor,'max_redemptions',c.max_redemptions,'replayed',false);
end $function$;

revoke all on function commerce_rpc.issue_cash_credit_code(p_code text, p_credit_minor numeric, p_max_redemptions bigint, p_funded_minor numeric, p_receipt_reference text, p_actor_telegram_id bigint, p_approval_reference text, p_bound_telegram_id bigint, p_expires_at timestamp with time zone) from public,anon,authenticated;

grant execute on function commerce_rpc.issue_cash_credit_code(p_code text, p_credit_minor numeric, p_max_redemptions bigint, p_funded_minor numeric, p_receipt_reference text, p_actor_telegram_id bigint, p_approval_reference text, p_bound_telegram_id bigint, p_expires_at timestamp with time zone) to service_role;

CREATE OR REPLACE FUNCTION commerce.customer_quote(p_telegram_user_id bigint, p_rows jsonb)
 RETURNS jsonb
 LANGUAGE plpgsql
 STABLE SECURITY DEFINER
 SET search_path TO ''
AS $function$
declare cfg jsonb;quote jsonb;row_data jsonb;prices jsonb:='{}';services integer:=0;accounts integer:=0;
    unit numeric;total numeric:=0;count integer:=jsonb_array_length(p_rows);
begin
    if count>4000 and not exists(select 1 from commerce.support_actors where telegram_user_id=p_telegram_user_id and active) then
        raise exception 'Customer sheets cannot exceed 4000 checks' using errcode='22023'; end if;
    cfg:=commerce_rpc.customer_pricing(p_telegram_user_id);quote:=commerce_rpc.quote_checks(count);
    for row_data in select value from jsonb_array_elements(p_rows) order by (value->>'row_number')::bigint loop
        if row_data->>'check_type'='service' then services:=services+1;else accounts:=accounts+1;end if;
        unit:=case when row_data->>'check_type'='service' and services>(cfg->>'service_threshold')::integer
                   then (cfg->>'service_excess_minor')::numeric else (cfg->>'base_minor')::numeric end;
        total:=total+unit;prices:=prices||jsonb_build_object(row_data->>'row_number',unit);
    end loop;
    if total>9223372036854775807 then raise exception 'Quote exceeds monetary range' using errcode='22023'; end if;
    return quote||cfg||jsonb_build_object('unit_price_minor',(cfg->>'base_minor')::numeric,'total_minor',total::numeric,
        'row_unit_minor',prices,'account_count',accounts,'service_count',services,
        'service_standard_count',least(services,(cfg->>'service_threshold')::integer),
        'service_excess_count',greatest(services-(cfg->>'service_threshold')::integer,0));
end $function$;

revoke all on function commerce.customer_quote(p_telegram_user_id bigint, p_rows jsonb) from public,anon,authenticated,service_role;

CREATE OR REPLACE FUNCTION commerce_rpc.configure_customer_pricing(p_actor_telegram_id bigint, p_target_telegram_id bigint, p_rules jsonb, p_reference text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO ''
AS $function$
declare setting bigint; key text; inherit boolean; base numeric; excess numeric; threshold integer;
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
        base:=(p_rules->>'base_minor')::numeric;excess:=(p_rules->>'service_excess_minor')::numeric;
        threshold:=(p_rules->>'service_threshold')::integer;
    end if;
    insert into commerce.customer_price_settings(target_telegram_id,base_minor,service_excess_minor,service_threshold,
        inherit_global,actor_telegram_id,reference)
        values(p_target_telegram_id,base,excess,threshold,inherit,p_actor_telegram_id,p_reference) returning id into setting;
    return jsonb_build_object('state','configured','pricing_version',setting);
end $function$;

revoke all on function commerce_rpc.configure_customer_pricing(p_actor_telegram_id bigint, p_target_telegram_id bigint, p_rules jsonb, p_reference text) from public,anon,authenticated;

grant execute on function commerce_rpc.configure_customer_pricing(p_actor_telegram_id bigint, p_target_telegram_id bigint, p_rules jsonb, p_reference text) to service_role;

drop function commerce.expire_user_promotional_credit(p_user_id uuid);

CREATE OR REPLACE FUNCTION commerce.expire_user_promotional_credit(p_user_id uuid)
 RETURNS numeric
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO ''
AS $function$
declare lot commerce.promotional_credit_lots%rowtype;w commerce.wallets%rowtype;total numeric:=0;
begin
    perform 1 from commerce.users where id=p_user_id for update;
    select * into w from commerce.wallets where user_id=p_user_id and kind='customer' for update;
    if not found then return 0;end if;
    for lot in select * from commerce.promotional_credit_lots where user_id=p_user_id
        and available_minor>0 and expires_at<=now() order by expires_at,id for update loop
        if lot.wallet_id<>w.id or lot.available_minor>w.promotional_available_minor or lot.available_minor>w.available_minor then
            raise exception 'Promotional attribution differs from wallet';end if;
        perform commerce.post_pair('promotion:expire:'||lot.id||':'||(lot.expired_minor+lot.available_minor),
            'adjustment',null,'wallet:'||w.id||':available','platform:expired_promotion',lot.available_minor,w.id,null);
        update commerce.wallets set available_minor=available_minor-lot.available_minor,
            promotional_available_minor=promotional_available_minor-lot.available_minor where id=w.id;
        update commerce.promotional_credit_lots set expired_minor=expired_minor+available_minor,available_minor=0 where id=lot.id;
        total:=total+lot.available_minor;
        w.available_minor:=w.available_minor-lot.available_minor;
        w.promotional_available_minor:=w.promotional_available_minor-lot.available_minor;
    end loop;
    return total;
end $function$;

revoke all on function commerce.expire_user_promotional_credit(p_user_id uuid) from public,anon,authenticated,service_role;

CREATE OR REPLACE FUNCTION commerce_rpc.wallet_state(p_telegram_user_id bigint)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO ''
AS $function$
declare uid uuid;result jsonb;promo jsonb;account_id bigint;
begin
    select id,legacy_account_id into uid,account_id from commerce.users where telegram_user_id=p_telegram_user_id;
    if found then perform commerce.expire_user_promotional_credit(uid);end if;
    result:=commerce.wallet_state_before_promotion_expiry(p_telegram_user_id);
    select jsonb_build_object('expired_promotional_minor',coalesce(sum(expired_minor),0),
        'next_promotional_expiry',min(expires_at) filter(where available_minor>0 and expires_at>now())) into promo
        from commerce.promotional_credit_lots where user_id=uid;
    return result||jsonb_build_object('legacy_account_id',account_id,'customer',
        (result->'customer')||promo||jsonb_build_object('cash_available_minor',
            (result->'customer'->>'available_minor')::numeric-(result->'customer'->>'promotional_available_minor')::numeric));
end $function$;

revoke all on function commerce_rpc.wallet_state(p_telegram_user_id bigint) from public,anon,authenticated;

grant execute on function commerce_rpc.wallet_state(p_telegram_user_id bigint) to service_role;

CREATE OR REPLACE FUNCTION commerce_rpc.activate_campaign(p_telegram_user_id bigint, p_code text, p_reference text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO ''
AS $function$
declare result jsonb;uid uuid;
begin
    select id into uid from commerce.users where telegram_user_id=p_telegram_user_id;
    if found then perform commerce.expire_user_promotional_credit(uid);end if;
    result:=commerce.activate_campaign_without_expiry_lots(p_telegram_user_id,p_code,p_reference);
    if result->>'state'='activated' and (result->>'wallet_credit_minor')::numeric>0 then
        insert into commerce.promotional_credit_lots(id,user_id,wallet_id,campaign_id,granted_minor,available_minor,expires_at)
            select cu.grant_journal_id,cu.user_id,w.id,cu.campaign_id,cu.discount_minor,cu.discount_minor,cu.expires_at
            from commerce.campaign_usage cu join commerce.users u on u.id=cu.user_id
                join commerce.wallets w on w.user_id=u.id and w.kind='customer'
            where cu.campaign_id=(result->>'campaign_id')::uuid and u.telegram_user_id=p_telegram_user_id;
    end if;
    return result;
end $function$;

revoke all on function commerce_rpc.activate_campaign(p_telegram_user_id bigint, p_code text, p_reference text) from public,anon,authenticated;

grant execute on function commerce_rpc.activate_campaign(p_telegram_user_id bigint, p_code text, p_reference text) to service_role;

CREATE OR REPLACE FUNCTION commerce_rpc.reserve_order(p_order_id uuid, p_telegram_user_id bigint, p_consent_reference text, p_payment_id uuid DEFAULT NULL::uuid)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO ''
AS $function$
declare uid uuid;result jsonb;remaining numeric;allocation numeric;item record;lot commerce.promotional_credit_lots%rowtype;
    already_reserved boolean;w commerce.wallets%rowtype;attributed numeric;
begin
    select id into uid from commerce.users where telegram_user_id=p_telegram_user_id for update;
    if not found then raise exception 'Registered customer required' using errcode='22023';end if;
    perform commerce.expire_user_promotional_credit(uid);
    select exists(select 1 from commerce.reservations where order_id=p_order_id) into already_reserved;
    result:=commerce.reserve_order_before_expiry_lots(p_order_id,p_telegram_user_id,p_consent_reference,p_payment_id);
    if result->>'state'<>'held' or already_reserved then return result;end if;
    for item in select row_number,promotional_payment_minor from commerce.checks
        where order_id=p_order_id and promotional_payment_minor>0 order by row_number loop
        remaining:=item.promotional_payment_minor;
        for lot in select * from commerce.promotional_credit_lots where user_id=uid and available_minor>0
            and (expires_at is null or expires_at>now()) order by expires_at nulls last,id for update loop
            if remaining=0 then exit;end if;
            allocation:=least(remaining,lot.available_minor);
            update commerce.promotional_credit_lots set available_minor=available_minor-allocation,reserved_minor=reserved_minor+allocation where id=lot.id;
            insert into commerce.promotional_credit_allocations(order_id,row_number,lot_id,amount_minor)
                values(p_order_id,item.row_number,lot.id,allocation);
            remaining:=remaining-allocation;
        end loop;
        if remaining<>0 then raise exception 'Promotional reserve is not fully attributed';end if;
    end loop;
    select * into w from commerce.wallets where user_id=uid and kind='customer';
    select coalesce(sum(available_minor::numeric),0) into attributed from commerce.promotional_credit_lots where user_id=uid;
    if attributed<>w.promotional_available_minor then raise exception 'Promotional available balance differs from grants';end if;
    return result;
end $function$;

revoke all on function commerce_rpc.reserve_order(p_order_id uuid, p_telegram_user_id bigint, p_consent_reference text, p_payment_id uuid) from public,anon,authenticated;

grant execute on function commerce_rpc.reserve_order(p_order_id uuid, p_telegram_user_id bigint, p_consent_reference text, p_payment_id uuid) to service_role;

CREATE OR REPLACE FUNCTION commerce_rpc.settle_order(p_order_id uuid)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO ''
AS $function$
declare result jsonb;uid uuid;item record;expired numeric;
begin
    select u.id into uid from commerce.users u join commerce.orders o on o.user_id=u.id where o.id=p_order_id for update of u;
    result:=commerce.settle_order_before_expiry_lots(p_order_id);
    for item in select a.lot_id,sum(a.amount_minor)::numeric as held,
        coalesce(sum(a.amount_minor) filter(where c.verified_result),0)::numeric as consumed,
        coalesce(sum(a.amount_minor) filter(where not c.verified_result),0)::numeric as refunded
        from commerce.promotional_credit_allocations a join commerce.checks c on c.order_id=a.order_id and c.row_number=a.row_number
        where a.order_id=p_order_id and a.state='held' group by a.lot_id order by a.lot_id loop
        update commerce.promotional_credit_lots set reserved_minor=reserved_minor-item.held,
            consumed_minor=consumed_minor+item.consumed,available_minor=available_minor+item.refunded where id=item.lot_id;
        update commerce.promotional_credit_allocations set state='settled' where order_id=p_order_id and lot_id=item.lot_id and state='held';
    end loop;
    expired:=commerce.expire_user_promotional_credit(uid);
    return result||jsonb_build_object('expired_promotional_minor',expired);
end $function$;

revoke all on function commerce_rpc.settle_order(p_order_id uuid) from public,anon,authenticated;

grant execute on function commerce_rpc.settle_order(p_order_id uuid) to service_role;

CREATE OR REPLACE FUNCTION commerce_rpc.confirm_binance_note(p_quote_id uuid, p_evidence jsonb)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO ''
AS $function$
declare r commerce.payment_requests%rowtype;c commerce.binance_note_challenges%rowtype;u commerce.users%rowtype;
    proof jsonb;received timestamptz;result jsonb;v_binding jsonb;
begin
    select usr.* into u from commerce.users usr join commerce.payment_requests pr on pr.user_id=usr.id
        where pr.id=p_quote_id and usr.registration_state='active' for update of usr;
    if not found then raise exception 'Active challenge customer required' using errcode='22023';end if;
    select * into r from commerce.payment_requests where id=p_quote_id for update;
    select * into c from commerce.binance_note_challenges where quote_id=r.id;
    if not found or r.provider<>'binance_pay' or jsonb_typeof(p_evidence) is distinct from 'object' then
        raise exception 'A stored pre-payment Binance challenge is required' using errcode='22023';end if;
    proof:=(p_evidence->>'proof_json')::jsonb;
    if jsonb_typeof(proof) is distinct from 'object' or proof->>'payment_note' is distinct from c.challenge
       or proof->>'payer_uid' is distinct from c.payer_uid or proof->>'order_type' is distinct from 'C2C'
       or p_evidence->>'provider' is distinct from r.provider or p_evidence->>'receiver' is distinct from r.receiver
       or p_evidence->>'asset' is distinct from r.quote->>'asset'
       or coalesce(p_evidence->>'reference','') !~ '^[A-Za-z0-9_-]{1,128}$'
       or proof->>'transaction_id' is distinct from p_evidence->>'reference'
       or proof->>'receiver' is distinct from r.receiver or proof->>'asset' is distinct from r.quote->>'asset'
       or coalesce(p_evidence->>'amount','') !~ '^[0-9]{1,40}(\.[0-9]{1,40})?$'
       or proof->>'amount' is distinct from p_evidence->>'amount' then
        raise exception 'Authenticated deposit does not match its private pre-payment challenge' using errcode='22023';end if;
    received:=(p_evidence->>'received_at')::timestamptz;
    if received is null or (p_evidence->>'amount')::numeric<>(r.quote->>'asset_amount')::numeric
       or received<c.issued_at or received>(r.quote->>'expires_at')::timestamptz or received>now()+interval '30 seconds' then
        raise exception 'Challenge payment amount or time requires reconciliation' using errcode='22023';end if;
    v_binding:=jsonb_build_object('kind','private_binance_note','challenge_quote_id',r.id,'payer_uid',c.payer_uid);
    if r.payment_id is not null then
        if r.evidence<>p_evidence or r.binding<>v_binding then raise exception 'Conflicting challenge replay' using errcode='22023';end if;
        return jsonb_build_object('payment_id',r.payment_id,'state','verified','replayed',true);
    end if;
    result:=commerce_rpc.confirm_receipt(u.telegram_user_id,'quote:'||r.id,r.provider,p_evidence->>'reference',
        (r.quote->>'amount_minor')::numeric,'payment-note:'||r.id,case when r.order_id is null then 'topup' else 'direct_order' end,
        r.order_id,jsonb_build_object('quote',r.quote,'evidence',p_evidence,'binding',v_binding));
    update commerce.payment_requests set payment_id=(result->>'payment_id')::uuid,evidence=p_evidence,binding=v_binding,confirmed_at=now() where id=r.id;
    return result;
end $function$;

revoke all on function commerce_rpc.confirm_binance_note(p_quote_id uuid, p_evidence jsonb) from public,anon,authenticated;

grant execute on function commerce_rpc.confirm_binance_note(p_quote_id uuid, p_evidence jsonb) to service_role;

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
    select sum(discount_minor)::numeric,max((funder_snapshot->'rules'->>'benefit_value')::bigint)
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

revoke all on function commerce.settle_partner_offer(p_order_id uuid) from public,anon,authenticated,service_role;

create or replace function commerce_rpc.health()
returns jsonb language sql stable security definer set search_path='' as $$
    select jsonb_build_object('status','ok','schema_version',30,'currency','EGP','money_precision','exact_decimal')
$$;
notify pgrst,'reload schema';
commit;
