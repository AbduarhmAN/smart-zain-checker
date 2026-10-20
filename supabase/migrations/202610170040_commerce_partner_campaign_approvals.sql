-- Optional owner review of newly issued partner discount codes only.
-- Old campaigns, quotes, cash credit codes and monetary arithmetic are unchanged.
begin;

create table commerce.partner_campaign_approval_settings (
    version bigint generated always as identity primary key,
    partner_id uuid not null references commerce.partners(id),
    required boolean not null,
    reviewer_telegram_id bigint,
    actor_telegram_id bigint not null,
    reference text not null unique,
    created_at timestamptz not null default now(),
    check ((required and reviewer_telegram_id=actor_telegram_id) or (not required and reviewer_telegram_id is null))
);
create index partner_campaign_approval_settings_latest on commerce.partner_campaign_approval_settings(partner_id,version desc);
create table commerce.partner_campaign_approvals (
    campaign_id uuid primary key references commerce.campaigns(id),
    partner_id uuid not null references commerce.partners(id),
    partner_telegram_id bigint not null,
    reviewer_telegram_id bigint not null,
    setting_version bigint not null references commerce.partner_campaign_approval_settings(version),
    request_key text not null unique,
    code text not null,
    rules_snapshot jsonb not null,
    campaign_snapshot jsonb not null,
    partner_snapshot jsonb not null,
    policy_snapshot jsonb not null,
    creation_reference text not null,
    state text not null default 'pending' check(state in ('pending','approved','rejected')),
    actor_telegram_id bigint,
    decision_reference text unique,
    decided_at timestamptz,
    created_at timestamptz not null default now(),
    check ((state='pending' and actor_telegram_id is null and decision_reference is null and decided_at is null)
       or (state in ('approved','rejected') and actor_telegram_id=reviewer_telegram_id
           and decision_reference is not null and decided_at is not null))
);
create index partner_campaign_approvals_pending on commerce.partner_campaign_approvals(reviewer_telegram_id,created_at,campaign_id) where state='pending';
alter table commerce.partner_campaign_approval_settings enable row level security;
alter table commerce.partner_campaign_approvals enable row level security;
revoke all on commerce.partner_campaign_approval_settings,commerce.partner_campaign_approvals from public,anon,authenticated,service_role;

create function commerce.guard_partner_campaign_approval() returns trigger
language plpgsql security definer set search_path='' as $$
begin
    if tg_table_name='partner_campaign_approvals' then
        if to_jsonb(new)-array['state','actor_telegram_id','decision_reference','decided_at'] is distinct from
           to_jsonb(old)-array['state','actor_telegram_id','decision_reference','decided_at']
           or old.state<>'pending' then
            raise exception 'Campaign approval snapshot and final decision are immutable' using errcode='22023';
        end if;
    elsif tg_table_name='campaigns' then
        if new.state='active' and exists(select 1 from commerce.partner_campaign_approvals a
            where a.campaign_id=new.id and a.state<>'approved') then
            raise exception 'Owner approval required for this partner code' using errcode='42501';end if;
    elsif new.active and exists(select 1 from commerce.partner_campaign_approvals a
        where a.campaign_id=new.campaign_id and a.state<>'approved') then
        raise exception 'Owner approval required for this partner code' using errcode='42501';
    end if;
    return new;
end $$;
create trigger partner_campaign_approval_immutable before update on commerce.partner_campaign_approvals
    for each row execute function commerce.guard_partner_campaign_approval();
create trigger partner_campaign_activation_guard before update on commerce.campaigns
    for each row execute function commerce.guard_partner_campaign_approval();
create trigger partner_campaign_code_activation_guard before insert or update on commerce.codes
    for each row execute function commerce.guard_partner_campaign_approval();
revoke all on function commerce.guard_partner_campaign_approval() from public,anon,authenticated,service_role;

