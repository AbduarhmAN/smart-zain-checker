-- A20-A22: verified receipts and reservations are transactional, backend-only.
begin;
alter table commerce.orders add column refund_consent_reference text;

create function commerce.post_pair(p_key text,p_kind text,p_order uuid,p_from text,p_to text,
    p_amount bigint,p_from_wallet uuid default null,p_to_wallet uuid default null)
returns uuid language plpgsql set search_path = '' as $$
declare j uuid; fingerprint text; old_fingerprint text;
begin
    if p_amount is null or p_amount <= 0 then raise exception 'Positive journal amount required' using errcode = '22023'; end if;
    fingerprint := md5(jsonb_build_object('kind',p_kind,'order',p_order,'from',p_from,'to',p_to,
        'amount',p_amount,'from_wallet',p_from_wallet,'to_wallet',p_to_wallet)::text);
    insert into commerce.journals(idempotency_key,kind,order_id,payload_fingerprint)
    values (p_key,p_kind,p_order,fingerprint) on conflict (idempotency_key) do nothing returning id into j;
    if not found then
        select id,payload_fingerprint into j,old_fingerprint from commerce.journals where idempotency_key = p_key;
        if old_fingerprint <> fingerprint then raise exception 'Conflicting journal replay' using errcode = '22023'; end if;
        return j;
    end if;
    insert into commerce.ledger_entries(journal_id,line_number,account_key,wallet_id,delta_minor)
    values (j,1,p_from,p_from_wallet,-p_amount),(j,2,p_to,p_to_wallet,p_amount);
    return j;
end $$;
revoke all on function commerce.post_pair(text,text,uuid,text,text,bigint,uuid,uuid) from public,anon,authenticated,service_role;

create function commerce_rpc.confirm_receipt(p_telegram_user_id bigint,p_key text,p_provider text,
    p_reference text,p_amount_minor bigint,p_proof text,p_purpose text default 'topup',
    p_order_id uuid default null,p_fx_snapshot jsonb default '{}')
returns jsonb language plpgsql security definer set search_path = '' as $$
declare u uuid; p commerce.payments%rowtype; w commerce.wallets%rowtype; o commerce.orders%rowtype;
begin
    if p_key is null or length(p_key) not between 1 and 160 or p_provider is null
       or length(p_provider) not between 1 and 80 or p_reference is null or length(p_reference) not between 1 and 256
       or p_amount_minor is null or p_amount_minor <= 0 or p_proof is null or length(p_proof) not between 1 and 1000
       or p_purpose is null or p_purpose not in ('topup','direct_order')
       or ((p_purpose = 'direct_order') <> (p_order_id is not null))
       or jsonb_typeof(p_fx_snapshot) is distinct from 'object' then
        raise exception 'Invalid verified payment receipt' using errcode = '22023';
    end if;
    u := (commerce_rpc.register_telegram_user(p_telegram_user_id,null)->>'user_id')::uuid;
    perform 1 from commerce.users where id = u and registration_state = 'active' for update;
    if not found then raise exception 'Account suspended' using errcode = '22023'; end if;
    select * into p from commerce.payments where idempotency_key = p_key;
    if found then
        if p.user_id <> u or p.provider <> p_provider or p.provider_reference <> p_reference
           or p.amount_minor <> p_amount_minor or p.purpose <> p_purpose
           or p.order_id is distinct from p_order_id or p.fx_snapshot is distinct from p_fx_snapshot
           or p.proof_reference <> p_proof then
            raise exception 'Conflicting payment replay' using errcode = '22023';
        end if;
        return jsonb_build_object('payment_id',p.id,'state',p.state,'replayed',true);
    end if;
    if p_purpose = 'direct_order' then
        select * into o from commerce.orders where id = p_order_id and user_id = u for update;
        if not found or o.order_state <> 'draft' or o.payment_state <> 'unpaid' or o.quoted_minor <> p_amount_minor then
            raise exception 'Direct payment must match an unpaid owned order' using errcode = '22023';
        end if;
        if exists (select 1 from commerce.payments where order_id = p_order_id and state <> 'rejected') then
            raise exception 'Order already has a direct payment' using errcode = '22023';
        end if;
    end if;
    if exists (select 1 from commerce.payments where provider = p_provider and provider_reference = p_reference) then
        raise exception 'Provider receipt already assigned' using errcode = '22023';
    end if;
    insert into commerce.payments(user_id,order_id,purpose,provider,provider_reference,idempotency_key,
        amount_minor,state,proof_reference,verified_at,fx_snapshot)
    values (u,p_order_id,p_purpose,p_provider,p_reference,p_key,p_amount_minor,'verified',p_proof,now(),p_fx_snapshot)
    returning * into p;
    if p_purpose = 'topup' then
        select * into w from commerce.wallets where user_id = u and kind = 'customer' and currency = 'EGP' for update;
        perform commerce.post_pair('payment:'||p.id,'topup',null,'external:'||p_provider,
            'wallet:'||w.id||':available',p_amount_minor,null,w.id);
        update commerce.wallets set available_minor = available_minor + p_amount_minor where id = w.id;
    else
        perform commerce.post_pair('payment:'||p.id,'reserve',p_order_id,'external:'||p_provider,
            'direct:'||p_order_id||':received',p_amount_minor);
    end if;
    return jsonb_build_object('payment_id',p.id,'state',p.state,'replayed',false);
