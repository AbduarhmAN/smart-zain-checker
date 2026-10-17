-- A74: preserve order-linked support at receipt time, including source edits.
begin;
create table commerce.support_actors (
    telegram_user_id bigint primary key check(telegram_user_id>0),
    active boolean not null,
    selection_reference text not null,
    updated_at timestamptz not null default now()
);
create table commerce.support_threads (
    order_id uuid primary key references commerce.orders(id),
    state text not null default 'open' check(state in('open','closed')),
    created_at timestamptz not null default now(),
    last_message_at timestamptz not null default now(),
    last_customer_at timestamptz,
    last_reply_at timestamptz,
    state_reference text,
    state_history jsonb not null default '[]' check(jsonb_typeof(state_history)='array')
);
create table commerce.support_messages (
    event_key text primary key check(event_key ~ '^[a-f0-9-]{36}$'),
    order_id uuid not null references commerce.support_threads(order_id),
    actor_telegram_id bigint not null check(actor_telegram_id>0),
    actor_role text not null check(actor_role in('customer','admin')),
    kind text not null check(kind in('customer_message','staff_reply','customer_edit','staff_edit')),
    record jsonb not null,
    evidence_sha256 text not null check(evidence_sha256 ~ '^[a-f0-9]{64}$'),
    delivery_state text not null check(delivery_state in('received','proposed','confirmed')),
    delivery_receipt jsonb,
    captured_at timestamptz not null default now(),
    delivered_at timestamptz,
    check((delivery_state='confirmed')=(delivered_at is not null)),
    check((delivery_state='confirmed')=(delivery_receipt is not null))
);
create index support_inbox on commerce.support_threads(state,last_message_at desc,order_id);
create index support_order_history on commerce.support_messages(order_id,captured_at,event_key);
create unique index support_original_identity on commerce.support_messages(
    (record->'source'->>'bot_id'),(record->'source'->>'chat_id'),(record->'source'->>'message_id'))
    where kind in('customer_message','staff_reply');
alter table commerce.support_actors enable row level security;
alter table commerce.support_threads enable row level security;
alter table commerce.support_messages enable row level security;
revoke all on commerce.support_actors,commerce.support_threads,commerce.support_messages from public,anon,authenticated,service_role;

create function commerce.require_support_actor(p_actor bigint)
returns void language plpgsql stable security definer set search_path='' as $$
begin
    if p_actor is null or not exists(select 1 from commerce.support_actors where telegram_user_id=p_actor and active) then
        raise exception 'Support administrator is not configured' using errcode='22023'; end if;
end $$;
revoke all on function commerce.require_support_actor(bigint) from public,anon,authenticated,service_role;

