-- Recipient acknowledgement is evidence, separate from the payout ledger.
begin;
create table commerce.withdrawal_receipts (
    id uuid primary key default gen_random_uuid(),
    withdrawal_id uuid not null unique references commerce.withdrawals(id),
    partner_id uuid not null references commerce.partners(id),
    recipient_telegram_id bigint not null check(recipient_telegram_id>0),
    amount_minor numeric not null check(amount_minor>0),
    payout_reference text not null,
    destination text,
    paid_actor_id bigint not null check(paid_actor_id>0),
    paid_at timestamptz not null,
    requested_by bigint not null check(requested_by>0),
    request_reference text not null unique,
    requested_at timestamptz not null default now(),
    state text not null default 'requested' check(state in ('requested','confirmed','disputed')),
    response_actor_id bigint,
    response_reference text unique,
    responded_at timestamptz,
    check((state='requested' and response_actor_id is null and response_reference is null and responded_at is null)
       or (state in ('confirmed','disputed') and response_actor_id is not null and response_actor_id=recipient_telegram_id
           and response_reference is not null and responded_at is not null))
);
alter table commerce.withdrawal_receipts enable row level security;
revoke all on commerce.withdrawal_receipts from public,anon,authenticated,service_role;

create or replace function commerce_rpc.withdrawal_record(p_withdrawal_id uuid)
returns jsonb language plpgsql stable security definer set search_path='' as $$
declare result jsonb;
begin
    select to_jsonb(wd)||jsonb_build_object('telegram_user_id',u.telegram_user_id,
        'receipt',(select to_jsonb(r) from commerce.withdrawal_receipts r where r.withdrawal_id=wd.id)) into result
    from commerce.withdrawals wd join commerce.partners p on p.id=wd.partner_id
        join commerce.users u on u.id=p.user_id where wd.id=p_withdrawal_id;
    if result is null then raise exception 'Unknown withdrawal' using errcode='22023';end if;
    return result;
end $$;

