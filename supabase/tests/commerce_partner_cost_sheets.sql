\set ON_ERROR_STOP on
begin;
do $$
declare pid uuid;refpid uuid;uid uuid;oid uuid;second_order uuid;rejected_order uuid;old_order uuid;
    v1 bigint;v2 bigint;policy bigint;result jsonb;notice jsonb;price numeric;earned numeric;refunded numeric;
    rows jsonb:='[{"row_number":2,"check_type":"account"},{"row_number":3,"check_type":"account"},{"row_number":4,"check_type":"service"}]';
    source jsonb:=jsonb_build_object('file_sha256',repeat('a',64),'original_filename','partner.xlsx');
    total bigint;anchor timestamptz;
begin
    insert into commerce.support_actors(telegram_user_id,active,selection_reference) values(889050,true,'isolated sheet admin');
    -- Preserve a quote made before this customer became a partner.
    old_order:=(commerce_rpc.create_order(889051,'sheet-old-quote',rows,source)->>'order_id')::uuid;
    select quoted_minor into price from commerce.orders where id=old_order;
    pid:=(commerce_rpc.create_partner(889051,'SHEET_PARTNER')->>'partner_id')::uuid;
    refpid:=(commerce_rpc.create_partner(889052,'SHEET_REFERRER')->>'partner_id')::uuid;
    perform commerce_rpc.configure_partner(pid,889050,3000,'forever',null,true);
    perform commerce_rpc.configure_partner(refpid,889050,3000,'forever',null,true);
    perform commerce_rpc.attribute_referral(889051,'SHEET_REFERRER','manual');
    select id into uid from commerce.users where telegram_user_id=889051;
    if not (commerce_rpc.partner_sheet_context(889051)->>'is_partner')::boolean then raise exception 'Active partner context missing';end if;
    begin perform commerce_rpc.configure_partner_sheet_price(889051,1500.5,'forged-price');raise exception 'Partner changed base price';
        exception when insufficient_privilege then null;end;
    begin perform commerce_rpc.configure_partner_sheet_price(889050,0,'zero-price');raise exception 'Unset/free price accepted';
        exception when invalid_parameter_value then null;end;
    result:=commerce_rpc.configure_partner_sheet_price(889050,1500.5,'sheet-price-v1');v1:=(result->>'version')::bigint;
    if (result->>'recipients')::integer<>2 then raise exception 'Price notifications missed active partners';end if;
    perform commerce_rpc.configure_partner_sheet_price(889050,1500.5,'sheet-price-v1');
    if (select count(*) from commerce.partner_sheet_notifications where event_key='price:'||v1)<>2 then raise exception 'Replayed price duplicated notices';end if;
    begin perform commerce_rpc.configure_partner_sheet_price(889050,1600,'sheet-price-v1');raise exception 'Conflicting price replay accepted';
        exception when invalid_parameter_value then null;end;
    oid:=(commerce_rpc.create_order(889051,'sheet-awaiting-approval',rows,source)->>'order_id')::uuid;
    result:=commerce_rpc.checkout_state(oid,889051);
    if result->'partner_sheet'->>'state'<>'pending' or (result->>'quoted_minor')::numeric<>0
       or (result->>'requested_checks')::integer<>3 or exists(select 1 from commerce.order_discounts where order_id=oid) then
        raise exception 'Partner sheet bypassed pending state or acquired discounts';end if;
    policy:=(result->'pricing_snapshot'->>'policy_version')::bigint;
    perform commerce_rpc.checkout_consent(oid,889051,policy,'sheet-consent');
    begin perform commerce_rpc.reserve_order(oid,889051,'sheet-consent');raise exception 'Unapproved sheet funded';
        exception when insufficient_privilege then null;end;
    begin perform commerce_rpc.decide_partner_sheet(889051,oid,true,v1,'forged-approve');raise exception 'Partner self-approved';
        exception when insufficient_privilege then null;end;
    begin perform commerce_rpc.bind_checker_job(oid,'sheet-forged-job');raise exception 'Unfunded sheet bound';
        exception when invalid_parameter_value then null;end;
    -- Database triggers also protect the payment-request and reservation boundary.
    begin
        perform commerce.require_approved_partner_sheet(oid);raise exception 'Pending funding guard passed';
        exception when insufficient_privilege then null;end;
    begin
        insert into commerce.payment_requests(id,user_id,order_id,provider,receiver,quote)
            values(gen_random_uuid(),uid,oid,'cryptopay','fixture','{}');
        raise exception 'Unapproved invoice request inserted';exception when insufficient_privilege then null;end;
    begin
        insert into commerce.reservations(order_id,wallet_id,amount_minor,idempotency_key)
            select oid,id,0,'forged-reservation' from commerce.wallets where user_id=uid and kind='customer';
        raise exception 'Unapproved reservation inserted';exception when insufficient_privilege then null;end;
    perform commerce_rpc.decide_partner_sheet(889050,oid,true,v1,'sheet-approval');
    if (select sum(unit_quote_minor) from commerce.checks where order_id=oid)<>1500.5
       or (select quoted_minor from commerce.orders where id=oid)<>1500.5 then raise exception 'Flat sheet amount was multiplied by rows';end if;
    result:=commerce_rpc.configure_partner_sheet_price(889050,2200,'sheet-price-v2');v2:=(result->>'version')::bigint;
    if (select quoted_minor from commerce.orders where id=oid)<>1500.5 then raise exception 'Settings repriced approved request';end if;
    perform commerce_rpc.decide_partner_sheet(889050,oid,true,v1,'approval-double-click');
    if (select reference from commerce.partner_sheet_requests where order_id=oid)<>'sheet-approval' then raise exception 'Replay overwrote approval evidence';end if;
    begin perform commerce_rpc.decide_partner_sheet(889050,oid,false,v2,'approval-opposite');raise exception 'Opposite final decision accepted';
        exception when invalid_parameter_value then null;end;
    second_order:=(commerce_rpc.create_order(889051,'sheet-second-needs-approval',rows,source)->>'order_id')::uuid;
    begin perform commerce_rpc.decide_partner_sheet(889050,second_order,true,v1,'stale-price');raise exception 'Stale price approval accepted';
        exception when invalid_parameter_value then null;end;
    if (select state from commerce.partner_sheet_requests where order_id=second_order)<>'pending' then raise exception 'Approval carried to another sheet';end if;
    perform commerce_rpc.decide_partner_sheet(889050,second_order,true,v2,'second-approved');
    if (select quoted_minor from commerce.orders where id=second_order)<>2200 then raise exception 'New sheet did not use reviewed current price';end if;
    rejected_order:=(commerce_rpc.create_order(889051,'sheet-rejected',rows,source)->>'order_id')::uuid;
    perform commerce_rpc.decide_partner_sheet(889050,rejected_order,false,null,'sheet-reject');
    if (select order_state from commerce.orders where id=rejected_order)<>'cancelled' then raise exception 'Rejection did not close sheet';end if;
    begin perform commerce.require_approved_partner_sheet(rejected_order);raise exception 'Rejected funding guard passed';
        exception when insufficient_privilege then null;end;
    -- Real reserve, result, delivery and settlement flow; one technical failure.
    perform commerce_rpc.confirm_receipt(889051,'sheet-funding','fixture','sheet-funding',10000,'fixture-proof');
    perform commerce_rpc.reserve_order(oid,889051,'sheet-consent');
    perform commerce_rpc.reserve_order(oid,889051,'sheet-consent');
    if (select amount_minor from commerce.reservations where order_id=oid)<>1500.5
       or (select promotional_minor from commerce.reservations where order_id=oid)<>0 then raise exception 'Reserved wrong flat price or used promotion';end if;
    perform commerce_rpc.bind_checker_job(oid,'sheet-real-job');
    perform commerce_rpc.record_terminal_results(oid,'sheet-real-job','[{"row_number":2,"state":"succeeded","verified_result":true},{"row_number":3,"state":"not_found","verified_result":true},{"row_number":4,"state":"failed","verified_result":false}]',repeat('b',64));
    perform commerce_rpc.confirm_result_delivery(oid,889051,1001,'sheet-telegram-result',repeat('b',64));
    select sum(unit_quote_minor) filter(where verified_result),sum(unit_quote_minor) filter(where not verified_result)
        into earned,refunded from commerce.checks where order_id=oid;
    result:=commerce_rpc.settle_order(oid);
    if (result->>'charged_minor')::numeric<>earned or (result->>'refunded_minor')::numeric<>refunded
       or earned+refunded<>1500.5 then raise exception 'Settlement lost or created flat-price money';end if;
    if exists(select 1 from commerce.commissions where order_id=oid)
       or (select referral_partner_id from commerce.orders where id=oid) is not null
       or (select first_paid_service_at from commerce.users where id=uid) is not null
       or (select successful_paid_checks from commerce.users where id=uid)<>0
       or (select locked_at from commerce.referrals where user_id=uid) is not null then raise exception 'Partner cost earned commission or consumed referral eligibility';end if;
    if (select sum(delta_minor) from commerce.ledger_entries where journal_id=(select id from commerce.journals where idempotency_key='service:'||oid) and account_key='platform:revenue') is distinct from earned then
        raise exception 'No platform service revenue';end if;
    select count(*) into total from commerce.ledger_entries;
    perform commerce_rpc.settle_order(oid);
    if (select count(*) from commerce.ledger_entries)<>total then raise exception 'Settlement replay duplicated money';end if;
    -- The flat base price is payable from real money, not promotional credit.
    update commerce.wallets set promotional_available_minor=available_minor where user_id=uid and kind='customer';
    select (pricing_snapshot->>'policy_version')::bigint into policy from commerce.orders where id=second_order;
    perform commerce_rpc.checkout_consent(second_order,889051,policy,'second-sheet-consent');
    result:=commerce_rpc.reserve_order(second_order,889051,'second-sheet-consent');
    if result->>'state'<>'insufficient_balance' or exists(select 1 from commerce.reservations where order_id=second_order) then
        raise exception 'Promotional credit paid partner base cost';end if;
    if (commerce_rpc.create_order(889051,'sheet-old-quote',rows,source)->>'quoted_minor')::numeric<>price
       or exists(select 1 from commerce.partner_sheet_requests where order_id=old_order) then raise exception 'Pre-existing quote contract was replaced';end if;
    -- Claims survive restart; uncertain sends require deliberate review.
    notice:=commerce_rpc.claim_partner_sheet_notification();
    perform commerce_rpc.complete_partner_sheet_notification((notice->>'id')::uuid,(notice->>'token')::uuid,'uncertain',null);
    if (select state from commerce.partner_sheet_notifications where id=(notice->>'id')::uuid)<>'uncertain' then raise exception 'Ambiguous notification lost';end if;
    begin perform commerce_rpc.partner_sheet_requests(889051);raise exception 'Partner read admin queue';exception when insufficient_privilege then null;end;
    if has_function_privilege('anon','commerce_rpc.decide_partner_sheet(bigint,uuid,boolean,bigint,text)','EXECUTE')
       or has_function_privilege('service_role','commerce.create_order_with_campaigns(bigint,text,jsonb,jsonb)','EXECUTE')
       or has_table_privilege('service_role','commerce.partner_sheet_requests','UPDATE') then raise exception 'Approval bypass privileges exposed';end if;
end $$;
rollback;
