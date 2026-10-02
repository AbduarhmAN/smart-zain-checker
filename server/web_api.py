"""Web REST API Handler for Smart Zain Checker Dashboard.
Serves static assets, templates, and provides JSON endpoints.
"""
from __future__ import annotations

import json
import logging
import mimetypes
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from domain.workbook import inspect_sheet_schema
from manager.orchestrator import Orchestrator
from manager.queue import QueueService

logger = logging.getLogger("WebAPI")


class WebApiHandler(BaseHTTPRequestHandler):
    orchestrator: Orchestrator
    queue_service: QueueService
    web_ui_dir: Path
    project_root: Path

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress noisy standard HTTP access logs
        pass

    def send_json(self, status_code: int, data: dict[str, Any]) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    def send_file_response(self, file_path: Path) -> None:
        if not file_path.exists() or not file_path.is_file():
            self.send_error(404, "File Not Found")
            return
        mime_type, _ = mimetypes.guess_type(str(file_path))
        mime_type = mime_type or "application/octet-stream"
        raw = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{mime_type}; charset=utf-8" if "text" in mime_type or "javascript" in mime_type else mime_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        # Static assets and template routing
        if path in ("/", "/index.html"):
            index_path = self.web_ui_dir / "index.html"
            self.send_file_response(index_path)
            return

        if path.startswith("/static/"):
            rel_path = path[len("/static/"):]
            target = self.web_ui_dir / "static" / rel_path
            self.send_file_response(target)
            return

        # API Endpoints
        if path == "/api/session-info":
            self.handle_session_info()
        elif path == "/api/workbook-sheets":
            self.handle_workbook_sheets(parsed)
        elif path == "/api/live-status":
            self.handle_live_status()
        elif path == "/api/service-verification-image":
            from urllib.parse import parse_qs
            from workers.stealth_service_engine import verification_image
            challenge_id = parse_qs(parsed.query).get("id", [""])[0]
            raw = verification_image(challenge_id)
            if raw is None:
                self.send_json(404, {"status": "error", "message": "انتهت صلاحية التحدي"})
            else:
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        elif path == "/api/queue":
            self.handle_get_queue()
        elif path == "/api/telegram/status":
            self.handle_telegram_status()
        elif path == "/api/check-sheet-status":
            self.handle_check_sheet_status(parsed)
        elif path == "/api/download-results":
            from urllib.parse import parse_qs, quote
            query_params = parse_qs(parsed.query)
            job_id = query_params.get("job_id", [None])[0]
            requested_file = query_params.get("file", [None])[0]

            res_file = None
            download_name = "نتائج فحص زين.xlsx"

            if job_id:
                jobs = self.queue_service.get_jobs()
                for j in jobs:
                    if j.get("id") == job_id:
                        rf_name = j.get("result_file")
                        if rf_name:
                            candidate = Path(self.project_root) / rf_name
                            if candidate.exists():
                                res_file = candidate
                                download_name = rf_name
                        break
            elif requested_file:
                candidate = Path(self.project_root) / requested_file
                if candidate.exists():
                    res_file = candidate
                    download_name = requested_file

            if not res_file or not res_file.exists():
                res_file = Path(self.project_root).resolve() / "نتائج فحص زين.xlsx"
                download_name = "نتائج فحص زين.xlsx"

            try:
                if hasattr(self.orchestrator, "all_completed_records") and self.orchestrator.all_completed_records:
                    from zain_checker.executive_reporter import export_executive_workbook
                    export_executive_workbook(list(self.orchestrator.all_completed_records.values()), res_file)
            except Exception as e:
                logger.warning(f"On-demand export warning: {e}")

            if res_file.exists():
                raw = res_file.read_bytes()
                encoded_name = quote(download_name.encode('utf-8'))
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{encoded_name}")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            else:
                self.send_json(404, {"status": "error", "message": "ملف النتائج غير موجود حالياً"})
            return
        else:
            self.send_json(404, {"status": "error", "message": f"Endpoint {path} not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else "{}"
        try:
            payload = json.loads(body) if body.strip() else {}
        except Exception:
            payload = {}

        if path == "/api/upload-workbook":
            self.handle_upload_workbook(payload)
        elif path == "/api/start-session":
            self.handle_start_session(payload)
        elif path == "/api/pause-session":
            self.orchestrator.pause_session()
            self.send_json(200, {"status": "ok", "message": "Session paused"})
        elif path == "/api/resume-session":
            from workers.stealth_service_engine import pending_verifications
            if pending_verifications():
                self.send_json(409, {"status": "error", "message": "أكمل التحقق الظاهر قبل استئناف الفحص"})
                return
            self.orchestrator.resume_session()
            self.send_json(200, {"status": "ok", "message": "Session resumed"})
        elif path == "/api/service-verification-submit":
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc != self.headers.get("Host"):
                self.send_json(403, {"status": "error", "message": "مصدر الطلب غير مسموح"})
                return
            from workers.stealth_service_engine import submit_verification
            accepted = submit_verification(str(payload.get("id", "")), payload.get("answer", ""))
            self.send_json(200 if accepted else 409, {"status": "ok" if accepted else "error",
                            "message": "تم إرسال الرمز؛ انتظر نتيجة زين" if accepted else "التحدي منتهٍ أو سبق إرسال الرمز"})
        elif path == "/api/cancel-session":
            self.orchestrator.cancel_session()
            self.send_json(200, {"status": "ok", "message": "Session cancelled"})
        elif path == "/api/inspect-sheet-columns":
            self.handle_inspect_sheet_columns(payload)
        elif path == "/api/queue/add":
            self.handle_queue_add(payload)
        elif path == "/api/queue/remove":
            self.handle_queue_remove(payload)
        elif path == "/api/queue/restart":
            self.handle_queue_restart(payload)
        elif path == "/api/queue/start":
            success = self.orchestrator.start_queue()
            if success:
                self.send_json(200, {"status": "ok", "message": "تم بدء تشغيل الطابور بنجاح"})
            else:
                self.send_json(400, {"status": "error", "message": "تعذر تشغيل الطابور: إما أن هناك جلسة نشطة بالفعل أو لا توجد شيتات قيد الانتظار."})
        elif path == "/api/telegram/test-ping":
            self.handle_telegram_ping()
        else:
            self.send_json(404, {"status": "error", "message": f"POST endpoint {path} not found"})

    def handle_session_info(self) -> None:
        # Discover all .xlsx files in the project directory
        xlsx_files = [f.name for f in self.project_root.glob("*.xlsx") if not f.name.startswith("~")]
        # Prioritize 2.xlsx and zain_data.xlsx
        xlsx_files.sort(key=lambda x: (x != "2.xlsx", x != "zain_data.xlsx", x))

        default_sheets = []
        if xlsx_files:
            try:
                from openpyxl import load_workbook
                wb = load_workbook(self.project_root / xlsx_files[0], read_only=True)
                default_sheets = wb.sheetnames
                wb.close()
            except Exception:
                pass

        self.send_json(200, {
            "status": "ok",
            "workbooks": xlsx_files,
            "default_sheets": default_sheets,
        })

    def handle_workbook_sheets(self, parsed: Any) -> None:
        from urllib.parse import parse_qs
        qs = parse_qs(parsed.query)
        wb_name = qs.get("workbook", ["2.xlsx"])[0]
        wb_path = self.project_root / Path(wb_name).name
        if not wb_path.exists():
            self.send_json(404, {"status": "error", "message": f"Workbook not found: {wb_name}"})
            return
        try:
            from openpyxl import load_workbook
            wb = load_workbook(wb_path, read_only=True)
            sheets = wb.sheetnames
            wb.close()
            self.send_json(200, {"status": "ok", "workbook": wb_name, "sheets": sheets})
        except Exception as exc:
            self.send_json(500, {"status": "error", "message": str(exc)})

    def handle_upload_workbook(self, payload: dict[str, Any]) -> None:
        filename = payload.get("filename", "").strip()
        data_base64 = payload.get("data_base64", "").strip()
        if not filename or not data_base64:
            self.send_json(400, {"status": "error", "message": "Missing filename or file data"})
            return

        import base64
        clean_name = Path(filename).name
        if not clean_name.lower().endswith((".xlsx", ".xls")):
            clean_name += ".xlsx"
        target_path = self.project_root / clean_name
        try:
            raw_bytes = base64.b64decode(data_base64)
            target_path.write_bytes(raw_bytes)
            from openpyxl import load_workbook
            wb = load_workbook(target_path, read_only=True)
            sheets = wb.sheetnames
            wb.close()
            self.send_json(200, {"status": "ok", "filename": clean_name, "sheets": sheets})
        except Exception as exc:
            self.send_json(500, {"status": "error", "message": str(exc)})

    def handle_live_status(self) -> None:
        status = self.orchestrator.get_live_status()
        self.send_json(200, status)

    def handle_get_queue(self) -> None:
        jobs = self.queue_service.get_jobs()
        self.send_json(200, {"status": "ok", "jobs": jobs})

    def handle_inspect_sheet_columns(self, payload: dict[str, Any]) -> None:
        wb_name = payload.get("workbook", "2.xlsx")
        wb_path = self.project_root / wb_name if not Path(wb_name).is_absolute() else Path(wb_name)
        sheet_idx = int(payload.get("sheet_index", 0))

        if not wb_path.exists():
            self.send_json(404, {"status": "error", "message": f"File not found: {wb_name}"})
            return

        from openpyxl import load_workbook
        wb = load_workbook(wb_path, read_only=True, data_only=True)
        try:
            ws = wb.worksheets[sheet_idx] if 0 <= sheet_idx < len(wb.worksheets) else wb.worksheets[0]
            analysis = inspect_sheet_schema(ws)
            analysis["sheet_name"] = ws.title
            analysis["total_sheets"] = len(wb.worksheets)
            self.send_json(200, {"status": "ok", "analysis": analysis})
        finally:
            wb.close()

    def handle_start_session(self, payload: dict[str, Any]) -> None:
        wb_name = payload.get("workbook", "2.xlsx")
        wb_path = self.project_root / wb_name if not Path(wb_name).is_absolute() else Path(wb_name)
        sheet_idx = int(payload.get("sheet_index", 0))
        mapping = payload.get("column_mapping") or payload.get("custom_config") or {}
        mode = payload.get("mode") or payload.get("scope") or "smart_hybrid"
        amount_target = payload.get("amount_target", "contract")
        target_url = payload.get("target_url", "https://business.zain.sa/dashboard/quick-pay")
        result_file = payload.get("result_file")
        force_restart = bool(payload.get("force_restart", False))

        # Check if queue has a saved job with full mapping for this workbook
        if not mapping or not mapping.get("customer_col") or not mapping.get("amount_col"):
            q_job = self.queue_service.get_job_by_file_and_sheet(wb_path.name, sheet_idx)
            if q_job and q_job.column_mapping:
                mapping = dict(q_job.column_mapping)
                mode = getattr(q_job, "mode", mode)
                amount_target = getattr(q_job, "amount_target", amount_target)
                if not result_file:
                    result_file = getattr(q_job, "result_file", None)

        success = self.orchestrator.start_session_from_config(
            workbook_path=wb_path,
            sheet_index=sheet_idx,
            mapping=mapping,
            mode=mode,
            amount_target=amount_target,
            target_url=target_url,
            force_restart=force_restart,
            result_file=result_file or "نتائج فحص زين.xlsx",
        )
        if success:
            self.send_json(200, {"status": "ok", "message": "Session started successfully"})
        else:
            self.send_json(400, {"status": "error", "message": "Failed to start session. Already running or invalid file."})

    def handle_queue_add(self, payload: dict[str, Any]) -> None:
        wb_name = payload.get("workbook", "2.xlsx")
        wb_path = self.project_root / wb_name if not Path(wb_name).is_absolute() else Path(wb_name)
        sheet_idx = int(payload.get("sheet_index", 0))
        sheet_name = str(payload.get("sheet_name", f"Sheet {sheet_idx+1}"))
        mode = str(payload.get("mode", "smart_hybrid"))
        amount_target = str(payload.get("amount_target", "contract"))
        col_mapping = payload.get("column_mapping") or {}
        total_records = int(payload.get("total_records", 0))
        target_url = str(payload.get("target_url", "https://business.zain.sa/dashboard/quick-pay"))

        job = self.queue_service.add_job(
            file_path=wb_path,
            sheet_index=sheet_idx,
            sheet_name=sheet_name,
            mode=mode,
            amount_target=amount_target,
            target_url=target_url,
            column_mapping=col_mapping,
            total_records=total_records,
        )
        self.send_json(200, {"status": "ok", "job": job.to_dict(), "jobs": self.queue_service.get_jobs()})

    def handle_queue_remove(self, payload: dict[str, Any]) -> None:
        job_id = str(payload.get("job_id", ""))
        removed = self.queue_service.remove_job(job_id)
        self.send_json(200, {"status": "ok", "removed": removed, "jobs": self.queue_service.get_jobs()})

    def handle_telegram_status(self) -> None:
        enabled = bool(getattr(self.orchestrator, "telegram_enabled", False))
        self.send_json(200, {"status": "ok", "enabled": enabled, "configured": enabled})

    def handle_telegram_ping(self) -> None:
        if not getattr(self.orchestrator, "telegram_enabled", False):
            self.send_json(409, {"status": "error", "message": "تكامل تلقرام غير مفعّل"})
            return
        try:
            from telegram_bot.notifier import TelegramNotifier
            notifier = TelegramNotifier()
            notifier.send_message("🔔 تجربة اتصال بوت تلقرام - منظومة فحص زين تعمل بشكل سليم ومستقل.")
            self.send_json(200, {"status": "ok", "message": "Ping sent to Telegram"})
        except Exception as exc:
            self.send_json(500, {"status": "error", "message": str(exc)})

    def handle_check_sheet_status(self, parsed: Any) -> None:
        from urllib.parse import parse_qs
        qs = parse_qs(parsed.query)
        wb_name = qs.get("workbook", [""])[0]
        try:
            sheet_idx = int(qs.get("sheet_index", [0])[0])
        except (ValueError, TypeError):
            sheet_idx = 0

        wb_path = self.project_root / wb_name if not Path(wb_name).is_absolute() else Path(wb_name)
        wb_stem = wb_path.stem

        job = self.queue_service.get_job_by_file_and_sheet(wb_path.name, sheet_idx)
        chk_path = self.project_root / f".checkpoint_{wb_stem}_{sheet_idx}.json"
        has_checkpoint = chk_path.exists()
        completed_count = 0
        matches = 0
        mismatches = 0
        errors = 0
        total_records = 0
        sheet_name = f"ورقة {sheet_idx+1}"
        is_completed = False

        if job:
            is_completed = (job.status == "completed")
            completed_count = job.completed
            matches = job.matches
            mismatches = job.mismatches
            errors = job.errors
            total_records = job.total_records
            sheet_name = job.sheet_name

        if has_checkpoint and completed_count == 0:
            try:
                raw_chk = json.loads(chk_path.read_text(encoding="utf-8"))
                completed_count = len(raw_chk.get("completed_indices", []))
                mismatches = len(raw_chk.get("mismatches", []))
                matches = max(0, completed_count - mismatches)
                if completed_count > 0:
                    is_completed = True
            except Exception:
                pass

        self.send_json(200, {
            "status": "ok",
            "is_completed": is_completed,
            "has_progress": (completed_count > 0 or has_checkpoint),
            "completed_count": completed_count,
            "total_records": total_records,
            "matches": matches,
            "mismatches": mismatches,
            "errors": errors,
            "filename": wb_path.name,
            "sheet_name": sheet_name,
            "job_id": job.id if job else None,
        })

    def handle_queue_restart(self, payload: dict[str, Any]) -> None:
        job_id = str(payload.get("job_id", "")).strip()
        auto_start = bool(payload.get("auto_start", True))
        if not job_id:
            self.send_json(400, {"status": "error", "message": "Missing job_id"})
            return

        success = self.orchestrator.restart_queue_job(job_id, auto_start=auto_start)
        if success:
            self.send_json(200, {
                "status": "ok",
                "message": "تمت إعادة تعيين الشيت بنجاح وبدء الفحص من الصفر",
                "jobs": self.queue_service.get_jobs(),
            })
        else:
            self.send_json(400, {
                "status": "error",
                "message": "تعذر إعادة تعيين الشيت. تأكد من أن الشيت ليس قيد الفحص حالياً."
            })

