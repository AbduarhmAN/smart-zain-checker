-- Manual EGP transfers. A submitted screenshot never creates money.
begin;
create table commerce.vodafone_requests (
    id uuid primary key,
    user_id uuid not null references commerce.users(id),
    amount_minor bigint not null check(amount_minor>0),
    receiver text not null default '01050740996',
    receiver_name text not null default 'عبدالرحمن',
    sender_phone text check(sender_phone ~ '^01[0125][0-9]{8}$'),
    proof jsonb,
    state text not null default 'awaiting_evidence' check(state in('awaiting_evidence','pending','approved','rejected')),
    transfer_reference text,
    payment_id uuid unique references commerce.payments(id),
    reviewer_id bigint,
    review_reference text,
    rejection_reason text,
    created_at timestamptz not null default now(),
    submitted_at timestamptz,
    reviewed_at timestamptz,
    check((state='approved')=(payment_id is not null)),
    check(state='awaiting_evidence' or (sender_phone is not null and proof is not null and submitted_at is not null))
);
create index vodafone_requests_owner on commerce.vodafone_requests(user_id,created_at desc);
create index vodafone_requests_pending on commerce.vodafone_requests(created_at) where state='pending';
create unique index vodafone_approved_attachment on commerce.vodafone_requests((proof->>'file_unique_id'))
    where state='approved' and proof->>'kind' in('photo','document');
create table commerce.vodafone_messages (
    id bigint generated always as identity primary key,
    request_id uuid not null references commerce.vodafone_requests(id),
    actor_id bigint not null check(actor_id>0),
    actor_role text not null check(actor_role in('customer','admin')),
    source_message_id bigint not null check(source_message_id>0),
    body text not null check(length(btrim(body)) between 1 and 3000),
    created_at timestamptz not null default now(),
    unique(request_id,actor_id,source_message_id)
);
create index vodafone_messages_thread on commerce.vodafone_messages(request_id,id);
alter table commerce.vodafone_requests enable row level security;
alter table commerce.vodafone_messages enable row level security;
revoke all on commerce.vodafone_requests,commerce.vodafone_messages from public,anon,authenticated,service_role;

create function commerce.vodafone_access(p_request_id uuid,p_actor bigint,p_admin boolean)
returns commerce.vodafone_requests language plpgsql security definer set search_path='' as $$
declare r commerce.vodafone_requests%rowtype;
begin
    if p_admin is null or p_actor is null or p_actor<=0 then raise exception 'Invalid payment actor' using errcode='42501';end if;
    if p_admin then perform commerce.require_sheet_admin(p_actor);end if;
    select v.* into r from commerce.vodafone_requests v join commerce.users u on u.id=v.user_id
        where v.id=p_request_id and (p_admin or u.telegram_user_id=p_actor);
    if not found then raise exception 'Owned Vodafone request required' using errcode='42501';end if;
    return r;
end $$;
revoke all on function commerce.vodafone_access(uuid,bigint,boolean) from public,anon,authenticated,service_role;

create function commerce_rpc.vodafone_request(p_request_id uuid,p_actor_telegram_id bigint,p_admin boolean default false)
returns jsonb language plpgsql stable security definer set search_path='' as $$
declare r commerce.vodafone_requests%rowtype; owner_id bigint;messages jsonb;
begin
    r:=commerce.vodafone_access(p_request_id,p_actor_telegram_id,p_admin);
    select telegram_user_id into owner_id from commerce.users where id=r.user_id;
    select coalesce(jsonb_agg(to_jsonb(m) order by m.id),'[]') into messages from
        (select actor_role,body,created_at,id from commerce.vodafone_messages where request_id=r.id order by id desc limit 20) m;
    return to_jsonb(r)||jsonb_build_object('telegram_user_id',owner_id,'messages',messages);
end $$;

create function commerce_rpc.create_vodafone_request(p_request_id uuid,p_telegram_user_id bigint,p_amount_minor bigint)
returns jsonb language plpgsql security definer set search_path='' as $$
declare u uuid;r commerce.vodafone_requests%rowtype;
begin
    if p_request_id is null or p_amount_minor is null or p_amount_minor<=0 then raise exception 'Positive EGP amount required' using errcode='22023';end if;
    u:=(commerce_rpc.register_telegram_user(p_telegram_user_id,null)->>'user_id')::uuid;
    perform 1 from commerce.users where id=u and registration_state='active' for update;
    if not found then raise exception 'Active customer required' using errcode='42501';end if;
    insert into commerce.vodafone_requests(id,user_id,amount_minor) values(p_request_id,u,p_amount_minor) on conflict(id) do nothing;
    select * into r from commerce.vodafone_requests where id=p_request_id;
    if r.user_id<>u or r.amount_minor<>p_amount_minor then raise exception 'Conflicting Vodafone request replay' using errcode='22023';end if;
    return commerce_rpc.vodafone_request(r.id,p_telegram_user_id,false);
