-- Partner-owned sheets: explicit owner approval and an immutable flat price.
begin;
create table commerce.partner_sheet_prices (
    version bigint generated always as identity primary key,
    amount_minor numeric not null check(amount_minor>0 and amount_minor<=100000000),
    actor_telegram_id bigint not null,
    reference text not null unique,
    created_at timestamptz not null default now()
);
create table commerce.partner_sheet_requests (
    order_id uuid primary key references commerce.orders(id),
    partner_id uuid not null references commerce.partners(id),
    state text not null default 'pending' check(state in ('pending','approved','rejected')),
    price_version bigint references commerce.partner_sheet_prices(version),
    amount_minor numeric,
    actor_telegram_id bigint,
    reference text unique,
    decided_at timestamptz,
    created_at timestamptz not null default now(),
    check((state='pending' and decided_at is null and actor_telegram_id is null and reference is null and amount_minor is null and price_version is null)
       or (state in ('approved','rejected') and decided_at is not null and actor_telegram_id is not null and reference is not null
           and (state='rejected' or (price_version is not null and amount_minor>0))))
);
create table commerce.partner_sheet_notifications (
    id uuid primary key default gen_random_uuid(),
    event_key text not null,
    recipient_telegram_id bigint not null,
    payload jsonb not null,
    state text not null default 'pending' check(state in ('pending','sending','sent','uncertain','rejected')),
    token uuid,
    telegram_message_id bigint,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique(event_key,recipient_telegram_id)
);
alter table commerce.partner_sheet_prices enable row level security;
alter table commerce.partner_sheet_requests enable row level security;
alter table commerce.partner_sheet_notifications enable row level security;
revoke all on commerce.partner_sheet_prices,commerce.partner_sheet_requests,commerce.partner_sheet_notifications from public,anon,authenticated,service_role;

create function commerce.require_sheet_admin(p_actor bigint) returns void
language plpgsql stable security definer set search_path='' as $$
begin
    if not exists(select 1 from commerce.support_actors where telegram_user_id=p_actor and active) then
        raise exception 'Active sheet administrator required' using errcode='42501';end if;
end $$;
revoke all on function commerce.require_sheet_admin(bigint) from public,anon,authenticated,service_role;

create function commerce_rpc.partner_sheet_context(p_telegram_user_id bigint) returns jsonb
language sql stable security definer set search_path='' as $$
    select jsonb_build_object('is_partner',exists(select 1 from commerce.partners p join commerce.users u on u.id=p.user_id
        where u.telegram_user_id=p_telegram_user_id and p.state='active' and u.registration_state='active'),
        'price',(select to_jsonb(v)-'reference'-'actor_telegram_id' from commerce.partner_sheet_prices v order by version desc limit 1))
$$;

create function commerce_rpc.configure_partner_sheet_price(p_actor_telegram_id bigint,p_amount_minor numeric,p_reference text) returns jsonb
language plpgsql security definer set search_path='' as $$
declare v commerce.partner_sheet_prices%rowtype;total bigint;
begin
    perform commerce.require_sheet_admin(p_actor_telegram_id);
    if p_amount_minor is null or p_amount_minor<=0 or p_amount_minor>100000000 or p_amount_minor::text in ('NaN','Infinity','-Infinity')
        or p_reference is null or length(btrim(p_reference)) not between 1 and 300 then
        raise exception 'Reviewed positive sheet price required' using errcode='22023';end if;
    perform pg_advisory_xact_lock(610150035);
    select * into v from commerce.partner_sheet_prices where reference=p_reference;
    if found then
        if v.amount_minor<>p_amount_minor or v.actor_telegram_id<>p_actor_telegram_id then
            raise exception 'Conflicting sheet price replay' using errcode='22023';end if;
    else
        insert into commerce.partner_sheet_prices(amount_minor,actor_telegram_id,reference)
            values(p_amount_minor,p_actor_telegram_id,p_reference) returning * into v;
        insert into commerce.partner_sheet_notifications(event_key,recipient_telegram_id,payload)
            select 'price:'||v.version,u.telegram_user_id,jsonb_build_object('kind','price','version',v.version,'amount_minor',v.amount_minor)
            from commerce.partners p join commerce.users u on u.id=p.user_id where p.state='active' and u.registration_state='active';
    end if;
    select count(*) into total from commerce.partner_sheet_notifications where event_key='price:'||v.version;
    return to_jsonb(v)||jsonb_build_object('recipients',total);
