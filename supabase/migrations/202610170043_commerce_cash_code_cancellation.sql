-- Stop future cash-code redemptions without reversing funded cash or wallets.
begin;

alter table commerce.codes add column cash_cancelled_at timestamptz;
alter table commerce.codes add column cash_cancelled_by_telegram_id bigint;
alter table commerce.codes add column cash_cancellation_reference text;
alter table commerce.codes add constraint cash_code_cancellation_check check (
    (cash_cancelled_at is null and cash_cancelled_by_telegram_id is null and cash_cancellation_reference is null)
    or (kind='cash_credit' and not active and cash_cancelled_at is not null
        and cash_cancelled_by_telegram_id is not null and cash_cancelled_by_telegram_id>0
        and cash_cancellation_reference is not null and length(btrim(cash_cancellation_reference)) between 1 and 1000)
);

create function commerce_rpc.cash_credit_code_record(p_actor_telegram_id bigint,p_code_id uuid)
returns jsonb language plpgsql stable security definer set search_path='' as $$
begin
    perform commerce.require_sheet_admin(p_actor_telegram_id);
    return (select jsonb_build_object('code_id',c.id,'code',c.code,'active',c.active,
        'credit_minor',c.credit_minor,'max_redemptions',c.max_redemptions,
        'cash_funded_minor',c.cash_funded_minor,'cash_redeemed_minor',c.cash_redeemed_minor,
        'redemptions',(select count(*) from commerce.code_redemptions where code_id=c.id),
        'cancelled_at',c.cash_cancelled_at)
        from commerce.codes c where c.id=p_code_id and c.kind='cash_credit' and c.cash_funded_minor is not null);
end $$;

create function commerce_rpc.cancel_cash_credit_code(p_actor_telegram_id bigint,p_code_id uuid,p_reference text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare c commerce.codes%rowtype;replayed boolean;
begin
    perform commerce.require_sheet_admin(p_actor_telegram_id);
    if p_reference is null or length(btrim(p_reference)) not between 1 and 1000 then
        raise exception 'Cash code cancellation receipt required' using errcode='22023';end if;
    -- Redemption takes this same row lock. Once cancellation commits, no new
    -- redemption can pass the existing active-code check.
    select * into c from commerce.codes where id=p_code_id and kind='cash_credit'
        and cash_funded_minor is not null for update;
    if not found then return jsonb_build_object('state','invalid_code');end if;
    replayed:=c.cash_cancelled_at is not null;
    if not replayed then
        update commerce.codes set active=false,cash_cancelled_at=now(),
            cash_cancelled_by_telegram_id=p_actor_telegram_id,cash_cancellation_reference=p_reference
            where id=c.id returning * into c;
    end if;
    return jsonb_build_object('state',case when replayed then 'already_cancelled' else 'cancelled' end,
        'code_id',c.id,'code',c.code,'active',c.active,'cancelled_at',c.cash_cancelled_at,
        'cash_redeemed_minor',c.cash_redeemed_minor,
        'unredeemed_minor',c.cash_funded_minor-c.cash_redeemed_minor);
end $$;

revoke all on function commerce_rpc.cash_credit_code_record(bigint,uuid),
    commerce_rpc.cancel_cash_credit_code(bigint,uuid,text) from public,anon,authenticated;
grant execute on function commerce_rpc.cash_credit_code_record(bigint,uuid),
    commerce_rpc.cancel_cash_credit_code(bigint,uuid,text) to service_role;
notify pgrst,'reload schema';
commit;
