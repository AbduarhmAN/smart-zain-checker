\set ON_ERROR_STOP on
begin;
do $$
declare ord uuid;quote jsonb;policy bigint;reply jsonb;before_state jsonb;reference text;
begin
    ord:=(commerce_rpc.create_order(889001,'stop-partial','[{"row_number":2,"check_type":"account"},{"row_number":3,"check_type":"service"},{"row_number":4,"check_type":"account"}]',
        jsonb_build_object('file_sha256',repeat('c',64)))->>'order_id')::uuid;
    perform commerce_rpc.confirm_receipt(889001,'stop-funding','fixture','stop-funding',3,'fixture-proof');
    select (pricing_snapshot->>'policy_version')::bigint into policy from commerce.orders where id=ord;
    perform commerce_rpc.checkout_consent(ord,889001,policy,'stop-wallet-refund-consent');
    perform commerce_rpc.reserve_order(ord,889001,'stop-wallet-refund-consent');
    perform commerce_rpc.bind_checker_job(ord,'stop-job');
    before_state:=commerce_rpc.wallet_state(889001);
    update commerce.users set registration_state='suspended' where telegram_user_id=889001;
    begin
        perform commerce_rpc.request_customer_stop(ord,889001,'suspended-owner-stop');
        raise exception 'Suspended customer stopped a request';
    exception when invalid_parameter_value then null;end;
    update commerce.users set registration_state='active' where telegram_user_id=889001;
    begin
        perform commerce_rpc.record_terminal_results(ord,'stop-job','[{"row_number":2,"state":"succeeded","verified_result":true},{"row_number":3,"state":"not_found","verified_result":true},{"row_number":4,"state":"cancelled","verified_result":false}]',repeat('c',64));
        raise exception 'Worker cancelled rows without customer confirmation';
    exception when invalid_parameter_value then null;end;
    begin
        perform commerce_rpc.request_customer_stop(ord,889002,'forged-stop');
        raise exception 'Customer stopped another owner request';
    exception when invalid_parameter_value then null;end;
    reply:=commerce_rpc.request_customer_stop(ord,889001,'native-owner-stop');
    if reply->>'state'<>'draining' or commerce_rpc.wallet_state(889001)<>before_state then
        raise exception 'Stop prematurely changed the reserved funds';end if;
    perform commerce_rpc.request_customer_stop(ord,889001,'stop-replay');
    if (select customer_stop_reference from commerce.orders where id=ord)<>'native-owner-stop' then
        raise exception 'Stop receipt was replaced on replay';end if;
    perform commerce_rpc.record_terminal_results(ord,'stop-job','[{"row_number":2,"state":"succeeded","verified_result":true},{"row_number":3,"state":"not_found","verified_result":true},{"row_number":4,"state":"cancelled","verified_result":false}]',repeat('c',64));
    begin perform commerce_rpc.settle_order(ord);raise exception 'Partial result settled without delivery';
    exception when invalid_parameter_value then null;end;
    perform commerce_rpc.confirm_result_delivery(ord,889001,124,'partial-stop-file',repeat('c',64));
    reply:=commerce_rpc.settle_order(ord);
    if (reply->>'charged_minor')::numeric<>2 or (reply->>'refunded_minor')::numeric<>1
       or (commerce_rpc.wallet_state(889001)->'customer'->>'available_minor')::numeric<>1 then
        raise exception 'Unused stopped work was not refunded after partial delivery';end if;
    perform commerce_rpc.settle_order(ord);perform commerce_rpc.request_customer_stop(ord,889001,'closed-stop-replay');
    if (commerce_rpc.wallet_state(889001)->'customer'->>'available_minor')::numeric<>1 then
        raise exception 'Repeated stop or settlement refunded twice';end if;
end $$;
rollback;
