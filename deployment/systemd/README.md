# systemd templates

These units run the four applications separately. They have not been installed
or validated on the production host. Verify the host, Python environment,
systemd version, cgroup v2 controllers, mounted paths and available Chrome first.
Use the repository deployment/cutover guide before enabling the units; never run
them against a bot token or queue already owned by the monolith.

The units intentionally use `Wants`/`After`, without `Requires`, `PartOf` or
`BindsTo`: dependency downtime must not stop Telegram. Readiness is reported by
the authenticated health endpoints, rather than inferred from startup ordering.

All roles use the same non-root account and private deployment root; their
service singleton locks prevent duplicate instances. Provision the token in
`/etc/smart-zain/services.env`, and restrict configuration and runtime files to
the service account. Do not commit the real environment file.

`CPUWeight` and `IOWeight` express relative priority during contention. The
200:100:50 weights give interactive Telegram preference; they are policy
choices, not benchmarked CPU guarantees. Files and engine retain ordinary
weight. Neither realtime scheduling nor CPU pinning is used. No unmeasured
absolute CPU quota, memory ceiling or process-count ceiling is imposed.
`MemoryAccounting` exposes a baseline; set measured `MemoryHigh`/`MemoryMax`
overrides only after staged large-file, API and Chromium workloads have run.
An OOM kills the affected unit as a group and systemd restarts it independently.

Chrome is launched inside the browser unit. `KillMode=control-group`, an ordinary
SIGTERM shutdown, and final SIGKILL after the stop deadline handle all remaining
children. The application first cancels jobs and closes its owned contexts.
It never kills arbitrary system Chrome processes or removes another profile's
locks. Confirm the cgroup at runtime using:

```sh
systemctl show smart-zain-browser -p ControlGroup -p MainPID -p MemoryCurrent -p CPUUsageNSec
systemd-cgls --unit smart-zain-browser.service
systemctl show smart-zain-telegram -p CPUWeight -p IOWeight
systemctl status smart-zain-{telegram,files,engine,browser}.service
journalctl -u smart-zain-browser.service -n 100 --no-pager
```

Before installation, validate the local unit files with the target host's
`systemd-analyze verify`. CPU/IO weights require the relevant controllers;
verify `/sys/fs/cgroup/cgroup.controllers` and `systemd --version`. Install unit
files as root-owned mode `0644`; Windows-mounted files appear writable and
executable to WSL and generate permission warnings if verified in place.

An isolated Ubuntu 24.04.4 WSL2 environment with systemd 255 was subsequently
checked. Syntax verification passed for temporary unit copies with the Python
path changed to the installed interpreter. A transient user unit running a
Python parent/child fixture confirmed inherited cgroup membership,
`CPUWeight=50`, `IOWeight=50`, complete child termination, and removal of the
cgroup after stop. Actual launcher process-group termination also passed.
See `docs/four_services/linux_environment.json` and the reproducible
`scratch/verify_four_services_linux.py`. This proves Linux process/cgroup
behavior for the fixture; Chrome/Playwright were absent, so it does not prove
actual Chromium behavior or the production VPS configuration.

Official references: [systemd v255 resource controls](https://raw.githubusercontent.com/systemd/systemd/v255/man/systemd.resource-control.xml)
and [process-group shutdown](https://raw.githubusercontent.com/systemd/systemd/v255/man/systemd.kill.xml).
