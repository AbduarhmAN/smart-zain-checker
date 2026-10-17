-- Explicitly reviewed excess payouts are advances against future earnings.
-- Customer wallets and ordinary withdrawal limits retain their invariants.
begin;
create table commerce.partner_advances (
    id uuid primary key default gen_random_uuid(),
    withdrawal_id uuid not null unique references commerce.withdrawals(id),
    partner_id uuid not null references commerce.partners(id),
    wallet_id uuid not null references commerce.wallets(id),
    amount_minor numeric not null check(amount_minor>0),
    outstanding_minor numeric not null check(outstanding_minor>=0 and outstanding_minor<=amount_minor),
    earned_paid_minor numeric not null check(earned_paid_minor>=0),
    available_before_minor numeric not null,
    available_after_minor numeric not null,
    account_after_minor numeric not null,
    reason text not null check(length(btrim(reason)) between 3 and 500),
    actor_telegram_id bigint not null check(actor_telegram_id>0),
    proof text not null,
    advance_journal_id uuid not null unique references commerce.journals(id),
    earned_journal_id uuid unique references commerce.journals(id),
    created_at timestamptz not null default now()
);
create index partner_advances_outstanding on commerce.partner_advances(wallet_id,created_at,id) where outstanding_minor>0;
create table commerce.partner_advance_repayments (
    id uuid primary key default gen_random_uuid(),
    advance_id uuid not null references commerce.partner_advances(id),
    amount_minor numeric not null check(amount_minor>0),
    journal_id uuid not null unique references commerce.journals(id),
    created_at timestamptz not null default now()
);
alter table commerce.partner_advances enable row level security;
alter table commerce.partner_advance_repayments enable row level security;
revoke all on commerce.partner_advances,commerce.partner_advance_repayments from public,anon,authenticated,service_role;

create function commerce.repay_partner_advances() returns trigger
language plpgsql security definer set search_path='' as $$
declare item commerce.partner_advances%rowtype;remaining numeric;allocation numeric;j uuid;repaid numeric;
begin
    if new.kind<>'partner' or new.available_minor<=old.available_minor then return new;end if;
    remaining:=new.available_minor-old.available_minor;
    for item in select * from commerce.partner_advances where wallet_id=new.id and outstanding_minor>0
        order by created_at,id for update loop
        allocation:=least(remaining,item.outstanding_minor);
        if allocation<=0 then exit;end if;
        repaid:=item.amount_minor-item.outstanding_minor+allocation;
        j:=commerce.post_pair('partner-advance:repay:'||item.id||':'||trim_scale(repaid),'adjustment',null,
            'wallet:'||new.id||':available','partner-advance:'||item.partner_id,allocation,new.id,null);
        insert into commerce.partner_advance_repayments(advance_id,amount_minor,journal_id) values(item.id,allocation,j);
        update commerce.partner_advances set outstanding_minor=outstanding_minor-allocation where id=item.id;
        new.available_minor:=new.available_minor-allocation;
        remaining:=remaining-allocation;
        if remaining=0 then exit;end if;
    end loop;
    return new;
end $$;
revoke all on function commerce.repay_partner_advances() from public,anon,authenticated,service_role;
create trigger repay_partner_advances before update of available_minor on commerce.wallets
    for each row execute function commerce.repay_partner_advances();

create or replace function commerce_rpc.withdrawal_record(p_withdrawal_id uuid)
returns jsonb language plpgsql stable security definer set search_path='' as $$
declare result jsonb;
begin
    select to_jsonb(wd)||jsonb_build_object('telegram_user_id',u.telegram_user_id,
        'receipt',(select to_jsonb(r) from commerce.withdrawal_receipts r where r.withdrawal_id=wd.id),
        'advance',(select jsonb_build_object('id',a.id,'amount_minor',a.amount_minor,'earned_paid_minor',a.earned_paid_minor,
            'available_before_minor',a.available_before_minor,'available_after_minor',a.available_after_minor,
            'account_after_minor',a.account_after_minor,'reason',a.reason,'actor_telegram_id',a.actor_telegram_id,
            'created_at',a.created_at) from commerce.partner_advances a where a.withdrawal_id=wd.id)) into result
    from commerce.withdrawals wd join commerce.partners p on p.id=wd.partner_id
        join commerce.users u on u.id=p.user_id where wd.id=p_withdrawal_id;
    if result is null then raise exception 'Unknown withdrawal' using errcode='22023';end if;
    return result;
