\set ON_ERROR_STOP on
begin;
do $$
declare pid uuid;wid uuid;wd uuid;receipt uuid;result jsonb;notice jsonb;claim_id uuid;claim_token uuid;
        balance jsonb;ledger_count bigint;counter integer;wd2 uuid;wd3 uuid;
begin
    insert into commerce.support_actors(telegram_user_id,active,selection_reference) values(888070,true,'isolated payout notices admin');
    pid:=(commerce_rpc.create_partner(888071,'PAYOUT_NOTICE_FIXTURE')->>'partner_id')::uuid;
    select w.id into wid from commerce.wallets w join commerce.partners p on p.user_id=w.user_id where p.id=pid and w.kind='partner';
    update commerce.wallets set available_minor=10000 where id=wid;
    perform commerce_rpc.configure_payout_notice_channels(888070,'[-100001]');
    begin perform commerce_rpc.configure_payout_notice_channels(888071,'[-100002]');raise exception 'Partner changed channel routing';
        exception when insufficient_privilege then null;end;
    begin perform commerce_rpc.configure_payout_notice_channels(888070,'[888071]');raise exception 'Private customer used as channel';
        exception when invalid_parameter_value then null;end;
    result:=commerce_rpc.record_partner_payout(888070,888071,1000.125,'private bank destination','bank','NOTICE-ONE','native-notice-proof','notice-paid-one');
    wd:=(result->>'id')::uuid;receipt:=(result->'receipt'->>'id')::uuid;
    if (select count(*) from commerce.payout_notifications where withdrawal_id=wd and kind='paid')<>2 then
        raise exception 'Partner and channel notices were not committed with payout';end if;
    if not exists(select 1 from commerce.payout_notifications where withdrawal_id=wd and recipient_kind='partner' and recipient_chat_id=888071)
       or not exists(select 1 from commerce.payout_notifications where withdrawal_id=wd and recipient_kind='channel' and recipient_chat_id=-100001) then
        raise exception 'Payout notification destination differs from owner/configuration';end if;
    perform commerce_rpc.record_partner_payout(888070,888071,1000.125,'private bank destination','bank','NOTICE-ONE','native-notice-proof','notice-paid-one');
    if (select count(*) from commerce.payout_notifications where withdrawal_id=wd)<>2 then raise exception 'Payout replay duplicated notices';end if;
    select to_jsonb(w) into balance from commerce.wallets w where id=wid;
    select count(*) into ledger_count from commerce.ledger_entries;
    perform commerce_rpc.respond_withdrawal_receipt(receipt,888071,true,'native-notice-confirm');
    perform commerce_rpc.respond_withdrawal_receipt(receipt,888071,true,'native-notice-confirm-again');
    if (select count(*) from commerce.payout_notifications where withdrawal_id=wd and kind='confirmed')<>2 then
        raise exception 'Partner response was lost or duplicated';end if;
    if (select to_jsonb(w) from commerce.wallets w where id=wid) is distinct from balance
       or (select count(*) from commerce.ledger_entries)<>ledger_count then raise exception 'Notification evidence moved money';end if;
    notice:=commerce_rpc.claim_payout_notification();claim_id:=(notice->>'id')::uuid;claim_token:=(notice->>'token')::uuid;
    result:=commerce_rpc.complete_payout_notification(claim_id,gen_random_uuid(),'sent',100);
    if (result->>'saved')::boolean then raise exception 'Stale claim changed delivery evidence';end if;
    perform commerce_rpc.complete_payout_notification(claim_id,claim_token,'sent',100);
    loop
        notice:=commerce_rpc.claim_payout_notification();exit when notice is null;
        perform commerce_rpc.complete_payout_notification((notice->>'id')::uuid,(notice->>'token')::uuid,'sent',101);
    end loop;
    if (select count(*) from commerce.payout_notifications where state='sent')<>4 then raise exception 'Sent state not persisted';end if;
    -- Known rejections may retry three times. Ambiguous acceptance never retries blindly.
    perform commerce_rpc.configure_payout_notice_channels(888070,'[]');
    result:=commerce_rpc.record_partner_payout(888070,888071,100,'private second destination','bank','NOTICE-TWO','native-proof-two','notice-paid-two');
    wd2:=(result->>'id')::uuid;
    if not exists(select 1 from commerce.payout_notifications where withdrawal_id=wd2 and state='waiting_config') then
        raise exception 'Missing channel discarded durable notification';end if;
    for counter in 1..3 loop
        notice:=commerce_rpc.claim_payout_notification();
        if notice is null or (notice->>'attempts')::integer<>counter then raise exception 'Rejection retry attempts not bounded';end if;
        perform commerce_rpc.complete_payout_notification((notice->>'id')::uuid,(notice->>'token')::uuid,'rejected');
        update commerce.payout_notifications set next_attempt_at=now() where id=(notice->>'id')::uuid;
    end loop;
    if commerce_rpc.claim_payout_notification() is not null then raise exception 'Fourth automatic retry accepted';end if;
    perform commerce_rpc.configure_payout_notice_channels(888070,'[-100002]');
    if (select count(*) from commerce.payout_notifications where withdrawal_id=wd2 and recipient_chat_id=-100002)<>1
       or exists(select 1 from commerce.payout_notifications where withdrawal_id=wd and recipient_chat_id=-100002) then
        raise exception 'Routing lost waiting message or rebroadcast old channel reports';end if;
    notice:=commerce_rpc.claim_payout_notification();
    update commerce.payout_notifications set updated_at=now()-interval '3 minutes' where id=(notice->>'id')::uuid;
    result:=commerce_rpc.claim_payout_notification();
    if result is not null
       or not exists(select 1 from commerce.payout_notifications where id=(notice->>'id')::uuid and state='uncertain') then
        raise exception 'Interrupted delivery: claim_present=%, stored_state=%, attempts=%',result is not null,
            (select state from commerce.payout_notifications where id=(notice->>'id')::uuid),
            (select attempts from commerce.payout_notifications where id=(notice->>'id')::uuid);end if;
    result:=commerce_rpc.record_partner_payout(888070,888071,100,'private third destination','bank','NOTICE-THREE','native-proof-three','notice-paid-three');
    wd3:=(result->>'id')::uuid;receipt:=(result->'receipt'->>'id')::uuid;
    perform commerce_rpc.respond_withdrawal_receipt(receipt,888071,false,'native-owner-dispute');
    if (select count(*) from commerce.payout_notifications where withdrawal_id=wd3 and kind='disputed')<>2 then
        raise exception 'Dispute was not queued for partner and channel';end if;
    if jsonb_array_length(commerce_rpc.payout_notification_status(888070,wd))<>4 then raise exception 'Admin status missing evidence';end if;
    if has_function_privilege('anon','commerce_rpc.claim_payout_notification()','EXECUTE')
       or has_table_privilege('service_role','commerce.payout_notifications','UPDATE') then
        raise exception 'Notice state bypasses controlled functions';end if;
end $$;
rollback;
