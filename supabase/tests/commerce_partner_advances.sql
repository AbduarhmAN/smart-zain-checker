\set ON_ERROR_STOP on
begin;
create function pg_temp.advance_fixture_check(p_customer bigint,p_key text,p_price numeric) returns uuid
language plpgsql as $$
declare oid uuid;policy bigint;
begin
    perform commerce_rpc.configure_customer_pricing(889080,p_customer,
        jsonb_build_object('base_minor',p_price,'service_excess_minor',p_price,'service_threshold',500),'price:'||p_key);
    oid:=(commerce_rpc.create_order(p_customer,p_key,'[{"row_number":2,"check_type":"account"}]',
        jsonb_build_object('file_sha256',repeat('a',64)))->>'order_id')::uuid;
    select (pricing_snapshot->>'policy_version')::bigint into policy from commerce.orders where id=oid;
    perform commerce_rpc.confirm_receipt(p_customer,p_key,'fixture',p_key,p_price,'fixture-proof');
    perform commerce_rpc.checkout_consent(oid,p_customer,policy,'consent:'||p_key);
    perform commerce_rpc.reserve_order(oid,p_customer,'consent:'||p_key);
    perform commerce_rpc.bind_checker_job(oid,'job:'||p_key);
    perform commerce_rpc.record_terminal_results(oid,'job:'||p_key,
        '[{"row_number":2,"state":"succeeded","verified_result":true}]',repeat('b',64));
    perform commerce_rpc.confirm_result_delivery(oid,p_customer,123,'file:'||p_key,repeat('b',64));
    perform commerce_rpc.settle_order(oid);
    return oid;
end $$;
do $$
declare pid uuid;uid uuid;wid uuid;wd uuid;receipt uuid;oid uuid;hold_id uuid;result jsonb;profile jsonb;
    wallet_before jsonb;entries bigint;withdrawals bigint;revenue numeric;recovered numeric;second_wd uuid;
