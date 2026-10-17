"""CryptoBot (Crypto Pay API) Integration for Zain Checker.
Allows remote customers to pay in USDT/Crypto before checking sheets.
"""
from __future__ import annotations

import json
import logging
import os
import math
import threading
from pathlib import Path
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import urllib.request
from typing import Optional, Dict, Any

logger = logging.getLogger("CryptoPay")


class ExchangeRateUnavailable(RuntimeError):
    """No current observed rate is available for a new payment quote."""

def _configured_token():
    value = os.environ.get("CRYPTO_PAY_TOKEN", "").strip()
    if value:
        return value
    path = Path(__file__).resolve().parent.parent / "crypto_pay_local.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return str(data.get("token", "")).strip()
    except (OSError, ValueError, AttributeError):
        return ""


DEFAULT_CRYPTO_TOKEN = ""


class CryptoPayClient:
    def __init__(self, token: Optional[str] = None, is_testnet: bool = False):
        self.token = _configured_token() if token is None else str(token).strip()
        self.base_url = "https://testnet-pay.crypt.bot/api" if is_testnet else "https://pay.crypt.bot/api"
        self.headers = {
            "Crypto-Pay-API-Token": self.token,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }

    def _call(self, endpoint: str, data: Optional[dict] = None) -> Any:
        if not self.token:
            logger.warning("Crypto Pay token is not configured")
            return None
        url = f"{self.base_url}/{endpoint}"
        try:
            if data is not None:
                payload = json.dumps(data).encode("utf-8")
                req = urllib.request.Request(url, data=payload, headers=self.headers)
            else:
                req = urllib.request.Request(url, headers=self.headers)
            with urllib.request.urlopen(req, timeout=12) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                if isinstance(res, dict) and res.get("ok") is True:
                    return res.get("result")
                logger.warning("CryptoPay provider rejected %s", endpoint)
                return None
        except Exception as e:
            logger.error("CryptoPay request failed on %s (%s)", endpoint, type(e).__name__)
            return None

    def get_me(self) -> Optional[dict]:
        return self._call("getMe")

    def create_invoice(
        self,
        amount: float,
        asset: str = "USDT",
        description: str = "",
        payload: str = "",
        paid_btn_name: Optional[str] = "openBot",
        paid_btn_url: Optional[str] = "https://t.me/ZainCheckerbot",
    ) -> Optional[dict]:
        """Creates an invoice in CryptoBot and returns the payment URLs and invoice_id."""
        try:
            value = Decimal(str(amount))
            if not value.is_finite() or value <= 0:
                return None
            value = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if value <= 0:
                return None
        except (InvalidOperation, ValueError, TypeError):
            return None
        data: Dict[str, Any] = {
            "asset": asset,
            "amount": format(value, ".2f"),
            "description": description[:1024],
            "payload": payload[:1024],
        }
        if paid_btn_name and paid_btn_url:
            data["paid_btn_name"] = paid_btn_name
            data["paid_btn_url"] = paid_btn_url
        return self._call("createInvoice", data)

    def get_invoice(self, invoice_id: int) -> Optional[dict]:
        """Fetches invoice details by invoice_id."""
        try:
            invoice_id = int(invoice_id)
        except (ValueError, TypeError):
            return None
        if invoice_id <= 0:
            return None
        res = self._call("getInvoices", {"invoice_ids": str(invoice_id)})
        if isinstance(res, dict) and isinstance(res.get("items"), list):
            for item in res["items"]:
                if isinstance(item, dict) and str(item.get("invoice_id")) == str(invoice_id):
                    return item
        return None

    def is_invoice_paid(self, invoice_id: int, *, expected_amount=None, expected_payload=None, expected_asset=None) -> bool:
        """Optionally bind a paid invoice to its exact order and charge."""
        inv = self.get_invoice(invoice_id)
        if not inv or inv.get("status") != "paid":
            return False
        if expected_payload is not None and inv.get("payload") != str(expected_payload):
            return False
        if expected_asset is not None and inv.get("asset") != expected_asset:
            return False
        if expected_amount is not None:
            try:
                expected = Decimal(str(expected_amount))
                actual = Decimal(str(inv.get("amount")))
                if not expected.is_finite() or expected <= 0 or not actual.is_finite() or actual != expected:
                    return False
            except (InvalidOperation, ValueError, TypeError):
                return False
        return True

    def delete_invoice(self, invoice_id: int) -> bool:
        """Deletes an invoice."""
        res = self._call("deleteInvoice", {"invoice_id": invoice_id})
        return bool(res)


CRYPTO_PAY = CryptoPayClient()

_LAST_RATE: Optional[float] = None
_LAST_RATE_TIME: float = 0.0
_RATE_LOCK = threading.RLock()


