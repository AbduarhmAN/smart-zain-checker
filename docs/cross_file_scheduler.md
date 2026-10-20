# Independent account and service scheduling across files

The application now uses one shared worker pool and a separate Orchestrator for each admitted file. Each worker takes the oldest available row matching its account/service scope. A later file can complete while an earlier file waits for another type or a deferred retry. Queue position is the upload order, not a promise of completion order.

Each file retains its own row identities, leases, checkpoint, WAL, writer, output and completion/settlement event. Service route reservations and verification blocks apply across all files. Finishing one file does not stop the shared workers. Customer stops affect only their own file; global pause affects all admitted files. Administrator status distinguishes the displayed file from the concurrent-file count and offers resume for individually paused files.

Manual starts approve the current local round; uploads added later are not silently approved. Customer admission retains the funded order, held reservation, owner and consent checks. No financial schema changes are needed for the scheduler. Concurrent residency is limited to 32 files and 200,000 declared rows; an oversized file can run alone. Additional jobs remain queued. Set `ZAIN_CROSS_FILE_SCHEDULING=0` before a process start to use the previous single-file engine.

## Verification

Run `python -B tests/run_scheduler_regressions.py` and `python -B tests/run_cross_file_acceptance.py`. The runners retain temporary fixtures, reject external network connections and do not run builds or deletion. The acceptance uses real Excel parsing, durable row commits, checkpoints, WAL and exported workbooks with controlled local lookup responses. These are not live customer checks or a bandwidth benchmark.

On 2026-10-20, the focused suite passed 225 tests; administrator/menu/management checks passed 40 tests. The queue-resilience function and six JavaScript bridge assertions also passed. The 20-workbook acceptance completed 10,010 account rows and 1,010 service rows, published file 19 while file 15 waited, resumed the remaining file with 100 completed rows preserved, ignored a stale result and verified ownership of every updated workbook and all 20 completion events. Source digests were unchanged throughout the final acceptance.

Five pre-existing failures in `test_telegram_workflow_reliability.py` reproduce on the unmodified VPS baseline: the old harness lacks `_maintenance_gate`; two invoice tests target an older checkout contract; customer navigation expects no status button; and wallet text expects a currency conversion no longer displayed. This change does not repair those unrelated contracts. They remain documented failures, not passing checks.

## Release scope

This branch starts from the sanitized VPS source baseline and includes local regression coverage and the existing local extension/management source overlay. The server deployment scope is restricted to the scheduler, its entrypoints, bridge task metadata and Telegram scheduling/status controls. Runtime credentials, worker/proxy configuration, financial state, customer workbooks and queue/checkpoint data must not be replaced. Source updates require a fresh idle check, retained backups, digest verification and readiness checks. No new customer file is submitted merely to test the release.

The published source also passed all 225 focused regressions in an isolated directory on the VPS using Python 3.12.3. The deployment preflight then observed a newly active file and deferred without stopping the application or changing production sources. A follow-up in the current conversation checks every five minutes and applies the reviewed update only when the queue is idle.
