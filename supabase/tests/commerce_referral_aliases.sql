-- Synthetic fixtures on an isolated retained database only.
\set ON_ERROR_STOP on
begin;
do $$
declare pid uuid;cid uuid;other_pid uuid;u uuid;r jsonb;original uuid;blocked boolean;
begin
    pid:=(commerce_rpc.create_partner(998401,'ALIAS_CURRENT')->>'partner_id')::uuid;
    perform commerce_rpc.configure_partner(pid,9,3000,'forever',null,true);
    select id into cid from commerce.codes where partner_id=pid and kind='referral';
    insert into commerce.referral_code_aliases(code,code_id,partner_id) values('ALIAS_OLD',cid,pid);
    r:=commerce_rpc.attribute_referral(998402,' alias_old ','deep_link');
    if r->>'status'<>'attributed' or (r->>'partner_id')::uuid<>pid then raise exception 'Old link did not attribute';end if;
    select code_id into original from commerce.referrals where user_id=(select id from commerce.users where telegram_user_id=998402);
    if original<>cid then raise exception 'Alias created a separate attribution code';end if;
    r:=commerce_rpc.attribute_referral(998402,'ALIAS_CURRENT','deep_link');
    if r->>'status'<>'already_attributed' then raise exception 'Canonical/alias replay differs';end if;
    r:=commerce_rpc.attribute_referral(998403,'ALIAS_OLD','manual');
    if r->>'status'<>'attributed' then raise exception 'Manual alias failed';end if;
    r:=commerce_rpc.attribute_referral(998401,'ALIAS_OLD','deep_link');
    if r->>'status'<>'self_referral' then raise exception 'Self referral bypass';end if;
    update commerce.codes set active=false where id=cid;
    r:=commerce_rpc.attribute_referral(998404,'ALIAS_OLD','deep_link');
    if r->>'status'<>'invalid_code' then raise exception 'Inactive canonical bypass';end if;
    update commerce.codes set active=true where id=cid;
    update commerce.partners set state='suspended' where id=pid;
    r:=commerce_rpc.attribute_referral(998404,'ALIAS_OLD','deep_link');
    if r->>'status'<>'inactive_partner' then raise exception 'Inactive partner bypass';end if;
    update commerce.partners set state='active' where id=pid;
    update commerce.codes set code='ALIAS_RENAMED' where id=cid;
    r:=commerce_rpc.attribute_referral(998405,'ALIAS_OLD','deep_link');
    if r->>'status'<>'attributed' then raise exception 'Alias lost stable code identity';end if;
    other_pid:=(commerce_rpc.create_partner(998406,'ALIAS_OTHER')->>'partner_id')::uuid;
    perform commerce_rpc.configure_partner(other_pid,9,3000,'forever',null,true);
    r:=commerce_rpc.attribute_referral(998405,'ALIAS_OTHER','deep_link');
    if r->>'status'<>'first_source_preserved' or (r->>'partner_id')::uuid<>pid then raise exception 'First source lost';end if;
    perform commerce_rpc.register_telegram_user(998407,null);
    update commerce.users set first_paid_service_at=now() where telegram_user_id=998407;
    r:=commerce_rpc.attribute_referral(998407,'ALIAS_OLD','deep_link');
    if r->>'status'<>'prior_paid_service' then raise exception 'Prior service bypass';end if;
    perform commerce_rpc.register_telegram_user(998408,null);
    update commerce.users set legacy_service_review_required=true where telegram_user_id=998408;
    r:=commerce_rpc.attribute_referral(998408,'ALIAS_OLD','deep_link');
    if r->>'status'<>'legacy_history_review' then raise exception 'Legacy history bypass';end if;
    update commerce.codes set max_redemptions=1 where id=cid;
    r:=commerce_rpc.attribute_referral(998409,'ALIAS_OLD','deep_link');
    if r->>'status'<>'code_exhausted' then raise exception 'Shared canonical cap bypass';end if;
    blocked:=false;
    begin
        perform commerce_rpc.create_partner(998410,'ALIAS_OLD');
    exception when sqlstate '22023' then blocked:=true;end;
    if not blocked then raise exception 'Alias namespace was stolen';end if;
    blocked:=false;
    begin
        insert into commerce.referral_code_aliases(code,code_id,partner_id) values('ALIAS_OTHER',cid,pid);
    exception when sqlstate '22023' then blocked:=true;end;
    if not blocked then raise exception 'Existing code shadowed';end if;
    blocked:=false;
    begin
        insert into commerce.referral_code_aliases(code,code_id,partner_id) values('ALIAS_WRONG_OWNER',cid,other_pid);
    exception when sqlstate '22023' then blocked:=true;end;
    if not blocked then raise exception 'Alias owner mismatch accepted';end if;
    if has_table_privilege('service_role','commerce.referral_code_aliases','INSERT')
       or has_function_privilege('anon','commerce_rpc.attribute_referral(bigint,text,text)','EXECUTE')
       or has_function_privilege('authenticated','commerce_rpc.attribute_referral(bigint,text,text)','EXECUTE')
       or not has_function_privilege('service_role','commerce_rpc.attribute_referral(bigint,text,text)','EXECUTE') then
        raise exception 'Alias RPC/table privileges changed';end if;
end $$;
rollback;
