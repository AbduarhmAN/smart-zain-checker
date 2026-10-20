\set ON_ERROR_STOP on
begin;
do $$
declare pid uuid;pid2 uuid;wid uuid;hold_id uuid;result jsonb;wd uuid;receipt uuid;
        saved_wallet jsonb;entries bigint;count_withdrawals bigint;amount numeric:=1000.125;
begin
    insert into commerce.support_actors(telegram_user_id,active,selection_reference)
        values(888060,true,'isolated admin-initiated payout fixture');
    pid:=(commerce_rpc.create_partner(888061,'ADMIN_PAYOUT_FIXTURE')->>'partner_id')::uuid;
    pid2:=(commerce_rpc.create_partner(888062,'ADMIN_PAYOUT_OTHER')->>'partner_id')::uuid;
    perform commerce_rpc.configure_partner(pid,888060,3000,'forever',null,true);
    select w.id into wid from commerce.wallets w join commerce.partners p on p.user_id=w.user_id
        where p.id=pid and w.kind='partner';
    update commerce.wallets set available_minor=10000 where id=wid;
    hold_id:=(commerce_rpc.request_withdrawal(888061,'admin-payout-unrelated-hold',250,'other pending destination')->>'withdrawal_id')::uuid;
    begin
        perform commerce_rpc.record_partner_payout(888061,888061,amount,'fixture bank','bank','TX-MAIN','native-review','admin-payout-fixture');
        raise exception 'Partner registered their own administrative payout';
    exception when insufficient_privilege then null;end;
    result:=commerce_rpc.record_partner_payout(888060,888061,amount,'fixture bank','bank','TX-MAIN','native-review','admin-payout-fixture');
    wd:=(result->>'id')::uuid;receipt:=(result->'receipt'->>'id')::uuid;
    if result->>'state'<>'paid' or (result->>'amount_minor')::numeric<>amount
       or (result->>'initiated_by_telegram_id')::bigint<>888060
       or result->'receipt'->>'state'<>'requested' or (result->'receipt'->>'recipient_telegram_id')::bigint<>888061 then
        raise exception 'Admin payout did not preserve paid record and recipient evidence';end if;
    if not exists(select 1 from commerce.wallets where id=wid and available_minor=8749.875
        and reserved_minor=250 and withdrawn_minor=amount) then raise exception 'Payout used held funds or rounded exact amount';end if;
    if (select state from commerce.withdrawals where id=hold_id)<>'requested' then raise exception 'Other pending withdrawal changed';end if;
    select to_jsonb(w) into saved_wallet from commerce.wallets w where id=wid;
    select count(*) into entries from commerce.ledger_entries;
    select count(*) into count_withdrawals from commerce.withdrawals;
    result:=commerce_rpc.record_partner_payout(888060,888061,amount,'fixture bank','bank','TX-MAIN','native-review','admin-payout-fixture');
    if (result->>'id')::uuid<>wd then raise exception 'Repeated confirmation created another withdrawal';end if;
    result:=commerce_rpc.record_partner_payout(888060,888061,amount,'fixture bank','bank','tx-main','new-review-after-lost-ack','admin-payout-reentered');
    if (result->>'id')::uuid<>wd then raise exception 'Re-entered receipt created another withdrawal';end if;
    begin
        perform commerce_rpc.record_partner_payout(888060,888061,amount+1,'fixture bank','bank','TX-MAIN','native-review','admin-payout-fixture');
        raise exception 'Changed amount accepted on replay';
    exception when invalid_parameter_value then null;end;
    begin
        perform commerce_rpc.record_partner_payout(888060,888062,amount,'fixture bank','bank','TX-MAIN','native-review','other-partner-key');
        raise exception 'Receipt assigned to another partner';
    exception when invalid_parameter_value then null;end;
    result:=commerce_rpc.respond_withdrawal_receipt(receipt,888061,true,'native-partner-confirm');
    if result->'receipt'->>'state'<>'confirmed' then raise exception 'Partner confirmation missing';end if;
    if (select to_jsonb(w) from commerce.wallets w where id=wid) is distinct from saved_wallet
       or (select count(*) from commerce.ledger_entries)<>entries
       or (select count(*) from commerce.withdrawals)<>count_withdrawals then
        raise exception 'Replay or acknowledgement duplicated money';end if;
    result:=commerce_rpc.record_partner_payout(888060,888061,1000000,'fixture bank','bank','TOO-MUCH','native-review','admin-payout-too-much');
    if result->>'state'<>'insufficient_balance' then raise exception 'Overspend accepted';end if;
    if (select count(*) from commerce.withdrawals)<>count_withdrawals then raise exception 'Overspend created withdrawal';end if;
    begin
        perform commerce_rpc.record_partner_payout(888060,888061,'NaN'::numeric,'fixture bank','bank','NAN','native-review','admin-payout-nan');
        raise exception 'NaN money accepted';
    exception when invalid_parameter_value then null;end;
    -- Deactivation must not confiscate earned available funds; fractions survive.
    update commerce.partners set state='pending' where id=pid;
    result:=commerce_rpc.record_partner_payout(888060,888061,0.001,'fixture bank','bank','TINY','native-review','admin-payout-tiny');
    if (result->>'amount_minor')::numeric<>0.001 or result->>'state'<>'paid'
       or not exists(select 1 from commerce.wallets where id=wid and withdrawn_minor=1000.126 and reserved_minor=250) then
        raise exception 'Tiny payout rounded or inactive partner lost earnings';end if;
    update commerce.support_actors set active=false where telegram_user_id=888060;
    begin
        perform commerce_rpc.record_partner_payout(888060,888061,1,'fixture bank','bank','REVOKED','native-review','admin-payout-revoked');
        raise exception 'Revoked admin registered payout';
    exception when insufficient_privilege then null;end;
    if has_function_privilege('anon','commerce_rpc.record_partner_payout(bigint,bigint,numeric,text,text,text,text,text)','EXECUTE')
       or has_function_privilege('authenticated','commerce_rpc.record_partner_payout(bigint,bigint,numeric,text,text,text,text,text)','EXECUTE') then
        raise exception 'Public role can execute payout';end if;
end $$;
rollback;
