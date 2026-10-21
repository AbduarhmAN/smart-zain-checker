# Telegram presentation update — 2026-10-21

Implemented and verified locally. This task has not published a GitHub commit, changed Supabase production, restarted the VPS or sent customer messages.

## Result

- `/start` opens one main panel. Existing command and old keyboard aliases remain recognized.
- Formal Arabic labels, a document icon for a new check, two-column navigation and restrained semantic button styles. The administrator's requested **رفع الشيتات** label is retained.
- Arabic paragraphs start on the right. Copyable numbers are isolated outside their `<code>` content, preserving their exact digits. Multiline code and preformatted content stay intact.
- File records have dividers, Telegram profile names, account identifiers, execution status and progress. Queue execution order and monitoring metrics are unchanged.
- Review drafts store their creation time and sort newest first before pagination, including after disk restoration. Older saved drafts fall back to their source modification time and stable insertion order.
- Current partners have two actions per record: details by name and payout. Details contain terms, price access, termination, earnings and referral links. Former partners retain their financial and lifecycle history.
- Ended partners, including a former AHMED record, are excluded from current presentation. This is based on actual end metadata, never a name/code heuristic. Suspended partners without an end date remain distinguishable from former partners. No partner is terminated, renamed or reactivated by this update.
- Earnings separate settled service revenue, partner commissions and platform share using the original server amounts. Financial pages contain four records to stay within Telegram's text limit. Previous partner totals remain included in the overall financial totals.
- Wallet history, payment review, customer activity, pricing, role lists and operational navigation use clearer labels and layouts. Financial decisions and their existing confirmation and authorization rules remain intact.

Telegram names come from private updates or read-only `getChat` lookups, using `first_name` and `last_name`. A local cache avoids repeated lookups. Missing names are shown as **الاسم غير متاح**. A bot cannot promise access to a person's private address-book label. No identity is inferred from a referral code.

## Database migration and release dependency

`supabase/migrations/202610210051_commerce_telegram_presentation.sql` is required for the complete presentation change:

1. It exposes `ended_at` in the existing earnings and partner-access read models. Ended partners use `state='suspended'` in the actual lifecycle schema; that state alone is insufficient to identify them.
2. It formats newly generated lifecycle notices and acknowledgements using Arabic labels and bidirectional isolates. Existing stored bodies, their hashes, and the exact-byte notification sender remain unchanged.

The migration preserves signatures, grants, ownership checks and financial expressions. It does not update partner identities, referral codes, historical events, wallets or ledger rows. The earlier migrations are unchanged.

For a later authorized release, apply migration 0051 before installing the UI source. Deploy against a newly checked production baseline and preserve ongoing work. Do not copy the whole dirty workspace or an older scheduler checkout over the VPS. The source-only task manifest and patch identify this task's runtime changes relative to its retained pre-edit snapshot.

## Verification

- **342 offline regression tests passed**, covering presentation, routing, permissions, payment review, payouts, lifecycle notices, pricing, drafts, queue durability and transport behavior.
- **79 Python source modules parsed** for syntax; no project build, compilation command or bytecode generation was run.
- A fresh retained PostgreSQL 18 instance accepted all migrations. Before/after assertions confirmed unchanged financial summary values, existing wallet/ledger/event snapshots and event hashes. The ended record remained available in former history and absent from the current-partner RPC. Newly generated notices had the expected Arabic direction and exact stored SHA-256.
- Existing partner-advance and partner-access SQL suites passed. Two older SQL fixtures failed both before and after migration 0051 with identical results: `Wallet hides historical role state` and `Partner earnings missing`. These are recorded baseline limitations, not a claim that every historical SQL suite passes.
- Two legacy percentage/crypto payment tests also fail on the retained pre-edit source: `test_percent_rate_failure_preserves_discount_and_claim` and `test_percent_quote_is_used_when_creating_replacement_invoice`. Their money/conversion behavior was not changed for this UI task. Three older workflow/display failures were independently reproduced on that baseline as well. Full details are in the retained baseline reports.
- **15 representative panels** were generated from the actual renderers using explicitly fictional fixtures. HTML structure, two-column rows, callback byte limits and Telegram's 4096-character text limit were checked, including a long-name/large-balance financial page. The local browser preview was visually inspected at phone width. This is not a claim of native Telegram device testing; final fonts and colors depend on the Telegram client.
- Test fixtures and temporary evidence were retained. No file/directory deletion or external checker traffic was requested by the validation scripts.

## Evidence

Local artifacts are retained under `outputs/telegram_ui_20261021/`:

- `preview.html`, `preview_validation.json`
- `verified_regressions.log`, `database_acceptance.json`
- `baseline_payment_failures.json`, `baseline_legacy_failures.json`
- `requirements.json`, `runtime_manifest.json`, `ui_changes.patch`
- `monitoring_diff.txt`, `source_before.json`, `source_recovery.json`, `before/`

One unreadable local legacy-wallet source file was restored from the exact tracked Git blob after confirming that blob matched the readable live VPS file. Its original bytes remain in the pre-edit snapshot. This was source recovery, not a financial logic change, and is excluded from the UI patch.
