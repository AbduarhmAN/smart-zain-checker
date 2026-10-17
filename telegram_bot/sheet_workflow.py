"""Telegram sheet review: all columns, explicit mapping, and offline row validation."""
from __future__ import annotations

import secrets
import time
from domain import decimal_json as json
import logging
import os
import re
import threading
import copy
from pathlib import Path
from domain.workbook import load_fast_workbook as load_workbook
from domain.workbook import inspect_sheet_schema
from . import views
from .authorization import admin_ids

from domain.workbook_validation import FIELDS, MODES, validate_sheet

logger = logging.getLogger("TelegramSheetReview")

CUSTOMER_SHEET_LIMIT = 4000


def customer_sheet_limit(draft):
    """Check populated customer rows before pricing, including repeated numbers."""
    validation = draft['validation']
    if not draft.get('admin') and validation.get('rows',0) > CUSTOMER_SHEET_LIMIT:
        validation['ok'] = False
        message = (f"هذا الشيت يحتوي على {validation['rows']:,} عميلًا؛ الحد الأقصى {CUSTOMER_SHEET_LIMIT:,}. "
                   'قلّل عدد العملاء أو قسّمهم إلى شيتات أصغر ثم أرسل الملف مجددًا. لم تُخصم رسوم.')
        if message not in validation.setdefault('problems',[]):validation['problems'].insert(0,message)
    return validation


def file_version(path):
    stat = Path(path).stat()
    return stat.st_size, stat.st_mtime_ns


def review_panel(draft, token):
    result = draft["validation"]
    text = ("<b>📋 مراجعة الملف · ② اختيار الأعمدة</b>\n\n" + views.safe(draft["path"].name) +
            "\nالورقة: <b>" + views.safe(draft["sheet_name"]) + "</b>\n" +
            ("✓ مناسب للفحص" if result["ok"] else "⚠ يحتاج تعديل قبل إضافته") +
            "\nصفوف البيانات: " + views.count(result["rows"]))
    if result["problems"]:
        text += "\n\n" + "\n".join(views.safe(p, 900) for p in result["problems"][:3])
    text += "\n\nاختر عمود الرقم ثم المبلغ. يمكنك اختيار أي عمود، حتى بدون عنوان."
    if draft.get("advanced"):
        rows = [[views.button(f"{label}: {draft['mapping'].get(key) or 'غير محدد'}", f"map:{token}:field:{key}:0")]
                for key, label in FIELDS.items()]
    else:
        number_col = draft['mapping'].get('lookup_col') or draft['mapping'].get('service_col')
        rows = [[views.button(f"رقم الحساب أو الخدمة: {number_col or 'اختر العمود'}", f"map:{token}:field:lookup_col:0")],
                [views.button(f"المبلغ: {draft['mapping'].get('amount_col') or 'اختر العمود'}", f"map:{token}:field:amount_col:0")]]
    if draft.get("advanced"):
        is_mawarid = bool(draft["mapping"].get("mawarid_format"))
        rows += [[views.button(("✓ " if draft["mode"] == mode else "") + label, f"map:{token}:mode:{mode}")
              for mode, label in MODES.items()],
             [views.button(("✓ " if draft["amount_target"] == target else "") + label, f"map:{token}:target:{target}")
              for target, label in (("remaining", "مبلغ الشيت"), ("contract", "المبلغ الثاني"), ("smart_dual", "كلاهما"))],
             [views.button(("✓ " if is_mawarid else "") + "مخصص شركة الموارد", f"map:{token}:mawarid")],
             [views.button("صف العناوين: " + ("نعم" if draft["mapping"].get("has_headers", True) else "لا"), f"map:{token}:headers")],
              ]
    rows += [[views.button("كل الأعمدة", f"map:{token}:all:0"), *([] if draft.get("job_id") else [views.button("تغيير الورقة", f"map:{token}:sheets:0")])],
             [views.button("إعدادات أبسط" if draft.get("advanced") else "أعمدة إضافية / خيارات متقدمة", f"map:{token}:advanced")],
             [views.button("↻ إعادة التحقق", f"map:{token}:review")]]
    if result["ok"]:
        if draft.get("admin", True):
            rows.append([views.button("🚀 بدء الفحص فوراً", f"map:{token}:save_and_start", "success")])
            rows.append([views.button("✓ حفظ الأعمدة" if draft.get("job_id") else "✓ إضافة إلى الطابور فقط", f"map:{token}:save")])
        if not draft.get("job_id"):
            rows.append([views.button("💳 الرسوم وطرق الدفع", f"map:{token}:pay")])
    rows.append([views.button("‹ ملفاتي للمراجعة", "drafts:0"), views.button("‹ الرئيسية", "home")])
    return views.Panel(text, rows)


