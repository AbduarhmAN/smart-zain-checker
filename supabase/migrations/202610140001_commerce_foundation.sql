-- A01-A03: private storage. No existing wallet data is migrated or modified.
-- Run against the intended Supabase project only after local acceptance.
begin;
create schema commerce;
create schema commerce_rpc;
revoke all on schema commerce, commerce_rpc from public, anon, authenticated;
grant usage on schema commerce_rpc to service_role;
alter default privileges in schema commerce revoke all on tables from public, anon, authenticated, service_role;
alter default privileges in schema commerce revoke all on sequences from public, anon, authenticated, service_role;
alter default privileges in schema commerce_rpc revoke execute on functions from public, anon, authenticated;

create table commerce.users (
    id uuid primary key default gen_random_uuid(),
    telegram_user_id bigint not null unique check (telegram_user_id > 0),
    telegram_username text,
    first_used_at timestamptz not null default now(),
    registration_state text not null default 'active' check (registration_state in ('active','suspended')),
    first_paid_service_at timestamptz,
    created_at timestamptz not null default now()
);
create table commerce.partners (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null unique references commerce.users(id),
    state text not null default 'pending' check (state in ('pending','active','suspended')),
    commission_basis_points integer check (commission_basis_points between 0 and 10000),
    terms_version text,
    terms jsonb not null default '{}' check (jsonb_typeof(terms) = 'object'),
    approved_at timestamptz,
    created_at timestamptz not null default now(),
    check (state <> 'active' or (commission_basis_points is not null and terms_version is not null and approved_at is not null))
);
create table commerce.campaigns (
    id uuid primary key default gen_random_uuid(),
    name text not null,
    state text not null default 'draft' check (state in ('draft','active','paused','ended')),
    benefit_kind text not null check (benefit_kind in ('percent','fixed','free_checks','wallet_credit')),
    benefit_value bigint not null check (benefit_value >= 0),
    funder_kind text not null check (funder_kind in ('platform','partner')),
    partner_id uuid references commerce.partners(id),
    eligible_check_types text[] not null,
    new_customer_only boolean not null default false,
    starts_at timestamptz,
    ends_at timestamptz,
    activation_duration_seconds bigint check (activation_duration_seconds > 0),
    max_checks_per_user bigint check (max_checks_per_user > 0),
    max_discount_minor_per_user bigint check (max_discount_minor_per_user > 0),
    allow_stacking boolean not null default false,
    rules_version text not null,
    rules jsonb not null default '{}' check (jsonb_typeof(rules) = 'object'),
    approved_at timestamptz,
    created_at timestamptz not null default now(),
    check ((funder_kind = 'partner') = (partner_id is not null)),
    check (benefit_kind <> 'percent' or benefit_value <= 10000),
    check (ends_at is null or (starts_at is not null and ends_at > starts_at)),
    check (state <> 'active' or approved_at is not null)
);
create table commerce.codes (
    id uuid primary key default gen_random_uuid(),
    code text not null unique check (code ~ '^[A-Z0-9_-]{3,48}$'),
    kind text not null check (kind in ('referral','discount','cash_credit')),
    partner_id uuid references commerce.partners(id),
    campaign_id uuid references commerce.campaigns(id),
    credit_minor bigint check (credit_minor > 0),
    currency text not null default 'EGP' check (currency = 'EGP'),
    active boolean not null default false,
    max_redemptions bigint check (max_redemptions > 0),
    expires_at timestamptz,
    proof_reference text,
    created_at timestamptz not null default now(),
    check ((kind = 'referral' and partner_id is not null and campaign_id is null and credit_minor is null)
        or (kind = 'discount' and campaign_id is not null and partner_id is null and credit_minor is null)
        or (kind = 'cash_credit' and partner_id is null and campaign_id is null and credit_minor is not null and proof_reference is not null))
);
create table commerce.referrals (
    user_id uuid primary key references commerce.users(id),
    partner_id uuid not null references commerce.partners(id),
    code_id uuid not null references commerce.codes(id),
    source text not null check (source in ('deep_link','manual')),
    attributed_at timestamptz not null default now(),
    locked_at timestamptz,
    policy_version text not null
);
create table commerce.wallets (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references commerce.users(id),
    kind text not null check (kind in ('customer','partner')),
    currency text not null default 'EGP' check (currency = 'EGP'),
    available_minor bigint not null default 0 check (available_minor >= 0),
    reserved_minor bigint not null default 0 check (reserved_minor >= 0),
    withdrawn_minor bigint not null default 0 check (withdrawn_minor >= 0),
    unique (user_id,kind,currency),
    unique (id,currency),
    created_at timestamptz not null default now()
);
create table commerce.orders (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references commerce.users(id),
    request_key text not null unique check (length(request_key) between 1 and 160),
    checker_job_id text unique,
    requested_checks bigint not null check (requested_checks > 0),
    currency text not null default 'EGP' check (currency = 'EGP'),
    quoted_minor bigint not null check (quoted_minor >= 0),
    pricing_snapshot jsonb not null check (jsonb_typeof(pricing_snapshot) = 'object'),
    partner_terms_snapshot jsonb not null default '{}' check (jsonb_typeof(partner_terms_snapshot) = 'object'),
    referral_partner_id uuid references commerce.partners(id),
    order_state text not null default 'draft' check (order_state in ('draft','confirmed','cancelled','closed')),
    payment_state text not null default 'unpaid' check (payment_state in ('unpaid','reserved','settled','refunded','partial_refund')),
    execution_state text not null default 'not_started' check (execution_state in ('not_started','queued','running','paused','terminal')),
    delivery_state text not null default 'not_ready' check (delivery_state in ('not_ready','ready','pending','confirmed','failed')),
    delivered_at timestamptz,
    final_charge_minor bigint check (final_charge_minor >= 0),
    created_at timestamptz not null default now(),
    unique (id,user_id),
    check (final_charge_minor is null or final_charge_minor <= quoted_minor),
    check ((delivery_state = 'confirmed') = (delivered_at is not null)),
    check (payment_state not in ('settled','refunded','partial_refund') or
        (execution_state = 'terminal' and delivery_state = 'confirmed' and final_charge_minor is not null))
);
-- Stable identity is order + input row, never just a Zain number shared by rows.
create table commerce.checks (
    order_id uuid not null references commerce.orders(id),
    row_number bigint not null check (row_number > 0),
    check_type text not null check (check_type in ('account','service')),
    state text not null default 'pending' check (state in ('pending','running','deferred','succeeded','failed','review','not_found')),
    unit_quote_minor bigint not null check (unit_quote_minor >= 0),
    discount_minor bigint not null default 0 check (discount_minor >= 0),
    outcome_version bigint not null default 0 check (outcome_version >= 0),
    terminal_at timestamptz,
    primary key (order_id,row_number),
    check (discount_minor <= unit_quote_minor),
    check ((state in ('succeeded','failed','review','not_found')) = (terminal_at is not null))
);
create table commerce.payments (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references commerce.users(id),
    order_id uuid,
    purpose text not null check (purpose in ('topup','direct_order')),
    provider text not null,
    provider_reference text,
    idempotency_key text not null unique,
    amount_minor bigint not null check (amount_minor > 0),
    currency text not null default 'EGP' check (currency = 'EGP'),
    provider_amount text,
    provider_currency text,
    fx_snapshot jsonb,
    state text not null default 'pending' check (state in ('pending','verified','rejected','allocated','settled')),
    proof_reference text,
    verified_at timestamptz,
    created_at timestamptz not null default now(),
    foreign key (order_id,user_id) references commerce.orders(id,user_id),
    check ((purpose = 'direct_order') = (order_id is not null)),
    check (state not in ('verified','allocated','settled') or (verified_at is not null and proof_reference is not null)),
    unique (provider,provider_reference)
);
create table commerce.reservations (
    id uuid primary key default gen_random_uuid(),
    order_id uuid not null unique references commerce.orders(id),
    wallet_id uuid references commerce.wallets(id),
    payment_id uuid references commerce.payments(id),
    amount_minor bigint not null check (amount_minor >= 0),
    state text not null default 'held' check (state in ('held','settled','released')),
    idempotency_key text not null unique,
    created_at timestamptz not null default now(),
    check ((wallet_id is not null) <> (payment_id is not null))
);
-- Balanced journals are posted by a transactional RPC (added in A06).
create table commerce.journals (
    id uuid primary key default gen_random_uuid(),
    idempotency_key text not null unique,
    kind text not null check (kind in ('topup','reserve','release','service','refund','commission','withdrawal','adjustment')),
    order_id uuid references commerce.orders(id),
    payload_fingerprint text not null,
    created_at timestamptz not null default now()
);
create table commerce.ledger_entries (
    journal_id uuid not null references commerce.journals(id),
    line_number integer not null check (line_number > 0),
    account_key text not null,
    wallet_id uuid,
    currency text not null default 'EGP' check (currency = 'EGP'),
    delta_minor bigint not null check (delta_minor <> 0),
    primary key (journal_id,line_number),
    foreign key (wallet_id,currency) references commerce.wallets(id,currency)
);
create table commerce.refunds (
    id uuid primary key default gen_random_uuid(),
    order_id uuid not null,
    row_number bigint not null,
    amount_minor bigint not null check (amount_minor > 0),
    destination text not null check (destination in ('wallet','original_source')),
    policy_version text not null,
    consent_reference text,
    state text not null default 'pending' check (state in ('pending','confirmed','failed')),
    provider_reference text,
    journal_id uuid unique references commerce.journals(id),
    idempotency_key text not null unique,
    created_at timestamptz not null default now(),
    foreign key (order_id,row_number) references commerce.checks(order_id,row_number),
    unique (order_id,row_number),
    check (destination <> 'wallet' or consent_reference is not null)
);
create table commerce.commissions (
    id uuid primary key default gen_random_uuid(),
    order_id uuid not null,
    row_number bigint not null,
    partner_id uuid not null references commerce.partners(id),
    revenue_minor bigint not null check (revenue_minor >= 0),
    amount_minor bigint not null check (amount_minor >= 0),
    terms_snapshot jsonb not null check (jsonb_typeof(terms_snapshot) = 'object'),
    state text not null default 'pending' check (state in ('pending','available','withdrawn','reversed')),
    journal_id uuid unique references commerce.journals(id),
    created_at timestamptz not null default now(),
    foreign key (order_id,row_number) references commerce.checks(order_id,row_number),
    unique (order_id,row_number),
    check (amount_minor <= revenue_minor)
);
create table commerce.withdrawals (
    id uuid primary key default gen_random_uuid(),
    partner_id uuid not null references commerce.partners(id),
    amount_minor bigint not null check (amount_minor > 0),
    state text not null default 'requested' check (state in ('requested','approved','paid','rejected')),
    payout_reference text unique,
    journal_id uuid unique references commerce.journals(id),
    idempotency_key text not null unique,
    created_at timestamptz not null default now(),
    check (state <> 'paid' or (payout_reference is not null and journal_id is not null))
);
create table commerce.campaign_usage (
    campaign_id uuid not null references commerce.campaigns(id),
    user_id uuid not null references commerce.users(id),
    activated_at timestamptz not null default now(),
    reserved_checks bigint not null default 0 check (reserved_checks >= 0),
    consumed_checks bigint not null default 0 check (consumed_checks >= 0),
    discount_minor bigint not null default 0 check (discount_minor >= 0),
    primary key (campaign_id,user_id)
);
create table commerce.code_redemptions (
    code_id uuid not null references commerce.codes(id),
    user_id uuid not null references commerce.users(id),
    idempotency_key text not null unique,
    journal_id uuid unique references commerce.journals(id),
    redeemed_at timestamptz not null default now(),
    primary key (code_id,user_id)
);
create table commerce.order_discounts (
    order_id uuid not null,
    row_number bigint not null,
    campaign_id uuid not null references commerce.campaigns(id),
    discount_minor bigint not null check (discount_minor >= 0),
    funder_snapshot jsonb not null check (jsonb_typeof(funder_snapshot) = 'object'),
    applied boolean not null default false,
    primary key (order_id,row_number,campaign_id),
    foreign key (order_id,row_number) references commerce.checks(order_id,row_number)
);
create table commerce.archive_files (
    order_id uuid not null references commerce.orders(id),
    kind text not null check (kind in ('original','result')),
    telegram_chat_id bigint,
    telegram_message_id bigint check (telegram_message_id > 0),
    telegram_file_id text,
    sha256 text check (sha256 ~ '^[a-f0-9]{64}$'),
    state text not null default 'pending' check (state in ('pending','confirmed','failed')),
    retention_until timestamptz,
    archived_at timestamptz,
    primary key (order_id,kind),
    check (state <> 'confirmed' or (telegram_chat_id is not null and telegram_message_id is not null and archived_at is not null))
);
create table commerce.events (
    id uuid primary key default gen_random_uuid(),
    source text not null,
    event_key text not null,
    order_id uuid not null references commerce.orders(id),
    kind text not null,
    payload jsonb not null check (jsonb_typeof(payload) = 'object'),
    processing_state text not null default 'pending' check (processing_state in ('pending','processed','failed')),
    received_at timestamptz not null default now(),
    processed_at timestamptz,
    unique (source,event_key)
);
create index orders_owner_created on commerce.orders(user_id,created_at desc);
create index checks_pending on commerce.checks(order_id,state);
create index events_pending on commerce.events(received_at) where processing_state <> 'processed';
create index commissions_partner on commerce.commissions(partner_id,state);

-- Storage is backend-only; Telegram identity must be authenticated by the bot,
-- never inferred from a caller-supplied user ID or a Supabase anonymous JWT.
do $$ declare t record; begin
    for t in select tablename from pg_tables where schemaname = 'commerce' loop
        execute format('alter table commerce.%I enable row level security', t.tablename);
        execute format('revoke all on commerce.%I from public, anon, authenticated, service_role', t.tablename);
    end loop;
end $$;
commit;
