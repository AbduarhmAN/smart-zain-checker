# Actual source changes

- `services/__main__.py`: independently invocable role runtimes, service kernel locks, signal cleanup and authenticated RPC listeners.
- `services/ipc.py`, `codec.py`, `locking.py`, `persistence.py`, `events.py`: explicit wire contracts, exact JSON values, stable-ID command handling, existing-store namespaces and durable event acknowledgements.
- `services/engine.py`: sole queue/orchestrator owner, background status snapshots, guarded mutation handlers, existing scheduler/gateway and remote Telegram management.
- `services/telegram.py`, `engine_client.py`: Telegram/commerce process, local observations, explicit mutation adapters and completion-event relay.
- `telegram_bot/update_inbox.py`, bot/integration/UI changes: durable update intake, bounded dispatch, token-scoped poller lock, reserved controls and no heavy workbook execution in handlers in service mode.
- `services/files.py`, `file_client.py`, domain workbook/validation/exporter hooks: remote sheet descriptors, parsing/export ownership, bounded preview writes, atomic workbook publication and original-file protections.
- `services/browser.py`, `browser_client.py`, `browser_store.py`, worker hooks: durable browser tasks, bounded execution/routes, cancellation tombstones, remote verification and no local browser fallback in the engine.
- `workers/launcher.py`: owned POSIX process groups and termination guards; unrelated/stale PIDs are not killed.
- `services/resources.py`, `circuit.py`, `logging.py`: kernel-aware admission, limited transport recovery probes and redacted structured service records.
- `deployment/systemd/`: four unit definitions, secret-free environment example and resource-control explanation.
- `tests/test_services_*`, isolated integration/baseline scripts: new reliability, integrity and process-isolation coverage. Existing regression fixtures corrected where they targeted obsolete extraction/loader boundaries; signature checks remain enforced.

Existing unrelated dirty changes remain in place. The Git diff against HEAD includes work predating this request and must not be described as exclusively produced by this refactor. Source inventory and retained test evidence distinguish the current implementation.