end $$;

create function commerce_rpc.reserve_order(p_order_id uuid,p_telegram_user_id bigint,
    p_consent_reference text,p_payment_id uuid default null)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare u uuid; o commerce.orders%rowtype; w commerce.wallets%rowtype; p commerce.payments%rowtype;
        r commerce.reservations%rowtype; v_total numeric;
begin
    if p_consent_reference is null or length(p_consent_reference) not between 1 and 1000 then
        raise exception 'Refund policy consent is required' using errcode = '22023';
    end if;
    select id into u from commerce.users where telegram_user_id = p_telegram_user_id and registration_state = 'active' for update;
    if not found then raise exception 'Unknown or suspended user' using errcode = '22023'; end if;
    select * into o from commerce.orders where id = p_order_id and user_id = u for update;
    if not found then raise exception 'Unknown or unowned order' using errcode = '22023'; end if;
    select * into r from commerce.reservations where order_id = p_order_id;
    if found then
        if r.payment_id is distinct from p_payment_id then raise exception 'Reservation funding differs' using errcode = '22023'; end if;
        return jsonb_build_object('reservation_id',r.id,'state',r.state,'replayed',true);
    end if;
    if o.order_state <> 'draft' or o.payment_state <> 'unpaid' then
        raise exception 'Order is not available for reservation' using errcode = '22023';
    end if;
    if o.pricing_snapshot->>'refund_destination' is distinct from 'wallet'
       or o.pricing_snapshot->>'refund_requires_consent' is distinct from 'true' then
        raise exception 'Unsupported refund policy for this funding path' using errcode = '22023';
    end if;
    select sum(unit_quote_minor::numeric-discount_minor) into v_total from commerce.checks where order_id = o.id;
    if v_total is distinct from o.quoted_minor::numeric then raise exception 'Quote and row charges differ' using errcode = '22023'; end if;
    if p_payment_id is null then
        if exists (select 1 from commerce.payments where order_id = o.id and state <> 'rejected') then
            raise exception 'Direct-funded order cannot also debit wallet' using errcode = '22023';
        end if;
        select * into w from commerce.wallets where user_id = u and kind = 'customer' and currency = 'EGP' for update;
        if w.available_minor < o.quoted_minor then return jsonb_build_object('state','insufficient_balance','available_minor',w.available_minor); end if;
        if o.quoted_minor > 0 then
            perform commerce.post_pair('reserve:'||o.id,'reserve',o.id,'wallet:'||w.id||':available',
                'wallet:'||w.id||':reserved',o.quoted_minor,w.id,w.id);
        end if;
        update commerce.wallets set available_minor = available_minor-o.quoted_minor,
            reserved_minor = reserved_minor+o.quoted_minor where id = w.id;
        insert into commerce.reservations(order_id,wallet_id,amount_minor,idempotency_key)
        values (o.id,w.id,o.quoted_minor,'reserve:'||o.id) returning * into r;
    else
        select * into p from commerce.payments where id = p_payment_id and order_id = o.id and user_id = u for update;
        if not found or p.state <> 'verified' or p.amount_minor <> o.quoted_minor or p.purpose <> 'direct_order' then
            raise exception 'Unverified or mismatched direct payment' using errcode = '22023';
        end if;
        perform commerce.post_pair('reserve:'||o.id,'reserve',o.id,'direct:'||o.id||':received',
            'direct:'||o.id||':reserved',o.quoted_minor);
        insert into commerce.reservations(order_id,payment_id,amount_minor,idempotency_key)
        values (o.id,p.id,o.quoted_minor,'reserve:'||o.id) returning * into r;
        update commerce.payments set state = 'allocated' where id = p.id;
    end if;
    update commerce.orders set order_state = 'confirmed',payment_state = 'reserved',
        refund_consent_reference = p_consent_reference where id = o.id;
    return jsonb_build_object('reservation_id',r.id,'state','held','replayed',false);
end $$;

create function commerce_rpc.wallet_state(p_telegram_user_id bigint)
returns jsonb language sql stable security definer set search_path = '' as $$
    select jsonb_build_object('user_id',u.id,'currency','EGP','customer',
        coalesce((select jsonb_build_object('available_minor',w.available_minor,'reserved_minor',w.reserved_minor)
            from commerce.wallets w where w.user_id = u.id and w.kind = 'customer'),'{}'::jsonb),
        'partner',coalesce((select jsonb_build_object('available_minor',w.available_minor,'reserved_minor',w.reserved_minor,
            'withdrawn_minor',w.withdrawn_minor) from commerce.wallets w where w.user_id = u.id and w.kind = 'partner'),'{}'::jsonb))
    from commerce.users u where u.telegram_user_id = p_telegram_user_id
$$;
revoke all on function commerce_rpc.confirm_receipt(bigint,text,text,text,bigint,text,text,uuid,jsonb),
    commerce_rpc.reserve_order(uuid,bigint,text,uuid),commerce_rpc.wallet_state(bigint) from public,anon,authenticated;
grant execute on function commerce_rpc.confirm_receipt(bigint,text,text,text,bigint,text,text,uuid,jsonb),
    commerce_rpc.reserve_order(uuid,bigint,text,uuid),commerce_rpc.wallet_state(bigint) to service_role;
commit;
