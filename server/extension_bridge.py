"""Chrome Extension Bridge Server for Smart Zain Checker.
Listens on Port 8766 to communicate with content scripts and background service workers.
Guarantees 100% JSON responses on ALL methods (GET, POST, OPTIONS, and Errors) to eliminate '<!DOCTYPE' syntax errors.
"""
from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import parse_qs, urlparse

from manager.orchestrator import Orchestrator

logger = logging.getLogger("ExtensionBridge")


class ExtensionBridgeHandler(BaseHTTPRequestHandler):
    orchestrator: Orchestrator

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def send_cors_json(self, status_code: int, data: dict[str, Any]) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Worker-Id, X-Zain-Bridge-Token")
        self.end_headers()
        self.wfile.write(raw)

    def send_error(self, code: int, message: str = None, explain: str = None) -> None:
        """Override BaseHTTPRequestHandler.send_error to NEVER return HTML error pages!"""
        self.send_cors_json(code, {
            "status": "error",
            "code": code,
            "message": message or "An error occurred on the bridge server",
        })

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Worker-Id, X-Zain-Bridge-Token")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        worker_id = qs.get("worker", [None])[0] or self.headers.get("X-Worker-Id") or "worker_1"

        if path in ("/task", "/lease-task", "/get-task"):
            self.handle_lease_task(worker_id)
        elif path in ("/health", "/status", "/ping"):
            self.send_cors_json(200, {"status": "ready", "running": getattr(self.orchestrator, "is_running", False)})
        else:
            self.send_cors_json(404, {"status": "error", "message": f"Bridge endpoint {path} not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else "{}"
        try:
            payload = json.loads(body) if body.strip() else {}
        except Exception:
            payload = {}

        worker_id = payload.get("worker_id") or self.headers.get("X-Worker-Id") or "worker_1"

        if path in ("/task", "/lease-task", "/get-task"):
            self.handle_lease_task(worker_id)
        elif path in ("/result", "/task-result", "/report-result"):
            self.handle_task_result(payload, worker_id)
        elif path == "/handoff-ready":
            self.send_cors_json(200, {"status": "handoff_acknowledged"})
        elif path == "/cycle-refreshed":
            self.send_cors_json(200, {"status": "ok"})
        else:
            self.send_cors_json(404, {"status": "error", "message": f"Bridge POST endpoint {path} not found"})

    def handle_lease_task(self, worker_id: str) -> None:
        task = self.orchestrator.lease_next_task_for_worker(worker_id)
        if task:
            total_count = len(getattr(self.orchestrator, "customers", [])) or 1
            sequence = task.get("row_number", 1)
            # Status MUST be 'check' for the Chrome content script / service worker loop to proceed
            self.send_cors_json(200, {
                "status": "check",
                "task_id": task["task_id"],
                "row_number": task["row_number"],
                "sequence": sequence,
                "total": total_count,
                "search_number": task["search_number"],
                "contract": task["contract"],
                "record_type": task["record_type"],
                "expected_amount_sar": task.get("expected_amount_sar", 0.0),
                "expected_sar": task.get("expected_amount_sar", 0.0),
                "target_url": task.get("target_url", "https://business.zain.sa/dashboard/quick-pay"),
            })
        else:
            self.send_cors_json(200, {
                "status": "wait",
                "message": "No task available or session idle.",
                "retry_after_seconds": 2.5,
            })

    def handle_task_result(self, payload: dict[str, Any], worker_id: str) -> None:
        task_id = payload.get("task_id", "")
        # Accept amount, website_amount, live_amount, due_amount
        live_amount = (
            payload.get("live_amount")
            or payload.get("amount")
            or payload.get("website_amount")
            or payload.get("current_amount")
            or payload.get("due_amount")
        )
        status = payload.get("status", "match")
        error_msg = payload.get("error_message") or payload.get("error_details") or payload.get("reason") or ""

        self.orchestrator.record_task_outcome(
            task_id=task_id,
            live_amount_raw=live_amount,
            status=status,
            error_msg=error_msg,
            worker_id=worker_id,
        )
        total_count = len(getattr(self.orchestrator, "customers", [])) or 1
        completed_count = len(getattr(self.orchestrator, "completed_indices", []))
        self.send_cors_json(200, {
            "status": "waiting",
            "retry_after_seconds": 1,
            "checked": completed_count,
            "total": total_count,
            "message": "Outcome recorded.",
        })