end $$;

alter function commerce_rpc.create_order(bigint,text,jsonb,jsonb) rename to create_order_with_campaigns;
alter function commerce_rpc.create_order_with_campaigns(bigint,text,jsonb,jsonb) set schema commerce;
revoke all on function commerce.create_order_with_campaigns(bigint,text,jsonb,jsonb) from public,anon,authenticated,service_role;
create function commerce_rpc.create_order(p_telegram_user_id bigint,p_request_key text,p_rows jsonb,p_source jsonb) returns jsonb
language plpgsql security definer set search_path='' as $$
declare uid uuid;pid uuid;result jsonb;oid uuid;pricing jsonb;
begin
    uid:=(commerce_rpc.register_telegram_user(p_telegram_user_id,null)->>'user_id')::uuid;
    perform 1 from commerce.users where id=uid for update;
    -- Existing quotes, invoices and reservations keep their original contract.
    if exists(select 1 from commerce.orders where request_key=p_request_key) then
        return commerce.create_order(p_telegram_user_id,p_request_key,p_rows,p_source);end if;
    select id into pid from commerce.partners where user_id=uid and state='active' for share;
    if not found then return commerce.create_order_with_campaigns(p_telegram_user_id,p_request_key,p_rows,p_source);end if;
    result:=commerce.create_order(p_telegram_user_id,p_request_key,p_rows,p_source);
    oid:=(result->>'order_id')::uuid;
    select jsonb_build_object('policy_version',pricing_snapshot->'policy_version','partner_cost_sheet',true,
        'fixed_scope','sheet','financial_precision','exact_decimal','total_minor',0,'discount_total_minor',0,
        'refund_destination','wallet','refund_requires_consent',true,
        'approval_required',true) into pricing from commerce.orders where id=oid;
    update commerce.orders set quoted_minor=0,pricing_snapshot=pricing,partner_terms_snapshot='{}',
        referral_partner_id=null,campaigns_evaluated_at=now() where id=oid;
    update commerce.checks set unit_quote_minor=0,discount_minor=0 where order_id=oid;
    insert into commerce.partner_sheet_requests(order_id,partner_id) values(oid,pid);
    insert into commerce.partner_sheet_notifications(event_key,recipient_telegram_id,payload)
        select 'request:'||oid,telegram_user_id,jsonb_build_object('kind','request','order_id',oid,
            'partner_telegram_id',p_telegram_user_id) from commerce.support_actors where active;
    return result||jsonb_build_object('quoted_minor',0,'partner_sheet_state','pending');
end $$;

create function commerce_rpc.partner_sheet_requests(p_actor_telegram_id bigint,p_offset integer default 0,p_order_id uuid default null) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare result jsonb;
begin
    perform commerce.require_sheet_admin(p_actor_telegram_id);
    if p_offset<0 then raise exception 'Invalid request offset' using errcode='22023';end if;
    select coalesce(jsonb_agg(item),'[]') into result from (
        select to_jsonb(r)||jsonb_build_object('telegram_user_id',u.telegram_user_id,'source',o.source_snapshot,
            'requested_checks',o.requested_checks,'order_state',o.order_state) as item
        from commerce.partner_sheet_requests r join commerce.orders o on o.id=r.order_id join commerce.users u on u.id=o.user_id
        where (p_order_id is not null and r.order_id=p_order_id) or (p_order_id is null and r.state='pending' and o.order_state='draft')
        order by r.created_at,r.order_id offset p_offset limit 10) page;
    return jsonb_build_object('items',result,'price',(select to_jsonb(v) from commerce.partner_sheet_prices v order by version desc limit 1));
end $$;

create function commerce_rpc.decide_partner_sheet(p_actor_telegram_id bigint,p_order_id uuid,p_approve boolean,p_price_version bigint,p_reference text) returns jsonb
language plpgsql security definer set search_path='' as $$
declare req commerce.partner_sheet_requests%rowtype;o commerce.orders%rowtype;v commerce.partner_sheet_prices%rowtype;
    uid uuid;recipient bigint;part numeric;lastrow bigint;
