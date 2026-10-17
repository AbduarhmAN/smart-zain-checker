-- Decisions supplied by the owner on 2026-10-14. Not a legacy billing switch.
begin;
alter table commerce.partners
    add column entitlement_mode text check (entitlement_mode in ('checks','duration','forever')),
    add column entitlement_checks bigint check (entitlement_checks > 0),
    add column entitlement_duration_seconds bigint check (entitlement_duration_seconds > 0),
    add constraint partner_entitlement_shape check (
        (entitlement_mode is null and entitlement_checks is null and entitlement_duration_seconds is null)
        or (entitlement_mode is not null and entitlement_mode = 'forever' and entitlement_checks is null and entitlement_duration_seconds is null)
        or (entitlement_mode is not null and entitlement_mode = 'checks' and entitlement_checks is not null and entitlement_duration_seconds is null)
        or (entitlement_mode is not null and entitlement_mode = 'duration' and entitlement_checks is null and entitlement_duration_seconds is not null)
    ),
    add constraint active_partner_needs_entitlement check (state <> 'active' or entitlement_mode is not null);

create table commerce.pricing_policies (
    version bigint generated always as identity primary key,
    unit text not null default 'check' check (unit = 'check'),
    unit_price_minor bigint not null check (unit_price_minor >= 0),
    currency text not null default 'EGP' check (currency = 'EGP'),
    refund_destination text not null check (refund_destination in ('wallet','original_source')),
    refund_requires_consent boolean not null default true,
    effective_at timestamptz not null default now(),
    decision_reference text not null,
    check (refund_destination <> 'wallet' or refund_requires_consent)
);
alter table commerce.pricing_policies enable row level security;
revoke all on commerce.pricing_policies from public,anon,authenticated,service_role;
revoke all on sequence commerce.pricing_policies_version_seq from public,anon,authenticated,service_role;
insert into commerce.pricing_policies(unit_price_minor,refund_destination,decision_reference)
values (3000,'wallet','owner_reply_2026-10-14');
commit;
