-- Read-only administration totals from posted revenue and partner wallets.
begin;
create function commerce_rpc.admin_earnings_summary(p_actor_telegram_id bigint,
    p_offset integer default 0,p_limit integer default 8)
returns jsonb language plpgsql stable security definer set search_path='' as $$
declare result jsonb;day_start timestamptz;
begin
    if not exists(select 1 from commerce.support_actors where telegram_user_id=p_actor_telegram_id and active) then
        raise exception 'Active finance administrator required' using errcode='42501';end if;
    if p_offset is null or p_offset not between 0 and 1000000 or p_limit is null or p_limit not between 1 and 50 then
        raise exception 'Bounded finance page required' using errcode='22023';end if;
    day_start:=date_trunc('day',now() at time zone 'Asia/Baghdad') at time zone 'Asia/Baghdad';
    with revenue as (
        select coalesce(sum(e.delta_minor) filter(where j.kind='service'),0) as gross_service_minor,
            -coalesce(sum(e.delta_minor) filter(where j.kind='commission'),0) as partner_commission_minor,
            coalesce(sum(e.delta_minor),0) as platform_revenue_minor,
            coalesce(sum(e.delta_minor) filter(where j.created_at>=day_start),0) as today_platform_revenue_minor
        from commerce.ledger_entries e join commerce.journals j on j.id=e.journal_id
        where e.account_key='platform:revenue'
    ), partner_totals as (
        select partner_id,coalesce(sum(amount_minor) filter(where state<>'reversed'),0) as earned_minor,
            coalesce(sum(amount_minor) filter(where state='pending'),0) as pending_minor
        from commerce.commissions group by partner_id
    ), page as (
        select p.id as partner_id,u.telegram_user_id,u.telegram_username,p.state,
            (select code from commerce.codes where partner_id=p.id and kind='referral' order by created_at,id limit 1) as referral_code,
            coalesce(w.available_minor,0) as available_minor,coalesce(w.reserved_minor,0) as reserved_minor,
            coalesce(w.withdrawn_minor,0) as withdrawn_minor,
            coalesce(t.earned_minor,0) as earned_minor,coalesce(t.pending_minor,0) as pending_minor
        from commerce.partners p join commerce.users u on u.id=p.user_id
        left join commerce.wallets w on w.user_id=p.user_id and w.kind='partner' and w.currency='EGP'
        left join partner_totals t on t.partner_id=p.id
        order by p.created_at desc,p.id desc limit p_limit offset p_offset
    )
    select to_jsonb(revenue)||jsonb_build_object('partners',coalesce((select jsonb_agg(to_jsonb(page)) from page),'[]'::jsonb),
        'partners_total',(select count(*) from commerce.partners),'offset',p_offset,'limit',p_limit,
        'awaiting_settlement_orders',(select count(*) from commerce.orders where payment_state='reserved'))
        into result from revenue;
    return result;
end $$;
revoke all on function commerce_rpc.admin_earnings_summary(bigint,integer,integer) from public,anon,authenticated;
grant execute on function commerce_rpc.admin_earnings_summary(bigint,integer,integer) to service_role;
notify pgrst,'reload schema';
commit;
