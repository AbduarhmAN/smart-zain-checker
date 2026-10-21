# Excel lookup column detection — 2026-10-21

`inspect_sheet_schema` delegates to `detect_smart_sheet_columns` in
`domain/workbook.py`. The change is confined to that detector.

## Local correction: empty wallet and separate account/service roles

The earlier deployed detector selected a wallet by its header even when every
wallet cell was empty, then assigned that column to both `lookup_col` and
`service_col`. This displaced populated account and service columns.

The local correction scans declared identifier candidates for readable data,
including data below the first five rows. Account/contract columns keep their
own `lookup_col`. Service selection uses populated wallet, primary service,
then secondary service columns, independently of the account column. Without
a separate account column, a populated service can remain the single lookup.
When all identifier data is absent or invalid, declared headers remain mapped
so row validation reports the actual affected rows; such input cannot pass
validation. Explicit user mappings remain unchanged.

The supplied real workbook has 1,574 populated account cells in L and service
cells in AF, zero populated wallet cells in AK, and 1,024 secondary-service cells
in AQ. The corrected mapping is L/AF. All 1,574 rows pass offline validation;
existing deduplication selects 1,504 checks: 1,220 service checks and 284 account
checks. Account-only selection contains 1,128 unique accounts. The executable
selection and financial quote have identical source-row IDs and counts, all
selected original accounts and services are preserved, and the source SHA-256
is unchanged. No external checking requests were sent.

Follow-up validation: 35 local unittest checks passed. A staged copy of current
production code and dependencies passed 53 Linux checks, including independent
account/service selection, empty and sparse columns, results/error re-ingestion,
financial quoting, the file-service boundary, and a 50,000-record extraction.
External traffic was blocked and all fixture files were retained. The unused
pytest import in the three pure results/error tests was omitted in an isolated
test copy; their test bodies ran unchanged with unittest.

The correction is local; the earlier deployment receipt below does not confirm
deployment of this follow-up. Verification is retained privately under
`outputs/empty_wallet_mapping_20261021/`.

## Earlier deployed behavior (superseded locally)

The earlier automatic lookup precedence, independent of column order, was:

1. Wallet: `رقم المحفظة`, `المحفظة`, `محفظة` (including normalized ة/ه forms).
2. Explicit service number: `رقم الخدمة` / `رقم الخدمه`.
3. Existing account, contract, combined identifier, and generic number aliases.

The selected wallet/service column is used for both `lookup_col` and
`service_col`, so a second service column cannot override the preferred lookup
during row extraction. Equal-priority candidates retain the first column.
Old account-only files and explicit user mappings continue to work.

Headers containing `الملغية`, `ملغية`, `الملغي`, or `ملغي` are excluded from
both identifier roles and sample-based number inference. They are also excluded
from sample-based amount inference. Keywords are normalized consistently with
headers. Wallet names and wallet values cannot displace identifier columns.

## Earlier rollout validation

- 52 relevant local tests passed, with 48 subtests: new lookup cases, legacy
  ingestion, results/error re-ingestion, file-service RPC, and column validation.
- 47 offline tests passed in an isolated copy of current VPS code, including
  wallet/service priority, cancellation, extraction, headerless input, account
  compatibility, and file-service behavior. Live network requests were blocked.
- Production-source AST comparison confirmed that code outside the detector
  matches the VPS baseline.
- A broader UI fixture run had 11 existing failures/errors among 30 tests;
  repeating it with the old and updated detectors produced identical outcomes.
  The unrelated full gateway integration test was blocked by occupied local
  application ports before reaching schema analysis.

Evidence is under `outputs/schema_lookup_20261021/`. The deployment helper
requires a matching source baseline, passing Linux tests, idle checking, and
healthy polling. It preserves access policy and proxy configuration, backs up
the original module, updates one runtime file atomically, and verifies recovery.
It sends no Telegram messages and modifies no customer spreadsheets.

Deployment completed successfully. The bot resumed healthy polling with the
existing access policy and proxy configuration. Backup:
`/root/zain_check/.code-backups/schema-lookup-20261021T111202Z`.
Published SHA-256:
`998aa2d9446524b718954a46391e7bdf8656ee21e865fbf5679b1b7ea33ad5b7`.