end $$;

create function commerce_rpc.submit_vodafone_request(p_request_id uuid,p_telegram_user_id bigint,p_sender_phone text,p_proof jsonb)
returns jsonb language plpgsql security definer set search_path='' as $$
declare r commerce.vodafone_requests%rowtype;kind text;
begin
    r:=commerce.vodafone_access(p_request_id,p_telegram_user_id,false);
    select * into r from commerce.vodafone_requests where id=r.id for update;
    kind:=p_proof->>'kind';
    if p_sender_phone is null or p_sender_phone !~ '^01[0125][0-9]{8}$'
       or jsonb_typeof(p_proof) is distinct from 'object' or octet_length(p_proof::text)>16000
       or kind is null or kind not in('photo','document','text')
       or p_proof->>'chat_id' is distinct from p_telegram_user_id::text
       or p_proof->>'sender_id' is distinct from p_telegram_user_id::text
       or coalesce(p_proof->>'message_id','') !~ '^[1-9][0-9]{0,15}$'
       or (kind in('photo','document') and (coalesce(length(p_proof->>'file_id'),0) not between 1 and 512 or coalesce(length(p_proof->>'file_unique_id'),0) not between 1 and 256))
       or (kind='text' and coalesce(length(btrim(p_proof->>'body')),0) not between 1 and 3000) then
        raise exception 'Sender phone and actual Telegram proof required' using errcode='22023';end if;
    if r.state<>'awaiting_evidence' then
        if r.sender_phone is distinct from p_sender_phone or r.proof is distinct from p_proof then raise exception 'Submitted evidence is immutable' using errcode='22023';end if;
        return commerce_rpc.vodafone_request(r.id,p_telegram_user_id,false);
    end if;
    update commerce.vodafone_requests set sender_phone=p_sender_phone,proof=p_proof,state='pending',submitted_at=now() where id=r.id;
    return commerce_rpc.vodafone_request(r.id,p_telegram_user_id,false);
end $$;

create function commerce_rpc.list_vodafone_requests(p_actor_telegram_id bigint,p_admin boolean default false,p_offset integer default 0)
returns jsonb language plpgsql stable security definer set search_path='' as $$
begin
    if p_admin is null or p_offset is null or p_offset<0 then raise exception 'Invalid request page' using errcode='22023';end if;
    if p_admin then perform commerce.require_sheet_admin(p_actor_telegram_id);end if;
    return (select coalesce(jsonb_agg(row_to_json(r)),'[]') from
        (select v.id,v.amount_minor,v.state,v.created_at,u.telegram_user_id from commerce.vodafone_requests v
         join commerce.users u on u.id=v.user_id where
         (p_admin and (v.state='pending' or exists(
             select 1 from commerce.vodafone_messages m where m.request_id=v.id and m.actor_role='customer'
             and not exists(select 1 from commerce.vodafone_messages reply where reply.request_id=v.id and reply.actor_role='admin' and reply.id>m.id))))
         or (not p_admin and u.telegram_user_id=p_actor_telegram_id)
         order by v.created_at desc,v.id limit 8 offset p_offset) r);
end $$;

