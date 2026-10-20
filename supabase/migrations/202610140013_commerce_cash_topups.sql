begin;
create function commerce_rpc.admin_cash_topup(p_telegram_user_id bigint,p_amount_minor bigint,
    p_receipt_reference text,p_actor_telegram_id bigint,p_approval_reference text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare u uuid; ref text; existing commerce.payments%rowtype; result jsonb;
begin
    if p_actor_telegram_id is null or p_actor_telegram_id<=0 or p_amount_minor is null or p_amount_minor<=0
       or p_receipt_reference is null or upper(btrim(p_receipt_reference)) !~ '^[A-Z0-9][A-Z0-9_.:-]{2,127}$'
       or p_approval_reference is null or length(btrim(p_approval_reference)) not between 1 and 1000 then
        raise exception 'Authenticated cash receipt and exact EGP amount required' using errcode='22023'; end if;
    ref:=upper(btrim(p_receipt_reference));
    u:=(commerce_rpc.register_telegram_user(p_telegram_user_id,null)->>'user_id')::uuid;
    perform 1 from commerce.users where id=u and registration_state='active' for update;
    if not found then raise exception 'Customer suspended' using errcode='22023'; end if;
    select * into existing from commerce.payments where provider='manual_cash' and provider_reference=ref for update;
    if found then
        if existing.user_id<>u or existing.amount_minor<>p_amount_minor or existing.purpose<>'topup' then
            raise exception 'Cash receipt already assigned to another customer or amount' using errcode='22023'; end if;
        return jsonb_build_object('payment_id',existing.id,'state',existing.state,'replayed',true);
    end if;
    result:=commerce_rpc.confirm_receipt(p_telegram_user_id,'manual-cash:'||ref,'manual_cash',ref,p_amount_minor,
        'admin-cash-receipt:'||ref,'topup',null,jsonb_build_object('kind','authenticated_cash_receipt',
            'actor_telegram_id',p_actor_telegram_id,'approval_reference',p_approval_reference));
    return result;
end $$;
revoke all on function commerce_rpc.admin_cash_topup(bigint,bigint,text,bigint,text) from public,anon,authenticated;
grant execute on function commerce_rpc.admin_cash_topup(bigint,bigint,text,bigint,text) to service_role;
create or replace function commerce_rpc.health()
returns jsonb language sql stable security definer set search_path='' as $$
    select jsonb_build_object('status','ok','schema_version',13,'currency','EGP')
$$;
notify pgrst,'reload schema';
commit;
