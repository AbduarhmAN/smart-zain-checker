-- Partnership notices and operational retirement; financial history is retained.
begin;
alter table commerce.partners add column ended_at timestamptz;
alter table commerce.partners add column ended_by_telegram_id bigint check(ended_by_telegram_id>0);
create table commerce.partner_lifecycle_events(
    id uuid primary key default gen_random_uuid(),partner_id uuid not null references commerce.partners(id),
    recipient_telegram_id bigint not null check(recipient_telegram_id>0),
    kind text not null check(kind in ('added','activated','ended')),
    actor_telegram_id bigint,reference text not null unique,occurred_at timestamptz not null default now(),
    snapshot jsonb not null,body text not null,body_sha256 text not null check(length(body_sha256)=64),
    template_version text not null default 'partner-lifecycle-ar-v1',
    acknowledged_at timestamptz,acknowledged_by bigint,acknowledgement_reference text unique,
    check((acknowledged_at is null and acknowledged_by is null and acknowledgement_reference is null)
       or (acknowledged_at is not null and acknowledged_by=recipient_telegram_id and acknowledgement_reference is not null))
);
create unique index one_partner_added_notice on commerce.partner_lifecycle_events(partner_id) where kind='added';
create unique index one_partner_end_notice on commerce.partner_lifecycle_events(partner_id) where kind='ended';

-- Preserve signed balances while exposing whether the role is historical.
create or replace function commerce.partner_advance_balance(p_partner_id uuid,p_available numeric,p_reserved numeric) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare debt numeric;reasons jsonb;role jsonb;
begin
    select coalesce(sum(outstanding_minor),0) into debt from commerce.partner_advances where partner_id=p_partner_id;
    select coalesce(jsonb_agg(item),'[]') into reasons from (
        select jsonb_build_object('withdrawal_id',withdrawal_id,'amount_minor',amount_minor,'outstanding_minor',outstanding_minor,
            'reason',reason,'created_at',created_at) item from commerce.partner_advances
            where partner_id=p_partner_id and outstanding_minor>0 order by created_at,id limit 5) page;
    select jsonb_build_object('partnership_state',p.state,'partnership_active',p.state='active' and p.ended_at is null,
        'partnership_ended_at',p.ended_at,'partnership_end_event_id',
        (select id from commerce.partner_lifecycle_events e where e.partner_id=p.id and kind='ended')) into role
        from commerce.partners p where p.id=p_partner_id;
    return jsonb_build_object('available_minor',p_available-debt,'withdrawable_minor',p_available,
        'account_balance_minor',p_available+p_reserved-debt,'advance_outstanding_minor',debt,'outstanding_advances',reasons)||coalesce(role,'{}');
end $$;
create table commerce.partner_lifecycle_notifications(
    id uuid primary key default gen_random_uuid(),event_id uuid not null references commerce.partner_lifecycle_events(id),
    kind text not null check(kind in ('event','ack')),
    recipient_kind text not null check(recipient_kind in ('partner','channel')),recipient_chat_id bigint,
    body text not null,body_sha256 text not null check(length(body_sha256)=64),
    state text not null default 'pending' check(state in ('pending','sending','sent','rejected','uncertain','waiting_config','routed')),
    token uuid,attempts integer not null default 0,next_attempt_at timestamptz not null default now(),
    telegram_message_id bigint,telegram_date timestamptz,completed_at timestamptz,
    created_at timestamptz not null default now(),updated_at timestamptz not null default now(),
    unique nulls not distinct(event_id,kind,recipient_kind,recipient_chat_id),
    check((recipient_kind='partner' and recipient_chat_id>0) or (recipient_kind='channel' and (recipient_chat_id<0 or recipient_chat_id is null)))
);
create table commerce.partner_lifecycle_delivery_attempts(
    token uuid primary key,notification_id uuid not null references commerce.partner_lifecycle_notifications(id),
    state text not null default 'sending' check(state in ('sending','sent','rejected','uncertain')),
    started_at timestamptz not null default now(),completed_at timestamptz,
    telegram_message_id bigint,telegram_date timestamptz
);
alter table commerce.partner_lifecycle_events enable row level security;
alter table commerce.partner_lifecycle_notifications enable row level security;
alter table commerce.partner_lifecycle_delivery_attempts enable row level security;
revoke all on commerce.partner_lifecycle_events,commerce.partner_lifecycle_notifications,commerce.partner_lifecycle_delivery_attempts
    from public,anon,authenticated,service_role;

