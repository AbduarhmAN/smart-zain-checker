"""Comprehensive Payment & Wallet Management System for Zain Checker.
Supports:
1. Promo Codes (Free checks, percentage discounts, wallet credits)
2. Internal User Wallets (Pay with Wallet balance, Top-up)
3. Direct Exchange Transfers (Binance Pay ID, Bybit UID)
4. On-chain Transfers (USDT BEP20 BSC Address)
5. CryptoBot Automated Invoices
"""
from __future__ import annotations

import json
import copy
import os
import secrets
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import datetime, timezone
import logging
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("PaymentManager")

CONFIG_FILE_NAME = "payment_config.json"
WALLETS_FILE_NAME = "user_wallets.json"
PENDING_PAYMENTS_FILE_NAME = "pending_payments.json"

DEFAULT_CONFIG: Dict[str, Any] = {
    "base_currency": "EGP",
    "sheet_price_egp": 30.0,
    "binance_pay_link": "https://app.binance.com/uni-qr/RiLBez57",
    "bep20_address": "0xa5f435e52a8752816569a11f0c8660aa877faa72",
    "bep20_currency": "USDT",
    "bep20_contract_info": "***d580d",
    "crypto_networks": {
        "bsc_usdt": {
            "currency": "USDT",
            "network": "BSC (BNB Smart Chain - BEP20)",
            "network_name": "BNB Smart Chain (BEP20)",
            "address": "0xa5f435e52a8752816569a11f0c8660aa877faa72",
            "case_sensitive": False,
            "auto_verify": True,
        },
        "bsc_usdc": {
            "currency": "USDC",
            "network": "BSC (BNB Smart Chain - BEP20)",
            "network_name": "BNB Smart Chain (BEP20)",
            "address": "0xa5f435e52a8752816569a11f0c8660aa877faa72",
            "case_sensitive": False,
            "auto_verify": True,
        },
        "ton_usdt": {
            "currency": "USDT",
            "network": "TON (The Open Network)",
            "network_name": "TON (The Open Network)",
            "address": "UQDPoVRVAmSzBFeWTqmCuI2DdnCICIkls3ymqaa4wwS6_B0",
            "case_sensitive": True,
            "auto_verify": False,
        },
        "sol_usdc": {
            "currency": "USDC",
            "network": "SOL (Solana)",
            "network_name": "SOL (Solana)",
            "address": "aswLvnzUiDpxX5x3T851AMZrXyvtBZc2FbXJ9FG6ub",
            "case_sensitive": True,
            "auto_verify": False,
        },
        "erc20_usdt": {
            "currency": "USDT",
            "network": "Ethereum (ERC20)",
            "network_name": "Ethereum (ERC20)",
            "address": "0xa5f435e52a8752816569a11f0c8660aa877faa72",
            "case_sensitive": False,
            "auto_verify": False,
        },
    },
    "promo_codes": {
        "WELCOME": {"type": "free", "max_uses": 100, "used_by": []},
        "FREECHECK": {"type": "free", "max_uses": 50, "used_by": []},
        "VIP2026": {"type": "free", "max_uses": 500, "used_by": []},
        "DISCOUNT50": {"type": "percent", "discount": 50, "max_uses": 100, "used_by": []},
    },
    "redeem_codes": {},
    "egp_price": 30.0,
}


class PaymentPersistenceError(RuntimeError):
    """Financial data could not be read or durably written."""


def money(value, *, allow_zero=False) -> Decimal:
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or (result == 0 and not allow_zero):
            raise ValueError("Amount must be finite and positive")
        result = result.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if result == 0 and not allow_zero:
            raise ValueError("Amount is below the minimum precision")
        return result
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("Invalid monetary amount") from exc


def _json_serial(obj: Any) -> Any:
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, Decimal):
        return float(obj)
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if isinstance(obj, (set, tuple)):
        return list(obj)
    return str(obj)


def make_json_safe(obj: Any) -> Any:
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, Decimal):
        return float(obj)
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): make_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [make_json_safe(v) for v in obj]
    return obj


