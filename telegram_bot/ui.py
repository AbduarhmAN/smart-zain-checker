"""Native Telegram navigation and single-use, session-bound confirmations."""
from __future__ import annotations

import time
import secrets
import json
from . import views


class TelegramUiMixin:
    def _show_panel(self, chat_id, panel, message_id=None):
        payload = {"chat_id":chat_id, "text":panel.text, "parse_mode":"HTML",
                   "reply_markup":{"inline_keyboard":panel.rows}}
        if message_id:
            response = self._api_call("editMessageText", {**payload, "message_id":message_id})
            if response and (response.get("ok") or "message is not modified" in str(response.get("description", "")).lower()):
                return response
        return self._api_call("sendMessage", payload)

    def _dispatch_ui(self, command, chat_id, message_id=None):
        with self._ui_lock:
            self._confirmations = {k:v for k,v in self._confirmations.items() if v["chat"] != chat_id}
        with self.orchestrator.lock:
            state = self.orchestrator.get_live_status()
            session_token = self.orchestrator.session_token
        jobs = self.queue_service.get_jobs()
        state["round_pending"] = any(job.get("status") in {"pending", "active"} for job in jobs)
        if command == "home":
            panel = views.home(state, jobs)
            panel.rows.append([views.button("ملفات تحتاج مراجعة", "drafts:0")])
        elif command == "status":
            panel = views.status(state)
        elif command.startswith("queue:"):
            try:
                page = int(command.partition(":")[2])
            except ValueError:
                return False
            panel = views.queue(self.queue_service.get_jobs(), page)
        elif command == "upload":
            panel = views.upload()
        elif command == "help":
            panel = views.help_panel()
        elif command.startswith("drafts:"):
            try:
                panel = self._drafts_panel(chat_id, int(command.partition(":")[2]))
            except ValueError:
                return False
        elif command == "start_queue":
            return self._review_queue_start(chat_id, message_id)
        elif command.startswith("job:") or command.startswith("edit:") or command.startswith("remove:"):
            return self._queue_action(command, chat_id, message_id)
        elif command in {"pause", "resume", "results", "repair"}:
            problem = self._action_problem(command, state)
            if problem:
                panel = views.notice("راجع حالة الجلسة", problem)
            else:
                token = secrets.token_hex(8)
                now = time.monotonic()
                with self._ui_lock:
                    self._confirmations = {k:v for k,v in self._confirmations.items() if v["expires"] > now}
                    if len(self._confirmations) >= 32:
                        self._confirmations.pop(next(iter(self._confirmations)))
                    self._confirmations[token] = {"chat":chat_id, "action":command, "expires":now+120,
                                                  "session":session_token,
                                                  "workbook":state.get("workbook")}
                panel = views.confirmation(command, state.get("workbook"), token)
        else:
            return False
        self._show_panel(chat_id, panel, message_id)
        return True

    @staticmethod
    def _action_problem(action, state):
        if action == "results":
            kpis = state.get("kpis", {})
            if state.get("running") or state.get("round_pending") or views.number(kpis.get("completed")) < views.number(kpis.get("total")):
                return "النتائج متاحة بعد اكتمال الجولة. يمكنك متابعة التقدم من الحالة."
            return "لا توجد نتائج محفوظة للجلسة الحالية." if not views.number(state.get("kpis", {}).get("completed")) else ""
        if action == "repair":
            if state.get("verifications"):
                return "أكمل تحقق زين أولًا قبل إعادة فحص الأخطاء."
            if state.get("running") and (not state.get("paused") or views.number(state.get("kpis", {}).get("active_leases"))):
                return "أوقف الجلسة مؤقتًا وانتظر انتهاء الفحوص الجارية، ثم أعد فحص الأخطاء."
            kpis = state.get("kpis", {})
            return "لا توجد أخطاء أو حالات مراجعة في الجلسة الحالية." if not (views.number(kpis.get("errors")) + views.number(kpis.get("needs_review"))) else ""
        if not state.get("running"):
            return "لا توجد جلسة قيد التشغيل."
        if action == "resume" and state.get("verifications"):
            return "أكمل تحقق زين في المتصفح أو لوحة التحكم أولًا."
        if action == "pause" and state.get("paused"):
            return "الجلسة متوقفة مؤقتًا بالفعل."
        if action == "resume" and not state.get("paused"):
            return "الجلسة تعمل بالفعل."
        return ""

    def _handle_callback(self, callback):
        chat_id = callback.get("message", {}).get("chat", {}).get("id")
        authorized = callback.get("from", {}).get("id") == self.authorized_id and chat_id == self.authorized_id
        self._api_call("answerCallbackQuery", {"callback_query_id":callback.get("id"),
                       **({} if authorized else {"text":"هذه اللوحة للحساب المصرح له فقط.", "show_alert":True})})
        if not authorized:
            return
        message_id = callback.get("message", {}).get("message_id")
        data = str(callback.get("data", ""))
        if not data.startswith("zc:"):
            return
        command = data[3:]
        if command.startswith("map:"):
            self._sheet_callback(command, chat_id, message_id)
        elif command.startswith("confirm:"):
            self._confirm_ui_action(command.partition(":")[2], chat_id, message_id)
        elif not self._dispatch_ui(command, chat_id, message_id):
            self._show_panel(chat_id, views.notice("زر غير متاح", "افتح القائمة الرئيسية وحاول مرة أخرى."), message_id)

    def _confirm_ui_action(self, token, chat_id, message_id):
        with self._ui_lock:
            action = self._confirmations.pop(token, None)
        if not action or action["chat"] != chat_id or action["expires"] <= time.monotonic():
            self._show_panel(chat_id, views.notice("انتهت صلاحية التأكيد", "افتح الحالة واطلب الإجراء من جديد."), message_id)
            return
        if action["action"] == "remove":
            with self.orchestrator.lock:
                job = self.queue_service.get_job(action["job_id"])
                same_job = job and job.filename == action["workbook"] and job.sheet_index == action["sheet_index"]
                active = job and (job.status == "active" or (self.orchestrator.is_running and self.orchestrator.current_job_id == job.id))
                removed = bool(same_job and not active and self.queue_service.remove_job(job.id))
            self._show_panel(chat_id, views.Panel("<b>✓ أُخرج الشيت من الطابور</b>\nالملف محفوظ." if removed else
                "<b>تعذر إخراج الشيت</b>\nتغيرت حالته أو أصبح قيد المعالجة. حدّث الطابور.",
                [[views.button("📁 عرض الطابور", "queue:0")], views.nav()]), message_id)
            return
        if action["action"] == "start_queue":
            with self.orchestrator.lock:
                try:
                    prepared = self._prepare_queue_start()
                    if prepared["signature"] != action["signature"] or prepared["versions"] != action["versions"]:
                        raise ValueError("تغيرت ملفات الجولة أو إعداداتها. راجع الطابور واطلب البدء من جديد.")
                    started = self.orchestrator.start_queue()
                    if not started:
                        raise ValueError("تعذر بدء الجولة. حدّث الحالة وراجع الملفات المنتظرة.")
                    problem = ""
                except Exception:
                    problem = "تغيرت الجولة أو هناك جلسة نشطة. راجع الطابور وتحقق من الملفات ثم اطلب البدء مجددًا."
            if problem:
                self._show_panel(chat_id, views.notice("لم تبدأ الجولة", problem), message_id)
            else:
                self._dispatch_ui("status", chat_id, message_id)
            return
        with self.orchestrator.lock:
            state = self.orchestrator.get_live_status()
            state["round_pending"] = any(job.get("status") in {"pending", "active"} for job in self.queue_service.get_jobs())
            records = []
            other_reports = []
            if action["session"] != self.orchestrator.session_token or action["workbook"] != state.get("workbook"):
                problem = "تغيرت الجلسة. راجع الملف الحالي قبل تأكيد الإجراء."
            else:
                problem = self._action_problem(action["action"], state)
            if not problem and action["action"] == "repair":
                current_job_id = self.orchestrator.current_job_id
                if any(j.get("status") == "active" and j.get("id") != current_job_id for j in self.queue_service.get_jobs()):
                    problem = "هناك شيت آخر قيد المعالجة أو على وشك البدء. انتظر انتهاءه قبل إعادة الفحص."
            if not problem:
                if action["action"] == "pause":
                    self.orchestrator.pause_session()
                elif action["action"] == "resume":
                    self.orchestrator.resume_session()
                elif action["action"] == "repair":
                    repair_result = self.orchestrator.repair_errors()
                else:
                    records = [dict(record) for record in self.orchestrator.all_completed_records.values()]
                    round_ids = getattr(self.orchestrator,"queue_round_job_ids",[])
                    if isinstance(round_ids,list):
                        other_reports = [self.project_root / job["result_file"] for job in self.queue_service.get_jobs()
                                         if job.get("id") in round_ids and job.get("status") == "completed" and
                                         job.get("id") != self.orchestrator.current_job_id and job.get("result_file")]
        if problem:
            self._show_panel(chat_id, views.notice("راجع حالة الجلسة", problem), message_id)
        elif action["action"] == "results":
            self._deliver_report(chat_id, records, action["workbook"], message_id, other_reports)
        elif action["action"] == "repair":
            self._show_panel(chat_id, views.notice("إعادة فحص الأخطاء", repair_result.get("message", "راجع الحالة لمعرفة تقدم الفحص.")), message_id)
        else:
            self._dispatch_ui("status", chat_id, message_id)

    def _prepare_queue_start(self):
        from openpyxl import load_workbook
        from domain.workbook import inspect_sheet_schema
        from domain.workbook_validation import validate_sheet
        from .sheet_workflow import file_version
        state = self.orchestrator.get_live_status()
        jobs = self.queue_service.get_jobs()
        if state.get("running") or any(job.get("status") == "active" for job in jobs):
            raise ValueError("هناك فحص جارٍ. تابع الحالة أو استأنف الجلسة المتوقفة مؤقتًا.")
        pending = [job for job in jobs if job.get("status") == "pending"]
        if not pending:
            raise ValueError("أضف ملفًا مناسبًا إلى الطابور أولًا.")
        versions = []
        for job in pending:
            path = (self.project_root / job["filename"]).resolve()
            if path.parent != self.project_root.resolve():
                raise ValueError("راجع مسار ملف الجولة.")
            before = file_version(path)
            wb = load_workbook(path, read_only=True, data_only=True)
            try:
                index = job["sheet_index"]
                if not isinstance(index,int) or index < 0 or index >= len(wb.worksheets):
                    raise ValueError("ورقة العمل غير موجودة. راجع الشيت من الطابور.")
                sheet = wb.worksheets[index]
                mapping = {**inspect_sheet_schema(sheet)["letters"], **job.get("column_mapping", {})}
                result = validate_sheet(sheet, mapping, job.get("mode", "smart_hybrid"), job.get("amount_target", "remaining"))
                if not result["ok"]:
                    raise ValueError(f"{path.name}: " + " · ".join(result["problems"]))
            finally:
                wb.close()
            if before != file_version(path):
                raise ValueError("تغير الملف أثناء التحقق. أعد مراجعته.")
            versions.append((job["id"],before))
        return {"signature":json.dumps(pending,sort_keys=True,ensure_ascii=False), "versions":versions, "jobs":pending}

    def _review_queue_start(self, chat_id, message_id):
        try:
            with self.orchestrator.lock:
                prepared = self._prepare_queue_start()
            token = secrets.token_hex(8)
            with self._ui_lock:
                self._confirmations[token] = {"chat":chat_id,"action":"start_queue","expires":time.monotonic()+120,
                                             "signature":prepared["signature"],"versions":prepared["versions"]}
            panel = views.confirmation("start_queue", f"{len(prepared['jobs'])} ملف", token)
            panel = views.Panel(panel.text + "\n\n" + "\n".join(views.safe(job["filename"],100) for job in prepared["jobs"][:15]), panel.rows)
        except ValueError as exc:
            panel = views.notice("راجع الجولة قبل البدء", str(exc))
        except Exception:
            panel = views.notice("تعذر التحقق من ملفات الجولة", "راجع وجود ملفات الإكسل والأوراق المحددة ثم حاول من جديد.")
        self._show_panel(chat_id, panel, message_id)
        return True

    def _queue_action(self, command, chat_id, message_id):
        kind, _, job_id = command.partition(":")
        with self.orchestrator.lock:
            job = self.queue_service.get_job(job_id)
            if not job:
                panel = views.notice("الشيت غير موجود", "حدّث الطابور ثم اختر الشيت مجددًا.")
            elif kind == "job":
                rows = []
                if job.status == "pending" and not job.completed:
                    rows.append([views.button("📋 اختيار الأعمدة", f"edit:{job.id}")])
                if self.orchestrator.current_job_id == job.id:
                    rows.append([views.button("🛠 إصلاح أخطاء الجلسة", "repair")])
                if job.status != "active":
                    rows.append([views.button("إخراج من الطابور", f"remove:{job.id}", "danger")])
                rows.append([views.button("‹ الطابور", "queue:0")])
                panel = views.Panel(f"<b>{views.safe(job.filename)}</b>\nالورقة: {views.safe(job.sheet_name)}\n\nيمكن تعديل أعمدة الشيت المنتظر قبل بدء معالجته.", rows)
            elif kind == "edit":
                if job.status != "pending" or job.completed or (self.orchestrator.is_running and self.orchestrator.current_job_id == job.id):
                    panel = views.notice("الأعمدة غير قابلة للتعديل", "يمكن تعديل الشيت المنتظر الذي لم تبدأ معالجته فقط.")
                else:
                    try:
                        path = (self.project_root / job.filename).resolve()
                        if path.parent != self.project_root.resolve():
                            raise ValueError("Invalid workbook path")
                        panel = self._open_sheet_review(path, chat_id, message_id, job, show=False)
                    except Exception:
                        panel = views.notice("تعذر قراءة الشيت", "راجع وجود ملف الإكسل وصيغته ثم حاول مجددًا.")
            elif kind == "remove":
                if job.status == "active" or (self.orchestrator.is_running and self.orchestrator.current_job_id == job.id):
                    panel = views.notice("الشيت قيد المعالجة", "لا يمكن إخراج الشيت الجاري أثناء فحصه.")
                else:
                    token = secrets.token_hex(8)
                    with self._ui_lock:
                        self._confirmations[token] = {"chat":chat_id, "action":"remove", "expires":time.monotonic()+120,
                            "job_id":job.id, "workbook":job.filename, "sheet_index":job.sheet_index}
                    panel = views.confirmation("remove", job.filename, token)
            else:
                return False
        self._show_panel(chat_id, panel, message_id)
        return True

    def _deliver_report(self, chat_id, records, workbook, message_id, other_reports=None):
        if not records:
            self._show_panel(chat_id, views.notice("لا توجد نتائج", "لم تتوفر سجلات محفوظة لهذه الجلسة لإرسالها."), message_id)
            return
        self._show_panel(chat_id, views.notice("📥 تجهيز النتائج", "جارٍ تجهيز نسخة من نتائج الجلسة التي أكدت إرسالها…"), message_id)
        try:
            from zain_checker.executive_reporter import export_executive_workbook
            from .notifier import TelegramNotifier
            report_dir = self.project_root / "audit_outputs" / "telegram_reports"
            report_dir.mkdir(parents=True, exist_ok=True)
            report = report_dir / f"نتائج_الجلسة_{secrets.token_hex(8)}.xlsx"
            export_executive_workbook(records, report)
            reports = []
            from shutil import copy2
            for source in other_reports or []:
                source = source.resolve()
                if source.parent != self.project_root.resolve() or not source.is_file():
                    raise ValueError("Round result file missing")
                copy = report_dir / f"{source.stem}_{secrets.token_hex(8)}.xlsx"
                copy2(source, copy)
                reports.append(copy)
            reports.append(report)
            notifier = TelegramNotifier(bot_token=self.bot_token, chat_id=chat_id)
            accepted = True
            for result_file in reports:
                accepted = notifier.send_excel_document(result_file, caption="نتائج الجولة التي طلبتها من تشيك", wait=True) and accepted
            panel = views.notice("✓ أُرسلت النتائج" if accepted else "تعذر إرسال النتائج",
                                 "أكد تليجرام قبول الملف." if accepted else "لم يؤكد تليجرام قبول الملف. يمكنك المحاولة من جديد.")
        except Exception:
            panel = views.notice("تعذر تجهيز النتائج", "راجع ملف النتائج في لوحة التحكم وحاول مرة أخرى.")
        self._show_panel(chat_id, panel, message_id)