begin
    perform commerce.require_sheet_admin(p_actor_telegram_id);
    if p_approve is null or p_reference is null or length(btrim(p_reference)) not between 1 and 300 then
        raise exception 'Authenticated sheet decision required' using errcode='22023';end if;
    select user_id into uid from commerce.orders where id=p_order_id;
    perform 1 from commerce.users where id=uid for update;
    select * into o from commerce.orders where id=p_order_id for update;
    select * into req from commerce.partner_sheet_requests where order_id=o.id for update;
    if not found then raise exception 'Partner sheet required' using errcode='22023';end if;
    if req.state<>'pending' then
        if (req.state='approved') is distinct from p_approve then raise exception 'Sheet decision already final' using errcode='22023';end if;
        return to_jsonb(req);end if;
    if o.order_state<>'draft' or o.payment_state<>'unpaid' or exists(select 1 from commerce.reservations where order_id=o.id)
        or exists(select 1 from commerce.payment_requests where order_id=o.id) then
        raise exception 'Unfunded draft sheet required' using errcode='22023';end if;
    if p_approve then
        perform 1 from commerce.partners p join commerce.users u on u.id=p.user_id
            where p.id=req.partner_id and p.state='active' and u.registration_state='active' for share of p;
        if not found then raise exception 'Active partner required' using errcode='22023';end if;
        perform pg_advisory_xact_lock(610150035);
        select * into v from commerce.partner_sheet_prices order by version desc limit 1;
        if not found or v.version is distinct from p_price_version then raise exception 'Review current sheet price' using errcode='22023';end if;
        part:=trunc(v.amount_minor/o.requested_checks,16);
        select max(row_number) into lastrow from commerce.checks where order_id=o.id;
        update commerce.checks set unit_quote_minor=case when row_number=lastrow
            then v.amount_minor-part*(o.requested_checks-1) else part end,discount_minor=0 where order_id=o.id;
        update commerce.orders set quoted_minor=v.amount_minor,pricing_snapshot=pricing_snapshot||jsonb_build_object(
            'total_minor',v.amount_minor,'sheet_price_minor',v.amount_minor,'sheet_price_version',v.version,
            'refund_allocation','proportional_check_share_remainder_on_last_row') where id=o.id;
    else
        update commerce.orders set order_state='cancelled' where id=o.id;
    end if;
    update commerce.partner_sheet_requests set state=case when p_approve then 'approved' else 'rejected' end,
        price_version=v.version,amount_minor=v.amount_minor,actor_telegram_id=p_actor_telegram_id,reference=p_reference,decided_at=now()
        where order_id=o.id returning * into req;
    select telegram_user_id into recipient from commerce.users where id=uid;
    insert into commerce.partner_sheet_notifications(event_key,recipient_telegram_id,payload)
        values('decision:'||o.id,recipient,jsonb_build_object('kind','decision','order_id',o.id,
            'state',req.state,'amount_minor',req.amount_minor));
    return to_jsonb(req);
end $$;

create function commerce.require_approved_partner_sheet(p_order_id uuid) returns void
language plpgsql stable security definer set search_path='' as $$
begin
    if exists(select 1 from commerce.partner_sheet_requests where order_id=p_order_id) and not exists(
        select 1 from commerce.partner_sheet_requests r join commerce.orders o on o.id=r.order_id
            join commerce.partners p on p.id=r.partner_id join commerce.users u on u.id=p.user_id
        where r.order_id=p_order_id and r.state='approved' and r.amount_minor=o.quoted_minor
            and p.state='active' and u.registration_state='active') then
        raise exception 'Owner approval required for each partner sheet' using errcode='42501';end if;
end $$;
revoke all on function commerce.require_approved_partner_sheet(uuid) from public,anon,authenticated,service_role;
create function commerce.guard_partner_sheet() returns trigger
language plpgsql security definer set search_path='' as $$
begin
    if tg_table_name='orders' then
        if new.checker_job_id is distinct from old.checker_job_id and new.checker_job_id is not null then
            perform commerce.require_approved_partner_sheet(new.id);end if;
    elsif tg_table_name='reservations' then
        if new.state='held' then perform commerce.require_approved_partner_sheet(new.order_id);end if;
    else perform commerce.require_approved_partner_sheet(new.order_id);
    end if;
    return new;