end $$;

create function commerce_rpc.record_partner_advance_payout(p_actor_telegram_id bigint,p_partner_telegram_id bigint,
    p_amount_minor numeric,p_destination text,p_provider text,p_reference text,p_proof text,p_key text,
    p_reason text,p_expected_available_minor numeric) returns jsonb
language plpgsql security definer set search_path='' as $$
declare pid uuid;uid uuid;w commerce.wallets%rowtype;wd commerce.withdrawals%rowtype;
    ref text;existing_key boolean;a commerce.partner_advances%rowtype;debt numeric;balance numeric;
    earned numeric;excess numeric;j uuid;earned_j uuid;
begin
    if not exists(select 1 from commerce.support_actors where telegram_user_id=p_actor_telegram_id and active) then
        raise exception 'Active payout administrator required' using errcode='42501';end if;
    if p_partner_telegram_id is null or p_partner_telegram_id<=0 or p_amount_minor is null
       or not (p_amount_minor>0 and p_amount_minor<=9223372036854775807)
       or p_expected_available_minor is null or p_expected_available_minor::text in ('NaN','Infinity','-Infinity')
       or p_destination is null or length(btrim(p_destination)) not between 3 and 500
       or p_provider is null or p_provider !~ '^[a-z0-9_]{1,40}$'
       or p_reference is null or length(btrim(p_reference)) not between 1 and 200
       or p_proof is null or length(btrim(p_proof)) not between 1 and 1000
       or p_key is null or length(p_key) not between 1 and 160
       or p_reason is null or length(btrim(p_reason)) not between 3 and 500 then
        raise exception 'Reviewed advance payout and reason required' using errcode='22023';end if;
    select p.id,p.user_id into pid,uid from commerce.partners p join commerce.users u on u.id=p.user_id
        where u.telegram_user_id=p_partner_telegram_id;
    if not found then raise exception 'Partner account required' using errcode='22023';end if;
    ref:=p_provider||':'||upper(btrim(p_reference));
    -- Share replay locks with ordinary payouts; a receipt cannot debit twice
    -- through two different financial endpoints.
    perform pg_advisory_xact_lock(hashtextextended('admin-payout-key:'||p_key,0));
    perform pg_advisory_xact_lock(hashtextextended('admin-payout-receipt:'||ref,0));
    select * into wd from commerce.withdrawals where idempotency_key=p_key for update;
    existing_key:=found;
    if not existing_key then select * into wd from commerce.withdrawals where payout_reference=ref for update;end if;
    if wd.id is not null then
        select * into a from commerce.partner_advances where withdrawal_id=wd.id;
        if not found or wd.state<>'paid' or wd.partner_id<>pid or wd.amount_minor<>p_amount_minor
           or wd.destination is distinct from btrim(p_destination) or wd.payout_reference is distinct from ref
           or a.reason<>btrim(p_reason)
           or (existing_key and (a.available_before_minor is distinct from p_expected_available_minor
               or wd.initiated_by_telegram_id is distinct from p_actor_telegram_id or wd.payment_proof is distinct from p_proof)) then
            raise exception 'Conflicting advance payout replay' using errcode='22023';end if;
        return commerce_rpc.prepare_withdrawal_receipt(wd.id,p_actor_telegram_id,'admin-advance-receipt:'||p_key)||jsonb_build_object('replayed',true);
    end if;
    select * into w from commerce.wallets where user_id=uid and kind='partner' and currency='EGP' for update;
    if not found then raise exception 'Partner earnings wallet unavailable' using errcode='22023';end if;
    select coalesce(sum(outstanding_minor),0) into debt from commerce.partner_advances where wallet_id=w.id;
    balance:=w.available_minor-debt;
    if balance is distinct from p_expected_available_minor then
        raise exception 'Partner balance changed; review advance again' using errcode='22023';end if;
    earned:=least(w.available_minor,p_amount_minor);excess:=p_amount_minor-earned;
    if excess<=0 then raise exception 'Advance payout must exceed available earnings' using errcode='22023';end if;
    insert into commerce.withdrawals(partner_id,amount_minor,idempotency_key,destination,initiated_by_telegram_id)
        values(pid,p_amount_minor,p_key,btrim(p_destination),p_actor_telegram_id) returning * into wd;
    if earned>0 then
        earned_j:=commerce.post_pair('withdrawal:paid:'||wd.id||':earned','withdrawal',null,
            'wallet:'||w.id||':available','external:payout:'||p_provider,earned,w.id,null);
    end if;
    j:=commerce.post_pair('withdrawal:paid:'||wd.id||':advance','withdrawal',null,
        'partner-advance:'||pid,'external:payout:'||p_provider,excess,null,null);
    insert into commerce.partner_advances(withdrawal_id,partner_id,wallet_id,amount_minor,outstanding_minor,earned_paid_minor,
        available_before_minor,available_after_minor,account_after_minor,reason,actor_telegram_id,proof,advance_journal_id,earned_journal_id)
        values(wd.id,pid,w.id,excess,excess,earned,balance,balance-p_amount_minor,balance+w.reserved_minor-p_amount_minor,
            btrim(p_reason),p_actor_telegram_id,p_proof,j,earned_j);
    update commerce.wallets set available_minor=available_minor-earned,withdrawn_minor=withdrawn_minor+p_amount_minor where id=w.id;
    update commerce.withdrawals set state='paid',payout_reference=ref,journal_id=j,paid_actor_id=p_actor_telegram_id,
        payment_proof=p_proof,paid_at=now(),decision_actor_id=p_actor_telegram_id,
        approval_reference='admin-advance-approval:'||p_key,approved_at=now() where id=wd.id;
    -- The receipt trigger atomically queues the amount, explanation and balance
    -- for the partner and configured administration channel.
    return commerce_rpc.prepare_withdrawal_receipt(wd.id,p_actor_telegram_id,'admin-advance-receipt:'||p_key);
