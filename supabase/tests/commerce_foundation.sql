-- Isolated test database only. Creates synthetic records and retains them.
-- Roles anon/authenticated/service_role must exist, as in Supabase.
\set ON_ERROR_STOP on
begin;
do $$
declare u uuid; o uuid; e uuid; again uuid; p uuid; first_seen timestamptz; payload jsonb;
begin
    u := (commerce_rpc.register_telegram_user(880001,'fixture')->>'user_id')::uuid;
    select first_used_at into first_seen from commerce.users where id = u;
    if (commerce_rpc.register_telegram_user(880001,'renamed')->>'user_id')::uuid <> u then
        raise exception 'Registration duplicated the user';
    end if;
    if (select first_used_at from commerce.users where id = u) <> first_seen then
        raise exception 'Registration changed first use';
    end if;
    if (select count(*) from commerce.wallets where user_id = u) <> 2 then
        raise exception 'Wallet separation failed';
    end if;
    perform commerce_rpc.register_telegram_user(880001,null);
    if (select telegram_username from commerce.users where id = u) <> 'renamed' then
        raise exception 'Financial registration erased known Telegram username';
    end if;
    if (select first_paid_service_at from commerce.users where id = u) is not null then
        raise exception 'Registration incorrectly counted as paid service';
    end if;
    begin
        perform commerce_rpc.register_telegram_user(-1,null);
        raise exception 'Invalid identity accepted';
    exception when invalid_parameter_value then null; end;
    insert into commerce.orders(user_id,request_key,checker_job_id,requested_checks,quoted_minor,pricing_snapshot)
    values (u,'fixture-order-1','fixture-job-1',2,400,'{"unit_minor":200}') returning id into o;
    payload := '{"schema_version":1,"job_id":"fixture-job-1","revision":1,"row_number":2,"check_type":"service","outcome":"succeeded"}';
    e := commerce_rpc.receive_checker_event(o,'zain_checker','fixture-event-1','check_outcome',payload);
    again := commerce_rpc.receive_checker_event(o,'zain_checker','fixture-event-1','check_outcome',payload);
    if again <> e or (select count(*) from commerce.events where order_id = o) <> 1 then
        raise exception 'Event replay duplicated the event';
    end if;
    begin
        perform commerce_rpc.receive_checker_event(o,'zain_checker','fixture-event-1','check_outcome',payload || '{"outcome":"failed"}');
        raise exception 'Conflicting replay accepted';
    exception when invalid_parameter_value then null; end;
    begin
        perform commerce_rpc.receive_checker_event(o,'zain_checker','fixture-event-2','check_outcome',payload || '{"job_id":"another-job"}');
        raise exception 'Wrong checker job accepted';
    exception when invalid_parameter_value then null; end;
    begin
        update commerce.orders set payment_state = 'settled',final_charge_minor = 400 where id = o;
        raise exception 'Settlement before terminal execution/delivery accepted';
    exception when check_violation then null; end;
    insert into commerce.checks(order_id,row_number,check_type,unit_quote_minor,state,terminal_at)
    values (o,2,'service',200,'failed',now());
    insert into commerce.refunds(order_id,row_number,amount_minor,destination,policy_version,consent_reference,idempotency_key)
    values (o,2,200,'wallet','fixture','fixture-consent','fixture-refund');
    begin
        insert into commerce.refunds(order_id,row_number,amount_minor,destination,policy_version,idempotency_key)
        values (o,2,200,'original_source','fixture','fixture-second-refund');
        raise exception 'Same row refunded to a second destination';
    exception when unique_violation then null; end;
    begin
        insert into commerce.codes(code,kind,credit_minor) values ('BAD_CASH','cash_credit',100);
        raise exception 'Cash credit without proof accepted';
    exception when check_violation then null; end;
    if exists (select 1 from pg_tables where schemaname = 'commerce' and not rowsecurity) then
        raise exception 'Commerce table missing RLS';
    end if;
    if has_schema_privilege('anon','commerce','USAGE') or has_schema_privilege('authenticated','commerce','USAGE') then
        raise exception 'Client can access private commerce schema';
    end if;
    if has_function_privilege('anon','commerce_rpc.register_telegram_user(bigint,text)','EXECUTE')
       or has_function_privilege('authenticated','commerce_rpc.receive_checker_event(uuid,text,text,text,jsonb)','EXECUTE') then
        raise exception 'Client can execute privileged commerce functions';
    end if;
    if not has_function_privilege('service_role','commerce_rpc.register_telegram_user(bigint,text)','EXECUTE') then
        raise exception 'Backend cannot register users';
    end if;
    raise notice 'PASS: identity, wallet separation, event replay/conflict, job ownership, settlement gate, single refund destination, cash proof, RLS and grants';
end $$;
commit;