create function commerce.partner_lifecycle_body(p_kind text,p_id uuid,p_recipient bigint,p_code text,p_at timestamptz,p_snapshot jsonb) returns text
language plpgsql immutable set search_path='' as $$
declare body text;
begin
    if p_kind='added' then
        body:='مرحباً بك، وشكراً لاهتمامك بالتعاون معنا.'||chr(10)||
            'تمت إضافة حسابك إلى برنامج شركاء المنصة، وملف الشراكة قيد استكمال الإعداد. سنبلغك عند تفعيل الشروط وكود الإحالة.';
    elsif p_kind='activated' then
        body:='يسعدنا إبلاغك بتفعيل مشاركتك في برنامج شركاء المنصة.'||chr(10)||
            'كود الإحالة: '||p_code||chr(10)||'النسبة المسجلة في النظام: '||trim_scale((p_snapshot->>'commission_basis_points')::numeric/100)||'%'||chr(10)||
            'يمكنك مراجعة أرباحك وسجلك من لوحة أرباح الشريك. نشكر لك تعاونك.';
    elsif p_kind='ended' then
        body:='نشكرك على تعاونك خلال الفترة الماضية.'||chr(10)||
            'نود إحاطتك علماً بانتهاء مشاركتك الحالية في برنامج الشركاء، وإيقاف كود الإحالة والميزات المرتبطة بالشراكة اعتباراً من وقت الإجراء الموضح أدناه.'||chr(10)||
            'تظل العمليات والمستحقات المسجلة سابقاً متاحة للمراجعة والتسوية من خلال الدعم. هذا الإشعار لا يمثل مخالصة مالية أو تنازلاً عن أي حق.'||chr(10)||
            'نقدّر تعاونك، ويسعدنا استقبال أي استفسار عبر الدعم.';
    else raise exception 'Unknown lifecycle event' using errcode='22023';end if;
    return body||chr(10)||chr(10)||'حساب Telegram: '||p_recipient||chr(10)||'رقم التوثيق: '||p_id||chr(10)||
        'وقت الإجراء UTC: '||to_char(p_at at time zone 'UTC','YYYY-MM-DD HH24:MI:SS')||' UTC'||chr(10)||
        'زر الاطلاع يثبت رد حسابك على الإشعار فقط، ولا يعد مخالصة مالية.';
end $$;
revoke all on function commerce.partner_lifecycle_body(text,uuid,bigint,text,timestamptz,jsonb) from public,anon,authenticated,service_role;

create function commerce.queue_partner_lifecycle_notice(p_event uuid,p_kind text,p_body text,p_partner boolean) returns void
language plpgsql security definer set search_path='' as $$
declare recipient bigint;routes bigint[];channel bigint;digest text;
begin
    digest:=encode(sha256(convert_to(p_body,'UTF8')),'hex');
    if p_partner then
        select recipient_telegram_id into recipient from commerce.partner_lifecycle_events where id=p_event;
        insert into commerce.partner_lifecycle_notifications(event_id,kind,recipient_kind,recipient_chat_id,body,body_sha256)
            values(p_event,p_kind,'partner',recipient,p_body,digest) on conflict do nothing;
    end if;
    select channels into routes from commerce.payout_notice_routes where id=true;
    if coalesce(cardinality(routes),0)=0 then
        insert into commerce.partner_lifecycle_notifications(event_id,kind,recipient_kind,body,body_sha256,state)
            values(p_event,p_kind,'channel',p_body,digest,'waiting_config') on conflict do nothing;
    else
        foreach channel in array routes loop
            insert into commerce.partner_lifecycle_notifications(event_id,kind,recipient_kind,recipient_chat_id,body,body_sha256)
                values(p_event,p_kind,'channel',channel,p_body,digest) on conflict do nothing;
        end loop;
    end if;
end $$;
revoke all on function commerce.queue_partner_lifecycle_notice(uuid,text,text,boolean) from public,anon,authenticated,service_role;

