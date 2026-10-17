# AGENT HANDOFF STATE & ORCHESTRATION MANIFEST
**Project:** Smart Zain Checker (ZainChecker) Modular Monolith  
**Date:** 2026-10-17  
**Lead Orchestrator:** Lead Engineering Agent  
**Execution Mode:** Multi-Subagent Concurrent Engineering

---

## 1. Multi-Subagent Deployment Manifest

| Subagent ID | Role | Focus Area | Status |
|---|---|---|---|
| `ffcad79a-0794-43df-85f0-8369afd7e7bc` | Continuous Engine Specialist | Single-consumer queue loop (`ContinuousQueueService`), FIFO job ordering, worker leasing, customer cancellation/stop drain (`apply_customer_stop`) | COMPLETED (73+ Tests Passed) |
| `631c2a51-e126-462c-9e1a-4b1768c99f47` | Telegram & Commerce Specialist | Telegram UI, customer order visibility ('ملفاتي للمراجعة', 'طلباتي'), SHA-256 duplicate handling, stop confirmation & wallet refunds | COMPLETED (146+ Tests Passed) |
| `1b47dc52-7e68-451d-9102-5779e365bf15` | Pricing & Campaigns Specialist | Flexible pricing (per-check vs per-sheet), customer custom pricing overrides, free check allowances, promotional codes, and promotional credit expiry | COMPLETED (46+ Tests Passed) |
| `2ba738f5-d0d1-48a0-80e9-6a1a6ba6a614` | QA & Security Specialist | Security boundaries (A75), RPC role isolation, local HTTP enforcement, bridge authority, and anti-abuse strategies (A76) | COMPLETED (607+ Tests Passed) |

---

## 2. System Architecture & Workstream Status

### Workstream 1: Continuous Queue & Engine Execution
- **Core Files:** `manager/continuous_service.py`, `manager/orchestrator.py`, `manager/queue.py`
- **Current State:**
  - `ContinuousQueueService` automatically processes pending funded customer jobs in FIFO order without requiring manual admin start per job.
  - Multi-customer concurrency: When multiple customers submit orders concurrently, orders queue deterministically; active jobs drain leases while pending jobs display waiting status with dynamic queue positions (`ملفات أمامك في الطابور: X`).
  - Customer Stop & Drain: When an owner stops an active job, inflight worker leases finish their current lookup; remaining unleased rows transition to `cancelled` and are refunded to the customer wallet upon settlement.
- **Verification Evidence (Subagent 1):**
  - `tests/test_customer_commerce_runtime.py`: 29 Passed (100%) in 1.3s
  - `tests/test_telegram_order_history.py`, `test_telegram_queue_durability.py`, `test_restart_and_queue_flow.py`: 44 Passed
  - Total verified: 73 tests passing with zero failures. Deterministic FIFO ordering, pre-start stop, inflight lease safety, and auto-wake transitions confirmed.

### Workstream 2: Telegram Commerce & Order Visibility
- **Core Files:** `telegram_bot/ui.py`, `telegram_bot/commerce/checkout.py`, `telegram_bot/order_history.py`, `telegram_bot/commerce/wallets.py`
- **Current State:**
  - Order history panel (`_history_orders`) displays paid orders with timestamps, check counts, and execution status (`بانتظار بدء الفحص`, `جارٍ الفحص`, `اكتمل الفحص`).
  - SHA-256 Duplicate Check: Uploading identical files detects existing paid orders and prompts the user to either track existing order without charge or explicitly initiate a new paid run.
  - Stop action warning: User is explicitly warned that active worker tasks will finish before unexamined rows are credited back to wallet.
- **Verification Evidence (Subagent 2):**
  - `tests/test_telegram_order_history.py`: 21 Passed
  - `tests/test_telegram_payment_reliability.py`: 44 Passed (+5 subtests)
  - `tests/test_telegram_ui.py`: 20 Passed
  - `tests/test_commerce_checkout.py`: 13 Passed
  - `tests/test_commerce_wallets.py`: 13 Passed
  - `tests/test_wallet_activity_display.py` & `test_customer_commerce_runtime.py`: 35 Passed
  - Total verified: 146+ tests passing with zero failures. UI role segregation verified between Customer and Admin.

