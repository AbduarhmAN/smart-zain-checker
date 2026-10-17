# Verified architecture and provenance

The authoritative development checkout is `E:\Projects\smart_zainchecker_gemini`, branch `main`, with extensive pre-existing uncommitted work. `source_inventory.json` records HEAD and selected source hashes. All nine compared entry point/core files differ from the other checkout, `E:\Projects\zainCheck_smart`; they must not be treated as interchangeable deployments. No reset, cleanup or production deployment was performed.

The legacy entry point is `app.py`. It constructs `WorkerSupervisor`, `QueueService`, `Orchestrator`, dashboard/extension gateway, `TelegramIntegration`, and `ContinuousQueueService` in one process. `main.py` is a legacy implementation and is not the new service entry point.

Before this change, Telegram handlers parsed workbooks, generated reports and took the orchestrator's lock for routine navigation. `start_queue` holds that lock during initialization. `EventBus` invokes subscribers synchronously. Telegram's 64-thread executor had an unbounded backlog and its persisted update offset advanced before handler completion. These are verified code paths, not a claim that CPU, GIL, network or memory pressure caused any particular production delay.

Execution state consists of the existing JSON queue, per-job checkpoints and authoritative WAL. Commerce financial truth remains PostgreSQL through the restricted commerce client; the existing local `.commerce/outbox.sqlite` persists commands/delivery evidence. These stores and their financial semantics have not been replaced.

Development host: Windows, Python 3.14, eight logical processors, approximately 25.66 GB physical memory. Separate authorized Linux tests used an existing WSL Ubuntu 24.04.4 installation, Python 3.12.3, systemd 255 and cgroups v2. `linux_environment.json` records evidence. These are **not measurements of the production VPS**. Its previously reported four CPUs/eight GB, Python and systemd configuration have not yet been independently verified in this run.
