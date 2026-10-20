begin;
create table commerce.cash_receipts(
    reference text primary key check(reference=upper(btrim(reference)) and reference ~ '^[A-Z0-9][A-Z0-9_.:-]{2,127}$'),
    allocation_kind text not null check(allocation_kind in ('direct_topup','cash_credit_code')),
    amount_minor bigint not null check(amount_minor>0),
    actor_telegram_id bigint not null check(actor_telegram_id>0),
    approval_reference text not null check(length(btrim(approval_reference)) between 1 and 1000),
    user_id uuid references commerce.users(id),
    code_id uuid unique references commerce.codes(id),
    payment_id uuid unique references commerce.payments(id),
    journal_id uuid unique references commerce.journals(id),
    created_at timestamptz not null default now(),
    check((allocation_kind='direct_topup' and user_id is not null and code_id is null)
        or (allocation_kind='cash_credit_code' and code_id is not null and user_id is null and payment_id is null))
);
alter table commerce.cash_receipts enable row level security;
revoke all on commerce.cash_receipts from public,anon,authenticated,service_role;
alter table commerce.codes add column cash_bound_user_id uuid references commerce.users(id);
alter table commerce.codes add column cash_funded_minor bigint check(cash_funded_minor>0);
alter table commerce.codes add column cash_redeemed_minor bigint not null default 0 check(cash_redeemed_minor>=0);
alter table commerce.codes add constraint cash_code_budget check(cash_funded_minor is null or cash_redeemed_minor<=cash_funded_minor);
-- Historical direct cash receipts reserve the same canonical identity domain.
-- Unexpected historical data requires human reconciliation, not a guessed actor.
do $$ begin
    if exists(select 1 from commerce.payments where provider='manual_cash' and
        (purpose<>'topup' or state not in ('verified','allocated') or provider_reference<>upper(btrim(provider_reference))
        or provider_reference !~ '^[A-Z0-9][A-Z0-9_.:-]{2,127}$'
        or coalesce(fx_snapshot->>'actor_telegram_id','') !~ '^[1-9][0-9]{0,18}$'
        or coalesce(fx_snapshot->>'approval_reference','')='')) then
        raise exception 'Historical cash receipts require reconciliation before code issuance'; end if;
end $$;
insert into commerce.cash_receipts(reference,allocation_kind,amount_minor,actor_telegram_id,approval_reference,user_id,payment_id)
select provider_reference,'direct_topup',amount_minor,(fx_snapshot->>'actor_telegram_id')::bigint,
    fx_snapshot->>'approval_reference',user_id,id from commerce.payments where provider='manual_cash';

