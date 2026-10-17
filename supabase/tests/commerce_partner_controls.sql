\set ON_ERROR_STOP on
begin;
do $$ declare p uuid; v text; r jsonb; n bigint; begin
    p := (commerce_rpc.create_partner(882001,'TERMS_FIXTURE')->>'partner_id')::uuid;
    r := commerce_rpc.configure_partner(p,882999,2500,'checks',100,true);
    v := r->>'version';
    if (select entitlement_checks from commerce.partners where id = p) <> 100
       or (select state from commerce.partners where id = p) <> 'active'
       or not (select active from commerce.codes where partner_id = p) then
        raise exception 'Partner activation failed';
    end if;
    if commerce_rpc.configure_partner(p,882999,2500,'checks',100,true)->>'version' <> v then
        raise exception 'Terms replay changed version';
    end if;
    r := commerce_rpc.configure_partner(p,882999,2000,'duration',604800,true);
    if r->>'version' = v or (select entitlement_duration_seconds from commerce.partners where id = p) <> 604800 then
        raise exception 'Duration/version failed';
    end if;
    perform commerce_rpc.configure_partner(p,882999,1500,'forever',null,false);
    if (select active from commerce.codes where partner_id = p) then
        raise exception 'Inactive partner code remained active';
    end if;
    if (select count(*) from commerce.partner_terms_history where partner_id = p) <> 3 then
        raise exception 'Wrong version history';
    end if;
    begin
        perform commerce_rpc.configure_partner(p,882999,10001,'forever',null,true);
        raise exception 'Invalid percentage accepted';
    exception when invalid_parameter_value then null; end;
    begin
        perform commerce_rpc.configure_partner(p,882999,1000,'duration',null,true);
        raise exception 'Unspecified duration accepted';
    exception when invalid_parameter_value then null; end;
    if commerce_rpc.referral_profile(882001)->>'eligible' <> 'true' then
        raise exception 'Profile eligibility wrong';
    end if;
    if commerce_rpc.health()->>'status' <> 'ok' then raise exception 'Health check failed'; end if;
    raise notice 'PASS: partner modes, activation, version freeze, replay, invalid terms and profile/health';
end $$;
commit;
