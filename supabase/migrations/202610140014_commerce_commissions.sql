begin;
-- Historical closed orders require a reviewed backfill, never guessed ordinals.
do $$ begin
    if exists(select 1 from commerce.orders where order_state='closed') then
        raise exception 'Review existing closed orders before enabling commission ordinals'; end if;
end $$;
alter table commerce.users add column successful_paid_checks bigint not null default 0 check(successful_paid_checks>=0);
alter table commerce.orders add column financial_completed_at timestamptz;
alter table commerce.orders add column commission_finalized_at timestamptz;
alter table commerce.referrals add column terms_snapshot jsonb not null default '{}';
alter table commerce.withdrawals add column destination text;
alter table commerce.withdrawals add column hold_journal_id uuid unique references commerce.journals(id);
alter table commerce.withdrawals add column decision_actor_id bigint check(decision_actor_id>0);
alter table commerce.withdrawals add column approval_reference text;
alter table commerce.withdrawals add column approved_at timestamptz;
alter table commerce.withdrawals add column paid_actor_id bigint check(paid_actor_id>0);
alter table commerce.withdrawals add column payment_proof text;
alter table commerce.withdrawals add column paid_at timestamptz;
alter table commerce.withdrawals add column rejection_reference text;
alter table commerce.withdrawals add column rejected_actor_id bigint check(rejected_actor_id>0);

create function commerce.partner_snapshot(p_partner_id uuid)
returns jsonb language sql stable set search_path='' as $$
    select jsonb_build_object('partner_id',id,'state',state,'terms_version',terms_version,
        'basis_points',commission_basis_points,'mode',entitlement_mode,'checks',entitlement_checks,
        'duration_seconds',entitlement_duration_seconds,'anchor','first_paid_service',
        'count_basis','successful_paid_checks') from commerce.partners where id=p_partner_id
$$;
create function commerce.capture_referral_terms()
returns trigger language plpgsql set search_path='' as $$
begin
    if new.terms_snapshot='{}' then new.terms_snapshot:=commerce.partner_snapshot(new.partner_id); end if;
    return new;
end $$;
create trigger capture_referral_terms before insert on commerce.referrals for each row execute function commerce.capture_referral_terms();
revoke all on function commerce.partner_snapshot(uuid),commerce.capture_referral_terms() from public,anon,authenticated,service_role;

