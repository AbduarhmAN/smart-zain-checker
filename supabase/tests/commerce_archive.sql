\set ON_ERROR_STOP on
begin;
do $$ declare o uuid; all_failed uuid; plan jsonb; r jsonb; old_expiry timestamptz;
    policy jsonb:='{"original_timing":"after_completion","protection":"plaintext_sha256","hash_algorithm":"sha256","original_retention_days":15,"result_retention_days":15,"automatic_deletion":false,"decision_reference":"synthetic-explicit-policy"}';
begin
    perform commerce_rpc.confirm_receipt(889901,'archive-topup','fixture','archive-tx',200000,'synthetic-proof');
    o:=(commerce_rpc.create_order(889901,'archive-order','[{"row_number":2,"check_type":"service"},{"row_number":3,"check_type":"account"}]',
        jsonb_build_object('file_sha256',repeat('a',64),'original_telegram_file_id','synthetic-original'))->>'order_id')::uuid;
    if commerce_rpc.archive_plan(o)->>'state'<>'awaiting_settlement' then raise exception 'Archive accepted unfinished input'; end if;
    begin
        perform commerce_rpc.confirm_archive_file(o,'original',-1001234567890,1,'file','u',10,repeat('a',64),policy);
        raise exception 'Unfinished archive accepted';
    exception when invalid_parameter_value then null; end;
    perform commerce_rpc.reserve_order(o,889901,'synthetic-wallet-consent');
    perform commerce_rpc.bind_checker_job(o,'archive-job');
    perform commerce_rpc.record_terminal_results(o,'archive-job','[{"row_number":2,"state":"not_found","verified_result":true},{"row_number":3,"state":"failed","verified_result":false}]',repeat('b',64));
    perform commerce_rpc.confirm_result_delivery(o,889901,1,'synthetic-result',repeat('b',64));
    perform commerce_rpc.settle_order(o);
    plan:=commerce_rpc.archive_plan(o);
    if plan->>'state'<>'ready' or (plan->>'valid_checks')::bigint<>1 or (plan->>'technical_failures')::bigint<>1
       or (plan->>'charged_minor')::numeric<>3000 or (plan->>'refunded_minor')::numeric<>3000 then raise exception 'Archive plan financial facts wrong'; end if;
    begin
        perform commerce_rpc.confirm_archive_file(o,'result',-1001234567890,1,'file','u',10,repeat('c',64),policy);
        raise exception 'Wrong SHA accepted';
    exception when invalid_parameter_value then null; end;
    r:=commerce_rpc.confirm_archive_file(o,'original',-1001234567890,1,'original','original-u',10,repeat('a',64),policy);
    old_expiry:=(r->>'retention_until')::timestamptz;
    if old_expiry<>now()+interval '15 days' then raise exception 'Retention deadline wrong'; end if;
    r:=commerce_rpc.confirm_archive_file(o,'original',-1001234567890,1,'original','original-u',10,repeat('a',64),policy);
    if r->>'replayed'<>'true' or (r->>'retention_until')::timestamptz<>old_expiry then raise exception 'Replay extended retention'; end if;
    begin
        perform commerce_rpc.confirm_archive_file(o,'original',-1001234567890,2,'other','u',10,repeat('a',64),policy);
        raise exception 'Conflicting receipt accepted';
    exception when invalid_parameter_value then null; end;
    perform commerce_rpc.confirm_archive_file(o,'result',-1001234567890,2,'result','result-u',20,repeat('b',64),policy);
    if jsonb_array_length(commerce_rpc.admin_archive_index(o)->'files')<>2 then raise exception 'Admin index incomplete'; end if;
    r:=commerce_rpc.customer_archive_index(o,889901);
    if jsonb_array_length(r->'files')<>1 or r->'files'->0->>'kind'<>'result'
       or r->'files'->0 ? 'telegram_chat_id' then raise exception 'Customer saw channel or original'; end if;
    begin
        perform commerce_rpc.customer_archive_index(o,889902);raise exception 'Cross-customer access accepted';
    exception when invalid_parameter_value then null; end;
    all_failed:=(commerce_rpc.create_order(889901,'archive-all-failed','[{"row_number":2,"check_type":"service"}]',
        jsonb_build_object('file_sha256',repeat('c',64)))->>'order_id')::uuid;
    perform commerce_rpc.reserve_order(all_failed,889901,'synthetic-consent');
    perform commerce_rpc.bind_checker_job(all_failed,'archive-failed-job');
    perform commerce_rpc.record_terminal_results(all_failed,'archive-failed-job','[{"row_number":2,"state":"failed","verified_result":false}]',repeat('d',64));
    perform commerce_rpc.confirm_result_delivery(all_failed,889901,3,'failed-result',repeat('d',64));perform commerce_rpc.settle_order(all_failed);
    plan:=commerce_rpc.archive_plan(all_failed);
    if plan->>'state'<>'ready' or plan->>'payment_state'<>'refunded' or (plan->>'charged_minor')::numeric<>0 then raise exception 'All-failed archive excluded'; end if;
    perform commerce_rpc.confirm_archive_file(all_failed,'original',-1001234567890,4,'failed-original','fu',10,repeat('c',64),policy);
    if exists(select 1 from commerce.archive_files where policy_snapshot->>'automatic_deletion'<>'false') then raise exception 'Deletion policy changed'; end if;
    raise notice 'PASS: terminal gate, SHA, receipt replay/conflict, retention, owned index, full-refund archive';
end $$;
rollback;
