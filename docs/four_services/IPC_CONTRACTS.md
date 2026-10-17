# Wire and persistence contract

POST `http://127.0.0.1:PORT/rpc`, JSON envelope:

```json
{"id":"stable-operation-reference","operation":"queue.get_jobs","payload":{}}
```

Ports default to engine 18701, files 18702, browser 18703 and Telegram 18704. `ZAIN_<ROLE>_PORT` overrides a port. Requests require `Authorization: Bearer ZAIN_IPC_TOKEN` (at least 32 characters) and the SHA-256 workspace identity header. Token values never belong in URLs, reports or source. Bind is loopback only; root/user permissions remain part of the trust boundary.

Only registered operation names are executable. The explicit JSON codec preserves Decimal text, paths, date/time, Customer fields and QueueJob fields; no dynamic wire-selected import, pickle or code evaluation is allowed. Body/response cap is 64 MiB. Financial commerce RPCs retain their existing independent authorization and idempotency semantics.

| Interface | Contract and ownership |
|---|---|
| `queue.get_jobs/get_job` | Engine snapshots; never instantiate a second JSON queue owner in Telegram |
| `queue.add_job` | Confined input path; existing payment-token identity prevents a paid job becoming a second job |
| `queue.update_pending_mapping/remove_job` | Engine rechecks untouched/pending state, paid-input immutability, active job and expected identity |
| `queue.request_customer_stop` | Owner/reference validation in existing queue logic; retained active work finishes before existing export/settlement |
| `queue.review_start`, `engine.start_reviewed` | Validate workbook, retain reviewed signature/versions, recheck under authoritative lock before start |
| `engine.session_action` | Expected session/workbook checked atomically; existing pause/resume/repair/result rules retained |
| `engine.snapshot/records` | Lightweight cached state and separately requested report records; stale state is explicit |
| `engine.completed_evidence` | Engine reads its own per-job checkpoint/WAL, verifies identities and returns completion evidence |
| `event.list/ack` | Named Telegram consumer, durable per-event acknowledgement; completion capture precedes acknowledgement |
| File operations | `workbook.names`, `sheet.schema/validate/extract/records/quote_rows/dimensions/rows`, `report.export`, `source.update`, `stream.append`, `audit.*` |
| Browser operations | `query_submit/status/cancel`, verification and configured native-worker operations; no workbook/queue/financial writes |
| Telegram management | status, enable/disable, connection check and explicit dashboard test-message route |
| GET `/health` | Authenticated independent health path; PID/readiness, service-specific status |

Durable commands acknowledge acceptance only after a FULL-synchronized transaction. ID plus operation/payload digest defines identity; mismatched replay returns HTTP 409. States are pending, running, succeeded, failed and uncertain. Lost HTTP acknowledgement requires the **same ID**. A request timeout is an observation boundary, not cancellation or permission to create a replacement job/payment. Known read/idempotent operations can recover after interruption; unreconciled mutations remain uncertain for explicit state reconciliation.

Heavy work uses bounded worker execution and durable pending rows. `RetryLater` defers the same accepted row with a not-before time. Reserved control capacity serves cancellation separately from heavy work. Ordinary waiting/backpressure is not a failed financial transaction. Browser accepted tasks have an additional durable lifecycle in the existing database and cancellation tombstones prevent delayed revival.

Telegram updates persist before advancing the remote offset. Duplicate update IDs are deduplicated per bot fingerprint. Dispatch is bounded and ordered per actor. Interrupted updates can be replayed; irreversible business effects still depend on the existing stable transaction/order references. Handler exceptions become review states instead of blindly repeating uncertain payments.

Socket, operation and observation timeouts are bounded and vary by operation: local observations are short; parsing permits 120 seconds and full reports 300 seconds with same-ID observation. Browser queries retain their original task ID and engine lease fencing. Transport circuits use configurable failure threshold/recovery intervals and a single half-open health probe; they do not wrap commerce financial transactions.