create function commerce_rpc.review_vodafone_request(p_request_id uuid,p_actor_telegram_id bigint,p_action text,p_value text,p_review_reference text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare r commerce.vodafone_requests%rowtype;owner_id bigint;result jsonb;ref text;
begin
    perform commerce.require_sheet_admin(p_actor_telegram_id);
    if p_action is null or p_action not in('approve','reject') or p_review_reference is null or length(btrim(p_review_reference)) not between 1 and 1000
       or p_value is null or length(btrim(p_value)) not between 1 and 1000 then raise exception 'Explicit payment review required' using errcode='22023';end if;
    select * into r from commerce.vodafone_requests where id=p_request_id for update;
    if not found or r.state='awaiting_evidence' then raise exception 'Submitted Vodafone request required' using errcode='22023';end if;
    if p_action='approve' then
        ref:=upper(btrim(p_value));
        if ref !~ '^[A-Z0-9][A-Z0-9_.:-]{2,127}$' then raise exception 'Verified transaction reference required' using errcode='22023';end if;
        if r.state='approved' and r.transfer_reference=ref then return commerce_rpc.vodafone_request(r.id,p_actor_telegram_id,true);end if;
    elsif r.state='rejected' and r.rejection_reason=btrim(p_value) then return commerce_rpc.vodafone_request(r.id,p_actor_telegram_id,true);
    end if;
    if r.state<>'pending' then raise exception 'Payment request already reviewed' using errcode='22023';end if;
    if p_action='approve' then
        -- Different requests cannot allocate the same transaction, even concurrently.
        perform pg_advisory_xact_lock(hashtextextended('vodafone-transaction:'||ref,0));
        select telegram_user_id into owner_id from commerce.users where id=r.user_id;
        result:=commerce_rpc.confirm_receipt(owner_id,'vodafone:'||r.id,'vodafone_cash',ref,r.amount_minor::numeric,
            'admin-vodafone:'||r.id,'topup',null,jsonb_build_object('kind','manual_vodafone_cash','request_id',r.id,
            'actor_telegram_id',p_actor_telegram_id,'review_reference',p_review_reference,'receiver',r.receiver,'sender_phone',r.sender_phone));
        update commerce.vodafone_requests set state='approved',transfer_reference=ref,payment_id=(result->>'payment_id')::uuid,
            reviewer_id=p_actor_telegram_id,review_reference=p_review_reference,reviewed_at=now() where id=r.id;
    else
        update commerce.vodafone_requests set state='rejected',rejection_reason=btrim(p_value),reviewer_id=p_actor_telegram_id,
            review_reference=p_review_reference,reviewed_at=now() where id=r.id;
    end if;
    return commerce_rpc.vodafone_request(r.id,p_actor_telegram_id,true);
end $$;

create function commerce_rpc.vodafone_message(p_request_id uuid,p_actor_telegram_id bigint,p_admin boolean,p_message_id bigint,p_body text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare r commerce.vodafone_requests%rowtype;existing commerce.vodafone_messages%rowtype;
begin
    r:=commerce.vodafone_access(p_request_id,p_actor_telegram_id,p_admin);
    if p_message_id is null or p_message_id<=0 or p_body is null or length(btrim(p_body)) not between 1 and 3000 then
        raise exception 'Order complaint or reply required' using errcode='22023';end if;
    insert into commerce.vodafone_messages(request_id,actor_id,actor_role,source_message_id,body)
        values(r.id,p_actor_telegram_id,case when p_admin then 'admin' else 'customer' end,p_message_id,btrim(p_body))
        on conflict(request_id,actor_id,source_message_id) do nothing;
    select * into existing from commerce.vodafone_messages where request_id=r.id and actor_id=p_actor_telegram_id and source_message_id=p_message_id;
    if existing.body<>btrim(p_body) or existing.actor_role<>(case when p_admin then 'admin' else 'customer' end) then raise exception 'Conflicting payment message replay' using errcode='22023';end if;
    return commerce_rpc.vodafone_request(r.id,p_actor_telegram_id,p_admin);
end $$;

revoke all on function commerce_rpc.vodafone_request(uuid,bigint,boolean),commerce_rpc.create_vodafone_request(uuid,bigint,bigint),
    commerce_rpc.submit_vodafone_request(uuid,bigint,text,jsonb),commerce_rpc.list_vodafone_requests(bigint,boolean,integer),
    commerce_rpc.review_vodafone_request(uuid,bigint,text,text,text),commerce_rpc.vodafone_message(uuid,bigint,boolean,bigint,text)
    from public,anon,authenticated;
grant execute on function commerce_rpc.vodafone_request(uuid,bigint,boolean),commerce_rpc.create_vodafone_request(uuid,bigint,bigint),
    commerce_rpc.submit_vodafone_request(uuid,bigint,text,jsonb),commerce_rpc.list_vodafone_requests(bigint,boolean,integer),
    commerce_rpc.review_vodafone_request(uuid,bigint,text,text,text),commerce_rpc.vodafone_message(uuid,bigint,boolean,bigint,text) to service_role;
notify pgrst,'reload schema';
commit;
