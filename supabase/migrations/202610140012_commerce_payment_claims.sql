begin;
alter table commerce.payment_requests add column route_snapshot jsonb not null default '{}'
    check (jsonb_typeof(route_snapshot)='object');
create table commerce.payment_claims (
    id uuid primary key default gen_random_uuid(),
    quote_id uuid not null references commerce.payment_requests(id),
    user_id uuid not null references commerce.users(id),
    reference text not null check(length(btrim(reference)) between 1 and 256),
    state text not null default 'pending' check(state in ('pending','approved','rejected')),
    actor_telegram_id bigint check(actor_telegram_id>0),
    decision_reference text,
    payment_id uuid references commerce.payments(id),
    created_at timestamptz not null default now(),
    decided_at timestamptz,
    unique(quote_id,reference),
    check((state='pending')=(decided_at is null)),
    check((state='pending')=(actor_telegram_id is null)),
    check((state='approved')=(payment_id is not null))
);
create unique index one_open_payment_claim on commerce.payment_claims(quote_id) where state='pending';
alter table commerce.payment_claims enable row level security;
revoke all on commerce.payment_claims from public,anon,authenticated,service_role;

alter function commerce_rpc.create_payment_request(jsonb,text,text) set schema commerce;
revoke all on function commerce.create_payment_request(jsonb,text,text) from public,anon,authenticated,service_role;
create function commerce_rpc.create_payment_request(p_quote jsonb,p_provider text,p_receiver text,p_route_snapshot jsonb default '{}')
returns jsonb language plpgsql security definer set search_path='' as $$
declare result jsonb; r commerce.payment_requests%rowtype;
begin
    if jsonb_typeof(p_route_snapshot) is distinct from 'object' then
        raise exception 'Provider route snapshot required' using errcode='22023'; end if;
    if p_provider='bsc' and (coalesce(p_route_snapshot->>'chain_id','')<>'56'
       or coalesce(p_route_snapshot->>'contract','') !~ '^0x[0-9a-f]{40}$'
       or coalesce(p_route_snapshot->>'decimals','') !~ '^(0|[1-9]|[12][0-9]|3[0-6])$'
       or p_route_snapshot->>'asset' is distinct from p_quote->>'asset') then
        raise exception 'BSC requires an explicit approved contract and decimals' using errcode='22023'; end if;
    result:=commerce.create_payment_request(p_quote,p_provider,p_receiver);
    select * into r from commerce.payment_requests where id=(result->>'quote_id')::uuid for update;
    if (result->>'replayed')::boolean then
        if r.route_snapshot<>p_route_snapshot then
            raise exception 'Conflicting provider route replay' using errcode='22023'; end if;
    else
        update commerce.payment_requests set route_snapshot=p_route_snapshot where id=r.id;
    end if;
    return result;
end $$;

create or replace function commerce_rpc.payment_request(p_quote_id uuid,p_telegram_user_id bigint)
returns jsonb language plpgsql security definer set search_path='' as $$
declare r commerce.payment_requests%rowtype;
begin
    select pr.* into r from commerce.payment_requests pr join commerce.users u on u.id=pr.user_id
        where pr.id=p_quote_id and u.telegram_user_id=p_telegram_user_id and u.registration_state='active';
    if not found then raise exception 'Unknown or unowned payment request' using errcode='22023'; end if;
    return jsonb_build_object('quote',r.quote,'provider',r.provider,'receiver',r.receiver,'route_snapshot',r.route_snapshot,
        'invoice_state',r.invoice_state,'invoice_id',r.invoice_id,'payment_id',r.payment_id);
end $$;

