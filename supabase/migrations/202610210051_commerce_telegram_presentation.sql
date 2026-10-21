-- Telegram presentation only: expose end metadata and format future notices.
-- No existing events, hashes, partner identities, balances or ledger entries are rewritten.
begin;

create or replace function commerce_rpc.admin_earnings_summary(p_actor_telegram_id bigint,p_offset integer default 0,p_limit integer default 8) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare result jsonb;items jsonb;
begin
    result:=commerce.admin_earnings_before_advances(p_actor_telegram_id,p_offset,p_limit);
    select coalesce(jsonb_agg(item||jsonb_build_object('withdrawable_minor',(item->>'available_minor')::numeric,
        'available_minor',(item->>'available_minor')::numeric-debt,'advance_outstanding_minor',debt,
        'account_balance_minor',(item->>'available_minor')::numeric+(item->>'reserved_minor')::numeric-debt,
        'ended_at',p.ended_at) order by ordinal),'[]')
        into items from jsonb_array_elements(result->'partners') with ordinality entry(item,ordinal)
        join commerce.partners p on p.id=(item->>'partner_id')::uuid
        cross join lateral (select coalesce(sum(outstanding_minor),0) debt from commerce.partner_advances where partner_id=(item->>'partner_id')::uuid) amounts;
    return result||jsonb_build_object('partners',items);
end $$;

create or replace function commerce_rpc.partner_sheet_access(p_actor_telegram_id bigint,p_partner_id uuid) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare result jsonb;
begin
    perform commerce.require_sheet_admin(p_actor_telegram_id);
    select jsonb_build_object('partner_id',p.id,'telegram_user_id',u.telegram_user_id,'state',p.state,'ended_at',p.ended_at,
        'code',(select code from commerce.codes where partner_id=p.id and kind='referral' limit 1),
        'enabled',commerce.partner_sheet_access_enabled(p.id),
        'effective',p.state='active' and p.ended_at is null and u.registration_state='active' and commerce.partner_sheet_access_enabled(p.id))
        into result from commerce.partners p join commerce.users u on u.id=p.user_id where p.id=p_partner_id;
    if result is null then raise exception 'Unknown partner' using errcode='22023';end if;
    return result;
end $$;

create or replace function commerce.partner_lifecycle_body(p_kind text,p_id uuid,p_recipient bigint,p_code text,p_at timestamptz,p_snapshot jsonb) returns text
language plpgsql immutable set search_path='' as $$
declare body text;result text;
begin
    if p_kind='added' then
        body:='مرحباً بك، وشكراً لاهتمامك بالتعاون معنا.'||chr(10)||
            'تمت إضافة حسابك إلى برنامج شركاء المنصة، وملف الشراكة قيد استكمال الإعداد. سنبلغك عند تفعيل الشروط وكود الإحالة.';
    elsif p_kind='activated' then
        body:='يسعدنا إبلاغك بتفعيل مشاركتك في برنامج شركاء المنصة.'||chr(10)||
            'رمز الشريك: '||chr(8294)||coalesce(p_code,'-')||chr(8297)||chr(10)||'النسبة المسجلة في النظام: '||trim_scale((p_snapshot->>'commission_basis_points')::numeric/100)||'%'||chr(10)||
            'يمكنك مراجعة أرباحك وسجلك من لوحة أرباح الشريك. نشكر لك تعاونك.';
    elsif p_kind='ended' then
        body:='نشكرك على تعاونك خلال الفترة الماضية.'||chr(10)||
            'نود إحاطتك علماً بانتهاء مشاركتك الحالية في برنامج الشركاء، وإيقاف كود الإحالة والميزات المرتبطة بالشراكة اعتباراً من وقت الإجراء الموضح أدناه.'||chr(10)||
            'تظل العمليات والمستحقات المسجلة سابقاً متاحة للمراجعة والتسوية من خلال الدعم. هذا الإشعار لا يمثل مخالصة مالية أو تنازلاً عن أي حق.'||chr(10)||
            'نقدّر تعاونك، ويسعدنا استقبال أي استفسار عبر الدعم.';
    elsif p_kind='reactivated' then
        body:='يسعدنا إبلاغك باستئناف وإعادة تفعيل مشاركتك في برنامج شركاء المنصة.'||chr(10)||
            'رمز الشريك: '||chr(8294)||coalesce(p_code,'-')||chr(8297)||chr(10)||
            'تمت استعادة ملف الشراكة بنجاح، وستُعتمد وتُفعّل الشروط والنسبة في لوحة الشريك.'||chr(10)||
            'نشكر لك تواصلك وتعاونك المستمر.';
    else raise exception 'Unknown lifecycle event' using errcode='22023';end if;
    result:=body||chr(10)||chr(10)||'حساب تيليجرام: '||chr(8294)||p_recipient||chr(8297)||chr(10)||'رقم التوثيق: '||chr(8294)||p_id||chr(8297)||chr(10)||
        'وقت الإجراء بالتوقيت العالمي: '||chr(8294)||to_char(p_at at time zone 'UTC','YYYY-MM-DD HH24:MI:SS')||' UTC'||chr(8297)||chr(10)||
        'زر الاطلاع يثبت رد حسابك على الإشعار فقط، ولا يعد مخالصة مالية.';
    return chr(8207)||replace(result,chr(10),chr(10)||chr(8207));
end $$;

create or replace function commerce_rpc.acknowledge_partner_notice(p_event_id uuid,p_telegram_user_id bigint,p_reference text) returns jsonb
language plpgsql security definer set search_path='' as $$
declare event commerce.partner_lifecycle_events%rowtype;body text;
begin
    if p_reference is null or length(btrim(p_reference)) not between 1 and 300 then raise exception 'Native notice acknowledgement required' using errcode='22023';end if;
    select * into event from commerce.partner_lifecycle_events where id=p_event_id for update;
    if not found or event.recipient_telegram_id is distinct from p_telegram_user_id then raise exception 'Notice recipient required' using errcode='42501';end if;
    if event.acknowledged_at is null then
        update commerce.partner_lifecycle_events set acknowledged_at=clock_timestamp(),acknowledged_by=p_telegram_user_id,
            acknowledgement_reference=p_reference where id=event.id returning * into event;
        body:='رد حساب الشريك بالاطلاع على الإشعار.'||chr(10)||'حساب تيليجرام: '||chr(8294)||p_telegram_user_id||chr(8297)||chr(10)||
            'رقم التوثيق: '||chr(8294)||event.id||chr(8297)||chr(10)||'بصمة النص الأصلي: '||chr(8294)||event.body_sha256||chr(8297)||chr(10)||
            'وقت رد الاطلاع بالتوقيت العالمي: '||chr(8294)||to_char(event.acknowledged_at at time zone 'UTC','YYYY-MM-DD HH24:MI:SS')||' UTC'||chr(8297)||chr(10)||
            'هذا رد بالاطلاع فقط، ولا يثبت مخالصة مالية أو تنازلاً عن مستحقات.';
        body:=chr(8207)||replace(body,chr(10),chr(10)||chr(8207));
        perform commerce.queue_partner_lifecycle_notice(event.id,'ack',body,false);
    end if;
    return commerce_rpc.partner_lifecycle_event(event.id,p_telegram_user_id);
end $$;

notify pgrst,'reload schema';
commit;
