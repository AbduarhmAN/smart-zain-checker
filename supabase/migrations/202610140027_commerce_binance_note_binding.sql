-- A private pre-payment challenge can bind an ordinary Binance Pay C2C transfer.
begin;
create table commerce.binance_note_challenges (
    quote_id uuid primary key references commerce.payment_requests(id),
    challenge text not null unique check(challenge ~ '^ZC[a-f0-9]{32}$'),
    payer_uid text not null check(payer_uid ~ '^[1-9][0-9]{0,31}$'),
    issued_at timestamptz not null default now()
);
alter table commerce.binance_note_challenges enable row level security;
revoke all on commerce.binance_note_challenges from public,anon,authenticated,service_role;

create function commerce_rpc.issue_binance_note(p_quote_id uuid,p_telegram_user_id bigint,p_payer_uid text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare r commerce.payment_requests%rowtype;c commerce.binance_note_challenges%rowtype;
begin
    perform 1 from commerce.users where telegram_user_id=p_telegram_user_id and registration_state='active' for update;
    if not found then raise exception 'Active customer required' using errcode='22023';end if;
    select pr.* into r from commerce.payment_requests pr join commerce.users u on u.id=pr.user_id
        where pr.id=p_quote_id and u.telegram_user_id=p_telegram_user_id for update of pr;
    if not found or r.provider<>'binance_pay' or p_payer_uid is null or p_payer_uid !~ '^[1-9][0-9]{0,31}$' then
        raise exception 'Owned Binance quote and canonical payer UID required' using errcode='22023';end if;
    select * into c from commerce.binance_note_challenges where quote_id=r.id;
    if found then
        if c.payer_uid<>p_payer_uid then raise exception 'Payment challenge payer is immutable' using errcode='22023';end if;
        return jsonb_build_object('quote_id',r.id,'challenge',c.challenge,'payer_uid',c.payer_uid,'issued_at',c.issued_at,'expires_at',r.quote->>'expires_at','replayed',true);
    end if;
    if r.payment_id is not null or now()<(r.quote->>'observed_at')::timestamptz or now()>=(r.quote->>'expires_at')::timestamptz then
        raise exception 'Payment challenge requires an unpaid current quote' using errcode='22023';end if;
    insert into commerce.binance_note_challenges(quote_id,challenge,payer_uid)
        values(r.id,'ZC'||replace(gen_random_uuid()::text,'-',''),p_payer_uid) returning * into c;
    return jsonb_build_object('quote_id',r.id,'challenge',c.challenge,'payer_uid',c.payer_uid,'issued_at',c.issued_at,'expires_at',r.quote->>'expires_at','replayed',false);
end $$;

create function commerce_rpc.binance_note(p_quote_id uuid,p_telegram_user_id bigint)
returns jsonb language plpgsql stable security definer set search_path='' as $$
declare result jsonb;
begin
    select jsonb_build_object('quote_id',r.id,'challenge',c.challenge,'payer_uid',c.payer_uid,'issued_at',c.issued_at,
        'expires_at',r.quote->>'expires_at') into result from commerce.binance_note_challenges c
        join commerce.payment_requests r on r.id=c.quote_id join commerce.users u on u.id=r.user_id
        where c.quote_id=p_quote_id and u.telegram_user_id=p_telegram_user_id and u.registration_state='active';
    if not found then raise exception 'Unknown or unowned private challenge' using errcode='22023';end if;
    return result;
end $$;

create function commerce_rpc.pending_binance_notes(p_after uuid default null,p_limit integer default 20)
returns jsonb language plpgsql stable security definer set search_path='' as $$
declare result jsonb;
begin
    if p_limit is null or p_limit not between 1 and 100 then raise exception 'Bounded private challenge page required' using errcode='22023';end if;
    select coalesce(jsonb_agg(jsonb_build_object('quote_id',quote_id,'telegram_user_id',telegram_user_id,
        'challenge',challenge,'payer_uid',payer_uid,'issued_at',issued_at) order by quote_id),'[]') into result
        from (select c.*,u.telegram_user_id from commerce.binance_note_challenges c join commerce.payment_requests r on r.id=c.quote_id
              join commerce.users u on u.id=r.user_id and u.registration_state='active'
              where r.payment_id is null and (p_after is null or c.quote_id>p_after) order by c.quote_id limit p_limit) page;
    return result;
end $$;

create function commerce_rpc.confirm_binance_note(p_quote_id uuid,p_evidence jsonb)
returns jsonb language plpgsql security definer set search_path='' as $$
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
        (r.quote->>'amount_minor')::bigint,'payment-note:'||r.id,case when r.order_id is null then 'topup' else 'direct_order' end,
        r.order_id,jsonb_build_object('quote',r.quote,'evidence',p_evidence,'binding',v_binding));
    update commerce.payment_requests set payment_id=(result->>'payment_id')::uuid,evidence=p_evidence,binding=v_binding,confirmed_at=now() where id=r.id;
    return result;
end $$;

revoke all on function commerce_rpc.issue_binance_note(uuid,bigint,text),commerce_rpc.binance_note(uuid,bigint),
    commerce_rpc.pending_binance_notes(uuid,integer),commerce_rpc.confirm_binance_note(uuid,jsonb) from public,anon,authenticated;
grant execute on function commerce_rpc.issue_binance_note(uuid,bigint,text),commerce_rpc.binance_note(uuid,bigint),
    commerce_rpc.pending_binance_notes(uuid,integer),commerce_rpc.confirm_binance_note(uuid,jsonb) to service_role;
create or replace function commerce_rpc.health()
returns jsonb language sql stable security definer set search_path='' as $$
    select jsonb_build_object('status','ok','schema_version',27,'currency','EGP')
$$;
notify pgrst,'reload schema';
commit;