end $$;
revoke all on function commerce.guard_partner_sheet() from public,anon,authenticated,service_role;
create trigger guard_partner_sheet_reservation before insert or update on commerce.reservations
    for each row execute function commerce.guard_partner_sheet();
create trigger guard_partner_sheet_invoice before insert on commerce.payment_requests
    for each row execute function commerce.guard_partner_sheet();
create trigger guard_partner_sheet_job before update of checker_job_id on commerce.orders
    for each row execute function commerce.guard_partner_sheet();

alter function commerce_rpc.checkout_state(uuid,bigint) rename to checkout_state_before_partner_sheet;
alter function commerce_rpc.checkout_state_before_partner_sheet(uuid,bigint) set schema commerce;
revoke all on function commerce.checkout_state_before_partner_sheet(uuid,bigint) from public,anon,authenticated,service_role;
create function commerce_rpc.checkout_state(p_order_id uuid,p_telegram_user_id bigint) returns jsonb
language plpgsql security definer set search_path='' as $$
declare result jsonb;
begin
    result:=commerce.checkout_state_before_partner_sheet(p_order_id,p_telegram_user_id);
    return result||jsonb_build_object('partner_sheet',(select to_jsonb(r) from commerce.partner_sheet_requests r where order_id=p_order_id));
end $$;

-- Persistent notification claims: uncertain sends are never retried blindly.
create function commerce_rpc.claim_partner_sheet_notification() returns jsonb
language plpgsql security definer set search_path='' as $$
declare result jsonb;
begin
    update commerce.partner_sheet_notifications set state='uncertain',updated_at=now()
        where state='sending' and updated_at<now()-interval '2 minutes';
    with next as (select id from commerce.partner_sheet_notifications where state='pending'
        order by created_at,id for update skip locked limit 1)
    update commerce.partner_sheet_notifications n set state='sending',token=gen_random_uuid(),updated_at=now()
        from next where n.id=next.id returning to_jsonb(n) into result;
    return result;
end $$;
create function commerce_rpc.complete_partner_sheet_notification(p_id uuid,p_token uuid,p_state text,p_message_id bigint default null) returns jsonb
language plpgsql security definer set search_path='' as $$
begin
    if p_state not in ('sent','uncertain','rejected') or p_state is null or (p_state='sent' and coalesce(p_message_id,0)<=0) then
        raise exception 'Telegram delivery evidence required' using errcode='22023';end if;
    update commerce.partner_sheet_notifications set state=p_state,telegram_message_id=p_message_id,updated_at=now()
        where id=p_id and token=p_token and state='sending';
    return jsonb_build_object('saved',found);
end $$;
create function commerce_rpc.partner_sheet_notification_status(p_actor_telegram_id bigint) returns jsonb
language plpgsql stable security definer set search_path='' as $$
begin
    perform commerce.require_sheet_admin(p_actor_telegram_id);
    return (select coalesce(jsonb_object_agg(state,n),'{}') from (
        select state,count(*) n from commerce.partner_sheet_notifications group by state) counts);
end $$;

-- Preserve ordinary settlement; partner cost orders do not consume referral eligibility.
CREATE OR REPLACE FUNCTION commerce.settle_order(p_order_id uuid)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO ''
AS $function$

declare u uuid; o commerce.orders%rowtype; r commerce.reservations%rowtype; w commerce.wallets%rowtype;

        earned numeric; refunded numeric; total numeric; source_account text; row_data record;

        j uuid; final_state text; v_ref commerce.referrals%rowtype; v_partner commerce.partners%rowtype;