create or replace function commerce_rpc.admin_cash_topup(p_telegram_user_id bigint,p_amount_minor bigint,
    p_receipt_reference text,p_actor_telegram_id bigint,p_approval_reference text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare u uuid;ref text;existing commerce.cash_receipts%rowtype;result jsonb;
begin
    if p_actor_telegram_id is null or p_actor_telegram_id<=0 or p_amount_minor is null or p_amount_minor<=0
       or p_receipt_reference is null or upper(btrim(p_receipt_reference)) !~ '^[A-Z0-9][A-Z0-9_.:-]{2,127}$'
       or p_approval_reference is null or length(btrim(p_approval_reference)) not between 1 and 1000 then
        raise exception 'Authenticated cash receipt required' using errcode='22023'; end if;
    ref:=upper(btrim(p_receipt_reference));
    perform pg_advisory_xact_lock(hashtextextended('cash-receipt:'||ref,0));
    u:=(commerce_rpc.register_telegram_user(p_telegram_user_id,null)->>'user_id')::uuid;
    perform 1 from commerce.users where id=u and registration_state='active' for update;
    if not found then raise exception 'Customer suspended' using errcode='22023'; end if;
    select * into existing from commerce.cash_receipts where reference=ref for update;
    if found then
        if existing.allocation_kind<>'direct_topup' or existing.user_id<>u or existing.amount_minor<>p_amount_minor then
            raise exception 'Cash receipt already assigned' using errcode='22023'; end if;
        return jsonb_build_object('payment_id',existing.payment_id,'state','verified','replayed',true);
    end if;
    result:=commerce_rpc.confirm_receipt(p_telegram_user_id,'manual-cash:'||ref,'manual_cash',ref,p_amount_minor,
        'admin-cash-receipt:'||ref,'topup',null,jsonb_build_object('kind','authenticated_cash_receipt',
        'actor_telegram_id',p_actor_telegram_id,'approval_reference',p_approval_reference));
    insert into commerce.cash_receipts(reference,allocation_kind,amount_minor,actor_telegram_id,approval_reference,user_id,payment_id)
        values(ref,'direct_topup',p_amount_minor,p_actor_telegram_id,p_approval_reference,u,(result->>'payment_id')::uuid);
    return result;
end $$;

create function commerce_rpc.issue_cash_credit_code(p_code text,p_credit_minor bigint,p_max_redemptions bigint,
    p_funded_minor bigint,p_receipt_reference text,p_actor_telegram_id bigint,p_approval_reference text,
    p_bound_telegram_id bigint default null,p_expires_at timestamptz default null)
returns jsonb language plpgsql security definer set search_path='' as $$
declare normalized text;ref text;bound uuid;c commerce.codes%rowtype;receipt commerce.cash_receipts%rowtype;j uuid;
begin
    normalized:=upper(btrim(p_code));ref:=upper(btrim(p_receipt_reference));
    if normalized is null or normalized !~ '^[A-Z0-9_-]{3,48}$' or ref is null or ref !~ '^[A-Z0-9][A-Z0-9_.:-]{2,127}$'
       or p_credit_minor is null or p_credit_minor<=0 or p_max_redemptions is null or p_max_redemptions<=0
       or p_funded_minor is null or p_funded_minor<=0
       or p_credit_minor::numeric*p_max_redemptions::numeric<>p_funded_minor::numeric
       or p_actor_telegram_id is null or p_actor_telegram_id<=0
       or p_approval_reference is null or length(btrim(p_approval_reference)) not between 1 and 1000
       or (p_bound_telegram_id is not null and (p_bound_telegram_id<=0 or p_max_redemptions<>1)) then
        raise exception 'Reviewed fully funded cash code required' using errcode='22023'; end if;
    perform pg_advisory_xact_lock(hashtextextended('cash-receipt:'||ref,0));
    perform pg_advisory_xact_lock(hashtextextended('cash-code:'||normalized,0));
    if p_bound_telegram_id is not null then
        bound:=(commerce_rpc.register_telegram_user(p_bound_telegram_id,null)->>'user_id')::uuid;
    end if;
    select * into receipt from commerce.cash_receipts where reference=ref for update;
    if found then
        select * into c from commerce.codes where id=receipt.code_id;
        if receipt.allocation_kind<>'cash_credit_code' or c.code<>normalized or c.credit_minor<>p_credit_minor
           or c.max_redemptions<>p_max_redemptions or c.cash_funded_minor<>p_funded_minor
           or c.cash_bound_user_id is distinct from bound or c.expires_at is distinct from p_expires_at then
            raise exception 'Conflicting cash receipt or code replay' using errcode='22023'; end if;
        return jsonb_build_object('code_id',c.id,'code',c.code,'funded_minor',c.cash_funded_minor,
            'credit_minor',c.credit_minor,'max_redemptions',c.max_redemptions,'replayed',true);
    end if;
    if exists(select 1 from commerce.payments where provider='manual_cash' and provider_reference=ref)
       or exists(select 1 from commerce.codes where code=normalized)
       or (p_expires_at is not null and p_expires_at<=now()) then
        raise exception 'Receipt or code already assigned, or new code expired' using errcode='22023'; end if;
    insert into commerce.codes(code,kind,credit_minor,active,max_redemptions,expires_at,proof_reference,
        cash_bound_user_id,cash_funded_minor)
        values(normalized,'cash_credit',p_credit_minor,true,p_max_redemptions,p_expires_at,'cash-receipt:'||ref,bound,p_funded_minor)
        returning * into c;
    j:=commerce.post_pair('cash-code:fund:'||c.id,'topup',null,'external:manual_cash','cash-code:'||c.id||':unredeemed',p_funded_minor);
    insert into commerce.cash_receipts(reference,allocation_kind,amount_minor,actor_telegram_id,approval_reference,code_id,journal_id)
        values(ref,'cash_credit_code',p_funded_minor,p_actor_telegram_id,p_approval_reference,c.id,j);
    return jsonb_build_object('code_id',c.id,'code',c.code,'funded_minor',c.cash_funded_minor,
        'credit_minor',c.credit_minor,'max_redemptions',c.max_redemptions,'replayed',false);
end $$;

create function commerce_rpc.redeem_cash_credit_code(p_telegram_user_id bigint,p_code text,p_reference text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare u uuid;c commerce.codes%rowtype;w commerce.wallets%rowtype;r commerce.code_redemptions%rowtype;p uuid;j uuid;n bigint;
begin
    if p_reference is null or length(btrim(p_reference)) not between 1 and 1000 then
        raise exception 'Redemption receipt required' using errcode='22023'; end if;
    u:=(commerce_rpc.register_telegram_user(p_telegram_user_id,null)->>'user_id')::uuid;
    perform 1 from commerce.users where id=u and registration_state='active' for update;
    if not found then return jsonb_build_object('state','user_suspended'); end if;
    select * into c from commerce.codes where code=upper(btrim(p_code)) and kind='cash_credit' for update;
    if not found or c.cash_funded_minor is null then return jsonb_build_object('state','invalid_code'); end if;
    if c.cash_bound_user_id is not null and c.cash_bound_user_id<>u then return jsonb_build_object('state','unowned_code'); end if;
    select * into r from commerce.code_redemptions where code_id=c.id and user_id=u;
    if found then return jsonb_build_object('state','already_redeemed','credit_minor',c.credit_minor,'journal_id',r.journal_id); end if;
    if not c.active or (c.expires_at is not null and c.expires_at<=now()) then return jsonb_build_object('state','inactive_code'); end if;
    select count(*) into n from commerce.code_redemptions where code_id=c.id;
    if n>=c.max_redemptions or c.cash_redeemed_minor::numeric+c.credit_minor>c.cash_funded_minor then
        return jsonb_build_object('state','code_exhausted'); end if;
    select * into w from commerce.wallets where user_id=u and kind='customer' and currency='EGP' for update;
    insert into commerce.payments(user_id,purpose,provider,provider_reference,idempotency_key,amount_minor,
        state,proof_reference,verified_at,fx_snapshot)
        values(u,'topup','cash_credit_code',c.id||':'||u,'cash-code:redeem:'||c.id||':'||u,c.credit_minor,'verified',
        c.proof_reference,now(),jsonb_build_object('kind','funded_cash_code','code_id',c.id,'redemption_reference',p_reference)) returning id into p;
    j:=commerce.post_pair('cash-code:redeem:'||c.id||':'||u,'topup',null,'cash-code:'||c.id||':unredeemed',
        'wallet:'||w.id||':available',c.credit_minor,null,w.id);
    update commerce.wallets set available_minor=available_minor+c.credit_minor where id=w.id;
    update commerce.codes set cash_redeemed_minor=cash_redeemed_minor+c.credit_minor where id=c.id;
    insert into commerce.code_redemptions(code_id,user_id,idempotency_key,journal_id)
        values(c.id,u,'cash-code:redeem:'||c.id||':'||u,j);
    return jsonb_build_object('state','redeemed','credit_minor',c.credit_minor,'payment_id',p,'journal_id',j);
end $$;

create function commerce_rpc.apply_customer_code(p_telegram_user_id bigint,p_code text,p_reference text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare kind text; result jsonb;
begin
    if p_reference is null or length(btrim(p_reference)) not between 1 and 1000 then
        raise exception 'Authenticated code input receipt required' using errcode='22023'; end if;
    select c.kind into kind from commerce.codes c where code=upper(btrim(p_code));
    if not found then return jsonb_build_object('kind','unknown','state','invalid_code'); end if;
    if kind='cash_credit' then result:=commerce_rpc.redeem_cash_credit_code(p_telegram_user_id,p_code,p_reference);
    elsif kind='discount' then result:=commerce_rpc.activate_campaign(p_telegram_user_id,p_code,p_reference);
    else result:=commerce_rpc.attribute_referral(p_telegram_user_id,p_code,'manual'); end if;
    return result||jsonb_build_object('kind',kind);
end $$;

create function commerce_rpc.list_cash_credit_codes(p_offset integer default 0)
returns jsonb language plpgsql stable security definer set search_path='' as $$
begin
    if p_offset is null or p_offset not between 0 and 1000000 then raise exception 'Invalid cash code page' using errcode='22023'; end if;
    return coalesce((select jsonb_agg(to_jsonb(record)) from (select c.id as code_id,c.code,c.credit_minor,c.max_redemptions,
        c.cash_funded_minor,c.cash_redeemed_minor,c.active,c.expires_at,receipt.reference as receipt_reference,
        receipt.actor_telegram_id,receipt.approval_reference,u.telegram_user_id as bound_telegram_user_id,
        (select count(*) from commerce.code_redemptions where code_id=c.id) as redemptions
        from commerce.codes c join commerce.cash_receipts receipt on receipt.code_id=c.id
        left join commerce.users u on u.id=c.cash_bound_user_id where c.kind='cash_credit'
        order by c.created_at desc,c.id limit 5 offset p_offset) record),'[]'::jsonb);
end $$;

revoke all on function commerce_rpc.issue_cash_credit_code(text,bigint,bigint,bigint,text,bigint,text,bigint,timestamptz),
    commerce_rpc.redeem_cash_credit_code(bigint,text,text),commerce_rpc.apply_customer_code(bigint,text,text),
    commerce_rpc.list_cash_credit_codes(integer) from public,anon,authenticated;
grant execute on function commerce_rpc.issue_cash_credit_code(text,bigint,bigint,bigint,text,bigint,text,bigint,timestamptz),
    commerce_rpc.redeem_cash_credit_code(bigint,text,text),commerce_rpc.apply_customer_code(bigint,text,text),
    commerce_rpc.list_cash_credit_codes(integer) to service_role;
create or replace function commerce_rpc.health()
returns jsonb language sql stable security definer set search_path='' as $$
    select jsonb_build_object('status','ok','schema_version',16,'currency','EGP')
$$;
notify pgrst,'reload schema';
commit;
