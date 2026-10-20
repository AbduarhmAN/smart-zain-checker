-- Exact proportional commissions also apply to not-yet-settled existing requests.
-- Customer prices and frozen partner percentages are unchanged. Already settled
-- journals and commissions return their saved values through the existing guard.
begin;
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

                amount:=row_data.revenue::numeric*rate/10000;journal:=null;

                if amount>0 then

                    journal:=commerce.post_pair('commission:'||o.id||':'||row_data.row_number,'commission',o.id,

                        'platform:revenue','wallet:'||w.id||':available',amount,null,w.id);

                end if;

                insert into commerce.commissions(order_id,row_number,partner_id,revenue_minor,amount_minor,terms_snapshot,state,journal_id)

                values(o.id,row_data.row_number,pid,row_data.revenue,amount,snapshot||jsonb_build_object(

                    'paid_check_ordinal',ordinal,'basis','collected_after_discounts_and_refunds','expenses_deducted',false,

                    'promotional_payment_excluded',true,

                    'rounding','exact_decimal','duration_measurement','paid_service_settled_at'),'available',journal);

                total:=total+amount;eligible_count:=eligible_count+1;

            end loop;

            update commerce.wallets set available_minor=available_minor+total::numeric where id=w.id;

        end if;

    end if;

    update commerce.users set successful_paid_checks=successful_paid_checks+paid_count where id=u.id;

    update commerce.orders set financial_completed_at=now(),commission_finalized_at=now() where id=o.id;

    return result||jsonb_build_object('collected_minor',o.collected_cash_minor,'commission_minor',total::numeric,'eligible_paid_checks',eligible_count);

end $function$;
create or replace function commerce_rpc.health()
returns jsonb language sql stable security definer set search_path='' as $$
    select jsonb_build_object('status','ok','schema_version',31,'currency','EGP','money_precision','exact_decimal')
$$;
notify pgrst,'reload schema';
commit;