create function commerce.record_partner_lifecycle(p_partner uuid,p_kind text,p_actor bigint,p_reference text) returns jsonb
language plpgsql security definer set search_path='' as $$
declare event commerce.partner_lifecycle_events%rowtype;partner commerce.partners%rowtype;recipient bigint;code text;snapshot jsonb;
begin
    select * into partner from commerce.partners where id=p_partner for update;
    if not found then raise exception 'Unknown partner' using errcode='22023';end if;
    select * into event from commerce.partner_lifecycle_events where reference=p_reference;
    if not found and p_kind in ('added','ended') then
        select * into event from commerce.partner_lifecycle_events where partner_id=p_partner and kind=p_kind;
    end if;
    if event.id is not null then
        if event.partner_id<>p_partner or event.kind<>p_kind then raise exception 'Conflicting lifecycle event replay' using errcode='22023';end if;
        return to_jsonb(event);end if;
    select telegram_user_id into recipient from commerce.users where id=partner.user_id;
    select c.code into code from commerce.codes c where c.partner_id=p_partner and kind='referral';
    snapshot:=jsonb_build_object('state',partner.state,'ended_at',partner.ended_at,'code',code,
        'commission_basis_points',partner.commission_basis_points,'terms_version',partner.terms_version,'terms',partner.terms,
        'financial_history_preserved',true,'country_context','EG','notice_ack_is_not_settlement',true);
    event.id:=gen_random_uuid();event.occurred_at:=case when p_kind='ended' then partner.ended_at else clock_timestamp() end;
    event.body:=commerce.partner_lifecycle_body(p_kind,event.id,recipient,code,event.occurred_at,snapshot);
    insert into commerce.partner_lifecycle_events(id,partner_id,recipient_telegram_id,kind,actor_telegram_id,reference,occurred_at,snapshot,body,body_sha256)
        values(event.id,p_partner,recipient,p_kind,p_actor,p_reference,event.occurred_at,snapshot,event.body,
            encode(sha256(convert_to(event.body,'UTF8')),'hex')) returning * into event;
    perform commerce.queue_partner_lifecycle_notice(event.id,'event',event.body,true);
    return to_jsonb(event);
end $$;
revoke all on function commerce.record_partner_lifecycle(uuid,text,bigint,text) from public,anon,authenticated,service_role;

alter function commerce_rpc.create_partner(bigint,text) rename to create_partner_before_lifecycle;
alter function commerce_rpc.create_partner_before_lifecycle(bigint,text) set schema commerce;
revoke all on function commerce.create_partner_before_lifecycle(bigint,text) from public,anon,authenticated,service_role;
create function commerce_rpc.create_partner(p_telegram_user_id bigint,p_code text) returns jsonb
language plpgsql security definer set search_path='' as $$
declare result jsonb;event jsonb;existed boolean;notice uuid;
begin
    select exists(select 1 from commerce.partners p join commerce.users u on u.id=p.user_id where u.telegram_user_id=p_telegram_user_id) into existed;
    result:=commerce.create_partner_before_lifecycle(p_telegram_user_id,p_code);
    if exists(select 1 from commerce.partners where id=(result->>'partner_id')::uuid and ended_at is not null) then
        return result||jsonb_build_object('partnership_ended',true);end if;
    if existed then
        select id into notice from commerce.partner_lifecycle_events where partner_id=(result->>'partner_id')::uuid and kind='added';
        return result||jsonb_build_object('notice_event_id',notice);end if;
    event:=commerce.record_partner_lifecycle((result->>'partner_id')::uuid,'added',null,'partner-added:'||(result->>'partner_id'));
    return result||jsonb_build_object('notice_event_id',event->'id');
end $$;
create function commerce_rpc.add_partner_with_notice(p_actor_telegram_id bigint,p_telegram_user_id bigint,p_code text) returns jsonb
language plpgsql security definer set search_path='' as $$
declare result jsonb;event jsonb;existed boolean;notice uuid;
begin
    perform commerce.require_sheet_admin(p_actor_telegram_id);
    select exists(select 1 from commerce.partners p join commerce.users u on u.id=p.user_id where u.telegram_user_id=p_telegram_user_id) into existed;
    result:=commerce.create_partner_before_lifecycle(p_telegram_user_id,p_code);
    if exists(select 1 from commerce.partners where id=(result->>'partner_id')::uuid and ended_at is not null) then
        return result||jsonb_build_object('partnership_ended',true);end if;
    if existed then
        select id into notice from commerce.partner_lifecycle_events where partner_id=(result->>'partner_id')::uuid and kind='added';
        return result||jsonb_build_object('notice_event_id',notice);end if;
    event:=commerce.record_partner_lifecycle((result->>'partner_id')::uuid,'added',p_actor_telegram_id,'partner-added:'||(result->>'partner_id'));
    return result||jsonb_build_object('notice_event_id',event->'id');