begin

    select user_id into u from commerce.orders where id = p_order_id;

    if not found then raise exception 'Unknown order' using errcode = '22023'; end if;

    perform 1 from commerce.users where id = u for update;

    select * into o from commerce.orders where id = p_order_id for update;

    if o.payment_state in ('settled','refunded','partial_refund') then

        return jsonb_build_object('state',o.payment_state,'charged_minor',o.final_charge_minor,

            'refunded_minor',o.quoted_minor-o.final_charge_minor,'replayed',true);

    end if;

    if o.payment_state <> 'reserved' or o.execution_state <> 'terminal' or o.delivery_state <> 'confirmed'

       or o.execution_terminal_at is null or o.result_sha256 is null or o.delivery_file_id is null

       or o.refund_consent_reference is null then

        raise exception 'Settlement requires funded terminal rows, delivery and consent' using errcode = '22023'; end if;

    if (select count(*) from commerce.checks where order_id = o.id and terminal_at is not null) <> o.requested_checks then

        raise exception 'Incomplete terminal coverage' using errcode = '22023'; end if;

    select sum(unit_quote_minor::numeric-discount_minor),

        coalesce(sum(unit_quote_minor::numeric-discount_minor) filter (where verified_result),0)

        into total,earned from commerce.checks where order_id = o.id;

    if not coalesce((o.pricing_snapshot->>'partner_future_offer')::boolean,false) and total is distinct from o.quoted_minor::numeric then raise exception 'Settlement quote mismatch' using errcode = '22023'; end if;

    if coalesce((o.pricing_snapshot->>'partner_future_offer')::boolean,false) then

        select sum(unit_quote_minor::numeric) into total from commerce.checks where order_id=o.id;

    end if;

    if total is distinct from o.quoted_minor::numeric then raise exception 'Settlement ceiling mismatch' using errcode='22023';end if;

    refunded := total-earned;

    select * into r from commerce.reservations where order_id = o.id for update;

    if not found or r.state <> 'held' or r.amount_minor <> total then raise exception 'Reservation is not held' using errcode = '22023'; end if;

    select * into w from commerce.wallets where user_id = u and kind = 'customer' and currency = 'EGP' for update;

    if r.wallet_id is not null then

        if r.wallet_id <> w.id or w.reserved_minor < r.amount_minor then raise exception 'Wallet reservation mismatch' using errcode = '22023'; end if;

        source_account := 'wallet:'||w.id||':reserved';

    else source_account := 'direct:'||o.id||':reserved'; end if;

    if earned > 0 then

        perform commerce.post_pair('service:'||o.id,'service',o.id,source_account,'platform:revenue',earned::numeric,r.wallet_id,null);

    end if;

    for row_data in select row_number,case when coalesce((o.pricing_snapshot->>'partner_future_offer')::boolean,false)

            then case when verified_result then discount_minor else unit_quote_minor end

            else case when not verified_result then unit_quote_minor-discount_minor else 0 end end as amount

        from commerce.checks where order_id=o.id and

            case when coalesce((o.pricing_snapshot->>'partner_future_offer')::boolean,false)

                then (not verified_result and unit_quote_minor>0) or (verified_result and discount_minor>0)

                else not verified_result and unit_quote_minor>discount_minor end loop

        j := commerce.post_pair('refund:'||o.id||':'||row_data.row_number,'refund',o.id,source_account,

            'wallet:'||w.id||':available',row_data.amount,r.wallet_id,w.id);

        insert into commerce.refunds(order_id,row_number,amount_minor,destination,policy_version,consent_reference,

            state,journal_id,idempotency_key)

        values (o.id,row_data.row_number,row_data.amount,'wallet',o.pricing_snapshot->>'policy_version',

            o.refund_consent_reference,'confirmed',j,'refund:'||o.id||':'||row_data.row_number);

    end loop;

    update commerce.wallets set available_minor = available_minor+refunded::numeric,

        reserved_minor = reserved_minor-case when r.wallet_id is not null then r.amount_minor else 0 end where id = w.id;

    update commerce.reservations set state = 'settled' where id = r.id;

    if r.payment_id is not null then update commerce.payments set state = 'settled' where id = r.payment_id; end if;

    final_state := case when refunded = 0 then 'settled' when earned = 0 then 'refunded' else 'partial_refund' end;

    if earned > 0 and not exists(select 1 from commerce.partner_sheet_requests where order_id=o.id) then

        update commerce.users set first_paid_service_at = coalesce(first_paid_service_at,now()) where id = u;

        update commerce.referrals set locked_at = coalesce(locked_at,now()) where user_id = u;

        -- An eligible source entered after quoting but before first paid service

        -- receives a terms snapshot when this first paid operation is finalized.

        if o.referral_partner_id is null then

            select * into v_ref from commerce.referrals where user_id = u;

            if found then

                select * into v_partner from commerce.partners where id = v_ref.partner_id;

                update commerce.orders set referral_partner_id = v_ref.partner_id,partner_terms_snapshot =

                    jsonb_build_object('partner_id',v_partner.id,'state',v_partner.state,'terms_version',v_partner.terms_version,

                        'basis_points',v_partner.commission_basis_points,'mode',v_partner.entitlement_mode,

                        'checks',v_partner.entitlement_checks,'duration_seconds',v_partner.entitlement_duration_seconds,

                        'anchor','first_paid_service','count_basis','successful_paid_checks') where id = o.id;

            end if;

        end if;

    end if;

    update commerce.orders set payment_state = final_state,order_state = 'closed',final_charge_minor = earned::numeric where id = o.id;

    return jsonb_build_object('state',final_state,'charged_minor',earned::numeric,'refunded_minor',refunded::numeric,'replayed',false);

