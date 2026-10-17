# Four-service deployment guide

Status: implementation and isolated tests only. No production deployment or VPS
restart was performed. Execute a production cutover only after explicit approval
and after the outstanding real-Chromium, production-host and workload gates in
the test report are satisfied.

## Existing installation and state

The examples use `/srv/smart-zain`, its existing `.venv`, and a dedicated
`smart-zain` account. Change those paths consistently if the verified installation
uses a different root. Determine the current systemd unit and executable from the
actual host; do not guess the old unit name or assume the WSL machine is the VPS.
The deployed entry point before migration is `app.py`, not legacy `main.py`.

Reuse the installed interpreter and packages. This migration does not require a
new framework, package, database server or database file. Check prerequisites
before stopping the old application:

```sh
cat /etc/os-release
python3 --version
systemd --version
cat /sys/fs/cgroup/cgroup.controllers
test -x /srv/smart-zain/.venv/bin/python
test -f /srv/smart-zain/.commerce/outbox.sqlite
/srv/smart-zain/.venv/bin/python -c 'import openpyxl; import playwright.sync_api'
command -v google-chrome
```

The existing `.commerce/outbox.sqlite` is mandatory. Four-service production mode
refuses to create a replacement database if it is missing. New `ipc_*`, event
and `service_browser_tasks` tables are namespaced inside that existing file;
financial outbox tables remain intact. SQLite uses `journal_mode=PERSIST` and
`synchronous=FULL`, matching the local outbox. Its journal is recovery data.
Preserve the complete database and journal, not selected tables.

Preserve the original `.zain-queue.json`, checkpoints, WAL/stream files, original
uploads, completed results, commerce evidence, bot update offset, delivery state,
settings, profiles and worker configuration. Existing jobs keep their identifiers
and saved financial terms. The engine owns queue/checkpoints, the files service
owns workbook operations, Telegram owns polling/business interactions, and the
browser service returns read-only lookup results.

## Stop the previous owner and capture a recoverable baseline

Record the deployed source revision or source-artifact checksum, environment,
actual unit configuration, active queue state and in-flight operations privately.
Do not put bot tokens, credentials or customer rows in the engineering report.
Stop admission at a maintenance boundary, let current work checkpoint where
possible, and stop the verified old unit. Also account for manually launched
copies of `app.py`; an old version may predate the new ownership locks.

```sh
# Set this to the unit name verified on this host, before running the commands.
: "${OLD_MONOLITH_UNIT:?Set the verified old monolith unit name}"
sudo systemctl stop "$OLD_MONOLITH_UNIT"
sudo systemctl is-active "$OLD_MONOLITH_UNIT"
```

`is-active` must report inactive. Confirm that no old poller or scheduler remains
and that no process is writing the queue or local outbox. Never start the old and
new pollers against the same token or let two schedulers own the same queue.
Startup locks add protection but do not replace this cutover check.

With all application writers stopped, make a protected full-state backup:

```sh
ZAIN_BACKUP_DIR="/var/backups/smart-zain/$(date -u +%Y%m%dT%H%M%SZ)"
sudo install -d -m 0700 "$ZAIN_BACKUP_DIR"
sudo tar --acls --xattrs -C /srv -cpf "$ZAIN_BACKUP_DIR/pre-cutover.tar" smart-zain
sudo sha256sum "$ZAIN_BACKUP_DIR/pre-cutover.tar"
sudo tar --compare --acls --xattrs -C /srv -f "$ZAIN_BACKUP_DIR/pre-cutover.tar"
```

Archive comparison must succeed while the source remains quiescent. Retain the
checksum privately. Extract a copy into a separate protected verification
directory and run `PRAGMA integrity_check` on the copied outbox, checking that
the copied queue and referenced input/result files exist. Do not open the restored
copy with the real bot token. A checksum alone does not prove recoverability.
Retain a separate pre-change code artifact so code can be rolled back without
restoring stale financial or queue data.

## Configure and install units

Deploy the reviewed source artifact into the verified installation without
overwriting runtime/customer state. The four templates are in
`deployment/systemd`. The example environment contains no usable secret; replace
the token with an independently generated random value of at least 32 characters.
All four services must use the same token, root and ports.

```sh
sudo install -d -m 0750 -o root -g smart-zain /etc/smart-zain
sudo install -m 0640 -o root -g smart-zain deployment/systemd/services.env.example /etc/smart-zain/services.env
sudoedit /etc/smart-zain/services.env
sudo install -m 0644 -o root -g root deployment/systemd/smart-zain-*.service /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/smart-zain-*.service
sudo systemctl daemon-reload
```