begin
    insert into commerce.support_actors(telegram_user_id,active,selection_reference) values(889080,true,'isolated advance admin');
    pid:=(commerce_rpc.create_partner(889081,'ADVANCE_PARTNER')->>'partner_id')::uuid;
    perform commerce_rpc.configure_partner(pid,889080,7000,'forever',null,true);
    perform commerce_rpc.attribute_referral(889082,'ADVANCE_PARTNER','manual');
    oid:=pg_temp.advance_fixture_check(889082,'advance-earned-seven',1000);
    select p.user_id,w.id into uid,wid from commerce.partners p join commerce.wallets w on w.user_id=p.user_id and w.kind='partner' where p.id=pid;
    profile:=commerce_rpc.partner_finance(889081);
    if (profile->>'available_minor')::numeric<>700 or (profile->>'today_earned_minor')::numeric<>700 then raise exception 'Fixture did not earn real commission of seven pounds';end if;
    select to_jsonb(w) into wallet_before from commerce.wallets w where user_id=uid and kind='customer';
    result:=commerce_rpc.record_partner_payout(889080,889081,1000,'fixture bank','bank','ADVANCE-NORMAL-DENIED','native','normal-excess');
    if result->>'state'<>'insufficient_balance' then raise exception 'Ordinary payout silently allowed debt';end if;
    begin perform commerce_rpc.record_partner_advance_payout(889081,889081,1000,'fixture bank','bank','FORGED','native','forged','دفعة مقدمة',700);
        raise exception 'Partner self-approved advance';exception when insufficient_privilege then null;end;
    begin perform commerce_rpc.record_partner_advance_payout(889080,889081,1000,'fixture bank','bank','NO-REASON','native','no-reason','',700);
        raise exception 'Unexplained debt accepted';exception when invalid_parameter_value then null;end;
    begin perform commerce_rpc.record_partner_advance_payout(889080,889081,1000,'fixture bank','bank','STALE','native','stale','دفعة مقدمة',701);
        raise exception 'Stale financial review accepted';exception when invalid_parameter_value then null;end;
    select coalesce(sum(delta_minor),0) into revenue from commerce.ledger_entries where account_key='platform:revenue';
    result:=commerce_rpc.record_partner_advance_payout(889080,889081,1000,'fixture bank','bank','ADVANCE-TEN','native-confirm','advance-ten','دفعة مقدمة على الأرباح القادمة',700);
    wd:=(result->>'id')::uuid;receipt:=(result->'receipt'->>'id')::uuid;
    if result->>'state'<>'paid' or (result->'advance'->>'amount_minor')::numeric<>300
       or (result->'advance'->>'earned_paid_minor')::numeric<>700
       or (result->'advance'->>'account_after_minor')::numeric<>-300 then raise exception 'Seven earned minus ten paid did not record a three-pound advance';end if;
    profile:=commerce_rpc.partner_finance(889081);
    if (profile->>'account_balance_minor')::numeric<>-300 or (profile->>'available_minor')::numeric<>-300
       or (profile->>'withdrawable_minor')::numeric<>0 or (profile->>'withdrawn_minor')::numeric<>1000
       or (profile->>'advance_outstanding_minor')::numeric<>300
       or profile->'outstanding_advances'->0->>'reason'<>'دفعة مقدمة على الأرباح القادمة' then raise exception 'Signed balance or debt explanation missing';end if;
    result:=commerce_rpc.wallet_state(889081);
    if (result->'partner'->>'account_balance_minor')::numeric<>-300 or (result->'partner'->>'withdrawable_minor')::numeric<>0
       or (result->'customer'->>'available_minor')::numeric<>0 then raise exception 'Wallet view hides negative partner balance or changes customer funds';end if;
    if (select to_jsonb(w) from commerce.wallets w where user_id=uid and kind='customer') is distinct from wallet_before then raise exception 'Advance changed customer wallet';end if;
    if (select sum(delta_minor) from commerce.ledger_entries where account_key='platform:revenue')<>revenue then raise exception 'Advance was misreported as revenue or commission';end if;
    if (select sum(e.delta_minor) from commerce.ledger_entries e join commerce.journals j on j.id=e.journal_id
        where j.id in ((select advance_journal_id from commerce.partner_advances where withdrawal_id=wd),
                       (select earned_journal_id from commerce.partner_advances where withdrawal_id=wd)) and e.account_key='external:payout:bank')<>1000 then
        raise exception 'Actual payout ledger does not sum to ten';end if;
    if not exists(select 1 from commerce.payout_notifications where withdrawal_id=wd and recipient_kind='partner'
        and (payload->'advance'->>'amount_minor')::numeric=300) then raise exception 'Partner notification lost advance explanation';end if;
    select count(*) into entries from commerce.ledger_entries;
    select count(*) into withdrawals from commerce.withdrawals;
    perform commerce_rpc.record_partner_advance_payout(889080,889081,1000,'fixture bank','bank','ADVANCE-TEN','native-confirm','advance-ten','دفعة مقدمة على الأرباح القادمة',700);
    result:=commerce_rpc.record_partner_advance_payout(889080,889081,1000,'fixture bank','bank','advance-ten','reentered','new-key','دفعة مقدمة على الأرباح القادمة',-300);
    if (result->>'id')::uuid<>wd then raise exception 'Receipt replay duplicated advance';end if;
    perform commerce_rpc.record_partner_payout(889080,889081,1000,'fixture bank','bank','ADVANCE-TEN','native-confirm','advance-ten');
    perform commerce_rpc.respond_withdrawal_receipt(receipt,889081,true,'partner-confirm');
    if (select count(*) from commerce.ledger_entries)<>entries or (select count(*) from commerce.withdrawals)<>withdrawals then raise exception 'Replay or receipt response moved money';end if;
    result:=commerce_rpc.request_withdrawal(889081,'cannot-borrow-through-withdrawal',1,'fixture bank');
    if result->>'state'<>'insufficient_balance' then raise exception 'Partner borrowed through ordinary withdrawal';end if;
    begin perform commerce_rpc.record_partner_advance_payout(889080,889081,1001,'fixture bank','bank','ADVANCE-TEN','changed','changed','دفعة مقدمة على الأرباح القادمة',700);
        raise exception 'Changed amount replay accepted';exception when invalid_parameter_value then null;end;
    -- Real future commissions first cover the recorded advance, then become available.
    perform commerce_rpc.configure_partner(pid,889080,10000,'forever',null,true);
    oid:=pg_temp.advance_fixture_check(889082,'advance-future-two-a',200);
    profile:=commerce_rpc.partner_finance(889081);
    if (profile->>'account_balance_minor')::numeric<>-100 or (profile->>'withdrawable_minor')::numeric<>0
       or (profile->>'today_earned_minor')::numeric<>900 then raise exception 'First future earnings did not reduce advance';end if;
    oid:=pg_temp.advance_fixture_check(889082,'advance-future-two-b',200);
    profile:=commerce_rpc.partner_finance(889081);
    if (profile->>'account_balance_minor')::numeric<>100 or (profile->>'advance_outstanding_minor')::numeric<>0
       or (profile->>'withdrawable_minor')::numeric<>100 or (profile->>'today_earned_minor')::numeric<>1100 then raise exception 'Advance payoff lost surplus earnings';end if;
    select sum(r.amount_minor) into recovered from commerce.partner_advance_repayments r join commerce.partner_advances a on a.id=r.advance_id where a.withdrawal_id=wd;
    if recovered<>300 then raise exception 'Repaid amount differs from original excess';end if;
    select count(*) into entries from commerce.ledger_entries;
    perform commerce_rpc.settle_order(oid);
    if (select count(*) from commerce.ledger_entries)<>entries then raise exception 'Settlement retry repaid advance twice';end if;
    result:=commerce_rpc.admin_earnings_summary(889080,0,50);
    if not exists(select 1 from jsonb_array_elements(result->'partners') item where item->>'partner_id'=pid::text and (item->>'account_balance_minor')::numeric=100) then raise exception 'Admin signed account balance missing';end if;
    if not exists(select 1 from jsonb_array_elements(commerce_rpc.partner_withdrawals(889081)) item where item->>'id'=wd::text
        and (item->'advance'->>'amount_minor')::numeric=300) then raise exception 'Withdrawal history lost advance reason';end if;
    -- Existing reserved earnings are never spent by the new payout path.
    hold_id:=(commerce_rpc.request_withdrawal(889081,'held-before-advance',50,'fixture held')->>'withdrawal_id')::uuid;
    result:=commerce_rpc.record_partner_advance_payout(889080,889081,100,'fixture bank','bank','ADVANCE-HELD','native-held','advance-held','دفعة مقدمة مع وجود سحب محجوز',50);
    second_wd:=(result->>'id')::uuid;
    if (select reserved_minor from commerce.wallets where id=wid)<>50 or (result->'advance'->>'amount_minor')::numeric<>50 then raise exception 'Advance spent another withdrawal hold';end if;
    perform commerce_rpc.reject_withdrawal(hold_id,889080,'release-held');
    profile:=commerce_rpc.partner_finance(889081);
    if (profile->>'account_balance_minor')::numeric<>0 or (profile->>'advance_outstanding_minor')::numeric<>0 then raise exception 'Released earnings bypassed advance recovery';end if;
    if exists(select 1 from commerce.ledger_entries group by journal_id having sum(delta_minor)<>0) then raise exception 'Unbalanced financial journal';end if;
    if has_table_privilege('service_role','commerce.partner_advances','UPDATE')
       or has_function_privilege('anon','commerce_rpc.record_partner_advance_payout(bigint,bigint,numeric,text,text,text,text,text,text,numeric)','EXECUTE')
       or has_function_privilege('service_role','commerce.partner_finance_before_advances(bigint)','EXECUTE') then raise exception 'Advance bypass privileges exposed';end if;
end $$;
rollback;
