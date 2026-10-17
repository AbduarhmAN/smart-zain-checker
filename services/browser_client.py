"""Engine-side browser RPC adapter. Importing this module never starts Chrome."""
from __future__ import annotations

import base64
import os
from pathlib import Path
import time
import uuid


def remote_browser_enabled() -> bool:
    return os.getenv('ZAIN_SERVICE_MODE') == '1' and os.getenv('ZAIN_SERVICE_ROLE') != 'browser'


def client():
    from services.ipc import RpcClient
    return RpcClient(service='browser', root=Path(os.getenv('ZAIN_PROJECT_ROOT', Path.cwd())))


def query(number, timeout_seconds=45.0, proxy_url=None, on_verification=None, *, task_id=None):
    """Submit once, then observe the same read-only task; never retry as a new task."""
    task_id = str(task_id or uuid.uuid4().hex)
    rpc = client()
    deadline = time.monotonic() + float(timeout_seconds) + 205.0
    noticed = False
    try:
        from services.ipc import ServiceUnavailable
        payload = {'task_id': task_id, 'number': number, 'timeout': timeout_seconds,
                   'proxy_url': proxy_url, 'allow_verification': on_verification is not None}
        while time.monotonic() < deadline:
            try:
                rpc.call('query_submit', payload, request_id=task_id, timeout=3.0)
                break
            except ServiceUnavailable:
                # Admission/memory pressure leaves this exact command durable
                # and pending. Observe it again; do not spend an engine retry.
                rpc.health(timeout=1.0)
                time.sleep(.2)
        while time.monotonic() < deadline:
            try:
                reply = rpc.call('query_status', {'task_id': task_id}, timeout=3.0)
            except ServiceUnavailable:
                rpc.health(timeout=1.0)
                time.sleep(.2)
                continue
            if reply.get('verification') and not noticed and on_verification:
                noticed = True
                on_verification(reply['verification'])
            if reply.get('done'):
                result = reply['result']
                return str(result[0]), result[1], str(result[2])
            time.sleep(0.2)
        rpc.call('query_cancel', {'task_id': task_id}, timeout=3.0)
    except Exception:
        # The engine's lease/retry policy owns recovery. Do not fall back to
        # in-process Chrome, switch routes, or submit a second task here.
        return 'network_error', None, 'محرك المتصفح غير متاح؛ سيحاول المحرك استعادة المهمة بأمان'
    return 'network_error', None, 'انتهت مهلة استعلام المتصفح'


def pending_verifications():
    try:
        return client().call('verification_list', {}, timeout=1.0)
    except Exception:
        return []


def verification_image(challenge_id):
    value = client().call('verification_image', {'id': challenge_id}, timeout=2.0)
    return base64.b64decode(value['image'], validate=True) if value.get('image') else None


def submit_verification(challenge_id, answer):
    return bool(client().call('verification_submit', {'id': challenge_id, 'answer': answer}, timeout=2.0))


def cancel_pending_verifications():
    try:
        client().call('verification_cancel', {}, timeout=2.0)
    except Exception:
        pass


def worker(operation, worker_id):
    return client().call(operation, {'worker_id': worker_id}, timeout=25.0 if operation == 'worker_launch' else 3.0)