def _fetch_live_usd_egp_rate() -> float:
    """Fetches real-time USD to EGP exchange rate from the internet with caching."""
    global _LAST_RATE, _LAST_RATE_TIME
    import time
    now = time.monotonic()
    # Cache rate for 30 minutes to minimize external calls
    if _LAST_RATE and (now - _LAST_RATE_TIME < 1800):
        return _LAST_RATE

    urls = [
        "https://open.er-api.com/v6/latest/USD",
        "https://api.exchangerate-api.com/v4/latest/USD",
    ]
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"), parse_float=Decimal)
                rate = data.get("rates", {}).get("EGP")
                if (isinstance(rate, (int, float, Decimal)) and not isinstance(rate, bool)
                        and Decimal(str(rate)).is_finite() and rate > 0 and math.isfinite(float(rate))):
                    _LAST_RATE = float(rate)
                    _LAST_RATE_TIME = now
                    logger.info("Updated live USD/EGP rate: %.2f", _LAST_RATE)
                    return _LAST_RATE
        except Exception as e:
            logger.warning("Failed to fetch USD/EGP from %s: %s", url, e)

    raise ExchangeRateUnavailable("Live USD/EGP rate unavailable; no estimated payment rate was substituted")


def get_live_usd_egp_rate() -> float:
    with _RATE_LOCK:
        return _fetch_live_usd_egp_rate()


