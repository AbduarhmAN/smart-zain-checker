"""Background Payment Watcher for Zain Checker.
Periodically polls Binance Pay and incoming crypto transactions,
detects new payments, notifies the admin on Telegram,
and automatically processes pending customer drafts.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Set

logger = logging.getLogger("PaymentWatcher")


class PaymentWatcher:
    def __init__(self, bot_runner=None, poll_interval: int = 30):
        self.bot_runner = bot_runner
        self.poll_interval = poll_interval
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self.seen_file = Path(__file__).resolve().parent.parent / "seen_transactions.json"
        self._lock = threading.Lock()

    def _load_seen(self) -> Set[str]:
        try:
            if self.seen_file.exists():
                data = json.loads(self.seen_file.read_text(encoding="utf-8"))
                return set(data.get("seen_ids", []))
        except Exception as e:
            logger.warning("Could not load seen transactions: %s", e)
        return set()

    def _save_seen(self, seen_set: Set[str]) -> None:
        try:
            payload = {"seen_ids": list(seen_set)[-500:]}
            self.seen_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning("Failed to save seen transactions: %s", e)

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run_loop, name="PaymentWatcherThread", daemon=True)
            self._thread.start()
            logger.info("PaymentWatcher started (interval: %ds).", self.poll_interval)

    def stop(self) -> None:
        with self._lock:
            self._running = False
            self._stop_event.set()
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=5)
            logger.info("PaymentWatcher stopped.")

    def _run_loop(self) -> None:
        # Initial brief delay before starting the loop
        time.sleep(5)
        seen_ids = self._load_seen()

        while not self._stop_event.is_set():
            try:
                self._check_bound_invoices()
            except Exception as e:
                logger.warning('Bound invoice monitoring unavailable (%s).', type(e).__name__)
            try:
                self._check_binance_pay(seen_ids)
            except Exception as e:
                logger.debug("PaymentWatcher Binance check error: %s", e)

            # Wait for poll_interval or until stop is requested
            self._stop_event.wait(self.poll_interval)

    def _check_bound_invoices(self):
        if self.bot_runner is None:
            return
        if not hasattr(self, '_bound_monitor'):
            from .commerce.invoice_monitor import BoundInvoiceMonitor
            self._bound_monitor = BoundInvoiceMonitor(self.bot_runner._commerce(),
                self.bot_runner._checkout_adapter, on_confirmed=self._bound_invoice_confirmed)
        result=self._bound_monitor.tick()
        from .commerce.binance_notes import enabled,BinanceNoteMonitor
        if enabled(self.bot_runner.project_root):
            if not hasattr(self,'_note_monitor'):
                from .crypto_pay import BINANCE_PAY
                from .commerce.provider_routes import verifier_for
                self._note_monitor=BinanceNoteMonitor(self.bot_runner._commerce(),verifier_for,BINANCE_PAY,
                    on_confirmed=self._bound_invoice_confirmed)
            self._note_monitor.tick()
        # Recover confirmed direct payments whose queue acknowledgement was
        # lost. This does not issue or charge any new payment.
        self.bot_runner._recover_commerce_checkouts()
        return result

    def _bound_invoice_confirmed(self, quote, result):
        # A payment stays attached to its original customer even if somebody
        # forwards the invoice URL or copies the invoice/transaction number.
        if quote.order_id:
            self.bot_runner._finish_assigned_payment(quote, result['payment_id'])

    def _check_binance_pay(self, seen_ids: Set[str]) -> None:
        from .crypto_pay import BINANCE_PAY, get_live_usd_egp_rate
        from .payment_manager import PAYMENT_MGR
        from .views import safe

        cfg = PAYMENT_MGR.get_config()
        if not cfg.get("binance_api_key") or not cfg.get("binance_api_secret"):
            return

        txs = BINANCE_PAY.get_recent_transactions(limit=15)
        if not txs:
            return

        new_detected = []
        for tx in txs:
            tx_id = str(tx.get("transactionId", "")).strip()
            order_id = str(tx.get("orderId", "")).strip()
            unique_key = tx_id or order_id
            if not unique_key:
                continue

            if unique_key in seen_ids:
                continue

            # Verify receiver matches our Pay ID
            rec_id = str(tx.get("receiverInfo", {}).get("binanceId", "")).strip()
            my_pay_id = str(cfg.get("binance_pay_id", "852195905")).strip()
            if rec_id and my_pay_id and rec_id != my_pay_id:
                continue

            seen_ids.add(unique_key)
            new_detected.append(tx)

        if new_detected:
            self._save_seen(seen_ids)
            for tx in new_detected:
                self._notify_admin(tx)

    def _notify_admin(self, tx: dict) -> None:
        from .views import safe
        from .crypto_pay import get_live_usd_egp_rate

        amount = tx.get("amount", "0")
        currency = tx.get("currency", "USDT")
        payer_name = tx.get("payerInfo", {}).get("name") or "مستخدم"
        payer_id = tx.get("payerInfo", {}).get("binanceId") or "—"
        tx_id = tx.get("transactionId", "—")
        order_id = tx.get("orderId", "—")

        try:
            live_rate = get_live_usd_egp_rate()
            egp_val = round(float(amount) * live_rate, 2)
            egp_str = f" (~{egp_val:.2f} ج.م)"
        except Exception:
            live_rate = 0.0
            egp_str = ""

        if self.bot_runner and hasattr(self.bot_runner, "_api_call"):
            from .authorization import admin_ids
            target_chats = admin_ids(self.bot_runner)
            msg = (
                f"⚡ <b>إشعار استلام دفعة جديدة عبر Binance Pay!</b>\n\n"
                f"🪙 المبلغ المستلم: <b>{amount} {currency}</b>{egp_str}\n"
                f"👤 الدافع: <b>{safe(payer_name)}</b> (ID: <code>{payer_id}</code>)\n"
                f"🧾 رقم العملية (TxID): <code>{safe(tx_id)}</code>\n"
                f"📦 رقم الطلب (Order ID): <code>{safe(order_id)}</code>\n\n"
                f"✅ تم تأكيد وصول المبلغ إلى حسابك في بينانس بنجاح 🚀"
            )
            for target_chat in target_chats:
                if target_chat:
                    try:
                        self.bot_runner._api_call("sendMessage", {
                            "chat_id": target_chat,
                            "text": msg,
                            "parse_mode": "HTML",
                        })
                    except Exception as e:
                        logger.warning("Failed to send Binance Pay alert to admin %s: %s", target_chat, e)
