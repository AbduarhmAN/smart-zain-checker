-- A64-A73: immutable receipt index; expiry is metadata, never automatic deletion.
begin;
alter table commerce.archive_files add column telegram_file_unique_id text,
    add column file_size bigint check (file_size >= 0),
    add column policy_snapshot jsonb not null default '{}',
    add column metadata_snapshot jsonb not null default '{}';

create function commerce_rpc.archive_plan(p_order_id uuid)
returns jsonb language plpgsql stable security definer set search_path='' as $$
declare o commerce.orders%rowtype; owner bigint; valid bigint; failed bigint; original_hash text;
begin
    select * into o from commerce.orders where id=p_order_id;
    if not found then raise exception 'Unknown archive order' using errcode='22023'; end if;
    if o.execution_state<>'terminal' or o.delivery_state<>'confirmed'
       or o.payment_state not in ('settled','partial_refund','refunded') then
        return jsonb_build_object('state','awaiting_settlement','order_id',o.id); end if;
    original_hash:=o.source_snapshot->>'file_sha256';
    if original_hash is null or original_hash !~ '^[a-f0-9]{64}$' or o.result_sha256 is null
       or o.delivery_file_id is null then raise exception 'Archive input identities missing' using errcode='22023'; end if;
    select telegram_user_id into owner from commerce.users where id=o.user_id;
    select count(*) filter(where verified_result),count(*) filter(where not verified_result)
        into valid,failed from commerce.checks where order_id=o.id and terminal_at is not null;
    if valid+failed<>o.requested_checks then raise exception 'Archive terminal coverage incomplete' using errcode='22023'; end if;
    return jsonb_build_object('state','ready','order_id',o.id,'telegram_user_id',owner,
        'requested_checks',o.requested_checks,'valid_checks',valid,'technical_failures',failed,
        'charged_minor',o.final_charge_minor,'refunded_minor',o.quoted_minor-o.final_charge_minor,'currency','EGP',
        'completed_at',o.execution_terminal_at,'payment_state',o.payment_state,
        'original_sha256',original_hash,'result_sha256',o.result_sha256,
        'original_file_id',o.source_snapshot->>'original_telegram_file_id','result_file_id',o.delivery_file_id);
end $$;

create function commerce_rpc.confirm_archive_file(p_order_id uuid,p_kind text,p_chat_id bigint,p_message_id bigint,
    p_file_id text,p_unique_id text,p_file_size bigint,p_sha256 text,p_policy jsonb)