### Workstream 3: Pricing, Promotions & Expiry
- **Core Files:** `manager/pricing.py`, `telegram_bot/commerce/campaigns.py`, `telegram_bot/commerce/partner_campaigns.py`, `web_ui/static/js/pricing.js`
- **Current State:**
  - Per-check pricing supported alongside per-sheet pricing. Exact decimal piastre math ensures 70/30 split reconciles to the cent (`owner_minor + partner_minor == billed_minor`).
  - Per-customer custom overrides supported via `commerce_rpc.configure_customer_pricing` (migration 020) with fallback to global rates.
  - Large sheet allowances (e.g. first 3,000 checks free, remainder charged at per-check rate) supported via `free_checks` benefit kind.
  - Promotional vouchers grant free check quotas or promotional wallet balance without requiring external cash deposit receipts.
  - Unused promotional credit expiration implemented via `expire_promotional_credit` (migration 021) and periodic sweep every 60s in `ContinuousQueueService`, strictly isolating promotional grants from customer deposited cash and held reservations.
- **Verification Evidence (Subagent 3):**
  - `tests/test_pricing.py`: 10 Passed
  - `tests/test_commerce_campaigns.py`: 24 Passed
  - `tests/test_commerce_partner_campaigns.py`: 12 Passed
  - `tests/test_pricing.js`: Passed (Node.js pricing engine verification)
  - Total verified: 46 tests (+14 subtests) passing with zero failures.

### Workstream 4: Security, RPC Grants & Anti-Abuse
- **Core Files:** `server/access.py`, `server/bridge_authority.py`, `telegram_bot/authorization.py`
- **Current State:**
  - Remote Supabase database (`Retail_Store`) verified with schema version 39, 46 commerce tables, 105 backend RPC functions, all RLS enabled, and anonymous/authenticated access completely denied (`401 Unauthorized`).
  - Admin paths restricted to local host (`127.0.0.1`, `::1`), Host header validated, DNS rebinding prevented, and safe relative project path resolution (`workspace_file`) enforced.
  - Extension bridge requires unique worker authorization token and valid `chrome-extension://` origin.
  - Anti-abuse (A76): First-valid referral locking, self-referral blocks, prior paid transaction locks, net-collected cash commission calculations, and target number/contract fingerprinting roadmapped.
- **Verification Evidence (Subagent 4):**
  - `tests/test_access_boundaries.py`: 19 Passed (0.70s)
  - `tests/run_telegram_suite.py`: 269 Passed (145.08s)
  - `tests/run_commerce_suite.py`: 319 Passed (48.23s)
  - `tests/run_customer_commerce_acceptance.py`: 8/8 SQL cases Passed in local PostgreSQL instance
  - Total verified: 607+ tests passing with zero failures.

---

## 3. Database Acceptance Verification Baseline (Local PostgreSQL Port 63782)

| Acceptance Suite | Database Target | Checks Performed | Status | Evidence File |
|---|---|---|---|---|
| `run_customer_commerce_acceptance.py` | `customer_commerce_c9e34dde1ad3` | Customer pricing, promotional expiry, customer stop, cent discount, legacy transition, customer history, bound invoice, binance note | **PASSED** | `acceptance_customer_commerce_c9e34dde1ad3.json` |
| `run_admin_payout_acceptance.py` | `commerce_admin_payout_1d024b02f582` | Transactional SQL cases, 8 concurrent requests, single paid withdrawal, exact amount preserved, held funds unchanged | **PASSED** | stdout proof |
| `run_partner_advances_acceptance.py` | `advance_acceptance_8e2a456bf4e9` | Partner advance lifecycle, exact precision, recovery, notification idempotency | **PASSED** | `acceptance_advance_acceptance_8e2a456bf4e9.json` |
| `run_partner_campaign_acceptance.py` | `commerce_partner_campaign_36a2ebc373cd` | Partner campaigns, budget caps, replay defense, non-owner authorization barriers | **PASSED** | `acceptance_commerce_partner_campaign_36a2ebc373cd.json` |
| `run_admin_roles_acceptance.py` | `commerce_admin_roles_f99121e14a7c` | Native review before grant, backend ack wait, instant revoke denial, preserved admin sets | **PASSED** | stdout proof |

---

## 4. Test Verification Baseline

- **Total Unit & Regression Tests:** 607+ tests across offline Telegram suite (269) and Commerce suite (319), plus access boundaries (19). All passed with 0 failures, 0 errors.
- **Runtime Verification (`telegram_bot.commerce.verify_runtime`):** Passed, proof stored at `docs/telegram_commerce/runtime_verification_20261017T045437Z.json`, backend health: `ok`, anonymous RPC access denied (`401 Unauthorized`).
- **Production Guard:** Local test executions only. Live VPS remains stopped (`retail-store` bot safely offline), live balances preserved, no untracked production writes.
