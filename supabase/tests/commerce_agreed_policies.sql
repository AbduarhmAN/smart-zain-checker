\set ON_ERROR_STOP on
begin;
do $$ declare u uuid; begin
    if not exists (select 1 from commerce.pricing_policies
                   where unit = 'check' and unit_price_minor = 3000 and currency = 'EGP'
                   and refund_destination = 'wallet' and refund_requires_consent) then
        raise exception 'Owner pricing/refund decisions missing';
    end if;
    u := (commerce_rpc.register_telegram_user(880004,'partner_fixture')->>'user_id')::uuid;
    insert into commerce.partners(user_id,entitlement_mode,entitlement_checks)
    values (u,'checks',100);
    update commerce.partners set entitlement_mode = 'duration',entitlement_checks = null,
        entitlement_duration_seconds = 86400 where user_id = u;
    update commerce.partners set entitlement_mode = 'forever',entitlement_duration_seconds = null where user_id = u;
    begin
        update commerce.partners set entitlement_mode = null,entitlement_checks = 100 where user_id = u;
        raise exception 'Limit without entitlement mode accepted';
    exception when check_violation then null; end;
    begin
        update commerce.partners set entitlement_mode = 'checks',entitlement_checks = null where user_id = u;
        raise exception 'Count mode without a count accepted';
    exception when check_violation then null; end;
    begin
        update commerce.partners set entitlement_mode = 'duration',entitlement_duration_seconds = 0 where user_id = u;
        raise exception 'Zero duration accepted';
    exception when check_violation then null; end;
    begin
        insert into commerce.pricing_policies(unit_price_minor,refund_destination,refund_requires_consent,decision_reference)
        values (3000,'wallet',false,'invalid');
        raise exception 'Wallet refund without consent policy accepted';
    exception when check_violation then null; end;
    raise notice 'PASS: 30 EGP/check, wallet consent, checks/duration/forever and invalid terms rejection';
end $$;
commit;