Keep the existing Telegram/commerce credentials in the supported private runtime
configuration. Do not copy Windows-encrypted credentials into Linux and assume
they decrypt there. Set `ZAIN_SERVICE_TESTING` **unset** in production; never copy
an isolated fixture's `.services/TEST_FIXTURE` marker into production. If unit
verification reports a missing interpreter, account, path or unsupported setting,
resolve it before starting services.

Default loopback endpoints are engine `18701`, files `18702`, browser `18703`,
Telegram `18704`. IPC validates the shared token and workspace identity. Do not
publish these endpoints through a public reverse proxy. The existing dashboard
and Chrome bridge remain bound to loopback and keep their existing authorization.

## Start and monitor independently

```sh
sudo systemctl start smart-zain-files.service
sudo systemctl start smart-zain-browser.service
sudo systemctl start smart-zain-engine.service
sudo systemctl start smart-zain-telegram.service
sudo systemctl status smart-zain-{files,browser,engine,telegram}.service --no-pager
```

Authenticate a health check without printing the token:

```sh
sudo -u smart-zain /bin/bash -c 'set -a; source /etc/smart-zain/services.env; set +a; cd /srv/smart-zain; .venv/bin/python -c "from services.ipc import PORTS,RpcClient; print({name:RpcClient(name).health() for name in PORTS})"'
```

After all health and controlled workflow checks pass, enable boot startup:

```sh
sudo systemctl enable smart-zain-files.service smart-zain-browser.service smart-zain-engine.service smart-zain-telegram.service
journalctl -u smart-zain-telegram.service -n 100 --no-pager
systemctl show smart-zain-browser.service -p MainPID -p ControlGroup -p MemoryCurrent -p CPUUsageNSec
systemd-cgls --unit smart-zain-browser.service
```

Each role can be restarted independently, for example
`sudo systemctl restart smart-zain-browser.service`. Browser restart requeues
accepted read-only browser tasks in the existing outbox. It does not charge a
wallet or commit a workbook result. The engine retains row leases and commit
fencing. Stop/cancel controls and health reads have priority over ordinary RPC
work; pressure defers admission rather than failing a paid job.

Manual startup is also supported, in four separate terminals with the same
private environment loaded and all systemd/monolith owners stopped:

```sh
cd /srv/smart-zain
set -a; . /etc/smart-zain/services.env; set +a
.venv/bin/python -B -m services files --root /srv/smart-zain
.venv/bin/python -B -m services browser --root /srv/smart-zain
.venv/bin/python -B -m services engine --root /srv/smart-zain
.venv/bin/python -B -m services telegram --root /srv/smart-zain
```

Run one final command per terminal; each command is a long-running process.

## Resource calibration and remaining host gates

The templates use relative CPU/IO weights: Telegram 200, engine/files 100,
browser 50. These are interactive-priority policy choices, not measured capacity
guarantees. There is no guessed `CPUQuota`, `MemoryHigh` or `MemoryMax`. Browser
task/concurrency/route bounds are explicit configurable admission limits.
The memory admission helper reads actual cgroup high/max/current and host
MemAvailable. Optional `ZAIN_MEMORY_RESERVE_BYTES` and
`ZAIN_BROWSER_TASK_MEMORY_BYTES` require measured values; invalid values fail
closed. Health exposes task states and process-tree memory observations.

Measure p50/p95/p99 command handling, CPU, memory, disk I/O, queue waiting and
lock waits under supported file/API/Chromium workloads. Set measured cgroup
overrides with `systemctl edit` only after those runs, then verify the applied
values. Do not pin CPUs or use real-time priorities without evidence.

Isolated WSL Ubuntu 24.04.4/systemd 255 syntax, process-group and synthetic cgroup
cleanup tests passed; see `linux_environment.json`. They do not establish the
VPS configuration or actual Chrome resource requirements. Chrome/Playwright were
absent in that Linux fixture. Real Chromium crash handling, production-host
resource controls and workload measurements remain explicit deployment gates.

Official semantics: [systemd resource control](https://raw.githubusercontent.com/systemd/systemd/v255/man/systemd.resource-control.xml)
and [KillMode/process cleanup](https://raw.githubusercontent.com/systemd/systemd/v255/man/systemd.kill.xml).