class PaymentManager:
    _locks_guard = threading.Lock()
    _locks = {}
    def __init__(self, data_dir: Optional[Path] = None):
        self.data_dir = Path(data_dir or Path(__file__).resolve().parent.parent).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.data_dir / CONFIG_FILE_NAME
        self.wallets_path = self.data_dir / WALLETS_FILE_NAME
        self.pending_path = self.data_dir / PENDING_PAYMENTS_FILE_NAME
        with self._locks_guard:
            self._lock = self._locks.setdefault(str(self.data_dir).casefold(), threading.RLock())
        self._init_files()

    def _init_files(self) -> None:
        with self._lock:
            if not self.config_path.exists():
                self._save_json(self.config_path, DEFAULT_CONFIG)
            if not self.wallets_path.exists():
                self._save_json(self.wallets_path, {})
            if not self.pending_path.exists():
                self._save_json(self.pending_path, {})

    def _load_json(self, path: Path) -> dict:
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Expected JSON object")
            return data
        except (OSError, ValueError) as exc:
            logger.error("Financial data unavailable: %s", path.name)
            raise PaymentPersistenceError(f"Cannot read {path.name}") from exc

    def _save_json(self, path: Path, data: dict) -> None:
        # A failed write preserves the previous financial file. Failed staging
        # files are retained for diagnosis; no files are deleted here.
        staging = path.with_name(f"{path.name}.{secrets.token_hex(6)}.pending")
        try:
            with staging.open("x", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False, default=_json_serial)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staging, path)
        except (OSError, ValueError, TypeError) as exc:
            logger.error("Financial write failed: %s (%s)", path.name, exc)
            raise PaymentPersistenceError(f"Cannot save {path.name}: {exc}") from exc

    # --- Configuration ---
    def get_config(self) -> dict:
        with self._lock:
            cfg = self._load_json(self.config_path)
            for k, v in DEFAULT_CONFIG.items():
                if k not in cfg:
                    cfg[k] = copy.deepcopy(v)
            return cfg

    def update_config(self, updates: dict) -> None:
        with self._lock:
            cfg = self.get_config()
            cfg.update(updates)
            self._save_json(self.config_path, cfg)

    # --- User Account & Wallet Management ---
    def _generate_unique_account_id(self, wallets: dict) -> int:
        """Generates a unique 9-digit account ID (between 100000000 and 999999999)."""
        import random
        existing = {
            v.get("account_id") for v in wallets.values() if isinstance(v, dict) and "account_id" in v
        }
        while True:
            cand = random.randint(100000000, 999999999)
            if cand not in existing:
                return cand

    def get_or_create_user(self, user_id: int | str) -> dict:
        with self._lock:
            wallets = self._load_json(self.wallets_path)
            uid = str(user_id)
            changed = False
            if uid not in wallets:
                wallets[uid] = {
                    "account_id": self._generate_unique_account_id(wallets),
                    "balance": 0.0,
                    "history": [],
                }
                changed = True
            elif "account_id" not in wallets[uid]:
                wallets[uid]["account_id"] = self._generate_unique_account_id(wallets)
                changed = True

            if changed:
                self._save_json(self.wallets_path, wallets)
            return wallets[uid]

    def get_user_account_id(self, user_id: int | str) -> int:
        user = self.get_or_create_user(user_id)
        return int(user.get("account_id", 0))

    def find_user_by_account_id(self, account_id: int | str) -> Optional[Tuple[str, dict]]:
        try:
            target = int(account_id)
        except (ValueError, TypeError):
            return None
        with self._lock:
            wallets = self._load_json(self.wallets_path)
            for uid, data in wallets.items():
                if isinstance(data, dict) and data.get("account_id") == target:
                    return uid, data
            return None

    def get_wallet_balance(self, user_id: int | str) -> float:
        with self._lock:
            user = self.get_or_create_user(user_id)
            return float(money(user.get("balance", 0.0), allow_zero=True))

    def credit_wallet(self, user_id: int | str, amount: float, reason: str = "deposit", *, idempotency_key: str = "") -> float:
        if self._legacy_wallet_sealed(user_id):
            raise PaymentPersistenceError('Legacy wallet is sealed; use the authoritative commerce funding request')
        value = money(amount)
        return self._wallet_change(user_id, value, "credit", reason, idempotency_key)[1]

    def debit_wallet(self, user_id: int | str, amount: float, reason: str = "sheet_check", *, idempotency_key: str = "") -> Tuple[bool, float]:
        if self._legacy_wallet_sealed(user_id):
            raise PaymentPersistenceError('Legacy wallet is sealed; reopen the existing commerce order')
        return self._wallet_change(user_id, money(amount, allow_zero=True), "debit", reason, idempotency_key)

    def _legacy_wallet_sealed(self,user_id):
        if (self.data_dir/'.commerce/legacy-transition-state.json').exists():return True
        if (self.data_dir/'.commerce/topups.sqlite').exists():
            from .commerce.topup_store import TopupStore
            return TopupStore(self.data_dir).is_owner(int(user_id))
        return False

    def _wallet_change(self, user_id, value, kind, reason, key):
        with self._lock:
            self.get_or_create_user(user_id)
            wallets = self._load_json(self.wallets_path)
            user = wallets[str(user_id)]
            current = money(user.get("balance", 0), allow_zero=True)
            history = user.setdefault("history", [])
            if key:
                previous = next((entry for entry in history if entry.get("idempotency_key") == key), None)
                if previous:
                    if previous.get("type") != kind or money(previous["amount"], allow_zero=True) != value:
                        raise ValueError("Payment key was already used for another amount")
                    return True, float(current)
            if kind == "debit" and current < value:
                return False, float(current)
            balance = current + value if kind == "credit" else current - value
            user["balance"] = float(balance)
            entry = {"type": kind, "amount": float(value), "reason": str(reason),
                     "balance": float(balance), "timestamp": datetime.now(timezone.utc).isoformat()}
            if key:
                entry["idempotency_key"] = key
            history.append(entry)
            self._save_json(self.wallets_path, wallets)
            return True, float(balance)

    # --- Promo Code Management ---
    def validate_promo_code(self, code: str, user_id: int | str) -> Tuple[bool, str, dict]:
        """Validates a promo code.
        Returns: (is_valid, message, details)
        """
        code = code.strip().upper()
        uid = str(user_id)
        with self._lock:
            cfg = self.get_config()
            promos = cfg.get("promo_codes", {})
            if code not in promos:
                return False, "❌ كود الخصم غير موجود. تأكد من صحة كتابته.", {}

            p = promos[code]
            if not isinstance(p, dict) or p.get("type", "free") not in {"free", "percent"}:
                return False, "⚠️ إعدادات هذا الكود غير صالحة. راجع المشرف.", {}
            try:
                discount = Decimal(str(p.get("discount", 100 if p.get("type", "free") == "free" else 0)))
                max_uses = int(p.get("max_uses", 1))
                if not discount.is_finite() or not 0 <= discount <= 100 or max_uses <= 0:
                    raise ValueError("Invalid coupon settings")
            except (ValueError, TypeError, InvalidOperation):
                return False, "⚠️ إعدادات هذا الكود غير صالحة. راجع المشرف.", {}
            used_by = p.get("used_by", [])
            if uid in used_by:
                return False, "⚠️ لقد استخدمت هذا الكود مسبقاً على حسابك.", {}

            if len(used_by) >= max_uses:
                return False, "⚠️ لقد وصل هذا الكود إلى الحد الأقصى من مرات الاستخدام.", {}

            return True, "✅ كود الخصم صالح!", p

    def apply_promo_code(self, code: str, user_id: int | str, *, claim_id: str = "") -> Tuple[bool, str, dict]:
        code = code.strip().upper()
        uid = str(user_id)
        with self._lock:
            cfg = self.get_config()
            existing = cfg.get("promo_codes", {}).get(code, {})
            if claim_id and existing.get("claims", {}).get(uid) == str(claim_id):
                return True, "✅ تم تفعيل كود الخصم لهذا الطلب مسبقاً.", copy.deepcopy(existing)
            ok, msg, p = self.validate_promo_code(code, user_id)
            if not ok:
                return False, msg, {}

            cfg = self.get_config()
            promos = cfg.get("promo_codes", {})
            promos[code].setdefault("used_by", []).append(uid)
            if claim_id:
                promos[code].setdefault("claims", {})[uid] = str(claim_id)
            cfg["promo_codes"] = promos
            self._save_json(self.config_path, cfg)
            return True, "✅ تم تفعيل كود الخصم بنجاح!", p

    def add_promo_code(self, code: str, promo_type: str = "free", max_uses: int = 100, discount: int = 100) -> None:
        code = code.strip().upper()
        with self._lock:
            cfg = self.get_config()
            cfg.setdefault("promo_codes", {})[code] = {
                "type": promo_type,
                "max_uses": max_uses,
                "discount": discount,
                "used_by": []
            }
            self._save_json(self.config_path, cfg)

    # --- Wallet Redeem / Recharge Codes ---
    def redeem_code(self, code: str, user_id: int | str) -> Tuple[bool, str, float]:
        """Redeems a recharge code to top up the user's wallet.
        Returns: (success, message, added_amount)
        """
        code = code.strip().upper()
        uid = str(user_id)
        with self._lock:
            cfg = self.get_config()
            redeem_codes = cfg.get("redeem_codes", {})
            if code not in redeem_codes:
                return False, "⚠️ كود الشحن غير صحيح أو غير موجود.", 0.0

            r = redeem_codes[code]
            used_by = r.setdefault("used_by", [])
            if uid in used_by:
                return False, "⚠️ لقد قمت بشحن هذا الكود مسبقاً على حسابك.", 0.0

            max_uses = r.get("max_uses", 1)
            if len(used_by) >= max_uses:
                return False, "⚠️ لقد استنفد هذا الكود الحد الأقصى من مرات الاستخدام.", 0.0

            try:
                amount = float(money(r.get("amount", 0.0)))
            except ValueError:
                return False, "⚠️ قيمة كود الشحن غير صالحة.", 0.0

            self.credit_wallet(user_id, amount, reason=f"شحن بكود: {code}", idempotency_key=f"redeem:{code}:{uid}")
            used_by.append(uid)
            cfg["redeem_codes"] = redeem_codes
            self._save_json(self.config_path, cfg)
            return True, f"✅ تم شحن {amount:.2f} جنيه مصري إلى محفظتك بنجاح!", amount

    def add_redeem_code(self, code: str, amount: float, max_uses: int = 1) -> None:
        code = code.strip().upper()
        amount = float(money(amount))
        if not code or int(max_uses) < 1:
            raise ValueError("Code and a positive usage limit are required")
        with self._lock:
            cfg = self.get_config()
            cfg.setdefault("redeem_codes", {})[code] = {
                "amount": round(float(amount), 2),
                "max_uses": int(max_uses),
                "used_by": []
            }
            self._save_json(self.config_path, cfg)

    def generate_redeem_code(self, amount: float = 30.0, max_uses: int = 1, prefix: str = "ZAIN") -> str:
        import secrets
        code = f"{prefix}-{secrets.token_hex(3).upper()}"
        self.add_redeem_code(code, amount, max_uses)
        return code

    def list_redeem_codes(self) -> dict:
        with self._lock:
            cfg = self.get_config()
            return cfg.get("redeem_codes", {})

    # --- Operation Tracking (System B: YYMMDD-XXXX) ---
    def generate_operation_id(self) -> str:
        """Generates a daily sequential operation ID in format YYMMDD-XXXX (e.g. 261007-0001).
        Thread-safe, resets sequence counter every day, and supports up to 9999 operations/day.
        """
        import datetime
        today_str = datetime.datetime.now().strftime("%y%m%d")
        with self._lock:
            counters_path = self.data_dir / "operation_counters.json"
            counters = self._load_json(counters_path)
            last_date = counters.get("date", "")
            if last_date == today_str:
                seq = int(counters.get("seq", 0)) + 1
            else:
                seq = 1
            counters["date"] = today_str
            counters["seq"] = seq
            self._save_json(counters_path, counters)
            return f"{today_str}-{seq:04d}"

    # --- Pending Manual Payments (Binance / Bybit / BEP20) ---
    def log_operation(self, op_id: str, op_type: str, user_id: int | str, details: dict) -> None:
        """Stores operation record in operations_log.json for audit and retrieval."""
        import datetime
        clean_id = str(op_id).strip().lstrip("#")
        with self._lock:
            log_path = self.data_dir / "operations_log.json"
            ops = self._load_json(log_path)
            ops[clean_id] = {
                "operation_id": clean_id,
                "type": op_type,
                "user_id": str(user_id),
                "account_id": self.get_user_account_id(user_id),
                "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                **make_json_safe(details)
            }
            self._save_json(log_path, ops)

    def get_operation(self, op_id: str) -> Optional[dict]:
        clean_id = str(op_id).strip().lstrip("#")
        with self._lock:
            log_path = self.data_dir / "operations_log.json"
            ops = self._load_json(log_path)
            pending = self._load_json(self.pending_path)
            for token, p in pending.items():
                if p.get("operation_id") == clean_id:
                    return {**ops.get(clean_id, {}), **p}
            return ops.get(clean_id)

    def record_pending_payment(
        self,
        token: str,
        method: str,
        tx_id: str,
        user_id: int | str,
        username: str,
        sheet_name: str,
        amount_usdt: float,
        draft_info: dict,
        operation_id: str = "",
        amount_egp: float = 30.0,
    ) -> None:
        with self._lock:
            pending = self._load_json(self.pending_path)
            account_id = self.get_user_account_id(user_id)
            if token in pending and pending[token].get("status") not in {"ready", "rejected"}:
                raise ValueError("Payment request already exists; use its current review")
            if token in pending and str(pending[token].get("user_id")) != str(user_id):
                raise ValueError("Payment belongs to another user")
            clean_tx = tx_id.strip()
            if not clean_tx:
                raise ValueError("Transaction ID is required")
            if any(item.get("tx_id") == clean_tx and item.get("status") != "rejected" for item in pending.values()):
                raise ValueError("This transaction is already assigned to a payment")
            amount_egp = float(money(amount_egp))
            amount_usdt = float(money(amount_usdt))
            previous_attempts = list(pending.get(token, {}).get("previous_attempts", []))
            if pending.get(token, {}).get("status") == "rejected":
                old = pending[token]
                previous_attempts.append({key: old.get(key) for key in ("tx_id", "method", "status", "resolved_at")})
            pending[token] = {
                "previous_attempts": previous_attempts,
                "token": token,
                "operation_id": operation_id,
                "account_id": account_id,
                "method": method,
                "tx_id": tx_id.strip(),
                "user_id": str(user_id),
                "username": username or "",
                "sheet_name": sheet_name,
                "amount_egp": amount_egp,
                "amount_usdt": amount_usdt,
                "status": "pending",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "draft_info": make_json_safe(draft_info),
            }
            self._save_json(self.pending_path, pending)
        if operation_id:
            self.log_operation(
                operation_id,
                op_type=f"payment_{method.lower().replace(' ', '_')}",
                user_id=user_id,
                details={
                    "method": method,
                    "tx_id": tx_id.strip(),
                    "sheet_name": sheet_name,
                    "amount_egp": amount_egp,
                    "amount_usdt": amount_usdt,
                    "status": "pending",
                    "token": token,
                }
            )

    def record_payment_draft(self, token, user_id, draft, *, kind="invoice") -> None:
        """Persist immutable payment metadata for callback retries after restart."""
        snapshot = copy.deepcopy(draft)
        snapshot["path"] = str(snapshot["path"])
        with self._lock:
            pending = self._load_json(self.pending_path)
            previous = pending.get(token)
            if previous and previous.get("status") in {"pending", "approved", "fulfilled"}:
                return
            if previous and str(previous.get("user_id")) != str(user_id):
                raise ValueError("Payment belongs to another user")
            pending[token] = {"token": token, "user_id": str(user_id), "kind": kind,
                              "status": "ready", "created_at": datetime.now(timezone.utc).isoformat(),
                              "draft_info": snapshot}
            self._save_json(self.pending_path, pending)

    def complete_payment_draft(self, token, job_id):
        with self._lock:
            pending = self._load_json(self.pending_path)
            if token in pending:
                pending[token]["status"] = "fulfilled"
                pending[token]["job_id"] = str(job_id)
                self._save_json(self.pending_path, pending)

    def get_pending_payment(self, token: str) -> Optional[dict]:
        with self._lock:
            pending = self._load_json(self.pending_path)
            return pending.get(token)

    def resolve_pending_payment(self, token: str, approved: bool) -> Optional[dict]:
        """Retain approved requests until queue fulfilment is durably recorded."""
        with self._lock:
            pending = self._load_json(self.pending_path)
            item = pending.get(token)
            if not item or item.get("status") not in {"pending", "approved"}:
                return None
            if item.get("status") == "approved" and not approved:
                return None
            item["status"] = "approved" if approved else "rejected"
            item["resolved_at"] = datetime.now(timezone.utc).isoformat()
            self._save_json(self.pending_path, pending)
            return copy.deepcopy(item)

    def mark_payment_fulfilled(self, token: str, job_id: str = "") -> None:
        with self._lock:
            pending = self._load_json(self.pending_path)
            item = pending.get(token)
            if not item or item.get("status") not in {"approved", "fulfilled"}:
                raise ValueError("Payment is not approved")
            item["status"] = "fulfilled"
            item["job_id"] = str(job_id)
            self._save_json(self.pending_path, pending)
            op_id = item.get("operation_id")
            if op_id:
                ops = self._load_json(self.data_dir / "operations_log.json")
                if op_id in ops:
                    ops[op_id]["status"] = "completed"
                    ops[op_id]["job_id"] = str(job_id)
                    self._save_json(self.data_dir / "operations_log.json", ops)


PAYMENT_MGR = PaymentManager()
