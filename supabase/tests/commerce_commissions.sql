\set ON_ERROR_STOP on
begin;
do $$ declare p uuid; duration_p uuid; discount_p uuid; late_p uuid; o uuid; o2 uuid; wd uuid; wd2 uuid;
    r jsonb; w jsonb; anchor timestamptz; source jsonb:=jsonb_build_object('file_sha256',repeat('a',64));
    rows jsonb:='[{"row_number":2,"check_type":"account"},{"row_number":3,"check_type":"service"},{"row_number":4,"check_type":"account"},{"row_number":5,"check_type":"account"}]';
begin
    p:=(commerce_rpc.create_partner(892100,'COMMISSION_COUNT')->>'partner_id')::uuid;
    perform commerce_rpc.configure_partner(p,892900,3000,'checks',2,true);
    perform commerce_rpc.attribute_referral(892101,'COMMISSION_COUNT','manual');
    perform commerce_rpc.confirm_receipt(892101,'commission-topup','fixture','commission-topup',30000,'fixture-proof');
    o:=(commerce_rpc.create_order(892101,'commission-order',rows,source)->>'order_id')::uuid;
    -- Current terms change after quoting; frozen 30% and the first two paid checks win.
    perform commerce_rpc.configure_partner(p,892900,9000,'forever',null,true);
    perform commerce_rpc.reserve_order(o,892101,'fixture-consent');
    perform commerce_rpc.bind_checker_job(o,'commission-job');
    perform commerce_rpc.record_terminal_results(o,'commission-job',
        '[{"row_number":2,"state":"succeeded","verified_result":true},{"row_number":3,"state":"failed","verified_result":false},{"row_number":4,"state":"not_found","verified_result":true},{"row_number":5,"state":"succeeded","verified_result":true}]',repeat('b',64));
    begin
        perform commerce_rpc.settle_order(o);
        raise exception 'Commission was available before confirmed delivery';
    exception when invalid_parameter_value then null; end;
    perform commerce_rpc.confirm_result_delivery(o,892101,10,'fixture-file',repeat('b',64));
    r:=commerce_rpc.settle_order(o);
    if (r->>'commission_minor')::numeric<>1800 or (r->>'charged_minor')::numeric<>9000
       or (r->>'refunded_minor')::numeric<>3000 then raise exception 'Frozen count entitlement or refund basis failed'; end if;
    if (select successful_paid_checks from commerce.users where telegram_user_id=892101)<>3 then
        raise exception 'Count included failed checks or excluded valid negative results'; end if;
    if (commerce_rpc.settle_order(o)->>'commission_minor')::numeric<>1800
       or (select count(*) from commerce.commissions where order_id=o)<>2 then
        raise exception 'Commission replay was not stable'; end if;
    select first_paid_service_at into anchor from commerce.users where telegram_user_id=892101;
    if (commerce_rpc.partner_finance(892100)->>'available_minor')::numeric<>1800 then
        raise exception 'Partner wallet was not credited'; end if;
    if (commerce_rpc.wallet_state(892100)->'customer'->>'available_minor')::numeric<>0 then
        raise exception 'Commission was credited to customer wallet'; end if;

    o2:=(commerce_rpc.create_order(892101,'commission-second','[{"row_number":2,"check_type":"account"}]',source)->>'order_id')::uuid;
    perform commerce_rpc.reserve_order(o2,892101,'fixture-consent-2');
    perform commerce_rpc.bind_checker_job(o2,'commission-job-2');
    perform commerce_rpc.record_terminal_results(o2,'commission-job-2','[{"row_number":2,"state":"succeeded","verified_result":true}]',repeat('c',64));
    perform commerce_rpc.confirm_result_delivery(o2,892101,11,'fixture-file-2',repeat('c',64));
    if (commerce_rpc.settle_order(o2)->>'commission_minor')::numeric<>2700 then
        raise exception 'New order did not use new terms'; end if;
    if (select first_paid_service_at from commerce.users where telegram_user_id=892101)<>anchor
       or (select successful_paid_checks from commerce.users where telegram_user_id=892101)<>4 then
        raise exception 'Entitlement anchor or cumulative count changed incorrectly'; end if;

    wd:=(commerce_rpc.request_withdrawal(892100,'withdraw-fixture',2000,'bank fixture destination')->>'withdrawal_id')::uuid;
    if (commerce_rpc.request_withdrawal(892100,'withdraw-fixture',2000,'bank fixture destination')->>'withdrawal_id')::uuid<>wd then
        raise exception 'Withdrawal replay created another hold'; end if;
    w:=commerce_rpc.partner_finance(892100);
    if (w->>'available_minor')::numeric<>2500 or (w->>'reserved_minor')::numeric<>2000 then
        raise exception 'Withdrawal did not separate available and held earnings'; end if;
    if commerce_rpc.request_withdrawal(892100,'withdraw-overspend',3000,'bank fixture destination')->>'state'<>'insufficient_balance' then
        raise exception 'Partner wallet overspent'; end if;
    begin
        perform commerce_rpc.complete_withdrawal(wd,892900,'bank','TX-001','fixture-proof');
        raise exception 'Unapproved withdrawal was paid';
    exception when invalid_parameter_value then null; end;
    perform commerce_rpc.approve_withdrawal(wd,892900,'fixture-approval');
    perform commerce_rpc.complete_withdrawal(wd,892900,'bank','TX-001','fixture-proof');
    perform commerce_rpc.complete_withdrawal(wd,892901,'bank','TX-001','other-proof');
    w:=commerce_rpc.partner_finance(892100);
    if (w->>'withdrawn_minor')::numeric<>2000 or (w->>'reserved_minor')::numeric<>0 then
        raise exception 'Payout replay changed earnings'; end if;
    begin
        perform commerce_rpc.reject_withdrawal(wd,892900,'after-paid');
        raise exception 'Paid withdrawal was released';
    exception when invalid_parameter_value then null; end;
    wd2:=(commerce_rpc.request_withdrawal(892100,'withdraw-fixture-2',1000,'another bank destination')->>'withdrawal_id')::uuid;
    perform commerce_rpc.approve_withdrawal(wd2,892900,'fixture-approval-2');
    begin
        perform commerce_rpc.complete_withdrawal(wd2,892900,'bank','tx-001','fixture-proof');
        raise exception 'Payout receipt was reused';
    exception when invalid_parameter_value then null; end;
    perform commerce_rpc.reject_withdrawal(wd2,892901,'fixture-rejection');
    perform commerce_rpc.reject_withdrawal(wd2,892902,'replay-rejection');
    if (commerce_rpc.partner_finance(892100)->>'available_minor')::numeric<>2500
       or (commerce_rpc.withdrawal_record(wd2)->>'rejected_actor_id')::bigint<>892901 then
        raise exception 'Rejection release or original actor audit failed'; end if;
    if jsonb_array_length(commerce_rpc.partner_withdrawals(892100))<>2
       or jsonb_array_length(commerce_rpc.partner_withdrawals(892101))<>0 then
        raise exception 'Withdrawal history is missing or exposed to another customer'; end if;

    discount_p:=(commerce_rpc.create_partner(892200,'COMMISSION_DISCOUNT')->>'partner_id')::uuid;
    perform commerce_rpc.configure_partner(discount_p,892900,3333,'forever',null,true);
    perform commerce_rpc.attribute_referral(892201,'COMMISSION_DISCOUNT','manual');
    perform commerce_rpc.confirm_receipt(892201,'discount-topup','fixture','discount-topup',30000,'fixture-proof');
    o:=(commerce_rpc.create_order(892201,'discount-order',rows,source)->>'order_id')::uuid;
    update commerce.checks set discount_minor=case row_number when 2 then 1000 when 4 then 3000 when 5 then 5 else 0 end where order_id=o;
    update commerce.orders set quoted_minor=7995 where id=o;
    perform commerce_rpc.reserve_order(o,892201,'fixture-consent');
    perform commerce_rpc.bind_checker_job(o,'discount-job');
    perform commerce_rpc.record_terminal_results(o,'discount-job',
        '[{"row_number":2,"state":"succeeded","verified_result":true},{"row_number":3,"state":"failed","verified_result":false},{"row_number":4,"state":"not_found","verified_result":true},{"row_number":5,"state":"review","verified_result":true}]',repeat('d',64));
    perform commerce_rpc.confirm_result_delivery(o,892201,12,'fixture-discount-file',repeat('d',64));
    r:=commerce_rpc.settle_order(o);
    if (r->>'commission_minor')::numeric<>1664.8335 or (r->>'charged_minor')::numeric<>4995
       or (select successful_paid_checks from commerce.users where telegram_user_id=892201)<>2 then
        raise exception 'Net collected basis, rounding or free-check exclusion failed'; end if;

    duration_p:=(commerce_rpc.create_partner(892300,'COMMISSION_DURATION')->>'partner_id')::uuid;
    perform commerce_rpc.configure_partner(duration_p,892900,2500,'duration',86400,true);
    perform commerce_rpc.attribute_referral(892301,'COMMISSION_DURATION','manual');
    perform commerce_rpc.confirm_receipt(892301,'duration-topup','fixture','duration-topup',30000,'fixture-proof');
    for i in 1..2 loop
        if i=2 then update commerce.users set first_paid_service_at=now()-interval '2 days' where telegram_user_id=892301; end if;
        o:=(commerce_rpc.create_order(892301,'duration-order-'||i,'[{"row_number":2,"check_type":"account"}]',source)->>'order_id')::uuid;
        perform commerce_rpc.reserve_order(o,892301,'fixture-consent');
        perform commerce_rpc.bind_checker_job(o,'duration-job-'||i);
        perform commerce_rpc.record_terminal_results(o,'duration-job-'||i,'[{"row_number":2,"state":"succeeded","verified_result":true}]',repeat('e',64));
        perform commerce_rpc.confirm_result_delivery(o,892301,20+i,'fixture-duration-file',repeat('e',64));
        r:=commerce_rpc.settle_order(o);
        if (r->>'commission_minor')::numeric<>(case when i=1 then 750 else 0 end) then
            raise exception 'Duration eligibility did not use first paid service'; end if;
    end loop;

    late_p:=(commerce_rpc.create_partner(892400,'COMMISSION_LATE')->>'partner_id')::uuid;
    perform commerce_rpc.configure_partner(late_p,892900,2000,'forever',null,true);
    perform commerce_rpc.confirm_receipt(892401,'late-topup','fixture','late-topup',3000,'fixture-proof');
    o:=(commerce_rpc.create_order(892401,'late-order','[{"row_number":2,"check_type":"account"}]',source)->>'order_id')::uuid;
    perform commerce_rpc.attribute_referral(892401,'COMMISSION_LATE','manual');
    perform commerce_rpc.configure_partner(late_p,892900,7777,'forever',null,true);
    perform commerce_rpc.reserve_order(o,892401,'fixture-consent');
    perform commerce_rpc.bind_checker_job(o,'late-job');
    perform commerce_rpc.record_terminal_results(o,'late-job','[{"row_number":2,"state":"not_found","verified_result":true}]',repeat('f',64));
    perform commerce_rpc.confirm_result_delivery(o,892401,30,'fixture-late-file',repeat('f',64));
    if (commerce_rpc.settle_order(o)->>'commission_minor')::numeric<>600 then
        raise exception 'Late attribution used changed current terms'; end if;

    perform commerce_rpc.attribute_referral(892402,'COMMISSION_LATE','manual');
    perform commerce_rpc.confirm_receipt(892402,'failed-topup','fixture','failed-topup',3000,'fixture-proof');
    o:=(commerce_rpc.create_order(892402,'all-failed-order','[{"row_number":2,"check_type":"account"}]',source)->>'order_id')::uuid;
    perform commerce_rpc.reserve_order(o,892402,'fixture-consent');
    perform commerce_rpc.bind_checker_job(o,'all-failed-job');
    perform commerce_rpc.record_terminal_results(o,'all-failed-job','[{"row_number":2,"state":"failed","verified_result":false}]',repeat('0',64));
    perform commerce_rpc.confirm_result_delivery(o,892402,31,'fixture-failed-file',repeat('0',64));
    r:=commerce_rpc.settle_order(o);
    if (r->>'commission_minor')::numeric<>0 or (r->>'refunded_minor')::numeric<>3000
       or (select first_paid_service_at is not null or successful_paid_checks<>0 from commerce.users where telegram_user_id=892402)
       or (select locked_at is not null from commerce.referrals where user_id=(select id from commerce.users where telegram_user_id=892402)) then
        raise exception 'All technical failures incorrectly consumed paid entitlement'; end if;

    perform commerce_rpc.configure_partner(late_p,892900,0,'forever',null,true);
    o:=(commerce_rpc.create_order(892402,'zero-rate-order','[{"row_number":2,"check_type":"account"}]',source)->>'order_id')::uuid;
    perform commerce_rpc.reserve_order(o,892402,'fixture-consent');
    perform commerce_rpc.bind_checker_job(o,'zero-rate-job');
    perform commerce_rpc.record_terminal_results(o,'zero-rate-job','[{"row_number":2,"state":"succeeded","verified_result":true}]',repeat('1',64));
    perform commerce_rpc.confirm_result_delivery(o,892402,32,'fixture-zero-file',repeat('1',64));
    r:=commerce_rpc.settle_order(o);
    if (r->>'commission_minor')::numeric<>0 or (r->>'eligible_paid_checks')::bigint<>1
       or (select successful_paid_checks from commerce.users where telegram_user_id=892402)<>1 then
        raise exception 'Zero commission incorrectly erased a paid service'; end if;

    if has_function_privilege('anon','commerce_rpc.request_withdrawal(bigint,text,numeric,text)','EXECUTE')
       or has_function_privilege('authenticated','commerce_rpc.complete_withdrawal(uuid,bigint,text,text,text)','EXECUTE')
       or has_function_privilege('service_role','commerce.settle_order(uuid)','EXECUTE') then
        raise exception 'Commission or payout trust boundary is exposed'; end if;
end $$;
rollback;