returns jsonb language plpgsql security definer set search_path='' as $$
declare plan jsonb; existing commerce.archive_files%rowtype; expected_hash text; days integer; saved timestamptz:=now();
begin
    if p_kind is null or p_kind not in ('original','result') or p_chat_id is null or p_chat_id>=0
       or p_chat_id<=-4503599627370496 or p_message_id is null or p_message_id<=0
       or p_file_id is null or length(p_file_id) not between 1 and 512
       or p_unique_id is null or length(p_unique_id) not between 1 and 512
       or p_file_size is null or p_file_size<0 or p_sha256 is null or p_sha256 !~ '^[a-f0-9]{64}$'
       or jsonb_typeof(p_policy) is distinct from 'object' then
        raise exception 'Confirmed archive document receipt required' using errcode='22023'; end if;
    if p_policy->>'original_timing' is distinct from 'after_completion'
       or p_policy->>'protection' is distinct from 'plaintext_sha256'
       or p_policy->>'hash_algorithm' is distinct from 'sha256'
       or p_policy->'automatic_deletion' is distinct from 'false'::jsonb
       or jsonb_typeof(p_policy->'original_retention_days') is distinct from 'number'
       or jsonb_typeof(p_policy->'result_retention_days') is distinct from 'number'
       or coalesce(p_policy->>'original_retention_days','') !~ '^[1-9][0-9]{0,3}$'
       or coalesce(p_policy->>'result_retention_days','') !~ '^[1-9][0-9]{0,3}$'
       or length(coalesce(p_policy->>'decision_reference','')) not between 1 and 1000 then
        raise exception 'Unsupported archive policy' using errcode='22023'; end if;
    -- Serialize both kinds and replays for the same completed order.
    perform 1 from commerce.orders where id=p_order_id for update;
    plan:=commerce_rpc.archive_plan(p_order_id);
    if plan->>'state'<>'ready' then raise exception 'Archive waits for completed delivered settlement' using errcode='22023'; end if;
    expected_hash:=plan->>(p_kind||'_sha256');
    if p_sha256<>expected_hash then raise exception 'Archive hash differs from frozen order' using errcode='22023'; end if;
    select * into existing from commerce.archive_files where order_id=p_order_id and kind=p_kind;
    if found then
        if existing.state<>'confirmed' or existing.telegram_chat_id<>p_chat_id
           or existing.telegram_message_id<>p_message_id or existing.telegram_file_id<>p_file_id
           or existing.telegram_file_unique_id is distinct from p_unique_id or existing.file_size is distinct from p_file_size
           or existing.sha256 is distinct from p_sha256 or existing.policy_snapshot<>p_policy then
            raise exception 'Conflicting archive receipt replay' using errcode='22023'; end if;
        return jsonb_build_object('state','confirmed','replayed',true,'retention_until',existing.retention_until); end if;
    days:=(p_policy->>(p_kind||'_retention_days'))::integer;
    insert into commerce.archive_files(order_id,kind,telegram_chat_id,telegram_message_id,telegram_file_id,
        telegram_file_unique_id,file_size,sha256,state,archived_at,retention_until,policy_snapshot,metadata_snapshot)
    values(p_order_id,p_kind,p_chat_id,p_message_id,p_file_id,p_unique_id,p_file_size,p_sha256,'confirmed',saved,
        saved+make_interval(days=>days),p_policy,plan-'original_file_id'-'result_file_id');
    return jsonb_build_object('state','confirmed','replayed',false,'retention_until',saved+make_interval(days=>days));
end $$;

create function commerce_rpc.admin_archive_index(p_order_id uuid)
returns jsonb language sql stable security definer set search_path='' as $$
    select jsonb_build_object('order_id',p_order_id,'files',coalesce((select jsonb_agg(to_jsonb(f) order by kind)
        from commerce.archive_files f where order_id=p_order_id and state='confirmed'),'[]'::jsonb))
$$;

create function commerce_rpc.customer_archive_index(p_order_id uuid,p_telegram_user_id bigint)
returns jsonb language plpgsql stable security definer set search_path='' as $$
declare owner uuid;
begin
    select id into owner from commerce.users where telegram_user_id=p_telegram_user_id and registration_state='active';
    if owner is null or not exists(select 1 from commerce.orders where id=p_order_id and user_id=owner) then
        raise exception 'Unknown or unowned archive order' using errcode='22023'; end if;
    -- Customer receives own result only; private channel address and original stay administrative.
    return jsonb_build_object('order_id',p_order_id,'files',coalesce((select jsonb_agg(jsonb_build_object(
        'kind',f.kind,'telegram_file_id',f.telegram_file_id,'telegram_file_unique_id',f.telegram_file_unique_id,
        'sha256',f.sha256,'file_size',f.file_size,'retention_until',f.retention_until))
        from commerce.archive_files f where order_id=p_order_id and kind='result' and state='confirmed'),'[]'::jsonb));
end $$;
revoke all on function commerce_rpc.archive_plan(uuid),
    commerce_rpc.confirm_archive_file(uuid,text,bigint,bigint,text,text,bigint,text,jsonb),
    commerce_rpc.admin_archive_index(uuid),commerce_rpc.customer_archive_index(uuid,bigint) from public,anon,authenticated;
grant execute on function commerce_rpc.archive_plan(uuid),
    commerce_rpc.confirm_archive_file(uuid,text,bigint,bigint,text,text,bigint,text,jsonb),
    commerce_rpc.admin_archive_index(uuid),commerce_rpc.customer_archive_index(uuid,bigint) to service_role;
create or replace function commerce_rpc.health()
returns jsonb language sql stable security definer set search_path='' as $$
    select jsonb_build_object('status','ok','schema_version',17,'currency','EGP')
$$;
notify pgrst,'reload schema';
commit;
