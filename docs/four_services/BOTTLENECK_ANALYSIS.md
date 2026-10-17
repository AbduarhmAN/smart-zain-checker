# Findings, measurements and unresolved hypotheses

Confirmed execution-path risks:

- Telegram workbook inspection, validation and export used to run inside the handler process. They now delegate through the File interface in service mode.
- Administrator navigation acquired the execution lock; initialization can hold it while loading files and starting workers. Service-mode navigation now reads a local snapshot refreshed by a background observer.
- Sixty-four executor workers did not bound accepted work. A durable inbox, bounded dispatch and reserved control lane now limit execution independently of durable intake.
- Empty RPC consumers originally acquired SQLite's writer lock repeatedly. A read-only pending-work probe now precedes the transactional claim; the actual claim is still revalidated atomically.
- Renderer microbenchmarks found repeated per-cell style assignments expensive. Registered public NamedStyles reuse identical styling; formula and formatting regression tests accompany the change. Microbenchmark speedup is not an end-to-end application speedup.

Controlled integration measurements are retained under `outputs/four_services/acceptance_*`. Early runs exposed high RPC wall-clock tails and slow engine-outage interactions; these findings drove background observation and idle-claim changes. Read `PERFORMANCE_COMPARISON.md` and the latest JSON before judging the final result. A healthy-process check does not establish a sub-500 ms customer response.

Not yet established on the actual VPS: CPU/GIL contribution, Chromium RSS under customer workloads, disk latency, network contribution, production lock-wait distribution and useful CPU affinity. No real-time scheduling, CPU pinning or uncalibrated memory ceiling was applied.