create function commerce_rpc.prepare_withdrawal_receipt(p_withdrawal_id uuid,p_actor_telegram_id bigint,p_reference text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare wd commerce.withdrawals%rowtype;owner_id bigint;
begin
    if not exists(select 1 from commerce.support_actors where telegram_user_id=p_actor_telegram_id and active) then
        raise exception 'Active payout administrator required' using errcode='42501';end if;
    if p_reference is null or length(btrim(p_reference)) not between 1 and 1000 then
        raise exception 'Authenticated receipt request required' using errcode='22023';end if;
    select * into wd from commerce.withdrawals where id=p_withdrawal_id for update;
    if not found or wd.state<>'paid' or wd.paid_actor_id is null or wd.paid_at is null then
        raise exception 'Recorded paid withdrawal required' using errcode='22023';end if;
    select u.telegram_user_id into owner_id from commerce.partners p join commerce.users u on u.id=p.user_id where p.id=wd.partner_id;
    insert into commerce.withdrawal_receipts(withdrawal_id,partner_id,recipient_telegram_id,amount_minor,
        payout_reference,destination,paid_actor_id,paid_at,requested_by,request_reference)
    values(wd.id,wd.partner_id,owner_id,wd.amount_minor,wd.payout_reference,wd.destination,
        wd.paid_actor_id,wd.paid_at,p_actor_telegram_id,p_reference)
    on conflict(withdrawal_id) do nothing;
    return commerce_rpc.withdrawal_record(wd.id);
end $$;

create function commerce_rpc.partner_withdrawal_receipt(p_withdrawal_id uuid,p_telegram_user_id bigint)
returns jsonb language plpgsql stable security definer set search_path='' as $$
begin
    if not exists(select 1 from commerce.withdrawals wd join commerce.partners p on p.id=wd.partner_id
        join commerce.users u on u.id=p.user_id where wd.id=p_withdrawal_id and u.telegram_user_id=p_telegram_user_id) then
        raise exception 'Owned withdrawal required' using errcode='42501';end if;
    return commerce_rpc.withdrawal_record(p_withdrawal_id);
end $$;

create function commerce_rpc.respond_withdrawal_receipt(p_receipt_id uuid,p_telegram_user_id bigint,p_accept boolean,p_reference text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare receipt commerce.withdrawal_receipts%rowtype;decision text;wd commerce.withdrawals%rowtype;
begin
    if p_accept is null or p_reference is null or length(btrim(p_reference)) not between 1 and 1000 then
        raise exception 'Authenticated recipient response required' using errcode='22023';end if;
    select * into receipt from commerce.withdrawal_receipts where id=p_receipt_id for update;
    if not found or receipt.recipient_telegram_id is distinct from p_telegram_user_id
       or not exists(select 1 from commerce.partners p join commerce.users u on u.id=p.user_id
           where p.id=receipt.partner_id and u.telegram_user_id=p_telegram_user_id) then
        raise exception 'Receipt recipient required' using errcode='42501';end if;
    select * into wd from commerce.withdrawals where id=receipt.withdrawal_id;
    if wd.state<>'paid' or wd.amount_minor is distinct from receipt.amount_minor
       or wd.payout_reference is distinct from receipt.payout_reference or wd.partner_id<>receipt.partner_id then
        raise exception 'Payout differs from receipt snapshot' using errcode='22023';end if;
    decision:=case when p_accept then 'confirmed' else 'disputed' end;
    if receipt.state<>'requested' then
        if receipt.state<>decision then raise exception 'Receipt already has an immutable response' using errcode='22023';end if;
        return commerce_rpc.withdrawal_record(receipt.withdrawal_id);
    end if;
    update commerce.withdrawal_receipts set state=decision,response_actor_id=p_telegram_user_id,
        response_reference=p_reference,responded_at=now() where id=receipt.id;
    return commerce_rpc.withdrawal_record(receipt.withdrawal_id);
end $$;

create function commerce_rpc.paid_withdrawal_receipts(p_actor_telegram_id bigint,p_offset integer default 0,p_limit integer default 10)
returns jsonb language plpgsql stable security definer set search_path='' as $$
declare result jsonb;
begin
    if not exists(select 1 from commerce.support_actors where telegram_user_id=p_actor_telegram_id and active) then
        raise exception 'Active payout administrator required' using errcode='42501';end if;
    if p_offset is null or p_offset not between 0 and 1000000 or p_limit is null or p_limit not between 1 and 100 then
        raise exception 'Bounded receipt page required' using errcode='22023';end if;
    select jsonb_build_object('items',coalesce((select jsonb_agg(commerce_rpc.withdrawal_record(page.id)
        order by page.paid_at desc,page.id desc) from (select id,paid_at from commerce.withdrawals
        where state='paid' order by paid_at desc,id desc limit p_limit offset p_offset) page),'[]'::jsonb),
        'total',(select count(*) from commerce.withdrawals where state='paid'),'offset',p_offset,'limit',p_limit) into result;
    return result;
end $$;

create or replace function commerce_rpc.partner_withdrawals(p_telegram_user_id bigint,p_offset integer default 0)
returns jsonb language plpgsql stable security definer set search_path='' as $$
begin
    if p_offset is null or p_offset not between 0 and 1000000 then raise exception 'Invalid withdrawal page' using errcode='22023';end if;
    return coalesce((select jsonb_agg(to_jsonb(record) order by record.created_at desc,record.id desc) from
        (select wd.id,wd.amount_minor,wd.destination,wd.state,wd.payout_reference,wd.created_at,
            r.id as receipt_id,r.state as receipt_state,r.responded_at as receipt_responded_at
         from commerce.withdrawals wd join commerce.partners p on p.id=wd.partner_id join commerce.users u on u.id=p.user_id
         left join commerce.withdrawal_receipts r on r.withdrawal_id=wd.id
         where u.telegram_user_id=p_telegram_user_id order by wd.created_at desc,wd.id desc limit 10 offset p_offset) record),'[]'::jsonb);
end $$;

revoke all on function commerce_rpc.prepare_withdrawal_receipt(uuid,bigint,text),
    commerce_rpc.partner_withdrawal_receipt(uuid,bigint),commerce_rpc.respond_withdrawal_receipt(uuid,bigint,boolean,text),
    commerce_rpc.paid_withdrawal_receipts(bigint,integer,integer) from public,anon,authenticated;
grant execute on function commerce_rpc.prepare_withdrawal_receipt(uuid,bigint,text),
    commerce_rpc.partner_withdrawal_receipt(uuid,bigint),commerce_rpc.respond_withdrawal_receipt(uuid,bigint,boolean,text),
    commerce_rpc.paid_withdrawal_receipts(bigint,integer,integer) to service_role;
notify pgrst,'reload schema';
commit;