create function commerce_rpc.configure_support_actors(p_actor_ids jsonb,p_reference text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare actor jsonb;
begin
    if p_actor_ids is null or jsonb_typeof(p_actor_ids)<>'array' or jsonb_array_length(p_actor_ids) not between 1 and 30
       or p_reference is null or length(btrim(p_reference)) not between 1 and 1000 then
        raise exception 'Verified local support administrators required' using errcode='22023'; end if;
    for actor in select value from jsonb_array_elements(p_actor_ids) loop
        if jsonb_typeof(actor)<>'number' or actor::text !~ '^[1-9][0-9]{0,15}$'
           or actor::text::numeric>=4503599627370496 then raise exception 'Invalid support administrator identity' using errcode='22023'; end if;
    end loop;
    perform pg_advisory_xact_lock(610140018);
    update commerce.support_actors set active=false,selection_reference=p_reference,updated_at=now();
    insert into commerce.support_actors(telegram_user_id,active,selection_reference)
        select distinct (value::text)::bigint,true,p_reference from jsonb_array_elements(p_actor_ids)
        on conflict(telegram_user_id) do update set active=true,selection_reference=excluded.selection_reference,updated_at=now();
    return jsonb_build_object('state','configured','active_count',(select count(*) from commerce.support_actors where active));
end $$;

create function commerce_rpc.support_order(p_order_id uuid,p_actor_telegram_id bigint,p_role text)
returns jsonb language plpgsql stable security definer set search_path='' as $$
declare o commerce.orders%rowtype; owner bigint;
begin
    if p_role is null or p_role not in('customer','admin') then raise exception 'Invalid support role' using errcode='22023'; end if;
    if p_role='admin' then perform commerce.require_support_actor(p_actor_telegram_id); end if;
    select * into o from commerce.orders where id=p_order_id;
    if not found then raise exception 'Unknown or unowned support order' using errcode='22023'; end if;
    select telegram_user_id into owner from commerce.users where id=o.user_id;
    if p_role='customer' and owner is distinct from p_actor_telegram_id then raise exception 'Unknown or unowned support order' using errcode='22023'; end if;
    return jsonb_build_object('order_id',o.id,'owner_telegram_id',owner,'execution_state',o.execution_state,
        'payment_state',o.payment_state,'delivery_state',o.delivery_state,
        'thread_state',(select state from commerce.support_threads where order_id=o.id));
end $$;

create function commerce_rpc.record_support_message(p_order_id uuid,p_actor_telegram_id bigint,p_role text,p_event_key text,p_record jsonb)
returns jsonb language plpgsql security definer set search_path='' as $$
declare profile jsonb; prior commerce.support_messages%rowtype; attachment jsonb; kind text; source jsonb; fingerprint text;
begin
    profile:=commerce_rpc.support_order(p_order_id,p_actor_telegram_id,p_role);
    if p_event_key is null or p_event_key !~ '^[a-f0-9-]{36}$' or jsonb_typeof(p_record) is distinct from 'object'
       or (select count(*) from jsonb_object_keys(p_record))<>4
       or not(p_record ?& array['kind','body','attachments','source'])
       or jsonb_typeof(p_record->'body') is distinct from 'string' or length(p_record->>'body')>4096
       or jsonb_typeof(p_record->'attachments') is distinct from 'array' or jsonb_array_length(p_record->'attachments')>10
       or jsonb_typeof(p_record->'source') is distinct from 'object' or octet_length(p_record::text)>65536 then
        raise exception 'Invalid support evidence' using errcode='22023'; end if;
    kind:=p_record->>'kind';source:=p_record->'source';
    if (p_role='customer' and kind not in('customer_message','customer_edit'))
       or (p_role='admin' and kind not in('staff_reply','staff_edit')) or kind is null
       or source->>'chat_id' is distinct from p_actor_telegram_id::text
       or exists(select 1 from jsonb_object_keys(source) k where k not in('bot_id','chat_id','message_id','date','edit_date'))
       or exists(select 1 from unnest(array['bot_id','chat_id','message_id','date']) k where
           jsonb_typeof(source->k) is distinct from 'number' or coalesce(source->>k,'') !~ '^[1-9][0-9]{0,15}$')
       or coalesce(source->>'edit_date','0') !~ '^[0-9]{1,12}$'
       or (source ? 'edit_date' and (jsonb_typeof(source->'edit_date')<>'number' or (source->>'edit_date')::numeric<(source->>'date')::numeric))
       or (source->>'date')::numeric>4102444800 then
        raise exception 'Support Telegram identity or role differs' using errcode='22023'; end if;
    if length(btrim(p_record->>'body'))=0 and jsonb_array_length(p_record->'attachments')=0 then raise exception 'Empty support message' using errcode='22023'; end if;
    if p_role='admin' and (length(p_record->>'body')>3000 or jsonb_array_length(p_record->'attachments')>0) then
        raise exception 'Staff reply requires text of at most 3000 characters' using errcode='22023'; end if;
    for attachment in select value from jsonb_array_elements(p_record->'attachments') loop
        if jsonb_typeof(attachment) is distinct from 'object'
           or coalesce(attachment->>'kind','') not in('document','photo','voice','audio','video')
           or jsonb_typeof(attachment->'file_id') is distinct from 'string' or length(attachment->>'file_id') not between 1 and 512
           or jsonb_typeof(attachment->'file_unique_id') is distinct from 'string' or length(attachment->>'file_unique_id') not between 1 and 512
           or exists(select 1 from jsonb_object_keys(attachment) k where k not in('kind','file_id','file_unique_id','file_size','file_name','mime_type'))
           or (attachment ? 'file_size' and (jsonb_typeof(attachment->'file_size')<>'number' or attachment->>'file_size' !~ '^[0-9]{1,16}$'))
           or length(coalesce(attachment->>'file_name',''))>256 or length(coalesce(attachment->>'mime_type',''))>128 then
            raise exception 'Invalid support attachment identity' using errcode='22023'; end if;
    end loop;
    fingerprint:=encode(sha256(convert_to(p_record::text,'UTF8')),'hex');
    perform pg_advisory_xact_lock(hashtextextended(p_event_key,610140018));
    select * into prior from commerce.support_messages where event_key=p_event_key;
    if found then
        if prior.order_id<>p_order_id or prior.actor_telegram_id<>p_actor_telegram_id or prior.actor_role<>p_role or prior.record<>p_record then
            raise exception 'Conflicting support evidence replay' using errcode='22023'; end if;
        return jsonb_build_object('state','stored','event_key',p_event_key,'delivery_state',prior.delivery_state,'replayed',true,'sha256',prior.evidence_sha256); end if;
    if kind in('customer_edit','staff_edit') and not exists(select 1 from commerce.support_messages m where
        m.order_id=p_order_id and m.actor_telegram_id=p_actor_telegram_id
        and m.record->'source'->>'bot_id'=source->>'bot_id' and m.record->'source'->>'chat_id'=source->>'chat_id'
        and m.record->'source'->>'message_id'=source->>'message_id' and m.kind in('customer_message','staff_reply')) then
        raise exception 'Edit requires a captured original support message' using errcode='22023'; end if;
    if kind in('customer_edit','staff_edit') and not(source ? 'edit_date') then raise exception 'Edit timestamp missing' using errcode='22023'; end if;
    insert into commerce.support_threads(order_id) values(p_order_id) on conflict do nothing;
    insert into commerce.support_messages(event_key,order_id,actor_telegram_id,actor_role,kind,record,evidence_sha256,delivery_state)
        values(p_event_key,p_order_id,p_actor_telegram_id,p_role,kind,p_record,fingerprint,case when kind='staff_reply' then 'proposed' else 'received' end);
    update commerce.support_threads set last_message_at=now(),
        last_customer_at=case when p_role='customer' then now() else last_customer_at end,
        state=case when p_role='customer' then 'open' else state end where order_id=p_order_id;
    return jsonb_build_object('state','stored','event_key',p_event_key,'delivery_state',case when kind='staff_reply' then 'proposed' else 'received' end,'replayed',false,'sha256',fingerprint);
end $$;

create function commerce_rpc.confirm_support_delivery(p_event_key text,p_actor_telegram_id bigint,p_receipt jsonb)
returns jsonb language plpgsql security definer set search_path='' as $$
declare m commerce.support_messages%rowtype; profile jsonb; expected_text text;
begin
    perform commerce.require_support_actor(p_actor_telegram_id);
    select * into m from commerce.support_messages where event_key=p_event_key for update;
    if not found or m.kind<>'staff_reply' or m.actor_telegram_id<>p_actor_telegram_id then raise exception 'Owned staff reply required' using errcode='22023'; end if;
    profile:=commerce_rpc.support_order(m.order_id,p_actor_telegram_id,'admin');
    expected_text:='دعم الطلب: '||m.order_id::text||E'\n\n'||(m.record->>'body');
    if jsonb_typeof(p_receipt) is distinct from 'object'
       or p_receipt->'chat'->>'id' is distinct from profile->>'owner_telegram_id'
       or p_receipt->'from'->>'id' is distinct from m.record->'source'->>'bot_id'
       or p_receipt->'from'->'is_bot' is distinct from 'true'::jsonb
       or jsonb_typeof(p_receipt->'message_id') is distinct from 'number'
       or coalesce(p_receipt->>'message_id','') !~ '^[1-9][0-9]{0,15}$'
       or jsonb_typeof(p_receipt->'date') is distinct from 'number' or coalesce(p_receipt->>'date','') !~ '^[1-9][0-9]{0,9}$'
       or p_receipt->>'text' is distinct from expected_text then
        raise exception 'Confirmed support reply receipt differs' using errcode='22023'; end if;
    if m.delivery_state='confirmed' then
        if m.delivery_receipt<>p_receipt then raise exception 'Conflicting support delivery replay' using errcode='22023'; end if;
        return jsonb_build_object('state','confirmed','replayed',true); end if;
    update commerce.support_messages set delivery_state='confirmed',delivery_receipt=p_receipt,delivered_at=now() where event_key=p_event_key;
    update commerce.support_threads set last_reply_at=now(),last_message_at=now() where order_id=m.order_id;
    return jsonb_build_object('state','confirmed','replayed',false);
end $$;

create function commerce_rpc.support_history(p_order_id uuid,p_actor_telegram_id bigint,p_role text,p_offset integer default 0)
returns jsonb language plpgsql stable security definer set search_path='' as $$
declare profile jsonb;
begin
    profile:=commerce_rpc.support_order(p_order_id,p_actor_telegram_id,p_role);
    if p_offset is null or p_offset not between 0 and 1000000 then raise exception 'Invalid support page' using errcode='22023'; end if;
    return profile||jsonb_build_object('messages',coalesce((select jsonb_agg(item order by captured_at,event_key) from
        (select captured_at,event_key,jsonb_build_object('event_key',event_key,'kind',kind,'body',record->>'body',
            'attachments',case when p_role='admin' then record->'attachments' else (select coalesce(jsonb_agg(jsonb_build_object('kind',value->>'kind','file_name',value->>'file_name')),'[]'::jsonb) from jsonb_array_elements(record->'attachments')) end,
            'source',case when p_role='admin' then record->'source' else null end,'captured_at',captured_at,
            'delivery_receipt',case when p_role='admin' then delivery_receipt else null end,
            'delivery_state',delivery_state,'sha256',evidence_sha256) as item
        from commerce.support_messages where order_id=p_order_id
            and (p_role='admin' or kind in('customer_message','customer_edit') or (kind='staff_reply' and delivery_state='confirmed'))
        order by captured_at desc,event_key desc limit 3 offset p_offset) page),'[]'::jsonb));
end $$;

create function commerce_rpc.support_inbox(p_actor_telegram_id bigint,p_offset integer default 0)
returns jsonb language plpgsql stable security definer set search_path='' as $$
begin
    perform commerce.require_support_actor(p_actor_telegram_id);
    if p_offset is null or p_offset not between 0 and 1000000 then raise exception 'Invalid support inbox page' using errcode='22023'; end if;
    return coalesce((select jsonb_agg(to_jsonb(item)) from (select t.order_id,t.state,t.last_message_at,
        u.telegram_user_id as owner_telegram_id,t.last_customer_at,t.last_reply_at,
        (t.last_reply_at is null or t.last_customer_at>t.last_reply_at) as needs_reply
        from commerce.support_threads t join commerce.orders o on o.id=t.order_id join commerce.users u on u.id=o.user_id
        order by (t.state='open') desc,t.last_message_at desc,t.order_id limit 5 offset p_offset) item),'[]'::jsonb);
end $$;

create function commerce_rpc.set_support_state(p_order_id uuid,p_actor_telegram_id bigint,p_state text,p_reference text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare t commerce.support_threads%rowtype; prior jsonb;
begin
    perform commerce.require_support_actor(p_actor_telegram_id);
    if p_state is null or p_state not in('open','closed') or p_reference is null or length(p_reference) not between 1 and 1000 then
        raise exception 'Explicit support state receipt required' using errcode='22023'; end if;
    select * into t from commerce.support_threads where order_id=p_order_id for update;
    if not found then raise exception 'Unknown support thread' using errcode='22023'; end if;
    select value into prior from jsonb_array_elements(t.state_history) where value->>'reference'=p_reference;
    if found then
        if prior->>'state'<>p_state or prior->>'actor'<>p_actor_telegram_id::text then raise exception 'Conflicting support state replay' using errcode='22023'; end if;
        return jsonb_build_object('order_id',p_order_id,'state',t.state,'replayed',true); end if;
    update commerce.support_threads set state=p_state,state_reference=p_reference,
        state_history=state_history||jsonb_build_array(jsonb_build_object('state',p_state,'actor',p_actor_telegram_id,'reference',p_reference,'at',now())) where order_id=p_order_id;
    return jsonb_build_object('order_id',p_order_id,'state',p_state,'replayed',false);
end $$;
revoke all on function commerce_rpc.configure_support_actors(jsonb,text),commerce_rpc.support_order(uuid,bigint,text),
    commerce_rpc.record_support_message(uuid,bigint,text,text,jsonb),commerce_rpc.confirm_support_delivery(text,bigint,jsonb),
    commerce_rpc.support_history(uuid,bigint,text,integer),commerce_rpc.support_inbox(bigint,integer),
    commerce_rpc.set_support_state(uuid,bigint,text,text) from public,anon,authenticated;
grant execute on function commerce_rpc.configure_support_actors(jsonb,text),commerce_rpc.support_order(uuid,bigint,text),
    commerce_rpc.record_support_message(uuid,bigint,text,text,jsonb),commerce_rpc.confirm_support_delivery(text,bigint,jsonb),
    commerce_rpc.support_history(uuid,bigint,text,integer),commerce_rpc.support_inbox(bigint,integer),
    commerce_rpc.set_support_state(uuid,bigint,text,text) to service_role;
create or replace function commerce_rpc.health()
returns jsonb language sql stable security definer set search_path='' as $$
    select jsonb_build_object('status','ok','schema_version',18,'currency','EGP')
$$;
notify pgrst,'reload schema';
commit;
