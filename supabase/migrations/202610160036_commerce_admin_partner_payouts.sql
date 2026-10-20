-- Record an administrator's completed external transfer, then request receipt.
begin;
alter table commerce.withdrawals add column initiated_by_telegram_id bigint
    check(initiated_by_telegram_id>0);

create function commerce_rpc.record_partner_payout(p_actor_telegram_id bigint,p_partner_telegram_id bigint,
    p_amount_minor numeric,p_destination text,p_provider text,p_reference text,p_proof text,p_key text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare pid uuid;uid uuid;w commerce.wallets%rowtype;wd commerce.withdrawals%rowtype;
        ref text;j uuid;existing_key boolean;
begin
    if not exists(select 1 from commerce.support_actors where telegram_user_id=p_actor_telegram_id and active) then
        raise exception 'Active payout administrator required' using errcode='42501';end if;
    if p_partner_telegram_id is null or p_partner_telegram_id<=0 or p_amount_minor is null
       or not (p_amount_minor>0 and p_amount_minor<=9223372036854775807)
       or p_destination is null or length(btrim(p_destination)) not between 3 and 500
       or p_provider is null or p_provider !~ '^[a-z0-9_]{1,40}$'
       or p_reference is null or length(btrim(p_reference)) not between 1 and 200
       or p_proof is null or length(btrim(p_proof)) not between 1 and 1000
       or p_key is null or length(p_key) not between 1 and 160 then
        raise exception 'Reviewed administration payout required' using errcode='22023';end if;
    select p.id,p.user_id into pid,uid from commerce.partners p join commerce.users u on u.id=p.user_id
        where u.telegram_user_id=p_partner_telegram_id;
    if not found then raise exception 'Partner account required' using errcode='22023';end if;
    ref:=p_provider||':'||upper(btrim(p_reference));
    -- Both the reviewed action and the provider receipt are single-use across
    -- concurrent clicks, fresh review tokens, restarts and lost responses.
    perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended('admin-payout-key:'||p_key,0));
    perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended('admin-payout-receipt:'||ref,0));
    select * into wd from commerce.withdrawals where idempotency_key=p_key for update;
    existing_key:=found;
    if not existing_key then
        select * into wd from commerce.withdrawals where payout_reference=ref for update;
    end if;
    if wd.id is not null then
        if wd.state<>'paid' or wd.partner_id<>pid or wd.amount_minor<>p_amount_minor
           or wd.destination is distinct from btrim(p_destination) or wd.payout_reference is distinct from ref
           or (existing_key and (wd.initiated_by_telegram_id is distinct from p_actor_telegram_id
                                 or wd.payment_proof is distinct from p_proof)) then
            raise exception 'Conflicting administration payout replay' using errcode='22023';end if;
        return commerce_rpc.prepare_withdrawal_receipt(wd.id,p_actor_telegram_id,'admin-payout-receipt:'||p_key);
    end if;
    -- Earned funds may be paid even after a partnership is deactivated. Only
    -- available earnings count: pending commissions and other holds are excluded.
    select * into w from commerce.wallets where user_id=uid and kind='partner' and currency='EGP' for update;
    if not found then raise exception 'Partner earnings wallet unavailable' using errcode='22023';end if;
    if w.available_minor<p_amount_minor then
        return jsonb_build_object('state','insufficient_balance','available_minor',w.available_minor);end if;
    insert into commerce.withdrawals(partner_id,amount_minor,idempotency_key,destination,initiated_by_telegram_id)
        values(pid,p_amount_minor,p_key,btrim(p_destination),p_actor_telegram_id) returning * into wd;
    j:=commerce.post_pair('withdrawal:hold:'||wd.id,'reserve',null,'wallet:'||w.id||':available',
        'wallet:'||w.id||':reserved',p_amount_minor,w.id,w.id);
    update commerce.wallets set available_minor=available_minor-p_amount_minor,
        reserved_minor=reserved_minor+p_amount_minor where id=w.id;
    update commerce.withdrawals set hold_journal_id=j where id=wd.id;
    perform commerce_rpc.approve_withdrawal(wd.id,p_actor_telegram_id,'admin-payout-approval:'||p_key);
    perform commerce_rpc.complete_withdrawal(wd.id,p_actor_telegram_id,p_provider,p_reference,p_proof);
    -- All ledger changes and receipt creation commit together. Telegram sending
    -- happens afterwards and cannot trigger another debit if delivery fails.
    return commerce_rpc.prepare_withdrawal_receipt(wd.id,p_actor_telegram_id,'admin-payout-receipt:'||p_key);
end $$;
revoke all on function commerce_rpc.record_partner_payout(bigint,bigint,numeric,text,text,text,text,text)
    from public,anon,authenticated;
grant execute on function commerce_rpc.record_partner_payout(bigint,bigint,numeric,text,text,text,text,text)
    to service_role;
notify pgrst,'reload schema';
commit;