end $$;

alter function commerce_rpc.configure_partner(uuid,bigint,integer,text,bigint,boolean) rename to configure_partner_before_lifecycle;
alter function commerce_rpc.configure_partner_before_lifecycle(uuid,bigint,integer,text,bigint,boolean) set schema commerce;
revoke all on function commerce.configure_partner_before_lifecycle(uuid,bigint,integer,text,bigint,boolean) from public,anon,authenticated,service_role;
create function commerce_rpc.configure_partner(p_partner_id uuid,p_actor_telegram_id bigint,p_basis_points integer,p_mode text,
    p_limit bigint default null,p_activate boolean default false) returns jsonb
language plpgsql security definer set search_path='' as $$
declare result jsonb;before_state text;event jsonb;
begin
    select state into before_state from commerce.partners where id=p_partner_id and ended_at is null for update;
    if not found then raise exception 'Partner is ended or unknown' using errcode='22023';end if;
    result:=commerce.configure_partner_before_lifecycle(p_partner_id,p_actor_telegram_id,p_basis_points,p_mode,p_limit,p_activate);
    if before_state<>'active' and result->>'state'='active' then
        event:=commerce.record_partner_lifecycle(p_partner_id,'activated',p_actor_telegram_id,
            'partner-activated:'||p_partner_id||':'||(result->>'version'));
        result:=result||jsonb_build_object('notice_event_id',event->'id');
    end if;
    return result;
end $$;

create function commerce_rpc.partner_end_preview(p_actor_telegram_id bigint,p_partner_id uuid) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare partner commerce.partners%rowtype;recipient bigint;body text;
begin
    perform commerce.require_sheet_admin(p_actor_telegram_id);
    select * into partner from commerce.partners where id=p_partner_id;
    if not found then raise exception 'Unknown partner' using errcode='22023';end if;
    select telegram_user_id into recipient from commerce.users where id=partner.user_id;
    body:=commerce.partner_lifecycle_body('ended',p_partner_id,recipient,null,now(),'{}');
    return jsonb_build_object('partner_id',partner.id,'telegram_user_id',recipient,'ended_at',partner.ended_at,
        'terms_version',partner.terms_version,'preview',body,'event_id',
        (select id from commerce.partner_lifecycle_events where partner_id=p_partner_id and kind='ended'));
end $$;
create function commerce_rpc.end_partner(p_actor_telegram_id bigint,p_partner_id uuid,p_expected_terms_version text,p_reference text) returns jsonb
language plpgsql security definer set search_path='' as $$
declare partner commerce.partners%rowtype;uid uuid;event jsonb;
begin
    perform commerce.require_sheet_admin(p_actor_telegram_id);
    if p_reference is null or length(btrim(p_reference)) not between 1 and 300 then raise exception 'Reviewed partnership end required' using errcode='22023';end if;
    select user_id into uid from commerce.partners where id=p_partner_id;
    perform 1 from commerce.users where id=uid for update;
    select * into partner from commerce.partners where id=p_partner_id for update;
    if not found then raise exception 'Unknown partner' using errcode='22023';end if;
    if partner.ended_at is null then
        if partner.terms_version is distinct from p_expected_terms_version then raise exception 'Partner terms changed; review again' using errcode='22023';end if;
        update commerce.partners set state='suspended',ended_at=clock_timestamp(),ended_by_telegram_id=p_actor_telegram_id where id=partner.id;
        update commerce.codes set active=false where partner_id=partner.id and kind='referral';
    end if;
    event:=commerce.record_partner_lifecycle(partner.id,'ended',p_actor_telegram_id,p_reference);
    return event;
end $$;

create or replace function commerce_rpc.list_partners(p_offset integer default 0) returns jsonb
language plpgsql stable security definer set search_path='' as $$
begin
    if p_offset is null or p_offset<0 then raise exception 'Invalid offset' using errcode='22023';end if;
    return coalesce((select jsonb_agg(to_jsonb(r)) from (
        select p.id,u.telegram_user_id,p.state,p.commission_basis_points,p.entitlement_mode,p.entitlement_checks,p.entitlement_duration_seconds,
            c.code,p.terms_version from commerce.partners p join commerce.users u on u.id=p.user_id
        left join commerce.codes c on c.partner_id=p.id and c.kind='referral' where p.ended_at is null
        order by p.created_at,p.id limit 10 offset p_offset) r),'[]');