end $function$;
CREATE OR REPLACE FUNCTION commerce.settle_order_commissions(p_order_id uuid)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO ''
AS $function$

declare o commerce.orders%rowtype; u commerce.users%rowtype; w commerce.wallets%rowtype;

    ref commerce.referrals%rowtype; result jsonb; row_data record; snapshot jsonb;

    paid_count bigint; eligible_count bigint:=0; prior_count bigint; amount numeric; total numeric:=0;

    prior_anchor timestamptz; prior_locked timestamptz; promo_held numeric; promo_refund numeric;

    ordinal bigint; rate integer; eligible boolean; mode text; journal uuid; pid uuid;

begin

    select usr.* into u from commerce.users usr join commerce.orders ord on ord.user_id=usr.id

        where ord.id=p_order_id for update of usr;

    if not found then raise exception 'Unknown order' using errcode='22023'; end if;

    select * into o from commerce.orders where id=p_order_id for update;

    if coalesce((o.pricing_snapshot->>'partner_future_offer')::boolean,false) then return commerce.settle_partner_offer(p_order_id);end if;

    if o.commission_finalized_at is not null then

        select coalesce(sum(amount_minor),0),count(*) into total,eligible_count from commerce.commissions where order_id=o.id;

        return jsonb_build_object('state',o.payment_state,'charged_minor',o.final_charge_minor,

            'collected_minor',o.collected_cash_minor,

            'refunded_minor',o.quoted_minor-o.final_charge_minor,'commission_minor',total::numeric,

            'eligible_paid_checks',eligible_count,'replayed',true);

    end if;

    if o.referral_partner_id is null and not exists(select 1 from commerce.partner_sheet_requests where order_id=o.id) then

        select * into ref from commerce.referrals where user_id=u.id;

        if found then

            if ref.terms_snapshot='{}' then raise exception 'Referral terms require historical review' using errcode='22023'; end if;

            update commerce.orders set referral_partner_id=ref.partner_id,partner_terms_snapshot=ref.terms_snapshot where id=o.id;

        end if;

    end if;

    prior_anchor:=u.first_paid_service_at;

    select locked_at into prior_locked from commerce.referrals where user_id=u.id;

    select promotional_minor into promo_held from commerce.reservations where order_id=o.id;

    select coalesce(sum(promotional_payment_minor),0) into promo_refund from commerce.checks where order_id=o.id and not verified_result;

    if coalesce(promo_held,0)>0 then

        update commerce.wallets set promotional_reserved_minor=promotional_reserved_minor-promo_held where user_id=u.id and kind='customer';

    end if;

    result:=commerce.settle_order(o.id);

    if promo_refund>0 then

        update commerce.wallets set promotional_available_minor=promotional_available_minor+promo_refund where user_id=u.id and kind='customer';

    end if;

    if not exists(select 1 from commerce.checks where order_id=o.id and verified_result and unit_quote_minor>discount_minor+promotional_payment_minor) then

        update commerce.users set first_paid_service_at=prior_anchor where id=u.id;

        update commerce.referrals set locked_at=prior_locked where user_id=u.id;

    end if;

    update commerce.orders set collected_cash_minor=final_charge_minor-(select coalesce(sum(promotional_payment_minor),0) from commerce.checks where order_id=o.id and verified_result) where id=o.id;

    select * into o from commerce.orders where id=o.id;

    select * into u from commerce.users where id=o.user_id;

    prior_count:=u.successful_paid_checks;

    select count(*) into paid_count from commerce.checks where order_id=o.id and verified_result and unit_quote_minor>discount_minor+promotional_payment_minor;

    snapshot:=o.partner_terms_snapshot;pid:=o.referral_partner_id;
    if exists(select 1 from commerce.partner_sheet_requests where order_id=o.id) then pid:=null;paid_count:=0;end if;

    if pid is not null and paid_count>0 then

        if snapshot->>'partner_id' is distinct from pid::text or snapshot->>'state' is distinct from 'active'

           or snapshot->>'basis_points' is null or snapshot->>'mode' is null then

            -- Pending/inactive quoted terms earn no commission; keep attribution.

            eligible:=false;

        else eligible:=true; end if;

        if eligible then

            rate:=(snapshot->>'basis_points')::integer;mode:=snapshot->>'mode';

            if rate not between 0 and 10000 or mode not in ('checks','duration','forever')

               or (mode='checks' and coalesce((snapshot->>'checks')::bigint,0)<=0)

               or (mode='duration' and coalesce((snapshot->>'duration_seconds')::bigint,0)<=0) then

                raise exception 'Invalid frozen commission terms' using errcode='22023'; end if;

            select wal.* into w from commerce.wallets wal join commerce.partners p on p.user_id=wal.user_id

                where p.id=pid and wal.kind='partner' and wal.currency='EGP' for update of wal;

            if not found then raise exception 'Partner earnings wallet unavailable' using errcode='22023'; end if;

            ordinal:=prior_count;

            for row_data in select row_number,unit_quote_minor-discount_minor-promotional_payment_minor as revenue from commerce.checks

                where order_id=o.id and verified_result and unit_quote_minor>discount_minor+promotional_payment_minor order by row_number loop

                ordinal:=ordinal+1;

                if (mode='checks' and ordinal>(snapshot->>'checks')::bigint)

                   or (mode='duration' and extract(epoch from (now()-u.first_paid_service_at))>=(snapshot->>'duration_seconds')::bigint) then

                    continue;

                end if;

                amount:=row_data.revenue::numeric*rate/10000;journal:=null;

                if amount>0 then

                    journal:=commerce.post_pair('commission:'||o.id||':'||row_data.row_number,'commission',o.id,

                        'platform:revenue','wallet:'||w.id||':available',amount,null,w.id);

                end if;

                insert into commerce.commissions(order_id,row_number,partner_id,revenue_minor,amount_minor,terms_snapshot,state,journal_id)

                values(o.id,row_data.row_number,pid,row_data.revenue,amount,snapshot||jsonb_build_object(

                    'paid_check_ordinal',ordinal,'basis','collected_after_discounts_and_refunds','expenses_deducted',false,

                    'promotional_payment_excluded',true,

                    'rounding','exact_decimal','duration_measurement','paid_service_settled_at'),'available',journal);

                total:=total+amount;eligible_count:=eligible_count+1;

            end loop;

            update commerce.wallets set available_minor=available_minor+total::numeric where id=w.id;

        end if;

    end if;

    update commerce.users set successful_paid_checks=successful_paid_checks+paid_count where id=u.id;

    update commerce.orders set financial_completed_at=now(),commission_finalized_at=now() where id=o.id;

    return result||jsonb_build_object('collected_minor',o.collected_cash_minor,'commission_minor',total::numeric,'eligible_paid_checks',eligible_count);

