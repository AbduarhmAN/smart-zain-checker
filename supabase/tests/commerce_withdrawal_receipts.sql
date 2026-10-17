\set ON_ERROR_STOP on
begin;
do $$
declare pid uuid;wid uuid;wd uuid;wd2 uuid;unpaid uuid;receipt_id uuid;receipt2 uuid;result jsonb;
        before_wallet jsonb;after_wallet jsonb;before_count bigint;first_response text;first_time timestamptz;
begin
    insert into commerce.support_actors(telegram_user_id,active,selection_reference) values(888040,true,'isolated payout admin');
    pid:=(commerce_rpc.create_partner(888041,'RECEIPT_FIXTURE')->>'partner_id')::uuid;
    perform commerce_rpc.configure_partner(pid,888040,3000,'forever',null,true);
    select w.id into wid from commerce.wallets w join commerce.partners p on p.user_id=w.user_id where p.id=pid and w.kind='partner';
    update commerce.wallets set available_minor=10000 where id=wid;
    wd:=(commerce_rpc.request_withdrawal(888041,'receipt-wd-one',1000.125,'fixture bank account')->>'withdrawal_id')::uuid;
    begin perform commerce_rpc.prepare_withdrawal_receipt(wd,888040,'too-early');raise exception 'Unpaid acknowledgement accepted';
        exception when invalid_parameter_value then null;end;
    perform commerce_rpc.approve_withdrawal(wd,888040,'native-approve-one');
    perform commerce_rpc.complete_withdrawal(wd,888040,'bank','RECEIPT-TX-ONE','native-payment-one');
    wd2:=(commerce_rpc.request_withdrawal(888041,'receipt-wd-two',2000,'fixture second account')->>'withdrawal_id')::uuid;
    perform commerce_rpc.approve_withdrawal(wd2,888040,'native-approve-two');
    perform commerce_rpc.complete_withdrawal(wd2,888040,'bank','RECEIPT-TX-TWO','native-payment-two');
    unpaid:=(commerce_rpc.request_withdrawal(888041,'receipt-wd-unpaid',500,'fixture unpaid account')->>'withdrawal_id')::uuid;
    select to_jsonb(w) into before_wallet from commerce.wallets w where id=wid;
    select count(*) into before_count from commerce.ledger_entries;
    begin perform commerce_rpc.prepare_withdrawal_receipt(wd,888042,'foreign-admin');raise exception 'Customer prepared acknowledgement';
        exception when insufficient_privilege then null;end;
    result:=commerce_rpc.prepare_withdrawal_receipt(wd,888040,'native-request-one');
    receipt_id:=(result->'receipt'->>'id')::uuid;
    if result->'receipt'->>'state'<>'requested' or (result->'receipt'->>'amount_minor')::numeric<>1000.125
       or result->'receipt'->>'payout_reference'<>'bank:RECEIPT-TX-ONE' then raise exception 'Receipt snapshot differs from actual paid withdrawal';end if;
    if (commerce_rpc.prepare_withdrawal_receipt(wd,888040,'native-request-replay')->'receipt'->>'id')::uuid<>receipt_id then
        raise exception 'Request replay duplicated receipt';end if;
    begin perform commerce_rpc.partner_withdrawal_receipt(wd,888042);raise exception 'Other customer read payout';
        exception when insufficient_privilege then null;end;
    begin perform commerce_rpc.respond_withdrawal_receipt(receipt_id,888040,true,'admin-cannot-accept');raise exception 'Admin accepted for partner';
        exception when insufficient_privilege then null;end;
    begin perform commerce_rpc.respond_withdrawal_receipt(receipt_id,888042,true,'other-customer-cannot-accept');raise exception 'Other customer accepted receipt';
        exception when insufficient_privilege then null;end;
    begin perform commerce_rpc.respond_withdrawal_receipt(receipt_id,888041,true,'');raise exception 'Unauthenticated response accepted';
        exception when invalid_parameter_value then null;end;
    result:=commerce_rpc.respond_withdrawal_receipt(receipt_id,888041,true,'native-owner-confirm');
    if result->'receipt'->>'state'<>'confirmed' or (result->'receipt'->>'response_actor_id')::bigint<>888041 then
        raise exception 'Owner confirmation not persisted';end if;
    first_response:=result->'receipt'->>'response_reference';first_time:=(result->'receipt'->>'responded_at')::timestamptz;
    result:=commerce_rpc.respond_withdrawal_receipt(receipt_id,888041,true,'second-click-same-decision');
    if result->'receipt'->>'response_reference'<>first_response or (result->'receipt'->>'responded_at')::timestamptz<>first_time then
        raise exception 'Replay overwrote original evidence';end if;
    begin perform commerce_rpc.respond_withdrawal_receipt(receipt_id,888041,false,'later-contradiction');raise exception 'Final evidence overwritten';
        exception when invalid_parameter_value then null;end;
    result:=commerce_rpc.prepare_withdrawal_receipt(wd2,888040,'native-request-two');receipt2:=(result->'receipt'->>'id')::uuid;
    result:=commerce_rpc.respond_withdrawal_receipt(receipt2,888041,false,'native-owner-dispute');
    if result->'receipt'->>'state'<>'disputed' or result->>'state'<>'paid' then raise exception 'Dispute fabricated payment/refund state';end if;
    result:=commerce_rpc.paid_withdrawal_receipts(888040,0,1);
    if (result->>'total')::integer<>2 or jsonb_array_length(result->'items')<>1 then raise exception 'Paid receipt pagination included unpaid requests';end if;
    if not exists(select 1 from jsonb_array_elements(commerce_rpc.partner_withdrawals(888041)) item
        where item->>'receipt_id'=receipt_id::text and item->>'receipt_state'='confirmed') then raise exception 'Partner history lost durable receipt';end if;
    select to_jsonb(w) into after_wallet from commerce.wallets w where id=wid;
    if before_wallet is distinct from after_wallet or (select count(*) from commerce.ledger_entries)<>before_count then
        raise exception 'Acknowledgement moved or duplicated money';end if;
    update commerce.support_actors set active=false where telegram_user_id=888040;
    begin perform commerce_rpc.paid_withdrawal_receipts(888040);raise exception 'Revoked admin read receipts';
        exception when insufficient_privilege then null;end;
    if has_function_privilege('anon','commerce_rpc.respond_withdrawal_receipt(uuid,bigint,boolean,text)','EXECUTE')
       or has_function_privilege('authenticated','commerce_rpc.prepare_withdrawal_receipt(uuid,bigint,text)','EXECUTE')
       or has_table_privilege('service_role','commerce.withdrawal_receipts','UPDATE') then
        raise exception 'Receipt evidence bypasses controlled functions';end if;
end $$;
rollback;
