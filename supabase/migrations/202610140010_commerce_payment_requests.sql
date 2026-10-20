-- A20-A22: frozen provider quotes, durable invoice attempts and customer binding.
begin;
create table commerce.payment_requests (
    id uuid primary key,
    user_id uuid not null references commerce.users(id),
    order_id uuid references commerce.orders(id),
    provider text not null check (provider in ('cryptopay','binance_pay','bsc')),
    receiver text not null check (length(receiver) between 1 and 256),
    quote jsonb not null check (jsonb_typeof(quote) = 'object'),
    invoice_state text not null default 'none' check (invoice_state in ('none','started','attached')),
    invoice_id bigint check (invoice_id > 0),
    payment_id uuid unique references commerce.payments(id),
    evidence jsonb,
    binding jsonb,
    created_at timestamptz not null default now(),
    confirmed_at timestamptz,
    unique (provider,receiver,invoice_id),
    check ((invoice_state = 'attached') = (invoice_id is not null)),
    check (provider = 'cryptopay' or invoice_state = 'none'),
    check ((payment_id is null) = (evidence is null)),
    check ((payment_id is null) = (binding is null)),
    check ((payment_id is null) = (confirmed_at is null))
);
alter table commerce.payment_requests enable row level security;
revoke all on commerce.payment_requests from public,anon,authenticated,service_role;

create function commerce_rpc.create_payment_request(p_quote jsonb,p_provider text,p_receiver text)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare u uuid; q uuid; o uuid; minor bigint; rate numeric; amount numeric; prec integer;
    started timestamptz; expires timestamptz; r commerce.payment_requests%rowtype;
begin
    if jsonb_typeof(p_quote) is distinct from 'object'
       or p_provider is null or p_provider not in ('cryptopay','binance_pay','bsc')
       or p_receiver is null or length(btrim(p_receiver)) not between 1 and 256
       or jsonb_typeof(p_quote->'user_id') is distinct from 'number'
       or (p_quote->>'user_id') !~ '^[1-9][0-9]*$'
       or jsonb_typeof(p_quote->'amount_minor') is distinct from 'number'
       or (p_quote->>'amount_minor') !~ '^[1-9][0-9]*$'
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
    minor := (p_quote->>'amount_minor')::bigint;
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
end $$;

create function commerce_rpc.payment_request(p_quote_id uuid,p_telegram_user_id bigint)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare r commerce.payment_requests%rowtype;
begin
    select pr.* into r from commerce.payment_requests pr join commerce.users u on u.id=pr.user_id
        where pr.id=p_quote_id and u.telegram_user_id=p_telegram_user_id and u.registration_state='active';
    if not found then raise exception 'Unknown or unowned payment request' using errcode='22023'; end if;
    return jsonb_build_object('quote',r.quote,'provider',r.provider,'receiver',r.receiver,
        'invoice_state',r.invoice_state,'invoice_id',r.invoice_id,'payment_id',r.payment_id);
end $$;

create function commerce_rpc.begin_payment_invoice(p_quote_id uuid,p_telegram_user_id bigint)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare r commerce.payment_requests%rowtype;
begin
    select pr.* into r from commerce.payment_requests pr join commerce.users u on u.id=pr.user_id
        where pr.id=p_quote_id and u.telegram_user_id=p_telegram_user_id and u.registration_state='active'
        for update of pr;
    if not found or r.provider<>'cryptopay' then
        raise exception 'Unknown or unowned invoice request' using errcode='22023'; end if;
    if r.invoice_state<>'none' then return jsonb_build_object('state',r.invoice_state); end if;
    if now() > (r.quote->>'expires_at')::timestamptz or r.payment_id is not null then
        raise exception 'Invoice request is no longer payable' using errcode='22023'; end if;
    update commerce.payment_requests set invoice_state='started' where id=r.id;
    return jsonb_build_object('state','claimed');
end $$;

create function commerce_rpc.attach_payment_invoice(p_quote_id uuid,p_telegram_user_id bigint,p_invoice_id bigint)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare r commerce.payment_requests%rowtype;
begin
    select pr.* into r from commerce.payment_requests pr join commerce.users u on u.id=pr.user_id
        where pr.id=p_quote_id and u.telegram_user_id=p_telegram_user_id and u.registration_state='active'
        for update of pr;
    if not found or r.provider<>'cryptopay' or p_invoice_id is null or p_invoice_id<=0 then
        raise exception 'Unknown or invalid invoice assignment' using errcode='22023'; end if;
    if r.invoice_id is not null then
        if r.invoice_id<>p_invoice_id then raise exception 'Invoice already bound' using errcode='22023'; end if;
        return jsonb_build_object('invoice_id',r.invoice_id,'replayed',true);
    end if;
    if r.invoice_state<>'started' then raise exception 'Invoice attempt was not started' using errcode='22023'; end if;
    update commerce.payment_requests set invoice_state='attached',invoice_id=p_invoice_id where id=r.id;
    return jsonb_build_object('invoice_id',p_invoice_id,'replayed',false);
end $$;

create function commerce_rpc.confirm_payment_request(p_quote_id uuid,p_telegram_user_id bigint,p_evidence jsonb,p_binding jsonb)
returns jsonb language plpgsql security definer set search_path = '' as $$
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
        (r.quote->>'amount_minor')::bigint,'payment-request:'||r.id,
        case when r.order_id is null then 'topup' else 'direct_order' end,r.order_id,
        jsonb_build_object('quote',r.quote,'evidence',p_evidence,'binding',p_binding));
    update commerce.payment_requests set payment_id=(result->>'payment_id')::uuid,
        evidence=p_evidence,binding=p_binding,confirmed_at=now() where id=r.id;
    return result;
end $$;

revoke all on function commerce_rpc.create_payment_request(jsonb,text,text),
    commerce_rpc.payment_request(uuid,bigint),commerce_rpc.begin_payment_invoice(uuid,bigint),
    commerce_rpc.attach_payment_invoice(uuid,bigint,bigint),
    commerce_rpc.confirm_payment_request(uuid,bigint,jsonb,jsonb) from public,anon,authenticated;
grant execute on function commerce_rpc.create_payment_request(jsonb,text,text),
    commerce_rpc.payment_request(uuid,bigint),commerce_rpc.begin_payment_invoice(uuid,bigint),
    commerce_rpc.attach_payment_invoice(uuid,bigint,bigint),
    commerce_rpc.confirm_payment_request(uuid,bigint,jsonb,jsonb) to service_role;
notify pgrst,'reload schema';
commit;