end $$;
create function commerce_rpc.ended_partners(p_actor_telegram_id bigint,p_offset integer default 0) returns jsonb
language plpgsql stable security definer set search_path='' as $$
begin
    perform commerce.require_sheet_admin(p_actor_telegram_id);
    if p_offset is null or p_offset<0 then raise exception 'Invalid offset' using errcode='22023';end if;
    return coalesce((select jsonb_agg(to_jsonb(r)) from (
        select p.id,u.telegram_user_id,p.ended_at,e.id as event_id from commerce.partners p join commerce.users u on u.id=p.user_id
        left join commerce.partner_lifecycle_events e on e.partner_id=p.id and e.kind='ended' where p.ended_at is not null
        order by p.ended_at desc,p.id limit 10 offset p_offset) r),'[]');
end $$;

create function commerce_rpc.partner_lifecycle_event(p_event_id uuid,p_actor_telegram_id bigint) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare event commerce.partner_lifecycle_events%rowtype;admin boolean;
begin
    admin:=exists(select 1 from commerce.support_actors where telegram_user_id=p_actor_telegram_id and active);
    select * into event from commerce.partner_lifecycle_events where id=p_event_id;
    if not found or (event.recipient_telegram_id is distinct from p_actor_telegram_id and not admin) then
        raise exception 'Owned lifecycle notice required' using errcode='42501';end if;
    return (to_jsonb(event)-'reference')||jsonb_build_object('is_admin',admin,'deliveries',
        (select coalesce(jsonb_agg(jsonb_build_object('id',id,'recipient_kind',recipient_kind,'state',state,
            'telegram_message_id',telegram_message_id,'telegram_date',telegram_date,'completed_at',completed_at,
            'created_at',created_at,'body_sha256',body_sha256) order by created_at,id),'[]') from commerce.partner_lifecycle_notifications
         where event_id=event.id and state<>'routed' and (admin or recipient_kind='partner')));
end $$;
create function commerce_rpc.acknowledge_partner_notice(p_event_id uuid,p_telegram_user_id bigint,p_reference text) returns jsonb
language plpgsql security definer set search_path='' as $$
declare event commerce.partner_lifecycle_events%rowtype;body text;
begin
    if p_reference is null or length(btrim(p_reference)) not between 1 and 300 then raise exception 'Native notice acknowledgement required' using errcode='22023';end if;
    select * into event from commerce.partner_lifecycle_events where id=p_event_id for update;
    if not found or event.recipient_telegram_id is distinct from p_telegram_user_id then raise exception 'Notice recipient required' using errcode='42501';end if;
    if event.acknowledged_at is null then
        update commerce.partner_lifecycle_events set acknowledged_at=clock_timestamp(),acknowledged_by=p_telegram_user_id,
            acknowledgement_reference=p_reference where id=event.id returning * into event;
        body:='رد حساب الشريك بالاطلاع على الإشعار.'||chr(10)||'حساب Telegram: '||p_telegram_user_id||chr(10)||
            'رقم التوثيق: '||event.id||chr(10)||'بصمة نص الإشعار SHA-256: '||event.body_sha256||chr(10)||
            'وقت رد الاطلاع UTC: '||to_char(event.acknowledged_at at time zone 'UTC','YYYY-MM-DD HH24:MI:SS')||' UTC'||chr(10)||
            'هذا رد بالاطلاع فقط، ولا يثبت مخالصة مالية أو تنازلاً عن مستحقات.';
        perform commerce.queue_partner_lifecycle_notice(event.id,'ack',body,false);
    end if;
    return commerce_rpc.partner_lifecycle_event(event.id,p_telegram_user_id);
end $$;

create function commerce.route_partner_lifecycle_notices() returns trigger
language plpgsql security definer set search_path='' as $$
declare notice commerce.partner_lifecycle_notifications%rowtype;channel bigint;
begin
    if coalesce(cardinality(new.channels),0)>0 then
        for notice in select * from commerce.partner_lifecycle_notifications where state='waiting_config' for update loop
            foreach channel in array new.channels loop
                insert into commerce.partner_lifecycle_notifications(event_id,kind,recipient_kind,recipient_chat_id,body,body_sha256)
                    values(notice.event_id,notice.kind,'channel',channel,notice.body,notice.body_sha256) on conflict do nothing;
            end loop;
            update commerce.partner_lifecycle_notifications set state='routed',updated_at=now() where id=notice.id;
        end loop;
    end if;
    return new;
