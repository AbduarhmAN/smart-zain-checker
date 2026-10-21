"""Native Telegram navigation and single-use, session-bound confirmations."""
from __future__ import annotations

import time
import secrets
from domain import decimal_json as json
from pathlib import Path
from functools import wraps
from . import views
from .authorization import admin_ids
from .subscription_policy import CHECK_CALLBACK, support_route
from .photo_panels import show_photo_panel,remember_media,is_media_message,retire_keyboard,show_photo_page
from .order_history import OrderHistoryMixin
from .commerce.customer_activity import CustomerActivityMixin


def _serialized_payment(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._ui_lock:
            if not hasattr(self,'_commerce_user_locks'):
                self._commerce_user_locks = {}
            import threading
            user_id = args[1] if len(args)>1 else kwargs.get('chat_id')
            lock = self._commerce_user_locks.setdefault(user_id,threading.RLock())
        with lock:
            return method(self, *args, **kwargs)
    return wrapped


class TelegramUiMixin(CustomerActivityMixin, OrderHistoryMixin):
    def _get_user_mode(self, chat_id: int) -> str:
        with self._ui_lock:
            if not hasattr(self, "_user_modes"):
                self._user_modes = {}
                modes_file = self.project_root / "user_modes.json"
                if modes_file.exists():
                    try:
                        self._user_modes = json.loads(modes_file.read_text(encoding="utf-8"))
                    except Exception:
                        pass
            if str(chat_id) in self._user_modes:
                return self._user_modes[str(chat_id)]
            auth_set = admin_ids(self)
            return "admin" if chat_id in auth_set else "customer"

    def _set_user_mode(self, chat_id: int, mode: str) -> None:
        with self._ui_lock:
            if not hasattr(self, "_user_modes"):
                self._user_modes = {}
            self._user_modes[str(chat_id)] = mode
            modes_file = self.project_root / "user_modes.json"
            try:
                modes_file.write_text(json.dumps(self._user_modes, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass

    def _show_panel(self, chat_id, panel, message_id=None):
        auth_set = admin_ids(self)
        is_admin_mode = (chat_id in auth_set and self._get_user_mode(chat_id) == "admin")
        rows = panel.rows if is_admin_mode else views.customer_rows(panel.rows)
        if not is_admin_mode and any(item.get('callback_data')=='zc:pc:home:0' for row in rows for item in row):
            if not getattr(self,'_partner_sheet_access_allowed',lambda _:True)(chat_id):
                rows=[[item for item in row if item.get('callback_data')!='zc:pc:home:0'] for row in rows]
                rows=[row for row in rows if row]
        if is_admin_mode and not getattr(self,'_role_owner',lambda _:False)(chat_id):
            rows=[[item for item in row if item.get('callback_data')!='zc:admin_roles' and
                not str(item.get('callback_data','')).startswith('zc:am:')] for row in rows]
            rows=[row for row in rows if row]
        rows = views.inline_rows(rows)
        if getattr(panel,'photo',None):
            handled,response=show_photo_panel(self,chat_id,views.copyable_message(panel.text),rows,panel.photo,message_id)
            if handled:return response
        payload = {"chat_id":chat_id, "text":views.copyable_message(panel.text), "parse_mode":"HTML",
                   "reply_markup":{"inline_keyboard":rows}}
        if message_id and is_media_message(self,chat_id,message_id):
            response=self._api_call('sendMessage',payload)
            if isinstance(response,dict) and response.get('ok'):retire_keyboard(self,chat_id,message_id)
            return response
        if message_id:
            response = self._api_call("editMessageText", {**payload, "message_id":message_id})
            if response and (response.get("ok") or "message is not modified" in str(response.get("description", "")).lower()):
                return response
        return self._api_call("sendMessage", payload)

    def _dispatch_ui(self, command, chat_id, message_id=None):
        with self._ui_lock:
            self._confirmations = {k:v for k,v in self._confirmations.items() if v["chat"] != chat_id}
            self._waiting_user_inputs.pop(chat_id, None)
        support=getattr(self,'_support_service',None)
        if support is not None:support.store.end_context(chat_id)
        if command == 'status' and (chat_id not in admin_ids(self) or self._get_user_mode(chat_id) == 'customer'):
            # A customer's status always means their own orders, including partners.
            self._history_callback('oh:list:0',chat_id,message_id)
            return True
        if command.startswith('oh:'):
            self._history_callback(command,chat_id,message_id)
            return True
        if command.startswith('cr:'):
            self._customer_activity(command,chat_id,message_id)
            return True
        if command.startswith('menu:'):
            section=command.partition(':')[2]
            if chat_id not in admin_ids(self):
                self._show_panel(chat_id,views.notice('غير مصرح','هذه الأقسام متاحة للإدارة فقط.'),message_id)
                return True
            if section == 'operations':
                state = self.orchestrator.get_live_status()
                jobs = (self.queue_service.cached_jobs() if getattr(self.orchestrator,'remote',False) is True
                        else self.queue_service.get_jobs())
                self._show_panel(chat_id,views.operations(state,jobs),message_id)
                return True
            if section not in {'money','partners','offers','settings','customers'}:return False
            panel=views.admin_menu(section)
            if section=='partners' and self._role_owner(chat_id):
                panel=views.Panel(panel.text,panel.rows[:-1]+[[views.button('🎟 موافقات أكواد الشركاء','pa:list:0')]]+panel.rows[-1:])
            self._show_panel(chat_id,panel,message_id)
            return True
        if command=='admin_roles':
            self._admin_roles_callback('am:list:0',chat_id,message_id,{})
            return True
        if command=='partner_sheet_settings':
            self._partner_sheet_admin('ps:settings:0',chat_id,message_id,{})
            return True
        if command=='prices':
            self._pricing_callback('pr:home:0',chat_id,message_id)
            return True
        if command == 'referral' or command.startswith('cp:'):
            self._commerce_callback(command, chat_id, message_id)
            return True
        if command=='payment_reviews':
            self._payment_review_callback('cm:list:0',chat_id,message_id,{})
            return True
        if command=='withdrawals':
            self._withdrawal_admin_callback('wa:list:0',chat_id,message_id,{})
            return True
        if command=='campaigns':
            self._campaign_admin_callback('oa:list:0',chat_id,message_id,{})
            return True
        if command=='cash_codes':
            self._cash_code_callback('cg:list:0',chat_id,message_id,{})
            return True
        if command=='archive':
            self._archive_prompt(chat_id,message_id)
            return True
        if command=='support':
            self._commerce_command('/support',chat_id,chat_id in self.current_authorized_ids,message_id=message_id)
            return True
        if command=='support_inbox':
            self._support_callback('sp:inbox:0',chat_id,message_id)
            return True
        self._commerce_cancel_pending(chat_id)
        if command=='wallet':
            self._show_wallet_panel(chat_id,message_id)
            return True

        auth_set = admin_ids(self)
        is_owner = (chat_id in auth_set)
        mode = self._get_user_mode(chat_id)

        if is_owner and mode == 'admin' and command in {'help', 'upload'}:
            self._show_panel(chat_id, views.help_panel() if command == 'help' else views.upload(), message_id)
            return True
        if is_owner and mode == 'admin' and command == 'home' and getattr(self.orchestrator, 'remote', False) is True:
            # Administrator navigation must not wait for a database/engine read.
            # Wallet details remain available through the explicit wallet action.
            state = self.orchestrator.get_live_status()
            self._show_panel(chat_id, views.home(state, self.queue_service.cached_jobs(),
                is_admin=True, mode='admin'), message_id)
            return True

        if command.startswith("switch_mode:"):
            target_mode = command.partition(":")[2]
            if not is_owner:
                self._show_panel(chat_id, views.notice("غير مصرح", "هذا الإجراء خاص بمدير البوت فقط."), message_id)
                return True
            self._set_user_mode(chat_id, target_mode)
            from .payment_manager import PAYMENT_MGR
            acc_id = getattr(self,"_wallet_account_label",PAYMENT_MGR.get_user_account_id)(chat_id)
            bal = getattr(self,"_wallet_display_balance",PAYMENT_MGR.get_wallet_balance)(chat_id)
            with self.orchestrator.lock:
                state = self.orchestrator.get_live_status()
            jobs = self.queue_service.get_jobs()
            panel = views.home(state, jobs, account_id=acc_id, is_admin=True, mode=target_mode, wallet_balance=bal)
            self._show_panel(chat_id, panel, message_id)
            mode_name_ar = "المدير (Admin)" if target_mode == "admin" else "الزبون (Customer)"
            self._reply(chat_id, f"🔄 <b>تم التبديل بنجاح إلى وضع {mode_name_ar}.</b>\nتم تحديث لوحة التحكم وقائمة الأزرار السريعة.")
            return True

        if not is_owner or mode == "customer":
            if command == "home":
                from .payment_manager import PAYMENT_MGR
                acc_id = getattr(self,"_wallet_account_label",PAYMENT_MGR.get_user_account_id)(chat_id)
                bal = getattr(self,"_wallet_display_balance",PAYMENT_MGR.get_wallet_balance)(chat_id)
                state, jobs = {}, []
                panel = views.home(state, jobs, account_id=acc_id, is_admin=is_owner, mode="customer", wallet_balance=bal)
                if chat_id in getattr(self,'_known_partner_ids',set()) and self._has_active_partner_role(chat_id):
                    panel=views.Panel(panel.text,panel.rows+[views.partner_home_rows()])
            elif command == "upload":
                panel = views.upload(admin=False)
            elif command == "help":
                panel = views.help_panel(admin=False)
            elif command.startswith("drafts:"):
                try:
                    panel = self._drafts_panel(chat_id, int(command.partition(":")[2]))
                except ValueError:
                    return False
            else:
                return False
            self._show_panel(chat_id, panel, message_id)
            return True

        with self.orchestrator.lock:
            state = self.orchestrator.get_live_status()
            session_token = self.orchestrator.session_token
        if state.get('engine_online') is False:
            if command == 'home':
                panel = views.home(state, [], is_admin=True, mode='admin')
            elif command == 'upload':panel = views.upload()
            elif command == 'help':panel = views.help_panel()
            elif command.startswith('drafts:'):panel = self._drafts_panel(chat_id, int(command.partition(':')[2]))
            else:
                panel = views.notice('محرك الفحص غير متصل مؤقتًا',
                    'لا نستطيع تأكيد حالة التنفيذ الآن. الطلبات والأموال محفوظة؛ لا تُعد الدفع. حدّث الحالة لاحقًا.')
            self._show_panel(chat_id, panel, message_id)
            return True
        jobs = (self.queue_service.cached_jobs() if command == 'status'
                and getattr(self.orchestrator, 'remote', False) is True else self.queue_service.get_jobs())
        state["round_pending"] = any(job.get("status") in {"pending", "active"} for job in jobs)
        if command == "home":
            from .payment_manager import PAYMENT_MGR
            acc_id = getattr(self,"_wallet_account_label",PAYMENT_MGR.get_user_account_id)(chat_id)
            bal = getattr(self,"_wallet_display_balance",PAYMENT_MGR.get_wallet_balance)(chat_id)
            panel = views.home(state, jobs, account_id=acc_id, is_admin=True, mode="admin", wallet_balance=bal)
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
        elif command == "wallet":
            self._show_wallet_panel(chat_id,message_id)
            return True
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
            if state.get("finalizing") or state.get("result_save_failed") or state.get("result_ready") is False:
                return "لم يكتمل حفظ النتائج بعد. راجع الحالة وحاول بعد تأكيد الحفظ."
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
        if action == "resume" and not state.get("paused") and not (state.get('scheduling') or {}).get('paused_files'):
            return "الجلسة تعمل بالفعل."
        return ""

    def _handle_callback(self, callback):
        if self._maintenance_gate({'callback_query':callback}):return
        from_user = callback.get("from", {})
        user_id = from_user.get("id")
        chat_id = callback.get("message", {}).get("chat", {}).get("id", user_id)
        auth_set = admin_ids(self)
        is_admin = (user_id in auth_set and chat_id in auth_set)
        data = str(callback.get("data", ""))
        is_pay_or_sheet = (
            data.startswith("zc:map:")
            or data.startswith("zc:pay:")
            or data.startswith("zc:paycheck:")
            or data.startswith("zc:co:")
            or data.startswith('zc:cw:')
            or data.startswith('zc:vc:')
            or data.startswith('zc:oh:')
            or data.startswith('zc:bn:')
            or data.startswith('zc:pf:')
            or data.startswith('zc:pc:')
            or data.startswith('zc:po:')
            or data.startswith('zc:of:')
            or data.startswith('zc:cc:')
            or data.startswith('zc:ar:result:')
            or data.startswith('zc:sp:history:')
            or data.startswith('zc:sp:compose:')
            or data.startswith('zc:panel:')
            or data == 'zc:archive'
            or data == 'zc:support'
            or data == CHECK_CALLBACK
            or data.startswith("zc:wallet")
            or data.startswith("zc:switch_mode:")
            or data == "zc:home"
            or data == "zc:upload"
            or data == "zc:help"
            or data == "zc:status"
            or data == "zc:referral"
            or data.startswith("zc:drafts:")
        )
        private_sender = (type(user_id) is int and user_id > 0 and user_id == chat_id
            and callback.get('message',{}).get('chat',{}).get('type','private')=='private'
            and from_user.get('is_bot') is not True)
        if not private_sender or (not is_admin and not is_pay_or_sheet):
            self._api_call("answerCallbackQuery", {
                "callback_query_id": callback.get("id"),
                "text": "هذه اللوحة للمشرف فقط.",
                "show_alert": True
            })
            return

        remember_media(self,chat_id,callback.get('message') or {})
        if support_route(data):
            self._api_call('answerCallbackQuery', {'callback_query_id': callback.get('id')})
            self._official_support(chat_id)
            return
        if not self._subscription_gate(chat_id, message_id=callback.get('message',{}).get('message_id'),
                callback_id=callback.get('id'), verify=data==CHECK_CALLBACK):
            return
        if data.startswith('zc:panel:'):
            show_photo_page(self,chat_id,callback)
            return

        if data.startswith("zc:switch_mode:"):
            target_mode = data.partition("zc:switch_mode:")[2]
            mode_name_ar = "المدير (Admin)" if target_mode == "admin" else "الزبون (Customer)"
            self._api_call("answerCallbackQuery", {
                "callback_query_id": callback.get("id"),
                "text": f"✅ جاري التحويل إلى وضع {mode_name_ar}..."
            })
        else:
            self._api_call("answerCallbackQuery", {"callback_query_id": callback.get("id")})
        message_id = callback.get("message", {}).get("message_id")
        support=getattr(self,'_support_service',None)
        if support is not None:support.store.end_context(chat_id)
        if not data.startswith("zc:"):
            return
        command = data[3:]
        if command.startswith('pa:'):
            self._partner_campaign_approval_callback(command,chat_id,message_id,callback)
            return
        if command.startswith('bn:'):
            self._binance_note_callback(command,chat_id,message_id,callback)
            return
        if command.startswith('pl:'):
            self._partner_lifecycle_callback(command,chat_id,message_id,callback)
            return
        if command.startswith('ps:'):
            self._partner_sheet_admin(command,chat_id,message_id,callback)
            return
        if command.startswith('pc:'):
            self._partner_sheet_customer(command,chat_id,message_id)
            return
        if command.startswith('pr:'):
            self._pricing_callback(command,chat_id,message_id,callback)
            return
        if command.startswith('oh:'):
            self._history_callback(command,chat_id,message_id,callback)
            return
        if command.startswith('ar:'):
            self._archive_callback(command,chat_id,message_id)
            return
        if command.startswith('sp:'):
            self._support_callback(command,chat_id,message_id,callback)
            return
        if command.startswith('am:'):
            if is_admin:self._admin_roles_callback(command,chat_id,message_id,callback)
            return
        if command == 'referral' or command.startswith('cp:'):
            self._commerce_callback(command, chat_id, message_id)
        elif command.startswith('cm:'):
            if is_admin:
                self._payment_review_callback(command,chat_id,message_id,callback)
        elif command.startswith('cw:'):
            self._wallet_callback(command,chat_id,message_id,callback)
        elif command.startswith('vc:'):
            self._vodafone_callback(command,chat_id,message_id,callback)
        elif command.startswith('ca:'):
            if is_admin:self._cash_topup_callback(command,chat_id,message_id,callback)
        elif command.startswith('cc:'):
            if command=='cc:redeem:0':self._customer_code_prompt(chat_id,message_id)
        elif command.startswith('cg:'):
            if is_admin:self._cash_code_callback(command,chat_id,message_id,callback)
        elif command.startswith('pf:'):
            self._partner_finance_callback(command,chat_id,message_id,callback)
        elif command.startswith('po:'):
            self._partner_campaign_callback(command,chat_id,message_id,callback)
        elif command.startswith('wa:'):
            if is_admin:self._withdrawal_admin_callback(command,chat_id,message_id,callback)
        elif command.startswith('of:'):
            self._offers_callback(command,chat_id,message_id,callback)
        elif command.startswith('oa:'):
            if is_admin:self._campaign_admin_callback(command,chat_id,message_id,callback)
        elif command.startswith("co:"):
            self._commerce_checkout_callback(command, chat_id, message_id, callback)
        elif command.startswith("map:"):
            self._sheet_callback(command, chat_id, message_id)
        elif command.startswith("pay:"):
            self._handle_payment_method_callback(command, chat_id, message_id, callback)
        elif command.startswith("paycheck:"):
            self._handle_crypto_payment_check(command, chat_id, message_id, callback.get("id"))
        elif command.startswith("admpay:"):
            if is_admin:
                self._handle_admin_payment_callback(command, chat_id, message_id)
        elif command == "wallet":
            self._waiting_user_inputs.pop(chat_id, None)
            self._admin_role_reviews={k:v for k,v in getattr(self,'_admin_role_reviews',{}).items() if v['actor']!=chat_id}
            self._show_wallet_panel(chat_id, message_id)
        elif command == "wallet:redeem":
            self._customer_code_prompt(chat_id, message_id)
        elif command == "wallet:topup_menu":
            self._waiting_user_inputs.pop(chat_id, None)
            self._show_wallet_topup_menu(chat_id, message_id)
        elif command.startswith("wallet:topup_net:"):
            net_key = command.split(":", 2)[2]
            self._show_wallet_topup_detail(net_key, chat_id, message_id)
        elif command.startswith("wallet:topup_tx:"):
            net_key = command.split(":", 2)[2]
            self._show_wallet_topup_tx_prompt(net_key, chat_id, message_id)
        elif command.startswith("confirm:"):
            self._confirm_ui_action(command.partition(":")[2], chat_id, message_id)
        elif not self._dispatch_ui(command, chat_id, message_id):
            self._show_panel(chat_id, views.notice("زر غير متاح", "افتح القائمة الرئيسية وحاول مرة أخرى."), message_id)

    def _show_wallet_panel(self, chat_id: int, message_id: int):
        from .payment_manager import PAYMENT_MGR
        balance = PAYMENT_MGR.get_wallet_balance(chat_id)
        acc_id = getattr(self,"_wallet_account_label",PAYMENT_MGR.get_user_account_id)(chat_id)
        # Merely viewing a wallet must not fetch a rate or invent a conversion.
        live_rate = 0.0
        panel = views.wallet_panel(chat_id, balance, account_id=acc_id, live_rate=live_rate,
                                   sheet_price_egp=float(PAYMENT_MGR.get_config()["sheet_price_egp"]))
        if chat_id in getattr(self,'_known_partner_ids',set()) and self._has_active_partner_role(chat_id):
            panel=views.Panel(panel.text,panel.rows+[[views.button('أرباح الشريك','pf:home:0')]])
        if chat_id in admin_ids(self):
            panel=views.Panel(panel.text,panel.rows+[[views.button('أرباحي وأرباح الشركاء','pf:admin:0')]])
        self._show_panel(chat_id, panel, message_id)

    def _show_wallet_topup_menu(self, chat_id: int, message_id: int):
        from .payment_manager import PAYMENT_MGR
        balance = PAYMENT_MGR.get_wallet_balance(chat_id)
        acc_id = getattr(self,"_wallet_account_label",PAYMENT_MGR.get_user_account_id)(chat_id)
        panel = views.wallet_topup_menu_panel(balance, account_id=acc_id, live_rate=0)
        panel = views.Panel(panel.text, [[views.button('فودافون كاش · دفع يدوي','vc:start:0')],
            [views.button('طلباتي بفودافون كاش','vc:list:0')]] + panel.rows)
        self._show_panel(chat_id, panel, message_id)

    def _show_wallet_topup_detail(self, method_key: str, chat_id: int, message_id: int):
        from .config import show_crypto_payments
        if not show_crypto_payments():
            self._show_wallet_topup_menu(chat_id, message_id)
            return
        from .payment_manager import PAYMENT_MGR
        from .crypto_pay import get_live_usd_egp_rate,ExchangeRateUnavailable
        acc_id = getattr(self,"_wallet_account_label",PAYMENT_MGR.get_user_account_id)(chat_id)
        try:
            live_rate = get_live_usd_egp_rate()
        except ExchangeRateUnavailable:
            self._show_panel(chat_id,views.Panel('<b>سعر الصرف غير متاح الآن</b>\n\nتعذر جلب السعر الحالي. أعد المحاولة قبل إجراء التحويل.',
                [[views.button('إعادة المحاولة','wallet:topup_net:'+method_key)],
                 [views.button('المحفظة','wallet')]]),message_id)
            return
        cfg = PAYMENT_MGR.get_config()
        nets = cfg.get("crypto_networks", {})
        net_info = nets.get(method_key, {})
        binance_id = cfg.get("binance_pay_id", "852195905")
        binance_link = cfg.get("binance_pay_link", "")
        panel = views.wallet_topup_detail_panel(
            method_key=method_key,
            net_info=net_info,
            account_id=acc_id,
            live_rate=live_rate,
            binance_pay_id=binance_id,
            binance_pay_link=binance_link,
        )
        self._show_panel(chat_id, panel, message_id)

    def _show_wallet_topup_tx_prompt(self, method_key: str, chat_id: int, message_id: int):
        from .config import show_crypto_payments
        if not show_crypto_payments():
            self._show_wallet_topup_menu(chat_id, message_id)
            return
        from .payment_manager import PAYMENT_MGR

        cfg = PAYMENT_MGR.get_config()
        nets = cfg.get("crypto_networks", {})
        if method_key == "binance":
            method_name = "Binance Pay"
        elif method_key in ("bsc", "bsc_usdt", "bsc_usdc"):
            method_name = "USDT / USDC (BSC - BEP20)"
        else:
            net_info = nets.get(method_key, {})
            method_name = net_info.get("network_name", net_info.get("network", method_key.upper()))

        self._waiting_user_inputs[chat_id] = {
            "action": "topup_tx",
            "method_key": method_key,
            "method_name": method_name,
            "message_id": message_id
        }
        panel = views.wallet_topup_tx_prompt_panel(method_name, method_key)
        self._show_panel(chat_id, panel, message_id)

    def _payment_draft(self, token, chat_id, *, recover=False):
        from .sheet_workflow import file_version
        draft = self._sheet_drafts.get(token)
        if recover:
            from .payment_manager import PAYMENT_MGR
            stored = PAYMENT_MGR.get_pending_payment(token)
            if stored:
                if stored.get("status") not in {"ready", "rejected"} or str(stored.get("user_id")) != str(chat_id):
                    return None
                draft = stored.get("draft_info")
                if draft:
                    draft = {**draft, "path": Path(draft["path"]), "version": tuple(draft.get("version", ())),
                             "chat": chat_id, "expires": time.monotonic() + 60}
        if not draft or draft.get("chat", draft.get("chat_id")) != chat_id:
            return None
        if draft.get("expires", 0) <= time.monotonic() or not draft.get("validation", {}).get("ok"):
            return None
        try:
            if tuple(draft.get("version", ())) != file_version(draft["path"]):
                return None
        except (OSError, TypeError, KeyError):
            return None
        return draft

    def _enqueue_paid_draft(self, token, draft, user_id):
        # The payment marker lives with the persisted queue job, allowing a
        # retry after a restart to find the already fulfilled order.
        marker = f"telegram:{user_id}:{token}"
        mapping = {**draft['metadata'], **draft['mapping'],
                   'telegram_chat_id':int(user_id),'payment_token':marker}
        path = Path(draft['path'])
        total = draft['validation']['rows']
        if draft.get('commerce_order_id'):
            from .commerce.bridge import file_sha256
            path = Path(draft['commerce_input'])
            if file_sha256(path) != draft['commerce_source']['file_sha256']:
                raise ValueError('Commerce input differs from paid quote')
            mapping.update(commerce_order_id=draft['commerce_order_id'],
                           commerce_input_sha256=draft['commerce_source']['file_sha256'],
                           commerce_rows=draft['commerce_rows'],
                           commerce_quoted_minor=draft['commerce_quoted_minor'],
                           commerce_pricing_snapshot=draft['commerce_pricing_snapshot'])
            total = len(draft['commerce_rows'])
        with self.orchestrator.lock:
            for job in self.queue_service.get_jobs():
                if job.get("column_mapping", {}).get("payment_token") == marker:
                    if job.get('column_mapping',{}).get('commerce_order_id') != draft.get('commerce_order_id'):
                        raise ValueError('Payment job belongs to another commerce order')
                    return job["id"]
            job = self.queue_service.add_job(
                file_path=path, sheet_index=draft["sheet_index"],
                sheet_name=draft["sheet_name"], mode=draft["mode"],
                amount_target=draft["amount_target"],
                column_mapping=mapping, total_records=total,
            )
            return job.id

    def _payment_success_rows(self, chat_id):
        auth_set = admin_ids(self)
        if chat_id in auth_set and self._get_user_mode(chat_id) == "admin":
            return [[views.button("▶ بدء الجولة", "start_queue", "success")],
                    [views.button("📁 عرض الطابور", "queue:0")], views.nav()]
        return [[views.button('متابعة طلباتي','oh:list:0')], [views.button("الرئيسية", "home"), views.button("مساعدة", "help")]]

    @_serialized_payment
    def _handle_payment_method_callback(self, command: str, chat_id: int, message_id: int, callback: dict):
        from .payment_manager import PAYMENT_MGR
        parts = command.split(":")
        if len(parts) < 3:
            return
        method = parts[1]
        if method == "net" and len(parts) >= 4:
            net_key = parts[2]
            token = parts[3]
        else:
            net_key = ""
            token = parts[2]
        if self._sheet_drafts.get(token,{}).get('commerce_request_key'):
            self._commerce_start_checkout(token,chat_id,message_id)
            return
        with self._ui_lock:
            draft = self._payment_draft(token, chat_id, recover=True)
        if not draft:
            self._show_panel(chat_id, views.notice("انتهت الجلسة", "أرسل الملف مرة أخرى للمراجعة والدفع."), message_id)
            return

        if (getattr(self,'_commerce_checkout_enabled',lambda uid:False)(chat_id)
                and not draft.get('commerce_request_key')):
            if self._history_existing_paid_input(token,draft,chat_id,message_id):return
            self._commerce_start_checkout(token,chat_id,message_id)
            return

        cfg = PAYMENT_MGR.get_config()
        sheet_name = draft["sheet_name"]
        amount_usdt = draft.get("usdt_amount", 0.57)

        if method == "promo":
            self._waiting_user_inputs[chat_id] = {"action": "promo", "token": token, "message_id": message_id}
            panel = views.promo_input_panel(sheet_name, token)
            self._show_panel(chat_id, panel, message_id)

        elif method == "wallet":
            try:
                if hasattr(self,'_history_existing_paid_input') and self._history_existing_paid_input(token,draft,chat_id,message_id):return
            except (OSError,ValueError,KeyError,TypeError,RuntimeError):
                self._show_panel(chat_id,views.notice('تعذر مراجعة الدفع السابق','لم نخصم مبلغًا جديدًا. راجع طلباتك قبل إعادة الدفع.'),message_id)
                return
            acc_id = getattr(self,"_wallet_account_label",PAYMENT_MGR.get_user_account_id)(chat_id)
            op_id = draft.get("operation_id", "")
            op_line = f"🧾 رقم العملية: <code>#{op_id}</code>\n" if op_id else ""
            amount_egp = draft.get("egp_amount", 30.0)
            PAYMENT_MGR.record_payment_draft(token, chat_id, draft, kind="wallet")
            ok, new_bal = PAYMENT_MGR.debit_wallet(chat_id, amount_egp, reason=f"فحص: {sheet_name} ({op_id})", idempotency_key=f"wallet:{chat_id}:{token}")
            if ok:
                try:
                    job_id = self._enqueue_paid_draft(token, draft, chat_id)
                    PAYMENT_MGR.complete_payment_draft(token, job_id)
                except Exception:
                    self._show_panel(chat_id, views.notice("تعذر إضافة الفحص", "تم حفظ الدفع. أعد الضغط على الدفع بالمحفظة لإعادة المحاولة دون خصم جديد."), message_id)
                    return
                if op_id:
                    PAYMENT_MGR.log_operation(op_id, "wallet_payment", chat_id,
                        {"method": "Wallet", "sheet_name": sheet_name, "amount_egp": amount_egp,
                         "amount_usdt": amount_usdt, "status": "completed"})
                with self._ui_lock:
                    self._sheet_drafts.pop(token, None)
                if hasattr(self, "_persist_sheet_drafts"):
                    self._persist_sheet_drafts()
                from .config import show_crypto_payments
                if show_crypto_payments():
                    from .crypto_pay import get_live_usd_egp_rate
                    lr = draft.get("live_rate", 0)
                    rem_usdt = f" (~{new_bal / lr:.2f} USDT)" if (lr > 0 and new_bal > 0) else ""
                else:
                    rem_usdt = ""
                panel = views.Panel(
                    f"<b>✅ تم الخصم من محفظتك بنجاح!</b>\n\n"
                    f"🆔 رقم المستخدم (User ID): <code>{acc_id}</code>\n"
                    f"{op_line}"
                    f"📁 الملف: <b>{views.safe(sheet_name)}</b>\n"
                    f"💵 المبلغ المخصوم: <b>{amount_egp:.2f} جنيه مصري</b>\n"
                    f"💼 رصيدك المتبقي: <b>{new_bal:.2f} جنيه مصري</b>{rem_usdt}\n\n"
                    f"تمت إضافة الشيت إلى الطابور. ستصلك النتيجة بعد إتمام الفحص.",
                    self._payment_success_rows(chat_id)
                )
                self._show_panel(chat_id, panel, message_id)
            else:
                self._api_call("answerCallbackQuery", {
                    "callback_query_id": callback.get("id"),
                    "text": f"⚠️ رصيد محفظتك ({new_bal:.2f} ج.م) غير كافٍ. المطلوب {amount_egp:.2f} ج.م. يرجى شحن المحفظة أولاً.",
                    "show_alert": True
                })

        elif method in ("binance", "crypto_menu", "net", "bep20"):
            from .config import show_crypto_payments
            if not show_crypto_payments():
                self._show_panel(chat_id, views.notice("طريقة الدفع غير متاحة", "طرق الدفع بالعملات الرقمية غير مفعلة حالياً. يمكنك الدفع من رصيد المحفظة أو باستخدام كود خصم."), message_id)
                return
            if method == "binance":
                pay_link = cfg.get("binance_pay_link", "https://app.binance.com/uni-qr/RiLBez57")
                panel = views.binance_pay_panel(
                    sheet_name,
                    amount_usdt,
                    token,
                    pay_link=pay_link,
                    amount_egp=draft.get("egp_amount", 30.0),
                    live_rate=draft.get("live_rate", 0.0),
                    binance_pay_id=cfg.get("binance_pay_id", "852195905"),
                )
                self._show_panel(chat_id, panel, message_id)


        elif method == "crypto_menu":
            panel = views.crypto_menu_panel(
                sheet_name,
                token,
                amount_usdt,
                amount_egp=draft.get("egp_amount", 30.0),
                live_rate=draft.get("live_rate", 0.0),
            )
            self._show_panel(chat_id, panel, message_id)

        elif method == "net":
            crypto_nets = cfg.get("crypto_networks", {})
            net_info = crypto_nets.get(net_key)
            if not net_info:
                panel = views.bep20_pay_panel(
                    sheet_name,
                    amount_usdt,
                    cfg.get("bep20_address", "0xa5f435e52a8752816569a11f0c8660aa877faa72"),
                    token,
                    currency=cfg.get("bep20_currency", "USDT"),
                    contract_info=cfg.get("bep20_contract_info", "***d580d"),
                    amount_egp=draft.get("egp_amount", 30.0),
                    live_rate=draft.get("live_rate", 0.0),
                )
            else:
                panel = views.crypto_network_detail_panel(
                    sheet_name,
                    net_key,
                    net_info,
                    token,
                    amount_usdt,
                    amount_egp=draft.get("egp_amount", 30.0),
                    live_rate=draft.get("live_rate", 0.0),
                )
            self._show_panel(chat_id, panel, message_id)

        elif method == "bep20":
            addr = cfg.get("bep20_address", "0xa5f435e52a8752816569a11f0c8660aa877faa72")
            curr = cfg.get("bep20_currency", "USDT")
            contract = cfg.get("bep20_contract_info", "***d580d")
            panel = views.bep20_pay_panel(
                sheet_name,
                amount_usdt,
                addr,
                token,
                currency=curr,
                contract_info=contract,
                amount_egp=draft.get("egp_amount", 30.0),
                live_rate=draft.get("live_rate", 0.0),
            )
            self._show_panel(chat_id, panel, message_id)

        elif method.startswith("tx_"):
            target_method = method[3:]
            crypto_nets = cfg.get("crypto_networks", {})
            net_info = crypto_nets.get(target_method)
            if net_info:
                label = f"{net_info.get('currency', 'Crypto')} ({net_info.get('network', target_method)})"
            else:
                method_labels = {
                    "binance": "Binance Pay",
                    "bep20": "USDT/USDC (BEP20)",
                    "bsc_usdt": "USDT (BSC - BEP20)",
                    "bsc_usdc": "USDC (BSC - BEP20)",
                    "ton_usdt": "USDT (TON Network)",
                    "sol_usdc": "USDC (Solana)",
                    "erc20_usdt": "USDT (Ethereum ERC20)",
                }
                label = method_labels.get(target_method, target_method)
            self._waiting_user_inputs[chat_id] = {
                "action": "tx_id",
                "method": label,
                "target_key": target_method,
                "token": token,
                "message_id": message_id
            }
            panel = views.tx_prompt_panel(sheet_name, label, token)
            self._show_panel(chat_id, panel, message_id)

    @_serialized_payment
    def _handle_admin_payment_callback(self, command: str, chat_id: int, message_id: int):
        auth_set = admin_ids(self)
        if chat_id not in auth_set:
            return
        from .payment_manager import PAYMENT_MGR
        parts = command.split(":")
        if len(parts) < 3:
            return
        action = parts[1]
        token = parts[2]
        if self._sheet_drafts.get(token,{}).get('commerce_request_key'):
            self._commerce_start_checkout(token,chat_id,message_id)
            return
        if action not in {"approve", "reject", "approve_topup", "reject_topup"}:
            return
        is_approve = action in {"approve", "approve_topup"}
        item = PAYMENT_MGR.resolve_pending_payment(token, approved=is_approve)
        if not item:
            self._show_panel(chat_id, views.notice("طلب غير موجود", "تمت معالجة هذا الطلب مسبقاً."), message_id)
            return

        user_id = item["user_id"]
        draft_info = item.get("draft_info", {})
        sheet_name = item.get("sheet_name", "ملف_عملاء.xlsx")
        account_id = item.get("account_id") or PAYMENT_MGR.get_user_account_id(user_id)
        op_id = item.get("operation_id", "")
        op_line = f"🧾 رقم العملية: <code>#{op_id}</code>\n" if op_id else ""

        is_topup = action in {"approve_topup", "reject_topup"} or token.startswith("topup_") or draft_info.get("kind") == "topup"

        if is_topup:
            if is_approve:
                amount_egp = float(item.get("amount_egp", 0.0))
                amount_usdt = float(item.get("amount_usdt", 0.0))
                method = item.get("method", "Crypto")
                new_bal = PAYMENT_MGR.credit_wallet(user_id, amount_egp, reason=f"topup_approved:{token}")
                PAYMENT_MGR.mark_payment_fulfilled(token)

                admin_text = f"<b>✅ تم اعتماد شحن المحفظة بنجاح!</b>\nأُضيف {amount_egp:.2f} ج.م إلى حساب العميل (ID: <code>{account_id}</code>)."
                self._show_panel(chat_id, views.Panel(admin_text, [views.nav()]), message_id)

                cust_text = (
                    f"<b>🎉 تم اعتماد شحن محفظتك بنجاح ⚡</b>\n\n"
                    f"🆔 رقم الحساب: <code>{account_id}</code>\n"
                    f"{op_line}"
                    f"💳 طريقة الدفع: <b>{views.safe(method)}</b>\n"
                    f"🪙 المبلغ المعتمد: <b>{amount_usdt:.2f} USDT/USDC</b>\n"
                    f"💵 الرصيد المضاف: <b>+{amount_egp:.2f} جنيه مصري</b>\n"
                    f"💼 رصيدك الإجمالي الجديد: <b>{new_bal:.2f} جنيه مصري</b>\n\n"
                    f"✅ يمكنك الآن استخدام رصيدك لفحص أي شيت بضغطة زر واحدة 🚀"
                )
                self._api_call("sendMessage", {
                    "chat_id": int(user_id),
                    "text": cust_text,
                    "parse_mode": "HTML",
                })
                return
            else:
                admin_text = f"<b>❌ تم رفض طلب شحن المحفظة.</b>\nالطلب: <code>{views.safe(token)}</code>"
                self._show_panel(chat_id, views.Panel(admin_text, [views.nav()]), message_id)

                cust_text = (
                    f"<b>❌ تم رفض طلب شحن المحفظة</b>\n\n"
                    f"🆔 رقم الحساب: <code>{account_id}</code>\n"
                    f"{op_line}"
                    f"لم تتمكن الإدارة من تأكيد وصول الحوالة برقم المعاملة المدخل.\n"
                    f"يرجى مراجعة إيصال التحويل أو التواصل مع الدعم."
                )
                self._api_call("sendMessage", {
                    "chat_id": int(user_id),
                    "text": cust_text,
                    "parse_mode": "HTML",
                })
                return

        if is_approve:
            from .sheet_workflow import file_version
            try:
                if not draft_info.get("validation", {}).get("ok") or tuple(draft_info.get("version", ())) != file_version(draft_info["path"]):
                    raise ValueError("Paid workbook changed")
                job_id = self._enqueue_paid_draft(token, draft_info, int(user_id))
                PAYMENT_MGR.mark_payment_fulfilled(token, job_id)
            except Exception:
                self._show_panel(chat_id, views.notice("الدفعة محفوظة ولم يكتمل الفحص", "تعذر إضافة الملف. راجع الملف والطابور ثم أعد الموافقة؛ لن يضاف الطلب مرتين."), message_id)
                return
            with self._ui_lock:
                self._sheet_drafts.pop(token, None)
                if hasattr(self, "_persist_sheet_drafts"):
                    self._persist_sheet_drafts()

            admin_text = f"<b>✅ تمت الموافقة على الدفعة للشيت {views.safe(sheet_name)}!</b>\nتمت إضافته إلى الطابور فوراً."
            self._show_panel(chat_id, views.Panel(admin_text, [views.nav()]), message_id)

            amount_egp = item.get("amount_egp", 30.0)
            cust_text = (
                f"<b>✅ تم تأكيد استلام دفعتك بنجاح!</b>\n\n"
                f"🆔 رقم المستخدم (User ID): <code>{account_id}</code>\n"
                f"{op_line}"
                f"📁 الملف: <b>{views.safe(sheet_name)}</b>\n"
                f"💵 المبلغ المعتمد: <b>{amount_egp:.2f} جنيه مصري</b> (المعادل: {item.get('amount_usdt', 0.57):.2f} USDT)\n\n"
                f"تمت إضافة الشيت إلى الطابور. ستصلك النتيجة بعد إتمام الفحص."
            )
            self._api_call("sendMessage", {
                "chat_id": int(user_id),
                "text": cust_text,
                "parse_mode": "HTML",
            })
        else:
            with self._ui_lock:
                self._sheet_drafts.pop(token, None)
            if hasattr(self, "_persist_sheet_drafts"):
                self._persist_sheet_drafts()

            admin_text = f"<b>❌ تم رفض الدفعة للشيت {views.safe(sheet_name)}.</b>\nتم إشعار العميل وإلغاء الطلب من الطابور نهائياً."
            self._show_panel(chat_id, views.Panel(admin_text, [views.nav()]), message_id)

            cust_text = (
                f"<b>❌ تم رفض إثبات الدفع</b>\n\n"
                f"🆔 رقم المستخدم: <code>{account_id}</code>\n"
                f"{op_line}"
                f"📁 الملف: <b>{views.safe(sheet_name)}</b>\n"
                f"💳 طريقة الدفع: <b>{views.safe(item.get('method', ''))}</b>\n"
                f"🔗 رقم العملية: <code>{views.safe(item.get('tx_id', ''))}</code>\n\n"
                f"⚠️ <b>السبب:</b> لم يتم العثور على المبلغ في الحساب أو أن البيانات غير صحيحة.\n"
                f"لم تتم إضافة الملف إلى طابور الفحص. يمكنك إعادة المحاولة برقم معاملة صالح أو الدفع بطريقة أخرى."
            )
            self._api_call("sendMessage", {
                "chat_id": int(user_id),
                "text": cust_text,
                "parse_mode": "HTML",
            })

    @_serialized_payment
    def _handle_crypto_payment_check(self, command: str, chat_id: int, message_id: int, callback_id: str = ""):
        from .crypto_pay import CRYPTO_PAY
        parts = command.split(":")
        if len(parts) < 3:
            return
        try:
            invoice_id = int(parts[1])
        except ValueError:
            return
        token = parts[2]
        with self._ui_lock:
            draft = self._payment_draft(token, chat_id, recover=True)
        if not draft:
            self._show_panel(chat_id, views.notice("انتهت الجلسة", "أرسل الملف مرة أخرى للمراجعة والدفع."), message_id)
            return

        prior = None
        expected_amount = draft.get("usdt_amount") if invoice_id == draft.get("invoice_id") else None
        if expected_amount is None:
            prior = next((item for item in draft.get("previous_invoices", []) if item.get("invoice_id") == invoice_id), None)
            expected_amount = prior.get("usdt_amount") if prior else None
        if invoice_id <= 0 or expected_amount is None:
            return
        is_paid = CRYPTO_PAY.is_invoice_paid(invoice_id, expected_amount=expected_amount,
            expected_payload=token, expected_asset="USDT")
        if is_paid:
            if callback_id:
                self._api_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": "✅ تم تأكيد استلام الدفع بنجاح!", "show_alert": False})
            try:
                from .payment_manager import PAYMENT_MGR
                job_id = self._enqueue_paid_draft(token, draft, chat_id)
                PAYMENT_MGR.complete_payment_draft(token, job_id)
            except Exception:
                self._show_panel(chat_id, views.notice("الدفع مؤكد", "تعذر إضافة الفحص. أعد التحقق من الدفع لإعادة المحاولة دون دفع جديد."), message_id)
                return
            with self._ui_lock:
                self._sheet_drafts.pop(token, None)
                if hasattr(self, "_persist_sheet_drafts"):
                    self._persist_sheet_drafts()
            op_id = draft.get("operation_id", "")
            amount_egp = prior.get("egp_amount", draft.get("egp_amount", 30.0)) if prior else draft.get("egp_amount", 30.0)
            amount_usdt = expected_amount
            if op_id:
                from .payment_manager import PAYMENT_MGR
                PAYMENT_MGR.log_operation(
                    op_id,
                    op_type="cryptobot_payment",
                    user_id=chat_id,
                    details={
                        "method": "CryptoBot",
                        "invoice_id": invoice_id,
                        "sheet_name": draft["sheet_name"],
                        "amount_egp": amount_egp,
                        "amount_usdt": amount_usdt,
                        "status": "completed",
                    }
                )
            op_line = f"🧾 رقم العملية: <code>#{op_id}</code>\n" if op_id else ""
            panel = views.Panel(
                f"<b>✅ تم تأكيد الدفع بنجاح!</b>\n\n"
                f"{op_line}"
                f"📁 الملف: <b>{views.safe(draft['sheet_name'])}</b>\n"
                f"💵 الرسوم الأساسية: <b>{amount_egp:.2f} جنيه مصري</b>\n"
                f"🪙 المعادل المدفوع: <b>{amount_usdt:.2f} USDT</b>\n\n"
                f"تمت إضافة الشيت إلى الطابور. ستصلك النتيجة بعد إتمام الفحص.",
                self._payment_success_rows(chat_id)
            )
            self._show_panel(chat_id, panel, message_id)
        else:
            if callback_id:
                self._api_call("answerCallbackQuery", {
                    "callback_query_id": callback_id,
                    "text": "⏳ لم يتم العثور على تأكيد الدفع بعد في CryptoBot. يرجى إتمام الدفع ثم الضغط مجدداً.",
                    "show_alert": True
                })

    def _confirm_ui_action(self, token, chat_id, message_id):
        with self._ui_lock:
            action = self._confirmations.get(token)
            if action and action["chat"] == chat_id:
                self._confirmations.pop(token, None)
        if not action or action["chat"] != chat_id or action["expires"] <= time.monotonic():
            self._show_panel(chat_id, views.notice("انتهت صلاحية التأكيد", "افتح الحالة واطلب الإجراء من جديد."), message_id)
            return
        if getattr(self.orchestrator, 'remote', False) is True:
            try:
                kind = action['action']
                if kind == 'remove':
                    removed = self.queue_service.remove_job(action['job_id'], expected={
                        'filename': action['workbook'], 'sheet_index': action['sheet_index']})
                    self._show_panel(chat_id, views.notice('حالة الطابور',
                        'أُخرج الشيت من الطابور. الملف محفوظ.' if removed else 'تغيرت حالة الشيت؛ حدّث الطابور.'), message_id)
                elif kind == 'start_queue':
                    self.orchestrator.start_reviewed(action['signature'], action['versions'])
                    self._dispatch_ui('status', chat_id, message_id)
                else:
                    outcome = self.orchestrator.session_action(kind, action['session'], action['workbook'])
                    if outcome.get('problem'):
                        self._show_panel(chat_id, views.notice('راجع حالة الجلسة', outcome['problem']), message_id)
                    elif kind == 'results':
                        self._deliver_report(chat_id, outcome.get('records', []), action['workbook'],
                            message_id, [Path(path) for path in outcome.get('other_reports', [])])
                    elif kind == 'repair':
                        self._show_panel(chat_id, views.notice('إعادة فحص الأخطاء',
                            (outcome.get('repair_result') or {}).get('message', 'راجع الحالة.')), message_id)
                    else:self._dispatch_ui('status', chat_id, message_id)
            except (RuntimeError, ValueError, OSError):
                self._show_panel(chat_id, views.notice('تعذر تأكيد حالة المحرك',
                    'الإجراء محفوظ للمراجعة. حدّث حالة الطلب قبل المحاولة مجددًا؛ لا تُعد الدفع.'), message_id)
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
        if getattr(self.orchestrator, 'remote', False) is True:
            return self.orchestrator.review_queue_start()
        from domain.workbook import load_fast_workbook as load_workbook
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
            root = self.project_root.resolve()
            saved_mapping = job.get("column_mapping", {})
            commerce_input = bool(saved_mapping.get('commerce_order_id'))
            if commerce_input:
                digest = saved_mapping.get('commerce_input_sha256')
                if (not isinstance(digest,str) or len(digest)!=64
                        or any(char not in '0123456789abcdef' for char in digest)):
                    raise ValueError("بصمة ملف الطلب غير صالحة. راجع الطلب الأصلي.")
                expected = (root / '.commerce' / 'inputs' / (digest+'.xlsx')).resolve()
                if path != expected or not path.is_relative_to(root):
                    raise ValueError("راجع مسار ملف الجولة.")
            elif path.parent != root:
                raise ValueError("راجع مسار ملف الجولة.")
            before = file_version(path)
            if commerce_input:
                from .commerce.bridge import file_sha256
                if file_sha256(path) != digest:
                    raise ValueError("تغير ملف الطلب عن النسخة المعتمدة. راجع الطلب الأصلي.")
            wb = load_workbook(path, read_only=True, data_only=True)
            try:
                index = job["sheet_index"]
                if not isinstance(index,int) or index < 0 or index >= len(wb.worksheets):
                    raise ValueError("ورقة العمل غير موجودة. راجع الشيت من الطابور.")
                sheet = wb.worksheets[index]
                mapping = {**inspect_sheet_schema(sheet, has_headers=saved_mapping.get("has_headers", True), count_rows=False)["letters"], **saved_mapping}
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
        if not records and not other_reports:
            self._show_panel(chat_id, views.notice("لا توجد نتائج", "لم تتوفر سجلات محفوظة لهذه الجلسة لإرسالها."), message_id)
            return
        self._show_panel(chat_id, views.notice("📥 تجهيز النتائج", "جارٍ تجهيز نسخة من نتائج الجلسة التي أكدت إرسالها…"), message_id)
        try:
            from zain_checker.executive_reporter import export_executive_workbook
            from .notifier import TelegramNotifier
            report_dir = self.project_root / "audit_outputs" / "telegram_reports"
            report_dir.mkdir(parents=True, exist_ok=True)
            report = report_dir / f"نتائج_الجلسة_{secrets.token_hex(8)}.xlsx"
            if records:
                mawarid = any(bool(r.get("mawarid_format")) for r in records if isinstance(r, dict))
                report = Path(export_executive_workbook(records, report, mawarid_format=mawarid))
            reports = []
            from shutil import copy2
            for source in other_reports or []:
                source = source.resolve()
                if source.parent != self.project_root.resolve() or not source.is_file():
                    raise ValueError("Round result file missing")
                copy = report_dir / f"{source.stem}_{secrets.token_hex(8)}.xlsx"
                copy2(source, copy)
                reports.append(copy)
            if records:
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
