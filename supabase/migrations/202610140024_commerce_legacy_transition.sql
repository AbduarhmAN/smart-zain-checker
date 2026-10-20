-- Audited existing balances and wallet debits become an opening liability, not a new payment.
begin;
create table commerce.legacy_wallet_transitions (
    id text primary key check(id ~ '^[a-f0-9]{64}$'),
    actor_telegram_id bigint not null,
    manifest jsonb not null,
    created_at timestamptz not null default now()
);
alter table commerce.legacy_wallet_transitions enable row level security;
revoke all on commerce.legacy_wallet_transitions from public,anon,authenticated,service_role;
alter table commerce.users add column legacy_account_id bigint unique;
alter table commerce.users add column legacy_service_review_required boolean not null default false;
alter table commerce.orders add column legacy_transition_id text references commerce.legacy_wallet_transitions(id);
alter table commerce.orders add column legacy_payment_token text unique;

create function commerce_rpc.import_legacy_wallets(p_actor_telegram_id bigint,p_manifest_text text,p_manifest_sha256 text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare document jsonb;prior jsonb;account jsonb;held jsonb;row_data jsonb;u uuid;w commerce.wallets%rowtype;
    o commerce.orders%rowtype;policy commerce.pricing_policies%rowtype;opening numeric;held_total numeric;
    count integer;ordinal integer;unit bigint;records jsonb;source jsonb;orders jsonb:='{}';accounts integer:=0;
begin
    if not exists(select 1 from commerce.support_actors where telegram_user_id=p_actor_telegram_id and active)
       or p_manifest_text is null or octet_length(p_manifest_text)>32*1024*1024
       or p_manifest_sha256 is null or p_manifest_sha256 !~ '^[a-f0-9]{64}$'
       or encode(sha256(convert_to(p_manifest_text,'UTF8')),'hex')<>p_manifest_sha256 then
        raise exception 'Authorized immutable audited manifest required' using errcode='22023';end if;
    document:=p_manifest_text::jsonb;
    if jsonb_typeof(document) is distinct from 'object' or document->>'version'<>'1'
       or jsonb_typeof(document->'accounts') is distinct from 'array'
       or jsonb_array_length(document->'accounts')>100000
       or coalesce(document->>'wallets_sha256','') !~ '^[a-f0-9]{64}$'
       or coalesce(document->>'payments_sha256','') !~ '^[a-f0-9]{64}$'
       or coalesce(document->>'queue_sha256','') !~ '^[a-f0-9]{64}$' then
        raise exception 'Audited legacy source fingerprints required' using errcode='22023';end if;
    perform pg_advisory_xact_lock(610140024);
    select manifest into prior from commerce.legacy_wallet_transitions where id=p_manifest_sha256;
    if found then
        if prior<>document then raise exception 'Conflicting transition replay' using errcode='22023';end if;
        select coalesce(jsonb_object_agg(legacy_payment_token,jsonb_build_object('order_id',id,'owner',
            (select telegram_user_id from commerce.users where id=orders.user_id),'source',source_snapshot,'rows',
            (select jsonb_agg(jsonb_build_object('row_number',row_number,'check_type',check_type) order by row_number) from commerce.checks where order_id=orders.id))), '{}')
            into orders from commerce.orders where legacy_transition_id=p_manifest_sha256;
        return jsonb_build_object('state','imported','orders',orders,'replayed',true);
    end if;
    insert into commerce.legacy_wallet_transitions(id,actor_telegram_id,manifest) values(p_manifest_sha256,p_actor_telegram_id,document);
    select * into policy from commerce.pricing_policies where effective_at<=now() order by version desc limit 1;
    for account in select value from jsonb_array_elements(document->'accounts') loop
        if jsonb_typeof(account->'telegram_user_id') is distinct from 'number' or account->>'telegram_user_id' !~ '^[1-9][0-9]{0,15}$'
           or jsonb_typeof(account->'legacy_account_id') is distinct from 'number' or account->>'legacy_account_id' !~ '^[1-9][0-9]{8}$'
           or jsonb_typeof(account->'available_minor') is distinct from 'number' or account->>'available_minor' !~ '^[0-9]{1,18}$'
           or jsonb_typeof(account->'held_orders') is distinct from 'array' then
            raise exception 'Invalid audited legacy account' using errcode='22023';end if;
        u:=(commerce_rpc.register_telegram_user((account->>'telegram_user_id')::bigint,null)->>'user_id')::uuid;
        perform 1 from commerce.users where id=u and registration_state='active' for update;
        if not found then raise exception 'Legacy account is suspended' using errcode='22023';end if;
        select * into w from commerce.wallets where user_id=u and kind='customer' for update;
        if w.available_minor<>0 or w.reserved_minor<>0 or w.promotional_available_minor<>0 or w.promotional_reserved_minor<>0
           or exists(select 1 from commerce.orders where user_id=u)
           or exists(select 1 from commerce.ledger_entries where wallet_id=w.id) then
            raise exception 'Existing commerce money or orders require individual reconciliation' using errcode='22023';end if;
        update commerce.users set legacy_account_id=(account->>'legacy_account_id')::bigint,
            legacy_service_review_required=coalesce((account->>'service_history_requires_review')::boolean,false) where id=u;
        held_total:=0;
        for held in select value from jsonb_array_elements(account->'held_orders') loop
            if jsonb_typeof(held->'amount_minor') is distinct from 'number' or held->>'amount_minor' !~ '^[1-9][0-9]{0,18}$'
               or coalesce(held->>'token','') !~ '^[a-f0-9]{12}$' or jsonb_typeof(held->'rows') is distinct from 'array'
               or jsonb_array_length(held->'rows') not between 1 and 30000
               or coalesce(held->'source'->>'file_sha256','') !~ '^[a-f0-9]{64}$'
               or coalesce(held->>'job_id','')='' or length(held->>'job_id')>120
               or held->>'receipt_verified' is distinct from 'true' then
                raise exception 'Exact existing wallet debit and immutable paid input required' using errcode='22023';end if;
            held_total:=held_total+(held->>'amount_minor')::numeric;
        end loop;
        opening:=(account->>'available_minor')::numeric+held_total;
        if opening>9223372036854775807 then raise exception 'Legacy liability exceeds monetary range' using errcode='22023';end if;
        if opening>0 then
            perform commerce.post_pair('legacy:opening:'||p_manifest_sha256||':'||u,'adjustment',null,
                'platform:legacy_wallet_opening','wallet:'||w.id||':available',opening::bigint,null,w.id);
            update commerce.wallets set available_minor=opening::bigint where id=w.id;
        end if;
        for held in select value from jsonb_array_elements(account->'held_orders') loop
            records:=held->'rows';count:=jsonb_array_length(records);
            if exists(select 1 from jsonb_array_elements(records) r where jsonb_typeof(r) is distinct from 'object'
                or jsonb_typeof(r->'row_number') is distinct from 'number' or r->>'row_number' !~ '^[1-9][0-9]{0,18}$'
                or coalesce(r->>'check_type','') not in ('account','service'))
                or (select count(distinct (r->>'row_number')::bigint) from jsonb_array_elements(records) r)<>count then
                raise exception 'Invalid exact legacy executable rows' using errcode='22023';end if;
            source:=held->'source'||jsonb_build_object('legacy_operation_id',held->>'operation_id',
                'legacy_wallet_transition',p_manifest_sha256,'rows_fingerprint',md5(records::text));
            insert into commerce.orders(user_id,request_key,checker_job_id,requested_checks,quoted_minor,pricing_snapshot,source_snapshot,
                order_state,payment_state,execution_state,legacy_transition_id,legacy_payment_token,campaigns_evaluated_at)
                values(u,'legacy-wallet:'||(account->>'telegram_user_id')||':'||(held->>'token'),held->>'job_id',count,(held->>'amount_minor')::bigint,
                    jsonb_build_object('policy_version',policy.version,'unit','legacy_sheet','total_minor',(held->>'amount_minor')::bigint,
                        'base_total_minor',(held->>'amount_minor')::bigint,'discount_total_minor',0,'currency','EGP',
                        'refund_destination','wallet','refund_requires_consent',true,'allocation_rule','cumulative_integer_piastres',
                        'legacy_price_preserved',true),source,'confirmed','reserved','queued',p_manifest_sha256,held->>'token',now()) returning * into o;
            ordinal:=0;
            for row_data in select value from jsonb_array_elements(records) order by (value->>'row_number')::bigint loop
                ordinal:=ordinal+1;
                unit:=(floor((held->>'amount_minor')::numeric*ordinal/count)-floor((held->>'amount_minor')::numeric*(ordinal-1)/count))::bigint;
                insert into commerce.checks(order_id,row_number,check_type,unit_quote_minor)
                    values(o.id,(row_data->>'row_number')::bigint,row_data->>'check_type',unit);
            end loop;
            perform commerce.post_pair('reserve:'||o.id,'reserve',o.id,'wallet:'||w.id||':available','wallet:'||w.id||':reserved',(held->>'amount_minor')::bigint,w.id,w.id);
            insert into commerce.reservations(order_id,wallet_id,amount_minor,idempotency_key)
                values(o.id,w.id,(held->>'amount_minor')::bigint,'reserve:'||o.id);
            update commerce.wallets set available_minor=available_minor-(held->>'amount_minor')::bigint,
                reserved_minor=reserved_minor+(held->>'amount_minor')::bigint where id=w.id;
            orders:=orders||jsonb_build_object(held->>'token',jsonb_build_object('order_id',o.id,'owner',(account->>'telegram_user_id')::bigint,
                'source',source,'rows',records));
        end loop;
        accounts:=accounts+1;
    end loop;
    return jsonb_build_object('state','imported','accounts',accounts,'orders',orders,'replayed',false);
end $$;

-- Existing debits remain reserved, but a client must explicitly agree to wallet refunds before execution.
create or replace function commerce_rpc.checkout_consent(p_order_id uuid,p_telegram_user_id bigint,p_policy_version bigint,p_reference text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare o commerce.orders%rowtype;
begin
    perform 1 from commerce.users where telegram_user_id=p_telegram_user_id and registration_state='active' for update;
    if not found then raise exception 'Unknown or suspended user' using errcode='22023';end if;
    select ord.* into o from commerce.orders ord join commerce.users u on u.id=ord.user_id
        where ord.id=p_order_id and u.telegram_user_id=p_telegram_user_id for update of ord;
    if not found or p_reference is null or length(btrim(p_reference)) not between 1 and 1000
       or p_policy_version is distinct from (o.pricing_snapshot->>'policy_version')::bigint
       or o.pricing_snapshot->>'refund_destination' is distinct from 'wallet'
       or o.pricing_snapshot->>'refund_requires_consent' is distinct from 'true' then
        raise exception 'Owned exact refund-policy consent required' using errcode='22023';end if;
    if o.refund_consent_reference is not null then return jsonb_build_object('reference',o.refund_consent_reference,'replayed',true);end if;
    if not (o.order_state='draft' and o.payment_state='unpaid')
       and not (o.legacy_transition_id is not null and o.payment_state='reserved' and o.execution_state='queued') then
        raise exception 'Request is no longer awaiting refund consent' using errcode='22023';end if;
    update commerce.orders set refund_consent_reference=p_reference where id=o.id;
    return jsonb_build_object('reference',p_reference,'replayed',false);
end $$;

alter function commerce_rpc.attribute_referral(bigint,text,text) rename to attribute_referral_before_legacy_review;
alter function commerce_rpc.attribute_referral_before_legacy_review(bigint,text,text) set schema commerce;
revoke all on function commerce.attribute_referral_before_legacy_review(bigint,text,text) from public,anon,authenticated,service_role;
create function commerce_rpc.attribute_referral(p_telegram_user_id bigint,p_code text,p_source text)
returns jsonb language plpgsql security definer set search_path='' as $$
begin
    if exists(select 1 from commerce.users where telegram_user_id=p_telegram_user_id and legacy_service_review_required)
       and not exists(select 1 from commerce.referrals r join commerce.users u on u.id=r.user_id where u.telegram_user_id=p_telegram_user_id) then
        return jsonb_build_object('status','legacy_history_review');end if;
    return commerce.attribute_referral_before_legacy_review(p_telegram_user_id,p_code,p_source);
end $$;

revoke all on function commerce_rpc.import_legacy_wallets(bigint,text,text),commerce_rpc.attribute_referral(bigint,text,text) from public,anon,authenticated;
grant execute on function commerce_rpc.import_legacy_wallets(bigint,text,text),commerce_rpc.attribute_referral(bigint,text,text) to service_role;
create or replace function commerce_rpc.health()
returns jsonb language sql stable security definer set search_path='' as $$
    select jsonb_build_object('status','ok','schema_version',24,'currency','EGP')
$$;
notify pgrst,'reload schema';
commit;
