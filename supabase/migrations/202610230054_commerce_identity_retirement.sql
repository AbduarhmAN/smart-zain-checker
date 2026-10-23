-- Retire merged identities without allowing a later registration to recreate them.
begin;
create table commerce.retired_telegram_identities (
    telegram_user_id bigint primary key check (telegram_user_id>0),
    canonical_user_id uuid not null references commerce.users(id),
    actor_telegram_id bigint not null check (actor_telegram_id>0),
    authorization_reference text not null unique,
    merged_at timestamptz not null default now(),
    audit_snapshot jsonb not null check (jsonb_typeof(audit_snapshot)='object')
);
alter table commerce.retired_telegram_identities enable row level security;
revoke all on commerce.retired_telegram_identities from public,anon,authenticated,service_role;

create function commerce.reject_retired_telegram_identity() returns trigger
language plpgsql security definer set search_path='' as $$
begin
    if exists(select 1 from commerce.retired_telegram_identities where telegram_user_id=new.telegram_user_id) then
        raise exception 'Retired Telegram identity' using errcode='22023';
    end if;
    return new;
end $$;
create trigger reject_retired_telegram_identity before insert or update of telegram_user_id
    on commerce.users for each row execute function commerce.reject_retired_telegram_identity();
revoke all on function commerce.reject_retired_telegram_identity() from public,anon,authenticated,service_role;

-- A merged notification's current owner can change while its original delivery
-- destination remains verifiable. Sent notifications must never be sent again.
alter table commerce.partner_lifecycle_notifications add column original_recipient_chat_id bigint;
commit;
