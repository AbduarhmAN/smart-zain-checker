"""Versioned boundary between the checker and commerce, without financial policy.

Deferred/retry results are not terminal. Export readiness is not delivery proof.
This module has no network, wallet, Telegram or filesystem side effects.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID


def _integer(value: object, name: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum or value > 9223372036854775807:
        raise ValueError(f"{name} must be an integer >= {minimum} within bigint range")
    return value


def _text(value: object, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"Invalid {name}")
    return value


@dataclass(frozen=True)
class CheckerEvent:
    order_id: str
    event_key: str
    kind: str
    # JSON string prevents mutation of an already validated payload.
    payload_json: str

    def rpc_arguments(self) -> dict:
        return {"p_order_id": self.order_id, "p_source": "zain_checker",
                "p_event_key": self.event_key, "p_kind": self.kind,
                "p_payload": json.loads(self.payload_json)}


def checker_event(order_id: str, job_id: str, kind: str, revision: int, **data) -> CheckerEvent:
    """Create a deterministic envelope for a final row, batch or Telegram receipt.

    The caller must derive terminal outcomes from committed checker records.
    'review' and 'not_found' remain distinct for the unresolved billing policy.
    """
    order = str(UUID(order_id))
    job = _text(job_id, "job ID", 120)
    version = _integer(revision, "revision", 1)
    payload = {"schema_version": 1, "job_id": job, "revision": version}
    if kind == "check_outcome":
        if set(data) != {"row_number", "check_type", "outcome"}:
            raise ValueError("Row outcome requires row number, check type and outcome only")
        row = _integer(data["row_number"], "row number", 1)
        if data["check_type"] not in {"account", "service"}:
            raise ValueError("Unknown check type")
        if data["outcome"] not in {"succeeded", "failed", "review", "not_found"}:
            raise ValueError("Only terminal outcomes may enter commerce")
        payload.update(data)
        suffix = f"row:{row}"
    elif kind == "execution_terminal":
        expected = {"total", "succeeded", "failed", "review", "not_found", "result_sha256"}
        if set(data) != expected:
            raise ValueError("Terminal execution requires a complete outcome partition and result hash")
        total = _integer(data["total"], "total", 1)
        counts = [_integer(data[key], key) for key in ("succeeded", "failed", "review", "not_found")]
        if sum(counts) != total:
            raise ValueError("Outcome counts do not cover the entire input")
        digest = data["result_sha256"]
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("Invalid result SHA256")
        payload.update(data)
        suffix = "terminal"
    elif kind == "result_delivery":
        if set(data) != {"telegram_chat_id", "telegram_message_id", "telegram_file_id"}:
            raise ValueError("Delivery needs a confirmed Telegram document receipt")
        _integer(data["telegram_chat_id"], "customer chat ID", 1)
        _integer(data["telegram_message_id"], "message ID", 1)
        _text(data["telegram_file_id"], "file ID", 512)
        payload.update(data)
        suffix = "delivery"
    else:
        raise ValueError("Unknown event kind")
    return CheckerEvent(order, f"{order}:{suffix}:v{version}", kind,
                        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False))
