\set ON_ERROR_STOP on
begin;
do $$ declare a uuid; b uuid; u uuid; r jsonb; begin
    a := (commerce_rpc.create_partner(881001,'PARTNER_A')->>'partner_id')::uuid;
    b := (commerce_rpc.create_partner(881002,'PARTNER_B')->>'partner_id')::uuid;
    if (commerce_rpc.create_partner(881001,' partner_a ')->>'partner_id')::uuid <> a then
        raise exception 'Partner replay duplicated identity';
    end if;
    begin
        perform commerce_rpc.create_partner(881003,'PARTNER_A');
        raise exception 'Code assigned to multiple partners';
    exception when invalid_parameter_value then null; end;
    if commerce_rpc.attribute_referral(881100,'PARTNER_A','manual')->>'status' <> 'invalid_code' then
        raise exception 'Inactive code attributed';
    end if;
    update commerce.partners set state = 'active',commission_basis_points = 3000,
        terms_version = 'fixture',approved_at = now(),entitlement_mode = 'forever'
    where id in (a,b);
    update commerce.codes set active = true where partner_id in (a,b);
    r := commerce_rpc.attribute_referral(881100,'PARTNER_A','deep_link');
    if r->>'status' <> 'attributed' then raise exception 'Valid link not attributed'; end if;
    r := commerce_rpc.attribute_referral(881100,'PARTNER_B','manual');
    if r->>'status' <> 'first_source_preserved' or (r->>'partner_id')::uuid <> a then
        raise exception 'Conflicting manual code replaced first source';
    end if;
    if commerce_rpc.attribute_referral(881100,'PARTNER_A','manual')->>'status' <> 'already_attributed' then
        raise exception 'Same source replay not idempotent';
    end if;
    if commerce_rpc.attribute_referral(881001,'PARTNER_A','manual')->>'status' <> 'self_referral' then
        raise exception 'Self referral accepted';
    end if;
    u := (commerce_rpc.register_telegram_user(881101,null)->>'user_id')::uuid;
    update commerce.wallets set available_minor = 10000 where user_id = u and kind = 'customer';
    if commerce_rpc.attribute_referral(881101,'PARTNER_A','manual')->>'status' <> 'attributed' then
        raise exception 'Topup incorrectly blocked new customer';
    end if;
    u := (commerce_rpc.register_telegram_user(881102,null)->>'user_id')::uuid;
    update commerce.users set first_paid_service_at = now() where id = u;
    if commerce_rpc.attribute_referral(881102,'PARTNER_A','manual')->>'status' <> 'prior_paid_service' then
        raise exception 'Prior paid service accepted referral';
    end if;
    if (select count(*) from commerce.referrals where user_id =
        (select id from commerce.users where telegram_user_id = 881100)) <> 1 then
        raise exception 'Attribution duplicated';
    end if;
    raise notice 'PASS: unique partner code, shared link/manual attribution, first source, self-referral, new customer topup and prior paid eligibility';
end $$;
commit;