def columns_panel(draft, token, field=None, page=0):
    columns = draft["columns"]
    page = max(0, min(page, max(0, (len(columns)-1)//8)))
    label = "رقم الحساب أو الخدمة" if field == "lookup_col" and not draft.get("advanced") else (FIELDS[field] if field else "كل الأعمدة")
    text = f"<b>📑 {views.safe(label)}</b>\n{len(columns)} عمود · صفحة {page+1} من {max(1,(len(columns)+7)//8)}\n"
    rows = []
    for col in columns[page*8:(page+1)*8]:
        letter = col["col_letter"]
        title = f"{letter} · {col['header'] or 'بدون عنوان'}"
        text += "\n" + views.safe(title, 100)
        if field:
            rows.append([views.button(title[:65], f"map:{token}:set:{field}:{letter}")])
    paging = []
    prefix = f"map:{token}:field:{field}" if field else f"map:{token}:all"
    if page:
        paging.append(views.button("‹ السابق", f"{prefix}:{page-1}"))
    if (page+1)*8 < len(columns):
        paging.append(views.button("التالي ›", f"{prefix}:{page+1}"))
    if paging:
        rows.append(paging)
    if field:
        rows.append([views.button("بدون عمود / إلغاء الاختيار", f"map:{token}:set:{field}:-")])
    rows.append([views.button("‹ مراجعة الشيت", f"map:{token}:review")])
    return views.Panel(text, rows)


class SheetWorkflowMixin:
    def _restore_sheet_drafts(self):
        """Recover unchanged local reviews using a wall-clock expiry across restarts."""
        self._draft_storage_lock = threading.RLock()
        self._draft_store_written = False
        self._draft_persist_generation = 0
        self._draft_written_generation = 0
        self.draft_storage_error = False
        store = self.project_root / ".zain-telegram-drafts.json"
        if not store.is_file():
            return
        try:
            if store.stat().st_size > 16 * 1024 * 1024:
                raise ValueError("Draft store too large")
            content = json.loads(store.read_text(encoding="utf-8"))
            if not isinstance(content, dict) or content.get("version") != 1 or not isinstance(content.get("drafts"), dict):
                raise ValueError("Invalid draft store")
            recovered = {}
            now, monotonic = time.time(), time.monotonic()
            for token, snapshot in list(content["drafts"].items())[:128]:
                try:
                    if not re.fullmatch(r"[0-9a-f]{12}", token) or not isinstance(snapshot, dict):
                        continue
                    remaining = float(snapshot["expires_at"]) - now
                    if snapshot.get('commerce_request_key'):
                        remaining = 86400
                    if not 0 < remaining <= 86400:
                        continue
                    path = Path(snapshot["path"]).resolve()
                    commerce_input = None
                    if snapshot.get('commerce_request_key'):
                        from .commerce.bridge import file_sha256
                        digest = snapshot['commerce_source']['file_sha256']
                        if not isinstance(digest,str) or not re.fullmatch(r'[a-f0-9]{64}',digest):
                            continue
                        commerce_input = Path(snapshot['commerce_input']).resolve()
                        expected = (self.project_root/'.commerce'/'inputs'/(digest+'.xlsx')).resolve()
                        if commerce_input != expected or file_sha256(commerce_input) != digest:
                            continue
                    if path.parent != self.project_root.resolve() or (not commerce_input and not path.is_file()):
                        continue
                    version = tuple(snapshot["version"])
                    if not commerce_input and version != file_version(path):
                        continue
                    if not isinstance(snapshot.get("chat"), int) or isinstance(snapshot["chat"], bool) or snapshot["chat"] <= 0:
                        continue
                    if not all(isinstance(snapshot.get(key), dict) for key in ("mapping", "metadata", "validation")):
                        continue
                    if not isinstance(snapshot.get("columns"), list) or not isinstance(snapshot.get("sheets"), list):
                        continue
                    if snapshot.get("mode") not in MODES or snapshot.get("amount_target") not in {"remaining", "contract", "smart_dual"}:
                        continue
                    draft = dict(snapshot)
                    draft.pop("expires_at", None)
                    draft.update(path=path, version=version, expires=monotonic + remaining,
                                 admin=snapshot["chat"] in admin_ids(self))
                    recovered[token] = draft
                except (ValueError, TypeError, KeyError, OSError):
                    continue
            with self._ui_lock:
                self._sheet_drafts.update(recovered)
            self._draft_store_written = bool(recovered)
        except (ValueError, TypeError, OSError):
            self.draft_storage_error = True
            logger.warning("Telegram draft recovery failed; the existing state file was retained.")

    def _persist_sheet_drafts(self):
        # Tests that use in-memory nonexistent workbooks never write a state file.
        if not hasattr(self, "_draft_storage_lock"):
            return True
        # Snapshot before taking the disk lock: payment handlers can already hold
        # the UI lock. Generations prevent an older snapshot overwriting a newer one.
        with self._ui_lock:
            copies = copy.deepcopy(self._sheet_drafts)
            self._draft_persist_generation += 1
            generation = self._draft_persist_generation
        with self._draft_storage_lock:
            try:
                if generation <= self._draft_written_generation:
                    return True
                now, monotonic = time.time(), time.monotonic()
                snapshots = {}
                for token, draft in copies.items():
                    remaining = draft["expires"] - monotonic
                    path = Path(draft["path"]).resolve()
                    if draft.get('commerce_request_key'):
                        remaining = 86400
                    if remaining <= 0 or path.parent != self.project_root.resolve() or (not draft.get('commerce_input') and not path.is_file()):
                        continue
                    snapshot = {key:value for key,value in draft.items() if key != "expires"}
                    snapshot.update(path=str(path), expires_at=now + remaining)
                    snapshots[token] = snapshot
                if not snapshots and not self._draft_store_written:
                    return True
                store = self.project_root / ".zain-telegram-drafts.json"
                temporary = store.with_name(store.name + "." + secrets.token_hex(8) + ".tmp")
                with temporary.open("x", encoding="utf-8") as stream:
                    json.dump({"version":1,"drafts":snapshots}, stream, ensure_ascii=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, store)
                self._draft_store_written = True
                self._draft_written_generation = generation
                self.draft_storage_error = False
                return True
            except (ValueError, TypeError, OSError):
                self.draft_storage_error = True
                logger.warning("Telegram draft checkpoint failed; existing files were retained.")
                return False

    def _drafts_panel(self, chat_id, page=0):
        with self._ui_lock:
            drafts = [(token,draft) for token,draft in self._sheet_drafts.items()
                      if draft["chat"] == chat_id and (draft.get('commerce_request_key') or draft["expires"] > time.monotonic())]
        page = max(0, min(page, max(0,(len(drafts)-1)//8)))
        text = "<b>ملفات تحتاج مراجعة</b>\nاختر ملفًا، راجع بياناته، ثم أضفه إلى الجولة.\n"
        rows = [[views.button(draft["path"].name[:65], f"co:review:{token}" if draft.get('commerce_request_key') else f"map:{token}:review")]
                for token,draft in drafts[page*8:(page+1)*8]]
        if not drafts:
            text += "\nلا توجد ملفات تنتظر مراجعتك."
        if hasattr(self,'_history_orders'):
            try:
                orders=self._history_orders(chat_id)
                if orders:
                    text += '\n\nطلبات الدفع المحفوظة تظهر أدناه، حتى بعد إضافتها للطابور:'
                    rows.extend([[views.button(self._history_title(record)[:100],f'oh:open:{record["token"]}')] for record in orders[:5]])
                rows.append([views.button('كل طلباتي ومدفوعاتي','oh:list:0')])
            except (OSError,ValueError,KeyError,TypeError):
                text += '\n\nتعذر تحميل سجل الطلبات الآن؛ لا تُعد الدفع.'
        if page:
            rows.append([views.button("‹ السابق", f"drafts:{page-1}")])
        if (page+1)*8 < len(drafts):
            rows.append([views.button("التالي ›", f"drafts:{page+1}")])
        rows += [[views.button("📎 إضافة ملف", "upload", "primary"), views.button("‹ الرئيسية", "home")]]
        auth_set = admin_ids(self)
        if chat_id in auth_set:
            rows.append([views.button("📁 الطابور", "queue:0")])
        return views.Panel(text, rows)

    def _load_review_sheet(self, draft, index):
        wb = load_workbook(draft["path"], read_only=True, data_only=True)
        try:
            if index < 0 or index >= len(wb.worksheets):
                raise ValueError("Unknown worksheet")
            sheet = wb.worksheets[index]
            has_headers = True
            if draft.get("job_id"):
                saved_job = self.queue_service.get_job(draft["job_id"])
                if saved_job:
                    has_headers = saved_job.column_mapping.get("has_headers", True)
            schema = inspect_sheet_schema(sheet, has_headers=has_headers, count_rows=False)
            mapping = {key: schema["letters"].get(key) for key in FIELDS}
            mapping["has_headers"] = has_headers
            mode = "smart_hybrid"
            target = "remaining"
            if draft.get("job_id"):
                job = self.queue_service.get_job(draft["job_id"])
                if not job or job.status != "pending" or job.completed:
                    raise ValueError("Job is no longer editable")
                mapping.update({key: job.column_mapping[key] for key in FIELDS if key in job.column_mapping})
                mapping["mawarid_format"] = bool(job.column_mapping.get("mawarid_format", False))
                mode, target = job.mode, job.amount_target
            draft.update(sheet_index=index, sheet_name=sheet.title, sheets=[s.title for s in wb.worksheets],
                         columns=schema["columns"], mapping=mapping, mode=mode, amount_target=target)
            # Preserve non-interactive metadata columns needed by the existing parser.
            draft["metadata"] = {k:v for k,v in schema["letters"].items() if k not in FIELDS}
            draft["validation"] = validate_sheet(sheet, mapping, mode, target)
            customer_sheet_limit(draft)
        finally:
            wb.close()

    def _open_sheet_review(self, path, chat_id, message_id=None, job=None, show=True, *, telegram_document=None):
        token = secrets.token_hex(6)
        auth_set = getattr(self, "authorized_ids", {self.authorized_id})
        is_admin_mode = (chat_id in auth_set and getattr(self, "_get_user_mode", lambda c: "customer")(chat_id) == "admin")
        draft = {"path":Path(path), "chat":chat_id, "expires":time.monotonic()+1800,
                 "version":file_version(path), "job_id":job.id if job else None,
                 "admin":is_admin_mode}
        if telegram_document is not None:
            file_id=telegram_document.get('file_id')
            unique_id=telegram_document.get('file_unique_id')
            if not isinstance(file_id,str) or not 1<=len(file_id)<=512:raise ValueError('Invalid original Telegram document')
            draft['original_telegram_file_id']=file_id
            if isinstance(unique_id,str) and 1<=len(unique_id)<=512:draft['original_telegram_file_unique_id']=unique_id
            draft['original_filename']=Path(path).name
        self._load_review_sheet(draft, job.sheet_index if job else 0)
        with self._ui_lock:
            self._sheet_drafts = {k:v for k,v in self._sheet_drafts.items()
                                  if v.get('commerce_request_key') or v["expires"] > time.monotonic()}
            if len(self._sheet_drafts) >= 128:
                replaceable = next((key for key,value in self._sheet_drafts.items() if not value.get("invoice_id")), None)
                if replaceable is None:
                    raise ValueError("Review capacity reached; complete existing reviews first")
                self._sheet_drafts.pop(replaceable)
            self._sheet_drafts[token] = draft
        self._persist_sheet_drafts()
        panel = review_panel(draft, token)
        if show:
            self._show_panel(chat_id, panel, message_id)
        return panel

    def _sheet_callback(self, command, chat_id, message_id):
        parts = command.split(":")
        token = parts[1] if len(parts) > 1 else ""
        with self._ui_lock:
            draft = self._sheet_drafts.get(token)
        if not draft or draft["chat"] != chat_id or (not draft.get('commerce_request_key') and draft["expires"] <= time.monotonic()):
            self._show_panel(chat_id, views.notice("مراجعة غير متاحة", "أرسل الملف مجددًا أو افتح أعمدة الشيت من الطابور."), message_id)
            return
        draft['admin']=chat_id in admin_ids(self) and getattr(self,'_get_user_mode',lambda c:'customer')(chat_id)=='admin'
        if draft.get('commerce_request_key'):
            if len(parts)>2 and parts[2]=='pay':
                self._commerce_start_checkout(token,chat_id,message_id)
            else:
                self._show_panel(chat_id,views.notice('الاختيار محفوظ للطلب',
                    'أصبح الملف والأعمدة مرتبطين بطلب مالي. افتح الرسوم لمتابعته، أو أرسل ملفًا جديدًا لاختيار أعمدة أخرى.'),message_id)
            return
        try:
            if draft["version"] != file_version(draft["path"]):
                raise ValueError("Workbook changed")
            action = parts[2]
            if action == "field" and len(parts) == 5 and parts[3] in FIELDS:
                panel = columns_panel(draft, token, parts[3], int(parts[4]))
            elif action == "all" and len(parts) == 4:
                panel = columns_panel(draft, token, page=int(parts[3]))
            elif action == "sheets" and len(parts) == 4:
                page = max(0, min(int(parts[3]), (len(draft["sheets"])-1)//8))
                rows = [[views.button(name[:60], f"map:{token}:sheet:{index}")]
                        for index,name in enumerate(draft["sheets"]) if page*8 <= index < (page+1)*8]
                if page:
                    rows.append([views.button("‹ السابق", f"map:{token}:sheets:{page-1}")])
                if (page+1)*8 < len(draft["sheets"]):
                    rows.append([views.button("التالي ›", f"map:{token}:sheets:{page+1}")])
                rows.append([views.button("‹ مراجعة الشيت", f"map:{token}:review")])
                panel = views.Panel("<b>اختر ورقة العمل</b>", rows)
            elif action == "sheet" and len(parts) == 4 and not draft.get("job_id"):
                self._load_review_sheet(draft, int(parts[3]))
                panel = review_panel(draft, token)
            elif action == "pay":
                customer_sheet_limit(draft)
                if not draft['validation']['ok']:raise ValueError('Customer sheet exceeds the payable limit')
                if hasattr(self,'_history_existing_paid_input') and self._history_existing_paid_input(token,draft,chat_id,message_id):return
                if getattr(self,'project_root',None) is not None and hasattr(self,'_history_capture_input') and draft['path'].is_file():self._history_capture_input(draft)
                if (getattr(self,'_commerce_checkout_enabled',lambda user:False)(chat_id)
                        and not draft.get('invoice_id') and not draft.get('invoice') and not draft.get('promo_applied')
                        and not self._commerce_legacy_payment(token)):
                    self._commerce_start_checkout(token,chat_id,message_id)
                    return
                from .crypto_pay import CRYPTO_PAY, calculate_egp_to_usdt,ExchangeRateUnavailable
                from .payment_manager import PAYMENT_MGR
                if draft.get("job_id") or not draft["validation"]["ok"]:
                    raise ValueError("Review the sheet before payment")
                rows_count = draft["validation"].get("rows", 0)
                inv = draft.get("invoice")
                invoice_created = not bool(inv)
                if inv:
                    egp_val, usdt_val, live_rate = draft["egp_amount"], draft["usdt_amount"], draft["live_rate"]
                else:
                    egp_val = float(draft.get("egp_amount", PAYMENT_MGR.get_config().get("sheet_price_egp", 30.0)))
                    if draft.get("promo_applied") and draft.get("usdt_amount") is not None and draft.get("live_rate"):
                        usdt_val, live_rate = draft["usdt_amount"], draft["live_rate"]
                    else:
                        try:
                            usdt_val, live_rate = calculate_egp_to_usdt(egp_val)
                        except ExchangeRateUnavailable:
                            self._show_panel(chat_id,views.Panel('<b>سعر الصرف غير متاح الآن</b>\n\n'
                                'بيانات الملف واختيار الأعمدة محفوظة. تعذر تسعير فاتورة جديدة، ولم ننشئ فاتورة أو نخصم مبلغًا.',
                                [[views.button('إعادة محاولة الدفع',f'map:{token}:pay')],
                                 [views.button('المحفظة','wallet')]]),message_id)
                            self._persist_sheet_drafts()
                            return
                op_id = draft.get("operation_id")
                if not op_id:
                    op_id = PAYMENT_MGR.generate_operation_id()
                    draft["operation_id"] = op_id
                if not inv:
                    inv = CRYPTO_PAY.create_invoice(
                        amount=usdt_val,
                        asset="USDT",
                        description=f"فحص شيت: {draft['sheet_name']} ({rows_count} صف) - {egp_val:.2f} ج.م",
                        payload=token,
                    )
                    if not inv or not inv.get("invoice_id") or not (inv.get("pay_url") or inv.get("bot_invoice_url")):
                        self._show_panel(chat_id, views.notice("تعذر إنشاء فاتورة الدفع", "لم يؤكد مزود الدفع إنشاء الفاتورة. حاول لاحقًا؛ لم نضف الملف إلى الطابور."), message_id)
                        return
                    draft["invoice"] = inv
                pay_url = inv.get("pay_url") or inv.get("bot_invoice_url") or ""
                inv_id = inv.get("invoice_id", 0) if inv else 0
                draft["invoice_id"] = inv_id
                draft["egp_amount"] = egp_val
                draft["usdt_amount"] = usdt_val
                draft["live_rate"] = live_rate
                draft["chat_id"] = chat_id
                # Keep an issued quote recoverable for one day; payment handlers still
                # verify current file/version and the actual provider receipt.
                draft["expires"] = max(draft["expires"], time.monotonic() + 86400)
                if invoice_created:
                    PAYMENT_MGR.record_payment_draft(token, chat_id, draft, kind="invoice")
                self._persist_sheet_drafts()
                wallet_bal = PAYMENT_MGR.get_wallet_balance(chat_id)
                account_id = PAYMENT_MGR.get_user_account_id(chat_id)
                panel = views.payment_invoice_panel(
                    sheet_name=draft["sheet_name"],
                    rows_count=rows_count,
                    amount_usdt=usdt_val,
                    pay_url=pay_url,
                    invoice_id=inv_id,
                    token=token,
                    live_rate=live_rate,
                    wallet_balance=wallet_bal,
                    account_id=account_id,
                    operation_id=op_id,
                    amount_egp=egp_val,
                )
            else:
                auth_set = getattr(self, "authorized_ids", {self.authorized_id})
                is_admin_mode = (chat_id in auth_set and getattr(self, "_get_user_mode", lambda c: "customer")(chat_id) == "admin") or (chat_id in auth_set)
                if action in ("save", "save_and_start") and not is_admin_mode:
                    self._show_panel(chat_id, views.notice("أكمل الدفع أولًا", "بعد اختيار الأعمدة والتحقق من البيانات، افتح الرسوم وطرق الدفع. ستُضاف الورقة بعد تأكيد الدفع."), message_id)
                    return
                previous_target = draft["amount_target"]
                if action == "advanced":
                    draft["advanced"] = not draft.get("advanced", False)
                    if not draft["advanced"]:
                        draft["amount_target"] = "remaining"
                elif action == "mawarid":
                    draft["mapping"]["mawarid_format"] = not draft["mapping"].get("mawarid_format", False)
                elif action == "headers":
                    draft["mapping"]["has_headers"] = not draft["mapping"].get("has_headers", True)
                elif action == "set":
                    field, letter = parts[3:]
                    if field not in FIELDS or (letter != "-" and letter not in {c["col_letter"] for c in draft["columns"]}):
                        raise ValueError("Unknown column")
                    draft["mapping"][field] = None if letter == "-" else letter
                    if field == "lookup_col" and not draft.get("advanced"):
                        draft["mapping"]["service_col"] = draft["mapping"][field]
                elif action == "mode":
                    if parts[3] not in MODES:
                        raise ValueError("Unknown mode")
                    draft["mode"] = parts[3]
                elif action == "target":
                    if parts[3] not in {"remaining", "contract", "smart_dual"}:
                        raise ValueError("Unknown target")
                    draft["amount_target"] = parts[3]
                elif action == "review":
                    pass
                elif action in ("save", "save_and_start"):
                    pass
                else:
                    raise ValueError(f"Unknown action: {action}")

                if action not in {"advanced", "mawarid"} or previous_target != draft["amount_target"]:
                    wb = load_workbook(draft["path"], read_only=True, data_only=True)
                    try:
                        sheet = wb.worksheets[draft["sheet_index"]]
                        if action == "headers":
                            schema = inspect_sheet_schema(sheet, has_headers=draft["mapping"]["has_headers"], count_rows=False)
                            draft["columns"] = schema["columns"]
                        draft["validation"] = validate_sheet(sheet, draft["mapping"], draft["mode"], draft["amount_target"])
                        customer_sheet_limit(draft)
                    finally:
                        wb.close()
                if draft["version"] != file_version(draft["path"]):
                    raise ValueError("Workbook changed during review")
                if action in ("save", "save_and_start") and draft["validation"]["ok"]:
                    with self.orchestrator.lock:
                        if draft.get("job_id"):
                            saved = self.queue_service.update_pending_mapping(draft["job_id"],
                                column_mapping={**draft["metadata"], **draft["mapping"], "telegram_chat_id": chat_id}, mode=draft["mode"],
                                amount_target=draft["amount_target"], total_records=draft["validation"]["rows"])
                            if not saved:
                                raise ValueError("Job is no longer waiting")
                            target_job_id = draft["job_id"]
                        else:
                            job = self.queue_service.add_job(file_path=draft["path"], sheet_index=draft["sheet_index"],
                                sheet_name=draft["sheet_name"], mode=draft["mode"], amount_target=draft["amount_target"],
                                column_mapping={**draft["metadata"], **draft["mapping"], "telegram_chat_id": chat_id}, total_records=draft["validation"]["rows"])
                            target_job_id = job.id
                    with self._ui_lock:
                        self._sheet_drafts.pop(token, None)

                    started_now = False
                    if action == "save_and_start" or not self.orchestrator.is_running:
                        try:
                            started_now = self.orchestrator.start_queue(target_job_id)
                        except Exception as start_exc:
                            logger.warning("Auto-starting queue for job %s failed: %s", target_job_id, start_exc)

                    if started_now or self.orchestrator.is_running:
                        panel = views.Panel(
                            f"<b>🚀 بدأ الفحص فوراً بنجاح!</b>\n<b>{views.safe(draft['sheet_name'])}</b>"
                            f"\n📊 صفوف البيانات: {views.count(draft['validation']['rows'])}\n"
                            f"⚡ العمال يعملون بالتوازي الآن. ستصلك تحديثات التقدم والنتائج فور الاكتمال.",
                            [[views.button("📊 متابعة حالة الفحص", "status", "primary")],
                             [views.button("📁 عرض الطابور", "queue:0"), views.button("⏸ إيقاف مؤقت", "pause")],
                             views.nav()]
                        )
                    else:
                        panel = views.Panel(
                            f"<b>✓ حُفظت الأعمدة وأُضيف للطابور</b>\n<b>{views.safe(draft['sheet_name'])}</b>\nالشيت في الطابور وجاهز للبدء.",
                            [[views.button("▶ بدء الفحص الآن", "start_queue", "success")],
                             [views.button("📎 إضافة ملف آخر", "upload"), views.button("ملفات تحتاج مراجعة", "drafts:0")],
                             [views.button("📁 عرض الطابور", "queue:0")], views.nav()]
                        )
                else:
                    panel = review_panel(draft, token)
        except Exception as exc:
            logger.warning('Sheet review action failed: action=%s kind=%s',parts[2] if len(parts)>2 else 'unknown',type(exc).__name__)
            panel = views.notice("تعذر إكمال المراجعة", "قد يكون الملف أو حالة الشيت تغيرت. افتح مراجعة جديدة من الطابور أو أرسل الملف مجددًا.")
        self._persist_sheet_drafts()
        self._show_panel(chat_id, panel, message_id)
