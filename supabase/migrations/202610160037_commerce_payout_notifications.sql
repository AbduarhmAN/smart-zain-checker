-- Durable partner/channel notifications are committed with receipt evidence.
begin;
create table commerce.payout_notice_routes(id boolean primary key default true check(id),channels bigint[] not null default '{}');
insert into commerce.payout_notice_routes(id) values(true);
create table commerce.payout_notifications(
    id uuid primary key default gen_random_uuid(),withdrawal_id uuid not null references commerce.withdrawals(id),
    kind text not null check(kind in ('paid','confirmed','disputed')),
    recipient_kind text not null check(recipient_kind in ('partner','channel')),
    recipient_chat_id bigint,payload jsonb not null,
    state text not null default 'pending' check(state in ('pending','sending','sent','rejected','uncertain','waiting_config','routed')),
    token uuid,attempts integer not null default 0,telegram_message_id bigint,
    next_attempt_at timestamptz not null default now(),created_at timestamptz not null default now(),updated_at timestamptz not null default now(),
    unique nulls not distinct(withdrawal_id,kind,recipient_kind,recipient_chat_id),
    check((recipient_kind='partner' and recipient_chat_id>0) or (recipient_kind='channel' and (recipient_chat_id<0 or recipient_chat_id is null)))
);
alter table commerce.payout_notifications enable row level security;
alter table commerce.payout_notice_routes enable row level security;
revoke all on commerce.payout_notifications,commerce.payout_notice_routes from public,anon,authenticated,service_role;

create function commerce.enqueue_payout_notifications() returns trigger
language plpgsql security definer set search_path='' as $$
declare event_kind text;record jsonb;channel bigint;channels bigint[];
begin
    if TG_OP='INSERT' then event_kind:='paid';
    elsif NEW.state is not distinct from OLD.state then return NEW;
    else event_kind:=NEW.state;end if;
    if event_kind not in ('paid','confirmed','disputed') then return NEW;end if;
    record:=commerce_rpc.withdrawal_record(NEW.withdrawal_id);
    if record->>'state'<>'paid' then raise exception 'Paid payout required for notice' using errcode='22023';end if;
    insert into commerce.payout_notifications(withdrawal_id,kind,recipient_kind,recipient_chat_id,payload)
        values(NEW.withdrawal_id,event_kind,'partner',NEW.recipient_telegram_id,record) on conflict do nothing;
    select r.channels into channels from commerce.payout_notice_routes r where id=true;
    if cardinality(channels)=0 then
        insert into commerce.payout_notifications(withdrawal_id,kind,recipient_kind,payload,state)
            values(NEW.withdrawal_id,event_kind,'channel',record,'waiting_config') on conflict do nothing;
    else
        foreach channel in array channels loop
            insert into commerce.payout_notifications(withdrawal_id,kind,recipient_kind,recipient_chat_id,payload)
                values(NEW.withdrawal_id,event_kind,'channel',channel,record) on conflict do nothing;
        end loop;
    end if;
    return NEW;
end $$;
create trigger payout_receipt_notifications after insert or update of state on commerce.withdrawal_receipts
    for each row execute function commerce.enqueue_payout_notifications();

