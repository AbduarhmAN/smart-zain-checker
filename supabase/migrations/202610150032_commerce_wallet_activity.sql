-- Customer-facing file totals. Raw accounting entries remain unchanged.
begin;
create function commerce_rpc.customer_wallet_activity(
    p_telegram_user_id bigint,p_offset integer default 0,p_limit integer default 8)
returns jsonb language plpgsql stable security definer set search_path='' as $$
declare uid uuid;result jsonb;
begin
    if p_telegram_user_id is null or p_telegram_user_id<=0 or p_offset is null
       or p_offset not between 0 and 1000000 or p_limit is null or p_limit not between 1 and 100 then
        raise exception 'Valid authenticated wallet activity page required' using errcode='22023';
    end if;
    select id into uid from commerce.users
        where telegram_user_id=p_telegram_user_id and registration_state='active';
    if not found then raise exception 'Active registered customer required' using errcode='22023';end if;

    with buckets as (
        select coalesce(j.order_id::text,j.id::text) as activity_id,j.order_id,j.kind,w.kind as wallet_kind,
            split_part(e.account_key,':',3) as bucket,sum(e.delta_minor) as delta_minor,
            max(j.created_at) as created_at,count(*) as entry_count
        from commerce.ledger_entries e join commerce.wallets w on w.id=e.wallet_id
            join commerce.journals j on j.id=e.journal_id
        where w.user_id=uid
        group by coalesce(j.order_id::text,j.id::text),j.order_id,j.kind,w.kind,split_part(e.account_key,':',3)
    ), activities as (
        select activity_id,order_id,kind,wallet_kind,max(created_at) as created_at,
            jsonb_object_agg(bucket,delta_minor) as bucket_deltas,sum(entry_count) as entry_count,
            case when kind in ('reserve','withdrawal') then -greatest(sum(greatest(delta_minor,0)),-sum(least(delta_minor,0)))
                 when kind in ('refund','release') then greatest(sum(greatest(delta_minor,0)),-sum(least(delta_minor,0)))
                 else sum(delta_minor) end as delta_minor
        from buckets group by activity_id,order_id,kind,wallet_kind
    ), page as (
        select a.*,case when o.user_id=uid then o.source_snapshot->>'original_filename' end as filename,
            coalesce(o.user_id=uid,false) as owned_order
        from activities a left join commerce.orders o on o.id=a.order_id
        order by a.created_at desc,a.activity_id desc,a.kind,a.wallet_kind limit p_limit offset p_offset
    )
    select jsonb_build_object('items',coalesce((select jsonb_agg(to_jsonb(page)
        order by created_at desc,activity_id desc,kind,wallet_kind) from page),'[]'::jsonb),
        'total',(select count(*) from activities),'offset',p_offset,'limit',p_limit) into result;
    return result;
end $$;
revoke all on function commerce_rpc.customer_wallet_activity(bigint,integer,integer) from public,anon,authenticated;
grant execute on function commerce_rpc.customer_wallet_activity(bigint,integer,integer) to service_role;
notify pgrst,'reload schema';
commit;
