"""Private service configuration and bounded, separate service execution loops."""
import json
import os
from pathlib import Path
from engine_v3.proxy_scheduler import route_key
from workers.low_bandwidth_transport import enabled, SCHEDULER


def service_worker_config(configured, root):
    """Replace only service workers, from an explicitly selected private file."""
    filename = os.environ.get('ZAIN_SERVICE_WORKERS_FILE')
    if not filename or os.environ.get('ZAIN_SERVICE_ENGINE') != 'low_bandwidth':
        return configured
    try:
        path = Path(filename)
        if not path.is_absolute():path = Path(root) / path
        data = json.loads(path.read_text(encoding='utf-8'))
        workers = data['workers']
        start, end = data['start_index'], data['end_index']
        if not workers or len(workers) != end - start + 1:raise ValueError()
        accounts = [w for w in configured if w.get('task_scope') == 'account']
        if any(w.get('task_scope') not in ('account', 'service') for w in configured):raise ValueError()
        ids = {w['worker_id'] for w in accounts}
        routes = set(); indices = set()
        for worker in workers:
            if worker['task_scope'] != 'service' or worker.get('use_proxy') is not True:raise ValueError()
            proxy = worker['proxy_url']
            active = worker.get('enabled', True)
            if type(active) is not bool or not proxy or (active and not enabled(proxy)):raise ValueError()
            key = route_key(proxy)
            if key in routes or worker['worker_id'] in ids:raise ValueError()
            routes.add(key); ids.add(worker['worker_id']); indices.add(worker['proxy_index'])
        if indices != set(range(start, end + 1)):raise ValueError()
        return accounts + [w for w in workers if w.get('enabled', True)]
    except Exception:
        # Never expose private file contents or a credential-bearing URL.
        raise ValueError('Invalid private HTTP service worker configuration') from None


def is_http_service_actor(actor):
    return (actor.use_direct_api and actor.can_check_services
            and not actor.can_check_accounts
            and enabled(actor.proxy_url if actor.use_proxy else None))


def worker_groups(actors):
    """Each route belongs to one loop; account/browser workers retain their loops."""
    service_ids = []
    groups = []
    for actor in actors:
        if not actor.use_direct_api:
            continue
        if is_http_service_actor(actor):
            service_ids.append(actor.worker_id)
        else:
            groups.append((actor.worker_id,))
    count = min(SCHEDULER.limit, len(service_ids))
    groups.extend(tuple(service_ids[index::count]) for index in range(count))
    return groups
