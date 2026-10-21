-- Reactivation of former partners; preserves financial history and audit trail.
begin;

-- 1. Allow 'reactivated' in lifecycle events check constraint
alter table commerce.partner_lifecycle_events drop constraint if exists partner_lifecycle_events_kind_check;
alter table commerce.partner_lifecycle_events add constraint partner_lifecycle_events_kind_check
    check (kind in ('added','activated','ended','reactivated'));

-- 2. Drop single end notice restriction so multiple lifecycle transitions over time are supported
drop index if exists commerce.one_partner_end_notice;

-- 3. Update lifecycle body generator to include 'reactivated'
create or replace function commerce.partner_lifecycle_body(p_kind text,p_id uuid,p_recipient bigint,p_code text,p_at timestamptz,p_snapshot jsonb) returns text
language plpgsql immutable set search_path='' as $$
declare body text;
begin
    if p_kind='added' then
        body:='مرحباً بك، وشكراً لاهتمامك بالتعاون معنا.'||chr(10)||
            'تمت إضافة حسابك إلى برنامج شركاء المنصة، وملف الشراكة قيد استكمال الإعداد. سنبلغك عند تفعيل الشروط وكود الإحالة.';
    elsif p_kind='activated' then
        body:='يسعدنا إبلاغك بتفعيل مشاركتك في برنامج شركاء المنصة.'||chr(10)||
            'كود الإحالة: '||coalesce(p_code,'-')||chr(10)||'النسبة المسجلة في النظام: '||trim_scale((p_snapshot->>'commission_basis_points')::numeric/100)||'%'||chr(10)||
            'يمكنك مراجعة أرباحك وسجلك من لوحة أرباح الشريك. نشكر لك تعاونك.';
    elsif p_kind='ended' then
        body:='نشكرك على تعاونك خلال الفترة الماضية.'||chr(10)||
            'نود إحاطتك علماً بانتهاء مشاركتك الحالية في برنامج الشركاء، وإيقاف كود الإحالة والميزات المرتبطة بالشراكة اعتباراً من وقت الإجراء الموضح أدناه.'||chr(10)||
            'تظل العمليات والمستحقات المسجلة سابقاً متاحة للمراجعة والتسوية من خلال الدعم. هذا الإشعار لا يمثل مخالصة مالية أو تنازلاً عن أي حق.'||chr(10)||
            'نقدّر تعاونك، ويسعدنا استقبال أي استفسار عبر الدعم.';
    elsif p_kind='reactivated' then
        body:='يسعدنا إبلاغك باستئناف وإعادة تفعيل مشاركتك في برنامج شركاء المنصة.'||chr(10)||
            'كود الإحالة: '||coalesce(p_code,'-')||chr(10)||
            'تمت استعادة ملف الشراكة بنجاح، وستُعتمد وتُفعّل الشروط والنسبة في لوحة الشريك.'||chr(10)||
            'نشكر لك تواصلك وتعاونك المستمر.';
    else raise exception 'Unknown lifecycle event' using errcode='22023';end if;
    return body||chr(10)||chr(10)||'حساب Telegram: '||p_recipient||chr(10)||'رقم التوثيق: '||p_id||chr(10)||
        'وقت الإجراء UTC: '||to_char(p_at at time zone 'UTC','YYYY-MM-DD HH24:MI:SS')||' UTC'||chr(10)||
        'زر الاطلاع يثبت رد حسابك على الإشعار فقط، ولا يعد مخالصة مالية.';
end $$;

-- 4. Reactivate partner RPC
create or replace function commerce_rpc.reactivate_partner(
    p_actor_telegram_id bigint,
    p_partner_id uuid,
    p_reference text,
    p_code text default null
) returns jsonb
language plpgsql security definer set search_path='' as $$
declare
    partner commerce.partners%rowtype;
    recipient bigint;
    v_code text;
    current_code text;
    event jsonb;
begin
    perform commerce.require_sheet_admin(p_actor_telegram_id);
    if p_reference is null or length(btrim(p_reference)) not between 1 and 300 then
        raise exception 'Reviewed partnership reactivation required' using errcode='22023';
    end if;

    select * into partner from commerce.partners where id=p_partner_id for update;
    if not found then
        raise exception 'Unknown partner' using errcode='22023';
    end if;

    if partner.ended_at is null then
        return jsonb_build_object('partner_id',partner.id,'replayed',true,'state',partner.state,'ended',false);
    end if;

    select telegram_user_id into recipient from commerce.users where id=partner.user_id;

    if p_code is not null and btrim(p_code)<>'' then
        v_code:=upper(btrim(p_code));
        if v_code !~ '^[A-Z0-9_-]{3,48}$' then
            raise exception 'Invalid referral code' using errcode='22023';
        end if;
        if exists (select 1 from commerce.codes where code=v_code and partner_id<>partner.id) then
            raise exception 'Code is assigned elsewhere' using errcode='22023';
        end if;
        update commerce.codes set code=v_code where partner_id=partner.id and kind='referral';
        if not found then
            insert into commerce.codes(code,kind,partner_id,active) values (v_code,'referral',partner.id,false);
        end if;
    end if;

    update commerce.partners
    set ended_at=null,
        ended_by_telegram_id=null,
        state='pending'
    where id=partner.id;

    event:=commerce.record_partner_lifecycle(partner.id,'reactivated',p_actor_telegram_id,p_reference);

    select c.code into current_code from commerce.codes c where c.partner_id=partner.id and kind='referral';

    return jsonb_build_object(
        'partner_id',partner.id,
        'telegram_user_id',recipient,
        'code',current_code,
        'state','pending',
        'notice_event_id',event->'id',
        'replayed',false
    );
end $$;

-- 5. Updated ended_partners function with code
create or replace function commerce_rpc.ended_partners(p_actor_telegram_id bigint,p_offset integer default 0) returns jsonb
language plpgsql stable security definer set search_path='' as $$
begin
    perform commerce.require_sheet_admin(p_actor_telegram_id);
    if p_offset is null or p_offset<0 then raise exception 'Invalid offset' using errcode='22023';end if;
    return coalesce((select jsonb_agg(to_jsonb(r)) from (
        select distinct on (p.id) p.id,u.telegram_user_id,p.ended_at,c.code,e.id as event_id
        from commerce.partners p join commerce.users u on u.id=p.user_id
        left join commerce.codes c on c.partner_id=p.id and c.kind='referral'
        left join commerce.partner_lifecycle_events e on e.partner_id=p.id and e.kind='ended'
        where p.ended_at is not null
        order by p.id, e.occurred_at desc) r),'[]');
end $$;

-- 6. Permissions
revoke all on function commerce_rpc.reactivate_partner(bigint,uuid,text,text) from public,anon,authenticated;
grant execute on function commerce_rpc.reactivate_partner(bigint,uuid,text,text) to service_role;

-- 7. Health update
create or replace function commerce_rpc.health() returns jsonb language sql stable security definer set search_path='' as $$
    select jsonb_build_object('status','ok','schema_version',41,'currency','EGP')
$$;

notify pgrst,'reload schema';
commit;
