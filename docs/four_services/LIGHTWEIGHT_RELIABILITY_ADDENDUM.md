# Integrated reliability addendum

The four role boundaries, original business logic and completed workbook/transport tests were preserved. No RocketMQ, Sentinel, TARS, Tarantool, NATS, Kafka, RabbitMQ, Celery, orchestration stack, new package or database server was introduced. IPC/update/browser tables reuse the application's existing `.commerce/outbox.sqlite`; production startup refuses a missing existing store. Fresh synthetic test roots explicitly create fixture persistence. Early development test-only per-role SQLite files were superseded by this shared existing-store design before any production cutover.

## Backpressure and ownership

The engine remains the only queue/checkpoint/WAL owner, Files the only workbook writer, Browser the only browser owner and Telegram the only bot poller. Kernel locks exclude competing service instances. Existing legacy entry points require the documented stop-before-cutover procedure.

RPC accepts at most 256 outstanding ordinary commands, with 16 reserved control positions. Two ordinary worker lanes plus a reserved control worker bound execution; the File application further serializes workbook work and bounds cached workbooks/previews. Browser execution and context limits are configurable and covered by capacity/cancellation tests. Telegram defaults to sixteen handlers, including two reserved control handlers, with bounded dispatch/inbox intake and per-actor ordering. These are tested software admission limits, not claims of optimal VPS sizing. Production resource tuning remains subject to the measured Linux workload gate.

Accepted work is durably pending under downstream pressure. `RetryLater` keeps the original ID, payload and owner. No paid job is removed merely because browser/file capacity is full. Browser task state and cancellation tombstones are durable; engine leases still fence authoritative result commits. Unknown monetary outcomes remain under existing commerce reconciliation.

Memory admission uses this process's Linux cgroup memory high/max and MemAvailable. Optional measured per-task estimates/reserves can be configured; no invented default Chromium RSS or memory ceiling is presented as measured. Health and cancellation have independent admission paths. Owned children stay in the systemd cgroup; POSIX launch groups permit targeted cleanup without killing unrelated browsers.

## Circuit behavior

`CircuitBreaker` uses a failure counter and monotonic recovery timestamp. CLOSED allows calls; OPEN temporarily defers unhealthy dependency requests; HALF_OPEN permits one health probe. Success closes the circuit, failure reopens it. Configurable defaults are three transport failures and five seconds before a probe. Healthy pending work/observation timeout is not a failed financial effect. These circuits wrap local service transport, not generic retries of payments, refunds or settlement.

## Evidence and remaining gates

Tests cover circuit states/single probe, memory pressure, retained deferred requests, reserved cancellation, duplicate IDs, durable task restart, file integrity and actual four-process failures. `TEST_RESULTS.md` and the retained JSON files identify executed runs and failures. `PERFORMANCE_COMPARISON.md` separates in-handler latency from IPC observation and Telegram network time.

Unverified: real VPS workload/resource ceilings, real Linux Chromium failure under production-equivalent sessions, all eight original workload scenarios and unexpected VPS reboot. WSL tests establish Linux process-group and systemd cgroup mechanics with synthetic owned children; they do not establish real Chromium memory sizing. Production remains unchanged and is not declared ready from partial tests.