end $function$;
CREATE OR REPLACE FUNCTION commerce.reserve_order_before_expiry_lots(p_order_id uuid, p_telegram_user_id bigint, p_consent_reference text, p_payment_id uuid DEFAULT NULL::uuid)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO ''
AS $function$

declare o commerce.orders%rowtype;w commerce.wallets%rowtype;held numeric:=0;remaining numeric;row_data record;result jsonb;

begin

    perform 1 from commerce.users where telegram_user_id=p_telegram_user_id and registration_state='active' for update;

    select ord.* into o from commerce.orders ord join commerce.users u on u.id=ord.user_id

        where ord.id=p_order_id and u.telegram_user_id=p_telegram_user_id for update of ord;

    if not found then raise exception 'Owned checkout required' using errcode='22023'; end if;

    if exists(select 1 from commerce.reservations where order_id=o.id) then

        return commerce.reserve_order_consent(p_order_id,p_telegram_user_id,p_consent_reference,p_payment_id); end if;

    if p_payment_id is null then

        select * into w from commerce.wallets where user_id=o.user_id and kind='customer' for update;

        if coalesce((o.pricing_snapshot->>'partner_future_offer')::boolean,false) or exists(select 1 from commerce.partner_sheet_requests where order_id=o.id) then

            if w.available_minor-w.promotional_available_minor<o.quoted_minor then

                return jsonb_build_object('state','insufficient_balance','available_minor',w.available_minor-w.promotional_available_minor,'cash_only',true);end if;

        elsif w.available_minor>=o.quoted_minor then

            held:=least(w.promotional_available_minor,o.quoted_minor);remaining:=held;

            -- Spend promotional credit first, preserving the paid-money portion.

            for row_data in select * from commerce.checks where order_id=o.id order by row_number loop

                update commerce.checks set promotional_payment_minor=least(remaining,row_data.unit_quote_minor-row_data.discount_minor)

                    where order_id=o.id and row_number=row_data.row_number;

                remaining:=greatest(remaining-(row_data.unit_quote_minor-row_data.discount_minor),0);

            end loop;

            update commerce.wallets set promotional_available_minor=promotional_available_minor-held where id=w.id;

        end if;

    end if;

    result:=commerce.reserve_order_consent(p_order_id,p_telegram_user_id,p_consent_reference,p_payment_id);

    if result->>'state'='held' and held>0 then

        update commerce.reservations set promotional_minor=held where order_id=o.id;

        update commerce.wallets set promotional_reserved_minor=promotional_reserved_minor+held where id=w.id;

    end if;

    return result;