end $$;

create function commerce.partner_advance_balance(p_partner_id uuid,p_available numeric,p_reserved numeric) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare debt numeric;reasons jsonb;
begin
    select coalesce(sum(outstanding_minor),0) into debt from commerce.partner_advances where partner_id=p_partner_id;
    select coalesce(jsonb_agg(item),'[]') into reasons from (
        select jsonb_build_object('withdrawal_id',withdrawal_id,'amount_minor',amount_minor,'outstanding_minor',outstanding_minor,
            'reason',reason,'created_at',created_at) item from commerce.partner_advances
            where partner_id=p_partner_id and outstanding_minor>0 order by created_at,id limit 5) page;
    return jsonb_build_object('available_minor',p_available-debt,'withdrawable_minor',p_available,
        'account_balance_minor',p_available+p_reserved-debt,
        'advance_outstanding_minor',debt,'outstanding_advances',reasons);
end $$;
revoke all on function commerce.partner_advance_balance(uuid,numeric,numeric) from public,anon,authenticated,service_role;

alter function commerce_rpc.partner_finance(bigint) rename to partner_finance_before_advances;
alter function commerce_rpc.partner_finance_before_advances(bigint) set schema commerce;
revoke all on function commerce.partner_finance_before_advances(bigint) from public,anon,authenticated,service_role;
create function commerce_rpc.partner_finance(p_telegram_user_id bigint) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare result jsonb;
begin
    result:=commerce.partner_finance_before_advances(p_telegram_user_id);
    if result->>'is_partner'<>'true' then return result;end if;
    return result||commerce.partner_advance_balance((result->>'partner_id')::uuid,
        (result->>'available_minor')::numeric,(result->>'reserved_minor')::numeric);
end $$;

