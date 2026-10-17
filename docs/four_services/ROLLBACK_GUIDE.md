# Four-service rollback guide

No production rollback has been executed. A rollback is an operational cutover
and requires approval. Prefer an independent service restart for an isolated
failure; do not restart Telegram to recover a browser or file-service outage.

## Preserve the current authoritative state

The old monolith and new services must never poll the same token or own the same
queue concurrently. Stop Telegram first to stop new bot admission, then stop
the engine, browser and files services. Shutdown requests checkpointing and owned
browser cleanup; inspect failures rather than deleting locks or journals.

```sh
sudo systemctl stop smart-zain-telegram.service
sudo systemctl stop smart-zain-engine.service
sudo systemctl stop smart-zain-browser.service
sudo systemctl stop smart-zain-files.service
sudo systemctl is-active smart-zain-telegram.service smart-zain-engine.service smart-zain-browser.service smart-zain-files.service
```

All four must be inactive, and no manual service copies may remain. Confirm
their PIDs and the browser cgroup are gone. `KillMode=control-group` cleans owned
children; do not run a global `pkill chrome` or remove another profile's lock.

Take another protected full-state archive and compare it to the stopped source
as in the deployment guide. Preserve `.commerce/outbox.sqlite`, its journal,
`.zain-queue.json`, all checkpoints/WAL/stream files, uploads, completed outputs,
Telegram offset, delivery evidence, browser profiles and current configuration.
Verify the copied database with `PRAGMA integrity_check`.

**Do not restore the pre-cutover database, queue or bot offset after new
transactions or jobs have occurred.** Doing so could forget charges, duplicate
updates, lose results or replay a notification. Roll back code against current
state. Keep both backup generations for recovery and reconciliation.

## Reconcile interrupted commands

The existing local outbox contains namespaced `ipc_<role>_commands`, engine event
and `service_browser_tasks` tables. Retain all of them. The legacy code ignores
these tables; removing them destroys evidence needed for a later migration.

Inspect command states and identifiers privately. A request left `uncertain`
must be reconciled against the original queue/order/payment/result evidence and
its original idempotency key. Do not generate a new payment, refund, payout,
receipt or charge to clear a transport error. Do not change a command to
`succeeded` by hand. Pending browser operations are reads and can be replayed
under the new service's engine fencing; they never independently settle money.

If an active job cannot be mapped safely to current queue/checkpoint evidence,
keep its source, completed rows, leases and financial hold and resolve the
specific mismatch before starting another owner. A clean rollback is not proven
by merely observing that the process starts.

## Restore reviewed code and the single old owner

Use the pre-cutover **code artifact** and its manifest to restore only application
source and matching unit configuration. Keep all runtime/customer state from the
current stopped installation. Do not extract an entire old project archive over
the live state directory, run `git reset --hard`, or delete untracked files.
The exact artifact command depends on the recorded deployment layout; verify its
allowlist and checksums before applying it.

Disable automatic startup of the four units so a reboot cannot create another
poller or scheduler:

```sh
sudo systemctl disable smart-zain-telegram.service smart-zain-engine.service smart-zain-browser.service smart-zain-files.service
sudo systemctl daemon-reload
: "${OLD_MONOLITH_UNIT:?Set the old unit name recorded before cutover}"
sudo systemctl start "$OLD_MONOLITH_UNIT"
sudo systemctl status "$OLD_MONOLITH_UNIT" --no-pager
```

The restored old unit must use the verified `app.py` entry point and current
workspace. Remove four-service-only environment flags from that unit's
environment. Do not enable `ZAIN_SERVICE_TESTING`; the fixture transport must
never receive real customer jobs. Enable the old unit at boot only after its
single-poller, queue recovery and customer workflow checks pass.

Verify customer ownership, wallet/holds, cancellation/refund behavior, completed
result integrity, pending payment evidence and delivery claims using the same
business records that existed immediately before rollback. Do not make
synthetic monetary mutations on production for verification.

## If rollback validation fails

Leave the affected owner stopped and retain current state and both verified
archives. Restarting the new four services against the same preserved outbox is
possible only after confirming the old owner is stopped. Reuse original request
IDs and reconciliation paths. Restoring old financial state is not a substitute
for reconciling an uncertain action.

Actual production rollback and old-version compatibility with every current
in-flight record remain unverified until exercised on a protected copy of the
production state. WSL process/cgroup tests do not establish that compatibility.
