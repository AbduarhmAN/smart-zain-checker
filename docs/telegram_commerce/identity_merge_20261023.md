# Retired identities after an owner-authorized account merge

The owner requested consolidation of duplicate partner accounts into one
existing Telegram identity. The reviewed operation preserves the canonical
partner and its financial records, transfers the duplicate's customer
attribution and partnership history, and changes the retired referral code
into an alias of the canonical code. No commission is recalculated.

Migration `202610230054_commerce_identity_retirement` provides a protected,
RLS-enabled retirement audit registry and a database trigger that rejects
registration of retired Telegram identities. Its contents are private.
The one-off account transfer statement and identifying evidence are retained
privately and are not part of this public migration.

The bot reads a private `.commerce/retired-telegram-identities.json` mirror.
Retired senders are stopped before profile caching, support routing, commands,
callbacks, payments or file handling. Their credentials never acquire the
canonical account's authority. Canonical and unrelated senders retain their
existing behavior. The duplicate's cached profile is removed at cutover.

Historical message bodies and their SHA-256 hashes remain unchanged.
Transferred sent lifecycle notifications preserve their actual original
destination in `original_recipient_chat_id`; their state stays sent so the
merge cannot resend them. Raw processed Telegram updates remain audit evidence,
not active account records.

Validation: 23 offline tests passed locally and against an isolated copy of the
current VPS source and dependencies. Tests cover registration retirement,
message/callback order, profile-cache suppression, canonical authority,
forwarded messages, corrupt registries, and existing presentation behavior.
The real database merge passed transactional rollback and replay validation;
fingerprints of financial records, orders, unrelated users, partners and
referrals stayed unchanged. Actual re-registration was rejected, and the
retired referral alias resolved to the canonical partner without a second
attribution.

The production cutover completed after idle checking was confirmed. Two runtime
modules and the private retirement/profile registries were published with
retained backups, and the database merge committed in one transaction. Healthy
public bot polling recovered; no deployment notices or paid checks were sent.

The independent completion audit checked 37 foreign-key columns, 43 active
identity columns and 32 structured JSON columns. No active binding to the
retired identity remained. Actual current/former partner-list RPCs excluded the
duplicate, and the canonical partner appeared once. Transferred records include
one customer attribution, three historical terms versions, four lifecycle events
and eight preserved notification records. Sent receipts and original message
bodies/hashes remained intact. Processed Telegram updates remain historical
evidence, and the duplicate profile cannot be cached again.

Runtime source commit: `c0857d990d634ea36846c00bdeac0763ba016b25` on
`codex/merge-retired-identities-20261023`. Private evidence, authorization,
financial fingerprints and detailed receipts are retained under
`outputs/merge_ahmed_20261023/` and in the protected database retirement audit.
The sanitized deployment receipt is
`docs/telegram_commerce/identity_merge_deployment_20261023.json`.