end $$;
revoke all on function commerce.route_partner_lifecycle_notices() from public,anon,authenticated,service_role;
create trigger route_partner_lifecycle_notices after update of channels on commerce.payout_notice_routes
    for each row execute function commerce.route_partner_lifecycle_notices();

create function commerce_rpc.claim_partner_lifecycle_notification() returns jsonb
language plpgsql security definer set search_path='' as $$
declare result jsonb;
begin
    with expired as(update commerce.partner_lifecycle_notifications set state='uncertain',updated_at=now()
        where state='sending' and updated_at<now()-interval '2 minutes' returning token)
    update commerce.partner_lifecycle_delivery_attempts set state='uncertain',completed_at=now() where token in(select token from expired) and state='sending';
    with next as(select id from commerce.partner_lifecycle_notifications where state in ('pending','rejected') and attempts<3
        and recipient_chat_id is not null and next_attempt_at<=now() order by created_at,id for update skip locked limit 1),
    claimed as(update commerce.partner_lifecycle_notifications n set state='sending',token=gen_random_uuid(),attempts=attempts+1,updated_at=now()
        from next where n.id=next.id returning n.*)
    select to_jsonb(claimed) into result from claimed;
    if result is not null then insert into commerce.partner_lifecycle_delivery_attempts(token,notification_id)
        values((result->>'token')::uuid,(result->>'id')::uuid);end if;
    return result||jsonb_build_object('event_kind',(select kind from commerce.partner_lifecycle_events where id=(result->>'event_id')::uuid));
end $$;
create function commerce_rpc.complete_partner_lifecycle_notification(p_id uuid,p_token uuid,p_state text,p_message_id bigint default null,p_telegram_timestamp bigint default null) returns jsonb
language plpgsql security definer set search_path='' as $$
declare receipt_date timestamptz;
begin
    if p_state is null or p_state not in ('sent','rejected','uncertain') or (p_state='sent' and
        (coalesce(p_message_id,0)<=0 or coalesce(p_telegram_timestamp,0)<=0 or p_telegram_timestamp>extract(epoch from now())+300)) then
        raise exception 'Native Telegram delivery evidence required' using errcode='22023';end if;
    if p_state='sent' then receipt_date:=to_timestamp(p_telegram_timestamp);end if;
    update commerce.partner_lifecycle_notifications set state=p_state,telegram_message_id=p_message_id,telegram_date=receipt_date,completed_at=now(),
        next_attempt_at=now()+interval '30 seconds'*attempts,updated_at=now() where id=p_id and token=p_token and state='sending';
    if found then
        update commerce.partner_lifecycle_delivery_attempts set state=p_state,completed_at=now(),telegram_message_id=p_message_id,telegram_date=receipt_date where token=p_token;
        return jsonb_build_object('saved',true);
    end if;
    return jsonb_build_object('saved',false);
end $$;

revoke all on function commerce_rpc.create_partner(bigint,text),commerce_rpc.add_partner_with_notice(bigint,bigint,text),
    commerce_rpc.configure_partner(uuid,bigint,integer,text,bigint,boolean),commerce_rpc.partner_end_preview(bigint,uuid),
    commerce_rpc.end_partner(bigint,uuid,text,text),commerce_rpc.ended_partners(bigint,integer),commerce_rpc.partner_lifecycle_event(uuid,bigint),
    commerce_rpc.acknowledge_partner_notice(uuid,bigint,text),commerce_rpc.claim_partner_lifecycle_notification(),
    commerce_rpc.complete_partner_lifecycle_notification(uuid,uuid,text,bigint,bigint) from public,anon,authenticated;
grant execute on function commerce_rpc.create_partner(bigint,text),commerce_rpc.add_partner_with_notice(bigint,bigint,text),
    commerce_rpc.configure_partner(uuid,bigint,integer,text,bigint,boolean),commerce_rpc.partner_end_preview(bigint,uuid),
    commerce_rpc.end_partner(bigint,uuid,text,text),commerce_rpc.ended_partners(bigint,integer),commerce_rpc.partner_lifecycle_event(uuid,bigint),
    commerce_rpc.acknowledge_partner_notice(uuid,bigint,text),commerce_rpc.claim_partner_lifecycle_notification(),
    commerce_rpc.complete_partner_lifecycle_notification(uuid,uuid,text,bigint,bigint) to service_role;
notify pgrst,'reload schema';
commit;