alter function commerce_rpc.wallet_state(bigint) rename to wallet_state_before_partner_advances;
alter function commerce_rpc.wallet_state_before_partner_advances(bigint) set schema commerce;
revoke all on function commerce.wallet_state_before_partner_advances(bigint) from public,anon,authenticated,service_role;
create function commerce_rpc.wallet_state(p_telegram_user_id bigint) returns jsonb
language plpgsql security definer set search_path='' as $$
declare result jsonb;pid uuid;balance jsonb;
begin
    result:=commerce.wallet_state_before_partner_advances(p_telegram_user_id);
    if result->>'is_partner'<>'true' then return result;end if;
    select p.id into pid from commerce.partners p join commerce.users u on u.id=p.user_id where u.telegram_user_id=p_telegram_user_id;
    balance:=commerce.partner_advance_balance(pid,(result->'partner'->>'available_minor')::numeric,(result->'partner'->>'reserved_minor')::numeric);
    return result||jsonb_build_object('partner',(result->'partner')||balance);
end $$;

alter function commerce_rpc.admin_earnings_summary(bigint,integer,integer) rename to admin_earnings_before_advances;
alter function commerce_rpc.admin_earnings_before_advances(bigint,integer,integer) set schema commerce;
revoke all on function commerce.admin_earnings_before_advances(bigint,integer,integer) from public,anon,authenticated,service_role;
create function commerce_rpc.admin_earnings_summary(p_actor_telegram_id bigint,p_offset integer default 0,p_limit integer default 8) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare result jsonb;items jsonb;
begin
    result:=commerce.admin_earnings_before_advances(p_actor_telegram_id,p_offset,p_limit);
    select coalesce(jsonb_agg(item||jsonb_build_object('withdrawable_minor',(item->>'available_minor')::numeric,
        'available_minor',(item->>'available_minor')::numeric-debt,'advance_outstanding_minor',debt,
        'account_balance_minor',(item->>'available_minor')::numeric+(item->>'reserved_minor')::numeric-debt) order by ordinal),'[]')
        into items from jsonb_array_elements(result->'partners') with ordinality entry(item,ordinal)
        cross join lateral (select coalesce(sum(outstanding_minor),0) debt from commerce.partner_advances where partner_id=(item->>'partner_id')::uuid) amounts;
    return result||jsonb_build_object('partners',items);
end $$;

alter function commerce_rpc.partner_withdrawals(bigint,integer) rename to partner_withdrawals_before_advances;
alter function commerce_rpc.partner_withdrawals_before_advances(bigint,integer) set schema commerce;
revoke all on function commerce.partner_withdrawals_before_advances(bigint,integer) from public,anon,authenticated,service_role;
create function commerce_rpc.partner_withdrawals(p_telegram_user_id bigint,p_offset integer default 0) returns jsonb
language sql stable security definer set search_path='' as $$
    select coalesce(jsonb_agg(item||jsonb_build_object('advance',
        (select jsonb_build_object('amount_minor',a.amount_minor,'reason',a.reason,'account_after_minor',a.account_after_minor)
         from commerce.partner_advances a where a.withdrawal_id=(item->>'id')::uuid)) order by ordinal),'[]')
    from jsonb_array_elements(commerce.partner_withdrawals_before_advances(p_telegram_user_id,p_offset)) with ordinality entry(item,ordinal)
$$;
revoke all on function commerce_rpc.record_partner_advance_payout(bigint,bigint,numeric,text,text,text,text,text,text,numeric),
    commerce_rpc.partner_finance(bigint),commerce_rpc.wallet_state(bigint),commerce_rpc.admin_earnings_summary(bigint,integer,integer),commerce_rpc.partner_withdrawals(bigint,integer)
    from public,anon,authenticated;
grant execute on function commerce_rpc.record_partner_advance_payout(bigint,bigint,numeric,text,text,text,text,text,text,numeric),
    commerce_rpc.partner_finance(bigint),commerce_rpc.wallet_state(bigint),commerce_rpc.admin_earnings_summary(bigint,integer,integer),commerce_rpc.partner_withdrawals(bigint,integer)
    to service_role;
notify pgrst,'reload schema';
commit;