create function commerce_rpc.submit_payment_claim(p_quote_id uuid,p_telegram_user_id bigint,p_reference text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare u uuid; q commerce.payment_requests%rowtype; c commerce.payment_claims%rowtype;
begin
    select id into u from commerce.users where telegram_user_id=p_telegram_user_id and registration_state='active' for update;
    if not found or p_reference is null or length(btrim(p_reference)) not between 1 and 256 then
        raise exception 'Active customer and transfer reference required' using errcode='22023'; end if;
    select * into q from commerce.payment_requests where id=p_quote_id and user_id=u for update;
    if not found or q.provider='cryptopay' then
        raise exception 'Owned direct-transfer request required' using errcode='22023'; end if;
    if q.provider='bsc' and btrim(p_reference) !~ '^0x[0-9a-f]{64}$' then
        raise exception 'Canonical BSC transfer hash required' using errcode='22023'; end if;
    select * into c from commerce.payment_claims where quote_id=q.id and reference=btrim(p_reference);
    if found then return jsonb_build_object('claim_id',c.id,'state',c.state,'replayed',true); end if;
    if q.payment_id is not null or exists(select 1 from commerce.payment_claims where quote_id=q.id and state='pending') then
        raise exception 'Request already paid or awaiting review' using errcode='22023'; end if;
    if (select count(*) from commerce.payment_claims where user_id=u and state='pending')>=5 then
        raise exception 'Open payment review limit reached' using errcode='22023'; end if;
    insert into commerce.payment_claims(quote_id,user_id,reference) values(q.id,u,btrim(p_reference)) returning * into c;
    return jsonb_build_object('claim_id',c.id,'state',c.state,'replayed',false);
end $$;

create function commerce_rpc.payment_claim(p_claim_id uuid)
returns jsonb language plpgsql stable security definer set search_path='' as $$
declare result jsonb;
begin
    select jsonb_build_object('claim_id',c.id,'quote_id',c.quote_id,'telegram_user_id',u.telegram_user_id,
        'username',u.telegram_username,'reference',c.reference,'state',c.state,'payment_id',c.payment_id,
        'provider',q.provider,'quote',q.quote,'receiver',q.receiver,'route_snapshot',q.route_snapshot,
        'created_at',c.created_at,'actor_telegram_id',c.actor_telegram_id,'decision_reference',c.decision_reference)
    into result from commerce.payment_claims c join commerce.payment_requests q on q.id=c.quote_id
        join commerce.users u on u.id=c.user_id where c.id=p_claim_id;
    if result is null then raise exception 'Payment claim unavailable' using errcode='22023'; end if;
    return result;
end $$;

create function commerce_rpc.list_payment_claims(p_limit integer default 10,p_offset integer default 0)
returns jsonb language plpgsql stable security definer set search_path='' as $$
begin
    if p_limit is null or p_limit not between 1 and 50 or p_offset is null or p_offset not between 0 and 1000000 then
        raise exception 'Invalid payment review page' using errcode='22023'; end if;
    return coalesce((select jsonb_agg(commerce_rpc.payment_claim(c.id)) from
        (select id from commerce.payment_claims where state='pending' order by created_at,id limit p_limit offset p_offset) c),'[]'::jsonb);
end $$;

create function commerce_rpc.approve_payment_claim(p_claim_id uuid,p_actor_telegram_id bigint,p_evidence jsonb)
returns jsonb language plpgsql security definer set search_path='' as $$
declare c commerce.payment_claims%rowtype; q commerce.payment_requests%rowtype; u uuid; tid bigint; proof jsonb; result jsonb;
begin
    select user_id into u from commerce.payment_claims where id=p_claim_id;
    select telegram_user_id into tid from commerce.users where id=u and registration_state='active' for update;
    if not found or p_actor_telegram_id is null or p_actor_telegram_id<=0 then
        raise exception 'Active owner and authenticated admin required' using errcode='22023'; end if;
    select * into c from commerce.payment_claims where id=p_claim_id for update;
    if c.state='approved' then
        return jsonb_build_object('payment_id',c.payment_id,'telegram_user_id',tid,'replayed',true); end if;
    if c.state<>'pending' or jsonb_typeof(p_evidence) is distinct from 'object' then
        raise exception 'Pending payment claim required' using errcode='22023'; end if;
    select * into q from commerce.payment_requests where id=c.quote_id;
    proof:=(p_evidence->>'proof_json')::jsonb;
    if jsonb_typeof(proof) is distinct from 'object' then
        raise exception 'Provider proof required before customer assignment' using errcode='22023'; end if;
    if q.provider='binance_pay' and (c.reference is distinct from p_evidence->>'reference'
        and c.reference is distinct from proof->>'order_id') then
        raise exception 'Provider transfer does not match the submitted reference' using errcode='22023'; end if;
    if q.provider='bsc' then
        if proof->>'contract' is distinct from q.route_snapshot->>'contract'
           or proof->>'decimals' is distinct from q.route_snapshot->>'decimals'
           or proof->>'chain_id' is distinct from '56'
           or proof->>'tx_hash' is distinct from c.reference
           or p_evidence->>'reference' is distinct from '56:'||c.reference then
            raise exception 'BSC proof differs from frozen contract' using errcode='22023'; end if;
    end if;
    result:=commerce_rpc.confirm_payment_request(q.id,tid,p_evidence,jsonb_build_object('kind','admin',
        'actor_id',p_actor_telegram_id,'approval_reference','telegram-payment-claim:'||c.id));
    update commerce.payment_claims set state='approved',actor_telegram_id=p_actor_telegram_id,
        decision_reference='telegram-payment-claim:'||c.id,payment_id=(result->>'payment_id')::uuid,decided_at=now() where id=c.id;
    return result||jsonb_build_object('telegram_user_id',tid);
end $$;

create function commerce_rpc.reject_payment_claim(p_claim_id uuid,p_actor_telegram_id bigint,p_reference text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare c commerce.payment_claims%rowtype; u uuid;
begin
    select user_id into u from commerce.payment_claims where id=p_claim_id;
    perform 1 from commerce.users where id=u for update;
    if not found or p_actor_telegram_id is null or p_actor_telegram_id<=0 or p_reference is null
       or length(btrim(p_reference)) not between 1 and 1000 then
        raise exception 'Authenticated review decision required' using errcode='22023'; end if;
    select * into c from commerce.payment_claims where id=p_claim_id for update;
    if c.state='approved' then raise exception 'Paid claim cannot be rejected' using errcode='22023'; end if;
    if c.state='rejected' then return jsonb_build_object('state','rejected','replayed',true); end if;
    update commerce.payment_claims set state='rejected',actor_telegram_id=p_actor_telegram_id,
        decision_reference=p_reference,decided_at=now() where id=c.id;
    return jsonb_build_object('state','rejected','replayed',false);
end $$;

create or replace function commerce_rpc.health()
returns jsonb language sql stable security definer set search_path='' as $$
    select jsonb_build_object('status','ok','schema_version',12,'currency','EGP')
$$;
revoke all on function commerce_rpc.create_payment_request(jsonb,text,text,jsonb),
    commerce_rpc.submit_payment_claim(uuid,bigint,text),commerce_rpc.payment_claim(uuid),
    commerce_rpc.list_payment_claims(integer,integer),commerce_rpc.approve_payment_claim(uuid,bigint,jsonb),
    commerce_rpc.reject_payment_claim(uuid,bigint,text) from public,anon,authenticated;
grant execute on function commerce_rpc.create_payment_request(jsonb,text,text,jsonb),
    commerce_rpc.submit_payment_claim(uuid,bigint,text),commerce_rpc.payment_claim(uuid),
    commerce_rpc.list_payment_claims(integer,integer),commerce_rpc.approve_payment_claim(uuid,bigint,jsonb),
    commerce_rpc.reject_payment_claim(uuid,bigint,text) to service_role;
notify pgrst,'reload schema';
commit;
