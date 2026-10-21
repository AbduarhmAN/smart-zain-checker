-- Retain old referral links while keeping one canonical code per partner.
begin;

create table commerce.referral_code_aliases (
    code text primary key check (code ~ '^[A-Z0-9_-]{3,48}$'),
    code_id uuid not null references commerce.codes(id),
    partner_id uuid not null references commerce.partners(id),
    created_at timestamptz not null default now()
);
alter table commerce.referral_code_aliases enable row level security;
revoke all on commerce.referral_code_aliases from public,anon,authenticated,service_role;

-- Reserve aliases in the existing code namespace. The shared lock prevents
-- a concurrent code issue/rename from claiming an alias during its creation.
create function commerce.guard_referral_code_alias() returns trigger
language plpgsql security definer set search_path='' as $$
begin
    perform pg_catalog.pg_advisory_xact_lock(610190049);
    if tg_table_name='referral_code_aliases' then
        if exists(select 1 from commerce.codes c where c.code=new.code) then
            raise exception 'Code is assigned elsewhere' using errcode='22023';end if;
        if not exists(select 1 from commerce.codes c where c.id=new.code_id
            and c.kind='referral' and c.partner_id=new.partner_id) then
            raise exception 'Owned referral code required' using errcode='22023';end if;
    elsif exists(select 1 from commerce.referral_code_aliases a where a.code=new.code) then
        raise exception 'Code is assigned elsewhere' using errcode='22023';
    end if;
    return new;
end $$;
create trigger referral_alias_namespace before insert or update on commerce.referral_code_aliases
    for each row execute function commerce.guard_referral_code_alias();
create trigger reserve_referral_alias_namespace before insert or update of code on commerce.codes
    for each row execute function commerce.guard_referral_code_alias();
revoke all on function commerce.guard_referral_code_alias() from public,anon,authenticated,service_role;

create or replace function commerce_rpc.attribute_referral(p_telegram_user_id bigint,p_code text,p_source text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare resolved_code text;
begin
    if exists(select 1 from commerce.users where telegram_user_id=p_telegram_user_id and legacy_service_review_required)
       and not exists(select 1 from commerce.referrals r join commerce.users u on u.id=r.user_id where u.telegram_user_id=p_telegram_user_id) then
        return jsonb_build_object('status','legacy_history_review');end if;
    select c.code into resolved_code from commerce.referral_code_aliases a
        join commerce.codes c on c.id=a.code_id and c.partner_id=a.partner_id and c.kind='referral'
        where a.code=upper(btrim(p_code));
    if not found and exists(select 1 from commerce.referral_code_aliases a where a.code=upper(btrim(p_code))) then
        return jsonb_build_object('status','invalid_code');end if;
    -- Delegate to the existing path: consent/history, active code/partner,
    -- self-referral, first-source and redemption limits all remain enforced.
    return commerce.attribute_referral_before_legacy_review(p_telegram_user_id,coalesce(resolved_code,p_code),p_source);
end $$;
revoke all on function commerce_rpc.attribute_referral(bigint,text,text) from public,anon,authenticated;
grant execute on function commerce_rpc.attribute_referral(bigint,text,text) to service_role;
notify pgrst,'reload schema';
commit;
