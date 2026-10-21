-- Read-only administrator reports. No activity copies or financial mutations.
begin;

create function commerce.customer_activity_events(p_kind text)
returns table(user_id uuid,event_id text,occurred_at timestamptz,code text,
    cash_credit_minor numeric,promotional_credit_minor numeric,
    free_account_checks bigint,free_service_checks bigint,zero_price_checks bigint,
    full_discount_checks bigint,promotional_checks bigint,discount_minor numeric,promotional_minor numeric)
language sql stable set search_path='' as $$
    select r.user_id,'code:'||r.code_id::text,r.redeemed_at,c.code,
        case when c.kind='cash_credit' then sum(e.delta_minor::numeric) else 0 end,
        case when c.kind='discount' then sum(e.delta_minor::numeric) else 0 end,
        0::bigint,0::bigint,0::bigint,0::bigint,0::bigint,0::numeric,0::numeric
    from commerce.code_redemptions r join commerce.codes c on c.id=r.code_id
        join commerce.ledger_entries e on e.journal_id=r.journal_id
        join commerce.wallets w on w.id=e.wallet_id and w.user_id=r.user_id and w.kind='customer'
    where p_kind='codes' and c.kind in ('cash_credit','discount') and e.delta_minor>0
        and e.account_key='wallet:'||w.id::text||':available'
    group by r.user_id,r.code_id,r.redeemed_at,c.code,c.kind
    union all
    select o.user_id,o.id::text,o.financial_completed_at,null::text,0::numeric,0::numeric,
        count(*) filter(where c.check_type='account'),count(*) filter(where c.check_type='service'),
        count(*) filter(where c.unit_quote_minor=0),
        count(*) filter(where c.unit_quote_minor>0 and c.discount_minor=c.unit_quote_minor),
        count(*) filter(where c.unit_quote_minor>c.discount_minor),
        sum(c.discount_minor::numeric),sum(c.promotional_payment_minor::numeric)
    from commerce.orders o join commerce.checks c on c.order_id=o.id
    where p_kind='free' and o.execution_state='terminal' and o.delivery_state='confirmed'
        and o.payment_state in ('settled','refunded','partial_refund') and o.financial_completed_at is not null
        and c.verified_result and c.unit_quote_minor-c.discount_minor-c.promotional_payment_minor=0
    group by o.user_id,o.id,o.financial_completed_at
$$;
revoke all on function commerce.customer_activity_events(text) from public,anon,authenticated,service_role;

create function commerce_rpc.admin_customer_activity(p_actor_telegram_id bigint,p_kind text,
    p_offset integer default 0,p_limit integer default 6,p_customer_telegram_id bigint default null,
    p_excluded_admin_ids bigint[] default '{}')
returns jsonb language plpgsql stable security definer set search_path='' as $$
declare result jsonb;
begin
    perform commerce.require_sheet_admin(p_actor_telegram_id);
    if p_kind is null or p_kind not in ('codes','free') or p_offset is null or p_offset not between 0 and 1000000
       or p_limit is null or p_limit not between 1 and 20
       or (p_customer_telegram_id is not null and p_customer_telegram_id<=0)
       or p_excluded_admin_ids is null or cardinality(p_excluded_admin_ids)>100
       or exists(select 1 from unnest(p_excluded_admin_ids) a where a is null or a<=0) then
        raise exception 'Valid customer activity page required' using errcode='22023';end if;
    with events as materialized (
        select e.*,u.telegram_user_id,u.telegram_username from commerce.customer_activity_events(p_kind) e
            join commerce.users u on u.id=e.user_id
        where u.telegram_user_id<>p_actor_telegram_id and not(u.telegram_user_id=any(p_excluded_admin_ids))
            and (p_customer_telegram_id is null or u.telegram_user_id=p_customer_telegram_id)
            and not exists(select 1 from commerce.partners p where p.user_id=u.id)
            and not exists(select 1 from commerce.support_actors a where a.telegram_user_id=u.telegram_user_id and a.active)
    ), customers as (
        select telegram_user_id,telegram_username,count(*) as event_count,max(occurred_at) as last_activity_at,
            sum(cash_credit_minor) as cash_credit_minor,sum(promotional_credit_minor) as promotional_credit_minor,
            sum(free_account_checks) as free_account_checks,sum(free_service_checks) as free_service_checks
        from events group by telegram_user_id,telegram_username
    ), customer_page as (
        select * from customers order by last_activity_at desc,telegram_user_id limit p_limit offset p_offset
    ), event_page as (
        select * from events order by occurred_at desc,event_id limit p_limit offset p_offset
    )
    select jsonb_build_object('kind',p_kind,'customer_telegram_id',p_customer_telegram_id,
        'offset',p_offset,'limit',p_limit,
        'total',case when p_customer_telegram_id is null then (select count(*) from customers)
                     else (select count(*) from events) end,
        'customer_count',(select count(*) from customers),
        'cash_credit_minor',coalesce((select sum(cash_credit_minor) from events),0),
        'promotional_credit_minor',coalesce((select sum(promotional_credit_minor) from events),0),
        'free_account_checks',coalesce((select sum(free_account_checks) from events),0),
        'free_service_checks',coalesce((select sum(free_service_checks) from events),0),
        'items',case when p_customer_telegram_id is null
            then coalesce((select jsonb_agg(to_jsonb(customer_page) order by last_activity_at desc,telegram_user_id) from customer_page),'[]'::jsonb)
            else coalesce((select jsonb_agg(to_jsonb(event_page)-'user_id' order by occurred_at desc,event_id) from event_page),'[]'::jsonb) end)
        into result;
    return result;
end $$;
revoke all on function commerce_rpc.admin_customer_activity(bigint,text,integer,integer,bigint,bigint[]) from public,anon,authenticated;
grant execute on function commerce_rpc.admin_customer_activity(bigint,text,integer,integer,bigint,bigint[]) to service_role;
notify pgrst,'reload schema';
commit;