end $function$;
revoke all on function commerce_rpc.partner_sheet_context(bigint) from public,anon,authenticated;
grant execute on function commerce_rpc.partner_sheet_context(bigint) to service_role;
revoke all on function commerce_rpc.configure_partner_sheet_price(bigint,numeric,text) from public,anon,authenticated;
grant execute on function commerce_rpc.configure_partner_sheet_price(bigint,numeric,text) to service_role;
revoke all on function commerce_rpc.create_order(bigint,text,jsonb,jsonb) from public,anon,authenticated;
grant execute on function commerce_rpc.create_order(bigint,text,jsonb,jsonb) to service_role;
revoke all on function commerce_rpc.partner_sheet_requests(bigint,integer,uuid) from public,anon,authenticated;
grant execute on function commerce_rpc.partner_sheet_requests(bigint,integer,uuid) to service_role;
revoke all on function commerce_rpc.decide_partner_sheet(bigint,uuid,boolean,bigint,text) from public,anon,authenticated;
grant execute on function commerce_rpc.decide_partner_sheet(bigint,uuid,boolean,bigint,text) to service_role;
revoke all on function commerce_rpc.checkout_state(uuid,bigint) from public,anon,authenticated;
grant execute on function commerce_rpc.checkout_state(uuid,bigint) to service_role;
revoke all on function commerce_rpc.claim_partner_sheet_notification() from public,anon,authenticated;
grant execute on function commerce_rpc.claim_partner_sheet_notification() to service_role;
revoke all on function commerce_rpc.complete_partner_sheet_notification(uuid,uuid,text,bigint) from public,anon,authenticated;
grant execute on function commerce_rpc.complete_partner_sheet_notification(uuid,uuid,text,bigint) to service_role;
revoke all on function commerce_rpc.partner_sheet_notification_status(bigint) from public,anon,authenticated;
grant execute on function commerce_rpc.partner_sheet_notification_status(bigint) to service_role;
notify pgrst,'reload schema';
commit;
