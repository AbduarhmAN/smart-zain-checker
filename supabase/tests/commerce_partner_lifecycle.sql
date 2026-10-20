\set ON_ERROR_STOP on
begin;
set local timezone='Asia/Baghdad';
do $$
declare pid uuid;uid uuid;added uuid;activated uuid;ended uuid;oid uuid;new_order uuid;version text;policy bigint;
    result jsonb;event jsonb;notice jsonb;next_notice jsonb;wallet_snapshot jsonb;count_events bigint;count_entries bigint;
    original_time timestamptz;original_hash text;claim_token uuid;
begin
    insert into commerce.support_actors(telegram_user_id,active,selection_reference) values(889100,true,'isolated lifecycle administrator');
    begin perform commerce_rpc.add_partner_with_notice(889101,889101,'LIFECYCLE_FORGED');raise exception 'Customer added partner';exception when insufficient_privilege then null;end;
    result:=commerce_rpc.add_partner_with_notice(889100,889101,'LIFECYCLE_PARTNER');
    pid:=(result->>'partner_id')::uuid;added:=(result->>'notice_event_id')::uuid;
    event:=commerce_rpc.partner_lifecycle_event(added,889100);
    if event->>'kind'<>'added' or event->>'body' not like '%قيد استكمال الإعداد%'
       or event->>'body' not like '%UTC%' or (event->>'actor_telegram_id')::bigint<>889100
       or event->>'body_sha256'<>encode(sha256(convert_to(event->>'body','UTF8')),'hex') then raise exception 'Addition notice lacks honest state or exact text evidence';end if;
    if (commerce_rpc.add_partner_with_notice(889100,889101,'LIFECYCLE_PARTNER')->>'notice_event_id')::uuid<>added then raise exception 'Addition replay changed event';end if;
    if (commerce_rpc.create_partner(889101,'LIFECYCLE_PARTNER')->>'notice_event_id')::uuid<>added then raise exception 'Legacy addition bypassed notice idempotency';end if;
    result:=commerce_rpc.configure_partner(pid,889100,3000,'forever',null,true);
    version:=result->>'version';activated:=(result->>'notice_event_id')::uuid;
    if activated is null then raise exception 'Activation notice missing';end if;
    perform commerce_rpc.configure_partner(pid,889100,3000,'forever',null,true);
    if (select count(*) from commerce.partner_lifecycle_events where partner_id=pid and kind='activated')<>1 then raise exception 'Activation replay duplicated message';end if;
    select user_id into uid from commerce.partners where id=pid;
    -- A paid referred order retains its frozen entitlement after retirement.
    perform commerce_rpc.attribute_referral(889102,'LIFECYCLE_PARTNER','manual');
    perform commerce_rpc.configure_customer_pricing(889100,889102,'{"base_minor":1000,"service_excess_minor":1000,"service_threshold":500}','lifecycle-customer-price');
    oid:=(commerce_rpc.create_order(889102,'lifecycle-old-order','[{"row_number":2,"check_type":"account"}]',jsonb_build_object('file_sha256',repeat('a',64)))->>'order_id')::uuid;
    select (pricing_snapshot->>'policy_version')::bigint into policy from commerce.orders where id=oid;
    perform commerce_rpc.confirm_receipt(889102,'lifecycle-topup','fixture','lifecycle-topup',1000,'fixture-proof');
    perform commerce_rpc.checkout_consent(oid,889102,policy,'lifecycle-consent');
    perform commerce_rpc.reserve_order(oid,889102,'lifecycle-consent');
    perform commerce_rpc.bind_checker_job(oid,'lifecycle-old-job');
    select jsonb_agg(to_jsonb(w) order by w.id) into wallet_snapshot from commerce.wallets w;
    select count(*) into count_entries from commerce.ledger_entries;
    result:=commerce_rpc.partner_end_preview(889100,pid);
    if result->>'preview' not like '%لا يمثل مخالصة مالية%' then raise exception 'End preview erased financial rights';end if;
    begin perform commerce_rpc.end_partner(889101,pid,version,'forged-end');raise exception 'Partner ended own role as administrator';exception when insufficient_privilege then null;end;
    begin perform commerce_rpc.end_partner(889100,pid,'stale-version','stale-end');raise exception 'Stale terms end accepted';exception when invalid_parameter_value then null;end;
    event:=commerce_rpc.end_partner(889100,pid,version,'native-end-reference');ended:=(event->>'id')::uuid;
    original_time:=(event->>'occurred_at')::timestamptz;original_hash:=event->>'body_sha256';
    if event->>'kind'<>'ended' or (select ended_at from commerce.partners where id=pid)<>original_time
       or event->>'body' not like '%'||to_char(original_time at time zone 'UTC','YYYY-MM-DD HH24:MI:SS')||' UTC%'
       or exists(select 1 from commerce.codes where partner_id=pid and kind='referral' and active) then raise exception 'Role or UTC notice not retired consistently';end if;
    if (select jsonb_agg(to_jsonb(w) order by w.id) from commerce.wallets w) is distinct from wallet_snapshot
       or (select count(*) from commerce.ledger_entries)<>count_entries then raise exception 'Ending partnership moved or erased money';end if;
    if exists(select 1 from jsonb_array_elements(commerce_rpc.list_partners()) item where item->>'id'=pid::text) then raise exception 'Ended partner remains in current list';end if;
    if not exists(select 1 from jsonb_array_elements(commerce_rpc.ended_partners(889100)) item where item->>'id'=pid::text and item->>'event_id'=ended::text) then raise exception 'Ended partner history missing';end if;
    result:=commerce_rpc.partner_finance(889101);
    if result->>'is_partner'<>'true' or result->>'partnership_active'<>'false' or result->>'partnership_end_event_id'<>ended::text then raise exception 'Historical financial account was lost or active role retained';end if;
    result:=commerce_rpc.wallet_state(889101);
    if result->'partner'->>'partnership_ended_at' is null then raise exception 'Wallet hides historical role state';end if;
    begin perform commerce_rpc.configure_partner(pid,889100,3000,'forever',null,true);raise exception 'Stale activation resurrected ended role';exception when invalid_parameter_value then null;end;
    result:=commerce_rpc.create_partner(889101,'LIFECYCLE_PARTNER');
    if result->>'partnership_ended'<>'true' then raise exception 'Repeated addition silently reactivated role';end if;
    if commerce_rpc.attribute_referral(889103,'LIFECYCLE_PARTNER','manual')->>'status'='attributed' then raise exception 'Ended referral accepted';end if;
    new_order:=(commerce_rpc.create_order(889102,'lifecycle-new-order','[{"row_number":2,"check_type":"account"}]',jsonb_build_object('file_sha256',repeat('c',64)))->>'order_id')::uuid;
    if (select partner_terms_snapshot->>'state' from commerce.orders where id=new_order)='active' then raise exception 'New order gained active partner terms';end if;
    event:=commerce_rpc.end_partner(889100,pid,version,'new-click-same-end');
    if (event->>'id')::uuid<>ended or (event->>'occurred_at')::timestamptz<>original_time or event->>'body_sha256'<>original_hash then raise exception 'End replay replaced original evidence';end if;
    -- Earlier paid work still settles and creates its frozen earned commission.
    perform commerce_rpc.record_terminal_results(oid,'lifecycle-old-job','[{"row_number":2,"state":"succeeded","verified_result":true}]',repeat('b',64));
    perform commerce_rpc.confirm_result_delivery(oid,889102,123,'lifecycle-result',repeat('b',64));
    perform commerce_rpc.settle_order(oid);
    if (commerce_rpc.partner_finance(889101)->>'available_minor')::numeric<>300 then raise exception 'Retirement forfeited earlier earned commission';end if;
    begin perform commerce_rpc.partner_lifecycle_event(ended,889104);raise exception 'Other customer read private notice';exception when insufficient_privilege then null;end;
    begin perform commerce_rpc.acknowledge_partner_notice(ended,889100,'admin-forged-ack');raise exception 'Administrator acknowledged for partner';exception when insufficient_privilege then null;end;
    event:=commerce_rpc.acknowledge_partner_notice(ended,889101,'native-partner-ack');
    original_time:=(event->>'acknowledged_at')::timestamptz;
    event:=commerce_rpc.acknowledge_partner_notice(ended,889101,'second-native-ack');
    if (event->>'acknowledged_at')::timestamptz<>original_time then raise exception 'Acknowledgement replay replaced first response';end if;
    if (select count(*) from commerce.partner_lifecycle_notifications where event_id=ended and kind='ack')<>1 then raise exception 'Repeated acknowledgement duplicated channel notice';end if;
    -- Waiting channel notices route when the existing documentation channel is configured.
    perform commerce_rpc.configure_payout_notice_channels(889100,'[-100222]');
    if exists(select 1 from commerce.partner_lifecycle_notifications where state='waiting_config') then raise exception 'Channel routing stranded notices';end if;
    if not exists(select 1 from commerce.partner_lifecycle_notifications where event_id=ended and kind='ack' and recipient_chat_id=-100222
        and body like '%لا يثبت مخالصة مالية%') then raise exception 'Channel acknowledgement audit missing';end if;
    notice:=commerce_rpc.claim_partner_lifecycle_notification();claim_token:=(notice->>'token')::uuid;
    result:=commerce_rpc.complete_partner_lifecycle_notification((notice->>'id')::uuid,gen_random_uuid(),'sent',99,extract(epoch from now())::bigint);
    if result->>'saved'<>'false' then raise exception 'Wrong delivery lease altered evidence';end if;
    result:=commerce_rpc.complete_partner_lifecycle_notification((notice->>'id')::uuid,claim_token,'sent',99,extract(epoch from now())::bigint);
    if result->>'saved'<>'true' or not exists(select 1 from commerce.partner_lifecycle_delivery_attempts where token=claim_token and state='sent' and telegram_message_id=99 and completed_at is not null) then raise exception 'Native Telegram delivery evidence missing';end if;
    next_notice:=commerce_rpc.claim_partner_lifecycle_notification();
    perform commerce_rpc.complete_partner_lifecycle_notification((next_notice->>'id')::uuid,(next_notice->>'token')::uuid,'uncertain');
    perform commerce_rpc.claim_partner_lifecycle_notification();
    if (select state from commerce.partner_lifecycle_notifications where id=(next_notice->>'id')::uuid)<>'uncertain' then raise exception 'Ambiguous notice was blindly resent';end if;
    if has_function_privilege('anon','commerce_rpc.end_partner(bigint,uuid,text,text)','EXECUTE')
       or has_function_privilege('service_role','commerce.create_partner_before_lifecycle(bigint,text)','EXECUTE')
       or has_table_privilege('service_role','commerce.partner_lifecycle_events','UPDATE') then raise exception 'Lifecycle audit bypass privileges exposed';end if;
end $$;
rollback;
