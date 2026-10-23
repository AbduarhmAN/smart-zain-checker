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

The production cutover is pending at this source commit. It requires idle
checking, validated source hashes, a single-attempt database transaction,
retained source/cache backups, and successful bot health verification.