create function commerce_rpc.configure_payout_notice_channels(p_actor_telegram_id bigint,p_channels jsonb)
returns jsonb language plpgsql security definer set search_path='' as $$
declare v_channels bigint[];item jsonb;notice commerce.payout_notifications%rowtype;channel bigint;
begin
    if not exists(select 1 from commerce.support_actors where telegram_user_id=p_actor_telegram_id and active) then
        raise exception 'Active payout administrator required' using errcode='42501';end if;
    if p_channels is null or jsonb_typeof(p_channels)<>'array' or jsonb_array_length(p_channels)>20 then
        raise exception 'Bounded payout channels required' using errcode='22023';end if;
    v_channels:='{}';
    for item in select value from jsonb_array_elements(p_channels) loop
        if jsonb_typeof(item)<>'number' or item::text !~ '^-[0-9]+$' or item::text::bigint>=0 then
            raise exception 'Private administration channel required' using errcode='22023';end if;
        if not item::text::bigint=any(v_channels) then v_channels:=array_append(v_channels,item::text::bigint);end if;
    end loop;
    update commerce.payout_notice_routes set channels=v_channels where id=true;
    if cardinality(v_channels)>0 then
        for notice in select * from commerce.payout_notifications where state='waiting_config' for update loop
            foreach channel in array v_channels loop
                insert into commerce.payout_notifications(withdrawal_id,kind,recipient_kind,recipient_chat_id,payload)
                    values(notice.withdrawal_id,notice.kind,'channel',channel,notice.payload) on conflict do nothing;
            end loop;
            update commerce.payout_notifications set state='routed',updated_at=now() where id=notice.id;
        end loop;
    end if;
    return jsonb_build_object('channels',cardinality(v_channels));
end $$;

create function commerce_rpc.claim_payout_notification() returns jsonb
language plpgsql security definer set search_path='' as $$
declare result jsonb;
begin
    update commerce.payout_notifications set state='uncertain',updated_at=now()
        where state='sending' and updated_at<now()-interval '2 minutes';
    with next as(select id from commerce.payout_notifications where state in ('pending','rejected')
        and attempts<3 and recipient_chat_id is not null and next_attempt_at<=now()
        order by created_at,id for update skip locked limit 1)
    update commerce.payout_notifications n set state='sending',token=gen_random_uuid(),attempts=attempts+1,updated_at=now()
        from next where n.id=next.id returning to_jsonb(n) into result;
    return result;
end $$;
create function commerce_rpc.complete_payout_notification(p_id uuid,p_token uuid,p_state text,p_message_id bigint default null)
returns jsonb language plpgsql security definer set search_path='' as $$
begin
    if p_state is null or p_state not in ('sent','rejected','uncertain')
       or (p_state='sent' and coalesce(p_message_id,0)<=0) then
        raise exception 'Telegram delivery evidence required' using errcode='22023';end if;
    update commerce.payout_notifications set state=p_state,telegram_message_id=p_message_id,
        next_attempt_at=now()+interval '30 seconds'*attempts,updated_at=now()
        where id=p_id and token=p_token and state='sending';
    return jsonb_build_object('saved',found);
end $$;
create function commerce_rpc.payout_notification_status(p_actor_telegram_id bigint,p_withdrawal_id uuid)
returns jsonb language plpgsql stable security definer set search_path='' as $$
begin
    if not exists(select 1 from commerce.support_actors where telegram_user_id=p_actor_telegram_id and active) then
        raise exception 'Active payout administrator required' using errcode='42501';end if;
    return coalesce((select jsonb_agg(jsonb_build_object('kind',kind,'recipient_kind',recipient_kind,'state',state,
        'telegram_message_id',telegram_message_id,'attempts',attempts) order by created_at,id)
        from commerce.payout_notifications where withdrawal_id=p_withdrawal_id and state<>'routed'),'[]'::jsonb);
end $$;
revoke all on function commerce.enqueue_payout_notifications() from public,anon,authenticated,service_role;
revoke all on function commerce_rpc.configure_payout_notice_channels(bigint,jsonb),commerce_rpc.claim_payout_notification(),
    commerce_rpc.complete_payout_notification(uuid,uuid,text,bigint),commerce_rpc.payout_notification_status(bigint,uuid)
    from public,anon,authenticated;
grant execute on function commerce_rpc.configure_payout_notice_channels(bigint,jsonb),commerce_rpc.claim_payout_notification(),
    commerce_rpc.complete_payout_notification(uuid,uuid,text,bigint),commerce_rpc.payout_notification_status(bigint,uuid)
    to service_role;
notify pgrst,'reload schema';
commit;