-- Lock partner terms before the capped code, matching configure_partner's order.
alter function commerce_rpc.attribute_referral(bigint,text,text) set schema commerce;
revoke all on function commerce.attribute_referral(bigint,text,text) from public,anon,authenticated,service_role;
create function commerce_rpc.attribute_referral(p_telegram_user_id bigint,p_code text,p_source text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare u uuid;
begin
    u:=(commerce_rpc.register_telegram_user(p_telegram_user_id,null)->>'user_id')::uuid;
    perform 1 from commerce.users where id=u for update;
    perform 1 from commerce.partners p join commerce.codes c on c.partner_id=p.id
        where c.code=upper(btrim(p_code)) and c.kind='referral' for share of p;
    return commerce.attribute_referral(p_telegram_user_id,p_code,p_source);
end $$;

alter function commerce_rpc.settle_order(uuid) set schema commerce;
revoke all on function commerce.settle_order(uuid) from public,anon,authenticated,service_role;
create function commerce_rpc.settle_order(p_order_id uuid)
returns jsonb language plpgsql security definer set search_path='' as $$
declare o commerce.orders%rowtype; u commerce.users%rowtype; w commerce.wallets%rowtype;
    ref commerce.referrals%rowtype; result jsonb; row_data record; snapshot jsonb;
    paid_count bigint; eligible_count bigint:=0; prior_count bigint; amount bigint; total numeric:=0;
    ordinal bigint; rate integer; eligible boolean; mode text; journal uuid; pid uuid;
begin
    select usr.* into u from commerce.users usr join commerce.orders ord on ord.user_id=usr.id
        where ord.id=p_order_id for update of usr;
    if not found then raise exception 'Unknown order' using errcode='22023'; end if;
    select * into o from commerce.orders where id=p_order_id for update;
    if o.commission_finalized_at is not null then
        select coalesce(sum(amount_minor),0),count(*) into total,eligible_count from commerce.commissions where order_id=o.id;
        return jsonb_build_object('state',o.payment_state,'charged_minor',o.final_charge_minor,
            'refunded_minor',o.quoted_minor-o.final_charge_minor,'commission_minor',total::bigint,
            'eligible_paid_checks',eligible_count,'replayed',true);
    end if;
    if o.referral_partner_id is null then
        select * into ref from commerce.referrals where user_id=u.id;
        if found then
            if ref.terms_snapshot='{}' then raise exception 'Referral terms require historical review' using errcode='22023'; end if;
            update commerce.orders set referral_partner_id=ref.partner_id,partner_terms_snapshot=ref.terms_snapshot where id=o.id;
        end if;
    end if;
    result:=commerce.settle_order(o.id);
    select * into o from commerce.orders where id=o.id;
    select * into u from commerce.users where id=o.user_id;
    prior_count:=u.successful_paid_checks;
    select count(*) into paid_count from commerce.checks where order_id=o.id and verified_result and unit_quote_minor>discount_minor;
    snapshot:=o.partner_terms_snapshot;pid:=o.referral_partner_id;
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
            for row_data in select row_number,unit_quote_minor-discount_minor as revenue from commerce.checks
                where order_id=o.id and verified_result and unit_quote_minor>discount_minor order by row_number loop
                ordinal:=ordinal+1;
                if (mode='checks' and ordinal>(snapshot->>'checks')::bigint)
                   or (mode='duration' and extract(epoch from (now()-u.first_paid_service_at))>=(snapshot->>'duration_seconds')::bigint) then
                    continue;
                end if;
                amount:=round(row_data.revenue::numeric*rate/10000)::bigint;journal:=null;
                if amount>0 then
                    journal:=commerce.post_pair('commission:'||o.id||':'||row_data.row_number,'commission',o.id,
                        'platform:revenue','wallet:'||w.id||':available',amount,null,w.id);
                end if;
                insert into commerce.commissions(order_id,row_number,partner_id,revenue_minor,amount_minor,terms_snapshot,state,journal_id)
                values(o.id,row_data.row_number,pid,row_data.revenue,amount,snapshot||jsonb_build_object(
                    'paid_check_ordinal',ordinal,'basis','collected_after_discounts_and_refunds','expenses_deducted',false,
                    'rounding','nearest_piastre_half_up_per_check','duration_measurement','paid_service_settled_at'),'available',journal);
                total:=total+amount;eligible_count:=eligible_count+1;
            end loop;
            update commerce.wallets set available_minor=available_minor+total::bigint where id=w.id;
        end if;
    end if;
    update commerce.users set successful_paid_checks=successful_paid_checks+paid_count where id=u.id;
    update commerce.orders set financial_completed_at=now(),commission_finalized_at=now() where id=o.id;
    return result||jsonb_build_object('commission_minor',total::bigint,'eligible_paid_checks',eligible_count);
end $$;

create function commerce_rpc.partner_finance(p_telegram_user_id bigint)
returns jsonb language plpgsql stable security definer set search_path='' as $$
declare pid uuid; uid uuid; w commerce.wallets%rowtype;
begin
    select p.id,p.user_id into pid,uid from commerce.partners p join commerce.users u on u.id=p.user_id
        where u.telegram_user_id=p_telegram_user_id;
    if not found then return jsonb_build_object('is_partner',false); end if;
    select * into w from commerce.wallets where user_id=uid and kind='partner';
    return jsonb_build_object('is_partner',true,'partner_id',pid,'available_minor',w.available_minor,
        'referral_code',(select code from commerce.codes where partner_id=pid and kind='referral' limit 1),
        'reserved_minor',w.reserved_minor,'withdrawn_minor',w.withdrawn_minor,
        'pending_commission_minor',(select coalesce(sum(amount_minor),0) from commerce.commissions where partner_id=pid and state='pending'),
        'today_earned_minor',(select coalesce(sum(amount_minor),0) from commerce.commissions where partner_id=pid
            and created_at>=date_trunc('day',now() at time zone 'Asia/Baghdad') at time zone 'Asia/Baghdad' and state<>'reversed'),
        'clients',(select count(*) from commerce.referrals where partner_id=pid),
        'paid_checks',(select count(*) from commerce.checks c join commerce.orders o on o.id=c.order_id
            where o.referral_partner_id=pid and o.commission_finalized_at is not null and c.verified_result and c.unit_quote_minor>c.discount_minor),
        'collected_minor',(select coalesce(sum(final_charge_minor),0) from commerce.orders where referral_partner_id=pid and commission_finalized_at is not null),
        'awaiting_settlement_orders',(select count(*) from commerce.orders where referral_partner_id=pid and payment_state='reserved'));
end $$;

create or replace function commerce_rpc.wallet_state(p_telegram_user_id bigint)
returns jsonb language sql stable security definer set search_path='' as $$
    select jsonb_build_object('user_id',u.id,'currency','EGP',
        'is_partner',exists(select 1 from commerce.partners where user_id=u.id),
        'customer',coalesce((select jsonb_build_object('available_minor',available_minor,'reserved_minor',reserved_minor)
            from commerce.wallets where user_id=u.id and kind='customer'),'{}'::jsonb),
        'partner',coalesce((select jsonb_build_object('available_minor',available_minor,'reserved_minor',reserved_minor,'withdrawn_minor',withdrawn_minor)
            from commerce.wallets where user_id=u.id and kind='partner'),'{}'::jsonb))
    from commerce.users u where u.telegram_user_id=p_telegram_user_id
$$;

create function commerce_rpc.request_withdrawal(p_telegram_user_id bigint,p_key text,p_amount_minor bigint,p_destination text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare uid uuid; pid uuid; w commerce.wallets%rowtype; request commerce.withdrawals%rowtype; j uuid;
begin
    if p_key is null or length(p_key) not between 1 and 160 or p_amount_minor is null or p_amount_minor<=0
       or p_destination is null or length(btrim(p_destination)) not between 3 and 500 then
        raise exception 'Withdrawal identity, amount and destination required' using errcode='22023'; end if;
    select id into uid from commerce.users where telegram_user_id=p_telegram_user_id and registration_state='active' for update;
    select id into pid from commerce.partners where user_id=uid and state='active' for share;
    if not found then raise exception 'Active partner required' using errcode='22023'; end if;
    select * into request from commerce.withdrawals where idempotency_key=p_key;
    if found then
        if request.partner_id<>pid or request.amount_minor<>p_amount_minor or request.destination<>btrim(p_destination) then
            raise exception 'Conflicting withdrawal replay' using errcode='22023'; end if;
        return jsonb_build_object('withdrawal_id',request.id,'state',request.state,'replayed',true);
    end if;
    select * into w from commerce.wallets where user_id=uid and kind='partner' for update;
    if w.available_minor<p_amount_minor then
        return jsonb_build_object('state','insufficient_balance','available_minor',w.available_minor); end if;
    insert into commerce.withdrawals(partner_id,amount_minor,idempotency_key,destination)
        values(pid,p_amount_minor,p_key,btrim(p_destination)) returning * into request;
    j:=commerce.post_pair('withdrawal:hold:'||request.id,'reserve',null,'wallet:'||w.id||':available',
        'wallet:'||w.id||':reserved',p_amount_minor,w.id,w.id);
    update commerce.wallets set available_minor=available_minor-p_amount_minor,reserved_minor=reserved_minor+p_amount_minor where id=w.id;
    update commerce.withdrawals set hold_journal_id=j where id=request.id;
    return jsonb_build_object('withdrawal_id',request.id,'state','requested','replayed',false);
end $$;

create function commerce_rpc.withdrawal_record(p_withdrawal_id uuid)
returns jsonb language plpgsql stable security definer set search_path='' as $$
declare result jsonb;
begin
    select to_jsonb(wd)||jsonb_build_object('telegram_user_id',u.telegram_user_id) into result
        from commerce.withdrawals wd join commerce.partners p on p.id=wd.partner_id join commerce.users u on u.id=p.user_id
        where wd.id=p_withdrawal_id;
    if result is null then raise exception 'Unknown withdrawal' using errcode='22023'; end if;
    return result;
end $$;
create function commerce_rpc.partner_withdrawals(p_telegram_user_id bigint,p_offset integer default 0)
returns jsonb language plpgsql stable security definer set search_path='' as $$
begin
    if p_offset is null or p_offset not between 0 and 1000000 then raise exception 'Invalid withdrawal page' using errcode='22023'; end if;
    return coalesce((select jsonb_agg(to_jsonb(record)) from
        (select wd.id,wd.amount_minor,wd.destination,wd.state,wd.payout_reference,wd.created_at
         from commerce.withdrawals wd join commerce.partners p on p.id=wd.partner_id join commerce.users u on u.id=p.user_id
         where u.telegram_user_id=p_telegram_user_id order by wd.created_at desc,wd.id desc limit 10 offset p_offset) record),'[]'::jsonb);
end $$;
create function commerce_rpc.list_withdrawals(p_offset integer default 0)
returns jsonb language plpgsql stable security definer set search_path='' as $$
begin
    if p_offset is null or p_offset not between 0 and 1000000 then raise exception 'Invalid withdrawal page' using errcode='22023'; end if;
    return coalesce((select jsonb_agg(commerce_rpc.withdrawal_record(id)) from
        (select id from commerce.withdrawals where state in ('requested','approved') order by created_at,id limit 10 offset p_offset) wd),'[]'::jsonb);
end $$;

create function commerce_rpc.approve_withdrawal(p_withdrawal_id uuid,p_actor_telegram_id bigint,p_reference text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare r commerce.withdrawals%rowtype;
begin
    if p_actor_telegram_id is null or p_actor_telegram_id<=0 or p_reference is null or length(btrim(p_reference)) not between 1 and 1000 then
        raise exception 'Authenticated withdrawal approval required' using errcode='22023'; end if;
    select * into r from commerce.withdrawals where id=p_withdrawal_id for update;
    if not found or r.hold_journal_id is null or r.state='rejected' then raise exception 'Held withdrawal required' using errcode='22023'; end if;
    if r.state in ('approved','paid') then return jsonb_build_object('state',r.state,'replayed',true); end if;
    update commerce.withdrawals set state='approved',decision_actor_id=p_actor_telegram_id,approval_reference=p_reference,approved_at=now() where id=r.id;
    return jsonb_build_object('state','approved','replayed',false);
end $$;

create function commerce_rpc.complete_withdrawal(p_withdrawal_id uuid,p_actor_telegram_id bigint,p_provider text,p_reference text,p_proof text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare r commerce.withdrawals%rowtype; w commerce.wallets%rowtype; ref text; j uuid;
begin
    if p_actor_telegram_id is null or p_actor_telegram_id<=0 or p_provider is null or p_provider !~ '^[a-z0-9_]{1,40}$'
       or p_reference is null or length(btrim(p_reference)) not between 1 and 200 or p_proof is null or length(btrim(p_proof)) not between 1 and 1000 then
        raise exception 'Authenticated external payout receipt required' using errcode='22023'; end if;
    ref:=p_provider||':'||upper(btrim(p_reference));
    select * into r from commerce.withdrawals where id=p_withdrawal_id for update;
    if not found then raise exception 'Unknown withdrawal' using errcode='22023'; end if;
    if r.state='paid' then
        if r.payout_reference<>ref then raise exception 'Payout receipt differs from recorded withdrawal' using errcode='22023'; end if;
        return jsonb_build_object('state','paid','replayed',true);
    end if;
    if r.state<>'approved' or r.hold_journal_id is null then raise exception 'Approved held withdrawal required' using errcode='22023'; end if;
    select wal.* into w from commerce.wallets wal join commerce.partners p on p.user_id=wal.user_id
        where p.id=r.partner_id and wal.kind='partner' for update of wal;
    if w.reserved_minor<r.amount_minor then raise exception 'Withdrawal reserve mismatch' using errcode='22023'; end if;
    if exists(select 1 from commerce.withdrawals where payout_reference=ref) then raise exception 'Payout receipt already assigned' using errcode='22023'; end if;
    j:=commerce.post_pair('withdrawal:paid:'||r.id,'withdrawal',null,'wallet:'||w.id||':reserved',
        'external:payout:'||p_provider,r.amount_minor,w.id,null);
    update commerce.wallets set reserved_minor=reserved_minor-r.amount_minor,withdrawn_minor=withdrawn_minor+r.amount_minor where id=w.id;
    update commerce.withdrawals set state='paid',payout_reference=ref,journal_id=j,paid_actor_id=p_actor_telegram_id,payment_proof=p_proof,paid_at=now() where id=r.id;
    return jsonb_build_object('state','paid','replayed',false);
end $$;
create function commerce_rpc.reject_withdrawal(p_withdrawal_id uuid,p_actor_telegram_id bigint,p_reference text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare r commerce.withdrawals%rowtype; w commerce.wallets%rowtype;
begin
    if p_actor_telegram_id is null or p_actor_telegram_id<=0 or p_reference is null or length(btrim(p_reference)) not between 1 and 1000 then
        raise exception 'Authenticated withdrawal rejection required' using errcode='22023'; end if;
    select * into r from commerce.withdrawals where id=p_withdrawal_id for update;
    if not found or r.state='paid' then raise exception 'Unpaid withdrawal required' using errcode='22023'; end if;
    if r.state='rejected' then return jsonb_build_object('state','rejected','replayed',true); end if;
    select wal.* into w from commerce.wallets wal join commerce.partners p on p.user_id=wal.user_id
        where p.id=r.partner_id and wal.kind='partner' for update of wal;
    if r.hold_journal_id is null or w.reserved_minor<r.amount_minor then raise exception 'Withdrawal reserve mismatch' using errcode='22023'; end if;
    perform commerce.post_pair('withdrawal:reject:'||r.id,'release',null,'wallet:'||w.id||':reserved',
        'wallet:'||w.id||':available',r.amount_minor,w.id,w.id);
    update commerce.wallets set reserved_minor=reserved_minor-r.amount_minor,available_minor=available_minor+r.amount_minor where id=w.id;
    update commerce.withdrawals set state='rejected',decision_actor_id=coalesce(decision_actor_id,p_actor_telegram_id),
        rejected_actor_id=p_actor_telegram_id,rejection_reference=p_reference where id=r.id;
    return jsonb_build_object('state','rejected','replayed',false);
end $$;

revoke all on function commerce_rpc.attribute_referral(bigint,text,text),commerce_rpc.settle_order(uuid),
    commerce_rpc.partner_finance(bigint),commerce_rpc.request_withdrawal(bigint,text,bigint,text),
    commerce_rpc.partner_withdrawals(bigint,integer),
    commerce_rpc.withdrawal_record(uuid),commerce_rpc.list_withdrawals(integer),
    commerce_rpc.approve_withdrawal(uuid,bigint,text),commerce_rpc.complete_withdrawal(uuid,bigint,text,text,text),
    commerce_rpc.reject_withdrawal(uuid,bigint,text) from public,anon,authenticated;
grant execute on function commerce_rpc.attribute_referral(bigint,text,text),commerce_rpc.settle_order(uuid),
    commerce_rpc.partner_finance(bigint),commerce_rpc.request_withdrawal(bigint,text,bigint,text),
    commerce_rpc.partner_withdrawals(bigint,integer),
    commerce_rpc.withdrawal_record(uuid),commerce_rpc.list_withdrawals(integer),
    commerce_rpc.approve_withdrawal(uuid,bigint,text),commerce_rpc.complete_withdrawal(uuid,bigint,text,text,text),
    commerce_rpc.reject_withdrawal(uuid,bigint,text) to service_role;
create or replace function commerce_rpc.health()
returns jsonb language sql stable security definer set search_path='' as $$
    select jsonb_build_object('status','ok','schema_version',14,'currency','EGP')
$$;
notify pgrst,'reload schema';
commit;