create function commerce_rpc.partner_campaign_approval_setting(p_actor_telegram_id bigint,p_partner_telegram_id bigint)
returns jsonb language plpgsql stable security definer set search_path='' as $$
declare pid uuid;s commerce.partner_campaign_approval_settings%rowtype;
begin
    perform commerce.require_sheet_admin(p_actor_telegram_id);
    select p.id into pid from commerce.partners p join commerce.users u on u.id=p.user_id where u.telegram_user_id=p_partner_telegram_id;
    if not found then raise exception 'Partner required' using errcode='22023';end if;
    select * into s from commerce.partner_campaign_approval_settings where partner_id=pid order by version desc limit 1;
    return jsonb_build_object('required',coalesce(s.required,false),'reviewer_telegram_id',s.reviewer_telegram_id,
        'partner_telegram_id',p_partner_telegram_id,'version',s.version);
end $$;

create function commerce_rpc.configure_partner_campaign_approval(p_actor_telegram_id bigint,p_partner_telegram_id bigint,p_required boolean,p_reference text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare pid uuid;s commerce.partner_campaign_approval_settings%rowtype;
begin
    perform commerce.require_sheet_admin(p_actor_telegram_id);
    if p_required is null or p_reference is null or length(btrim(p_reference)) not between 1 and 300 then
        raise exception 'Reviewed partner approval setting required' using errcode='22023';end if;
    select p.id into pid from commerce.partners p join commerce.users u on u.id=p.user_id
        where u.telegram_user_id=p_partner_telegram_id for update of p;
    if not found then raise exception 'Partner required' using errcode='22023';end if;
    select * into s from commerce.partner_campaign_approval_settings where reference=p_reference;
    if found then
        if s.partner_id<>pid or s.required<>p_required or s.actor_telegram_id<>p_actor_telegram_id then
            raise exception 'Conflicting approval setting replay' using errcode='22023';end if;
        -- A stale replay returns its durable receipt; it cannot undo a later switch.
        return jsonb_build_object('required',s.required,'reviewer_telegram_id',s.reviewer_telegram_id,
            'partner_telegram_id',p_partner_telegram_id,'version',s.version,'replayed',true);
    end if;
    insert into commerce.partner_campaign_approval_settings(partner_id,required,reviewer_telegram_id,actor_telegram_id,reference)
        values(pid,p_required,case when p_required then p_actor_telegram_id end,p_actor_telegram_id,p_reference) returning * into s;
    return jsonb_build_object('required',s.required,'reviewer_telegram_id',s.reviewer_telegram_id,
        'partner_telegram_id',p_partner_telegram_id,'version',s.version,'replayed',false);
end $$;

create or replace function commerce_rpc.partner_campaign_context(p_telegram_user_id bigint)
returns jsonb language plpgsql stable security definer set search_path='' as $$
declare p commerce.partners%rowtype;s commerce.partner_campaign_approval_settings%rowtype;
begin
    select pa.* into p from commerce.partners pa join commerce.users u on u.id=pa.user_id
        where u.telegram_user_id=p_telegram_user_id and u.registration_state='active';
    if not found then return jsonb_build_object('is_partner',false);end if;
    select * into s from commerce.partner_campaign_approval_settings where partner_id=p.id order by version desc limit 1;
    return jsonb_build_object('is_partner',true,'state',p.state,'basis_points',p.commission_basis_points,
        'terms_version',p.terms_version,'policy',commerce_rpc.campaign_policy(),'funding','current_order_original_share',
        'approval_required',coalesce(s.required,false),'reviewer_telegram_id',s.reviewer_telegram_id);
end $$;

alter function commerce_rpc.create_partner_campaign(bigint,text,text,jsonb,text) rename to create_partner_campaign_before_approval;
alter function commerce_rpc.create_partner_campaign_before_approval(bigint,text,text,jsonb,text) set schema commerce;
revoke all on function commerce.create_partner_campaign_before_approval(bigint,text,text,jsonb,text) from public,anon,authenticated,service_role;

create function commerce_rpc.create_partner_campaign(p_telegram_user_id bigint,p_key text,p_code text,p_rules jsonb,p_reference text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare p commerce.partners%rowtype;s commerce.partner_campaign_approval_settings%rowtype;
    c commerce.campaigns%rowtype;a commerce.partner_campaign_approvals%rowtype;result jsonb;
begin
    select pa.* into p from commerce.partners pa join commerce.users u on u.id=pa.user_id
        where u.telegram_user_id=p_telegram_user_id and u.registration_state='active' for update of pa;
    if not found then raise exception 'Active partner required' using errcode='22023';end if;
    result:=commerce.create_partner_campaign_before_approval(p_telegram_user_id,p_key,p_code,p_rules,p_reference);
    select * into c from commerce.campaigns where id=(result->>'campaign_id')::uuid for update;
    select * into a from commerce.partner_campaign_approvals where campaign_id=c.id;
    if coalesce((result->>'replayed')::boolean,false) then
        return result||jsonb_build_object('approval_state',coalesce(a.state,'not_required'),'state',c.state);
    end if;
    select * into s from commerce.partner_campaign_approval_settings where partner_id=p.id order by version desc limit 1;
    if coalesce(s.required,false) then
        -- The delegated issuer and these updates share one SQL transaction:
        -- no active campaign/code is ever visible to another connection.
        update commerce.campaigns set state='draft',approved_at=null where id=c.id returning * into c;
        update commerce.codes set active=false where campaign_id=c.id and kind='discount';
        insert into commerce.partner_campaign_approvals(campaign_id,partner_id,partner_telegram_id,reviewer_telegram_id,
            setting_version,request_key,code,rules_snapshot,campaign_snapshot,partner_snapshot,policy_snapshot,creation_reference)
        values(c.id,p.id,p_telegram_user_id,s.reviewer_telegram_id,s.version,c.rules->>'request_key',c.rules->>'code',
            p_rules,to_jsonb(c),commerce.partner_snapshot(p.id),commerce_rpc.campaign_policy(),p_reference) returning * into a;
        insert into commerce.partner_sheet_notifications(event_key,recipient_telegram_id,payload)
            values('campaign-request:'||c.id,s.reviewer_telegram_id,jsonb_build_object('kind','campaign_request',
                'campaign_id',c.id,'partner_telegram_id',p_telegram_user_id,'reviewer_telegram_id',s.reviewer_telegram_id,
                'code',a.code,'state','pending'));
    end if;
    return result||jsonb_build_object('approval_state',coalesce(a.state,'not_required'),'state',c.state);
end $$;

create function commerce_rpc.partner_campaign_approval_record(p_actor_telegram_id bigint,p_campaign_id uuid)
returns jsonb language plpgsql stable security definer set search_path='' as $$
declare a commerce.partner_campaign_approvals%rowtype;
begin
    perform commerce.require_sheet_admin(p_actor_telegram_id);
    select * into a from commerce.partner_campaign_approvals where campaign_id=p_campaign_id;
    if not found or a.reviewer_telegram_id<>p_actor_telegram_id then
        raise exception 'Assigned campaign reviewer required' using errcode='42501';end if;
    return to_jsonb(a)||jsonb_build_object('approval_state',a.state,'rules',a.rules_snapshot,
        'basis_points',a.partner_snapshot->'basis_points');
end $$;

create function commerce_rpc.list_partner_campaign_approvals(p_actor_telegram_id bigint,p_offset integer default 0)
returns jsonb language plpgsql stable security definer set search_path='' as $$
begin
    perform commerce.require_sheet_admin(p_actor_telegram_id);
    if p_offset is null or p_offset not between 0 and 1000000 then raise exception 'Invalid approval page' using errcode='22023';end if;
    return coalesce((select jsonb_agg(commerce_rpc.partner_campaign_approval_record(p_actor_telegram_id,page.campaign_id))
        from (select campaign_id from commerce.partner_campaign_approvals where state='pending' and reviewer_telegram_id=p_actor_telegram_id
            order by created_at,campaign_id limit 10 offset p_offset) page),'[]'::jsonb);
end $$;

create function commerce_rpc.decide_partner_campaign(p_actor_telegram_id bigint,p_campaign_id uuid,p_approve boolean,p_reference text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare a commerce.partner_campaign_approvals%rowtype;p commerce.partners%rowtype;c commerce.campaigns%rowtype;
    policy commerce.campaign_policies%rowtype;code commerce.codes%rowtype;desired text;
begin
    perform commerce.require_sheet_admin(p_actor_telegram_id);
    if p_approve is null or p_reference is null or length(btrim(p_reference)) not between 1 and 300 then
        raise exception 'Reviewed campaign decision required' using errcode='22023';end if;
    select * into a from commerce.partner_campaign_approvals where campaign_id=p_campaign_id;
    if not found or a.reviewer_telegram_id<>p_actor_telegram_id then
        raise exception 'Assigned campaign reviewer required' using errcode='42501';end if;
    -- Match issuance/configuration lock order: partner before campaign/request.
    select * into p from commerce.partners where id=a.partner_id for update;
    select * into a from commerce.partner_campaign_approvals where campaign_id=p_campaign_id for update;
    select * into c from commerce.campaigns where id=p_campaign_id for update;
    desired:=case when p_approve then 'approved' else 'rejected' end;
    if a.state<>'pending' then
        if a.state<>desired or a.actor_telegram_id<>p_actor_telegram_id or a.decision_reference<>p_reference then
            raise exception 'Campaign decision is already final' using errcode='22023';end if;
        return commerce_rpc.partner_campaign_approval_record(p_actor_telegram_id,p_campaign_id)||jsonb_build_object('replayed',true);
    end if;
    if p_approve then
        if p.state<>'active' or not exists(select 1 from commerce.users where id=p.user_id and registration_state='active')
           or commerce.partner_snapshot(p.id) is distinct from a.partner_snapshot
           or c.state<>'draft' or to_jsonb(c) is distinct from a.campaign_snapshot
           or c.benefit_kind<>'percent' or c.benefit_value>p.commission_basis_points then
            raise exception 'Current active partner and unchanged draft within share required' using errcode='22023';end if;
        select * into code from commerce.codes where campaign_id=c.id and kind='discount' for update;
        if not found or code.code<>a.code or code.active or code.max_redemptions<>(a.rules_snapshot->>'max_redemptions')::bigint then
            raise exception 'Original inactive campaign code required' using errcode='22023';end if;
        select * into policy from commerce.campaign_policies order by version desc limit 1 for share;
        if not found or c.max_checks_per_user>(policy.rules->>'max_checks_per_user')::bigint
           or c.max_discount_minor_per_user>(policy.rules->>'max_discount_minor_per_user')::numeric
           or c.max_total_discount_minor>(policy.rules->>'max_total_discount_minor')::numeric
           or c.activation_duration_seconds>(policy.rules->>'max_duration_seconds')::bigint
           or code.max_redemptions>(policy.rules->>'max_redemptions')::bigint
           or c.benefit_value>(policy.rules->>'max_percent_basis_points')::bigint then
            raise exception 'Campaign exceeds current reviewed limits' using errcode='22023';end if;
    end if;
    update commerce.partner_campaign_approvals set state=desired,actor_telegram_id=p_actor_telegram_id,
        decision_reference=p_reference,decided_at=now() where campaign_id=c.id returning * into a;
    update commerce.campaigns set state=case when p_approve then 'active' else 'ended' end,
        approved_at=case when p_approve then now() else null end,
        actor_telegram_id=p_actor_telegram_id,approval_reference=p_reference where id=c.id;
    update commerce.codes set active=p_approve where campaign_id=c.id and kind='discount';
    insert into commerce.partner_sheet_notifications(event_key,recipient_telegram_id,payload)
        values('campaign-decision:'||c.id,a.partner_telegram_id,jsonb_build_object('kind','campaign_decision',
            'campaign_id',c.id,'partner_telegram_id',a.partner_telegram_id,'reviewer_telegram_id',a.reviewer_telegram_id,
            'code',a.code,'state',a.state));
    return commerce_rpc.partner_campaign_approval_record(p_actor_telegram_id,p_campaign_id)||jsonb_build_object('replayed',false);
end $$;

create or replace function commerce_rpc.list_partner_campaigns(p_telegram_user_id bigint,p_offset integer default 0)
returns jsonb language plpgsql stable security definer set search_path='' as $$
declare pid uuid;
begin
    if p_offset is null or p_offset not between 0 and 1000000 then raise exception 'Invalid campaign page' using errcode='22023';end if;
    select p.id into pid from commerce.partners p join commerce.users u on u.id=p.user_id where u.telegram_user_id=p_telegram_user_id;
    if not found then raise exception 'Active partner required' using errcode='22023';end if;
    return coalesce((select jsonb_agg(to_jsonb(c)) from (select ca.id,ca.name,ca.state,ca.benefit_value,ca.rules->>'code' as code,
        coalesce(a.state,'not_required') as approval_state,ca.max_checks_per_user,ca.max_discount_minor_per_user,
        ca.max_total_discount_minor,ca.activation_duration_seconds,
        (select count(*) from commerce.campaign_usage where campaign_id=ca.id) as activations,
        (select coalesce(sum(discount_minor),0) from commerce.campaign_usage where campaign_id=ca.id) as used_discount_minor,
        (select coalesce(sum(reserved_discount_minor),0) from commerce.campaign_usage where campaign_id=ca.id) as held_discount_minor
        from commerce.campaigns ca left join commerce.partner_campaign_approvals a on a.campaign_id=ca.id
        where ca.partner_id=pid and ca.funder_kind='partner' order by ca.created_at desc,ca.id desc limit 10 offset p_offset) c),'[]'::jsonb);
end $$;

create or replace function commerce_rpc.claim_partner_sheet_notification() returns jsonb
language plpgsql security definer set search_path='' as $$
declare result jsonb;record jsonb;
begin
    update commerce.partner_sheet_notifications set state='uncertain',updated_at=now()
        where state='sending' and updated_at<now()-interval '2 minutes';
    with next as (select id from commerce.partner_sheet_notifications where state='pending'
        order by created_at,id for update skip locked limit 1)
    update commerce.partner_sheet_notifications n set state='sending',token=gen_random_uuid(),updated_at=now()
        from next where n.id=next.id returning to_jsonb(n) into result;
    if result->'payload'->>'kind'='campaign_request' then
        -- Include the immutable review record inside the accepted claim. A
        -- subsequent metadata outage cannot strand an unsent claimed notice.
        select to_jsonb(a)||jsonb_build_object('approval_state',a.state,'rules',a.rules_snapshot,
            'basis_points',a.partner_snapshot->'basis_points') into record
            from commerce.partner_campaign_approvals a where a.campaign_id=(result->'payload'->>'campaign_id')::uuid;
        result:=jsonb_set(result,'{payload,approval_record}',coalesce(record,'null'::jsonb),true);
    end if;
    return result;
end $$;

revoke all on function commerce_rpc.partner_campaign_approval_setting(bigint,bigint),
    commerce_rpc.configure_partner_campaign_approval(bigint,bigint,boolean,text),
    commerce_rpc.partner_campaign_approval_record(bigint,uuid),commerce_rpc.list_partner_campaign_approvals(bigint,integer),
    commerce_rpc.decide_partner_campaign(bigint,uuid,boolean,text),commerce_rpc.create_partner_campaign(bigint,text,text,jsonb,text)
    from public,anon,authenticated;
grant execute on function commerce_rpc.partner_campaign_approval_setting(bigint,bigint),
    commerce_rpc.configure_partner_campaign_approval(bigint,bigint,boolean,text),
    commerce_rpc.partner_campaign_approval_record(bigint,uuid),commerce_rpc.list_partner_campaign_approvals(bigint,integer),
    commerce_rpc.decide_partner_campaign(bigint,uuid,boolean,text),commerce_rpc.create_partner_campaign(bigint,text,text,jsonb,text)
    to service_role;
create or replace function commerce_rpc.health() returns jsonb language sql stable security definer set search_path='' as $$
    select jsonb_build_object('status','ok','schema_version',40,'currency','EGP')
$$;
notify pgrst,'reload schema';
commit;