def calculate_egp_to_usdt(egp_amount: float = 30.0) -> tuple[float, float]:
    """Calculates equivalent USDT amount for given EGP amount.
    Returns: (usdt_amount, live_rate) with usdt_amount rounded to 2 decimal places.
    """
    rate = get_live_usd_egp_rate()
    try:
        amount = Decimal(str(egp_amount))
        if not amount.is_finite() or amount <= 0:
            raise ValueError("Payment amount must be positive")
        usdt = (amount / Decimal(str(rate))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if usdt <= 0:
            raise ValueError("Converted payment is below the minimum precision")
        return float(usdt), rate
    except InvalidOperation as exc:
        raise ValueError("Invalid payment amount") from exc


class BinancePayClient:
    def __init__(self, api_key: Optional[str] = None, secret_key: Optional[str] = None, receiver_id: Optional[int] = None):
        self.api_key = api_key
        self.secret_key = secret_key
        self.receiver_id = receiver_id
        self.base_url = "https://api.binance.com"

    def _ensure_credentials(self) -> bool:
        if not self.api_key or not self.secret_key:
            from .payment_manager import PAYMENT_MGR
            cfg = PAYMENT_MGR.get_config()
            self.api_key = cfg.get("binance_api_key", "").strip()
            self.secret_key = cfg.get("binance_api_secret", "").strip()
            pay_id = cfg.get("binance_pay_id")
            if pay_id:
                try:
                    self.receiver_id = int(pay_id)
                except (ValueError, TypeError):
                    self.receiver_id = None
        return bool(self.api_key and self.secret_key)

    def get_recent_transactions(self, limit: int = 50) -> list:
        if not self._ensure_credentials():
            logger.warning("Binance API credentials are not configured")
            return []
        import hmac
        import hashlib
        import time
        timestamp = int(time.time() * 1000)
        query = f"timestamp={timestamp}&limit={limit}"
        sig = hmac.new(self.secret_key.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
        url = f"{self.base_url}/sapi/v1/pay/transactions?{query}&signature={sig}"
        try:
            req = urllib.request.Request(
                url,
                headers={"X-MBX-APIKEY": self.api_key, "User-Agent": "ZainCheckerBot/1.0"}
            )
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("code") == "000000" and "data" in data:
                    return data["data"]
        except Exception as e:
            # The request URL includes a signature; never put it in a log.
            logger.warning("Binance Pay transactions query failed (%s)", type(e).__name__)
        return []

    def verify_payment(self, query_id: str, min_amount: float = 0.0) -> tuple[bool, str, Optional[dict]]:
        """An unbound receipt cannot authorize a customer's wallet or order.

        This legacy interface intentionally never returns authorization. Use an
        issued invoice, or the reviewed transfer service, for financial actions.
        """
        clean_id = query_id.strip()
        if not clean_id:
            return False, "رقم العملية مطلوب", None
        txs = self.get_recent_transactions(limit=50)
        for tx in txs:
            order_id = str(tx.get("orderId", "")).strip()
            tx_id = str(tx.get("transactionId", "")).strip()
            try:
                amount = float(tx.get("amount", 0.0))
            except (ValueError, TypeError):
                amount = 0.0

            if clean_id in (order_id, tx_id):
                if self.receiver_id:
                    rec_id = tx.get("receiverInfo", {}).get("binanceId")
                    if rec_id and rec_id != self.receiver_id:
                        continue
                if min_amount > 0 and amount < (min_amount - 0.05):
                    return False, f"المبلغ المحول ({amount:.2f} {tx.get('currency', 'USDT')}) أقل من المطلوب ({min_amount:.2f})", tx
                return False, "التحويل موجود، لكن رقم العملية لا يثبت ملكيتك له. استخدم فاتورة مرتبطة بطلبك أو اطلب مراجعة صاحب التحويل.", tx
        return False, "لم يتم العثور على معاملة مطابقة برقم العملية المدخل في Binance Pay.", None


BINANCE_PAY = BinancePayClient()


class BscBlockchainVerifier:
    def __init__(self, deposit_address: Optional[str] = None):
        self._deposit_address = deposit_address
        self.rpc_urls = [
            "https://bsc-dataseed.binance.org/",
            "https://binance.llamarpc.com",
            "https://bsc-dataseed1.defibit.io/",
            "https://bsc-dataseed1.ninicoin.io/"
        ]

    def _get_target_addresses(self) -> list[str]:
        if self._deposit_address:
            return [self._deposit_address.strip()]
        from .payment_manager import PAYMENT_MGR
        cfg = PAYMENT_MGR.get_config()
        addresses = []
        bep = cfg.get("bep20_address")
        if bep:
            addresses.append(str(bep).strip())
        nets = cfg.get("crypto_networks", {})
        for net_info in nets.values():
            if isinstance(net_info, dict):
                n_str = net_info.get("network", "").lower()
                if "bsc" in n_str or "bep20" in n_str:
                    a = net_info.get("address", "").strip()
                    if a and a not in addresses:
                        addresses.append(a)
        legacy = "0x44a69c95e80b4b9f2dd318b62288f2bf287d39b6"
        if legacy not in addresses:
            addresses.append(legacy)
        return addresses

    def _rpc_call(self, method: str, params: list) -> Optional[dict]:
        payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode("utf-8")
        for url in self.rpc_urls:
            try:
                req = urllib.request.Request(
                    url,
                    data=payload,
                    headers={"Content-Type": "application/json", "User-Agent": "ZainCheckerBot/1.0"}
                )
                with urllib.request.urlopen(req, timeout=9) as resp:
                    res = json.loads(resp.read().decode("utf-8"))
                    if "result" in res:
                        return res.get("result")
            except Exception as e:
                logger.debug("BSC RPC call failed on %s: %s", url, e)
        return None

    def verify_transaction(self, tx_hash: str, min_amount: float = 0.0) -> tuple[bool, str, float, str]:
        import re
        clean_tx = tx_hash.strip().lower()
        if not clean_tx.startswith("0x"):
            clean_tx = "0x" + clean_tx
        if not re.match(r"^0x[0-9a-fA-F]{64}$", clean_tx):
            return False, "صيغة هاش المعاملة (TxID) غير صحيحة. يجب أن يتكون من 64 حرفاً ورقماً بعد 0x.", 0.0, ""

        receipt = self._rpc_call("eth_getTransactionReceipt", [clean_tx])
        if not receipt:
            return False, "لم يتم العثور على المعاملة في شبكة BSC بعد (قد تكون قيد المعالجة، انتظر لحظات وحاول مجدداً).", 0.0, ""

        if receipt.get("status") != "0x1":
            return False, "المعاملة فشلت على شبكة البلوكشين (Status != 0x1).", 0.0, ""

        target_addresses = self._get_target_addresses()
        targets_clean = [a.lower().replace("0x", "") for a in target_addresses if a]
        transfer_topic = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

        for log in receipt.get("logs", []):
            topics = [t.lower() for t in log.get("topics", [])]
            if len(topics) >= 3 and topics[0] == transfer_topic:
                to_addr = topics[2]
                if any(tc in to_addr for tc in targets_clean):
                    contract = log.get("address", "").lower()
                    data_hex = log.get("data", "0x0")
                    try:
                        raw_val = int(data_hex, 16)
                    except ValueError:
                        continue
                    val_18 = raw_val / 1e18
                    val_6 = raw_val / 1e6
                    val = val_18 if val_18 >= (min_amount - 0.05) else (val_6 if val_6 >= (min_amount - 0.05) else val_18)
                    if min_amount > 0 and val < (min_amount - 0.05):
                        return False, f"المبلغ المحول ({val:.2f}) أقل من المطلوب ({min_amount:.2f}).", val, contract
                    return True, "تم التحقق من المعاملة بنجاح على البلوكشين!", val, contract

        # Fallback: check for native BNB transfer
        tx_data = self._rpc_call("eth_getTransactionByHash", [clean_tx])
        if tx_data:
            to_addr = str(tx_data.get("to") or "").lower()
            if any(to_addr == a.lower() for a in target_addresses):
                try:
                    val_bnb = int(tx_data.get("value", "0x0"), 16) / 1e18
                except ValueError:
                    val_bnb = 0.0
                if val_bnb > 0:
                    return True, "تم التحقق من تحويل BNB بنجاح على البلوكشين!", val_bnb, "BNB"

        return False, "لم يتم العثور على تحويل إلى عنوان محفظتك في هذه المعاملة.", 0.0, ""


BSC_CHAIN = BscBlockchainVerifier()



