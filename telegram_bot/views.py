"""Native Telegram panels. Pure rendering, with no requests or state changes."""
from __future__ import annotations

from dataclasses import dataclass
from html import escape
import math
import re
from datetime import datetime, timedelta, timezone
from .official_support import support_button


@dataclass(frozen=True)
class Panel:
    text: str
    rows: list[list[dict]]
    photo: str | None = None


def safe(value, limit=110):
    return escape(str("—" if value is None or value == "" else value)[:limit])


def number(value):
    try:
        result = float(value or 0)
        return result if math.isfinite(result) else 0
    except (ValueError, TypeError):
        return 0


def count(value):
    return f"{max(0, int(number(value))):,}"


def button(text, action, style=None, icon_custom_emoji_id=None):
    result = {"text": text, "callback_data": "zc:" + action}
    if style:
        result["style"] = style
    if not icon_custom_emoji_id:
        try:
            from telegram_bot.config import settings
            btn_icons = settings().get("button_icons", {})
            icon_custom_emoji_id = btn_icons.get(action) or btn_icons.get(action.split(":")[0])
        except Exception:
            pass
    if icon_custom_emoji_id:
        result["icon_custom_emoji_id"] = str(icon_custom_emoji_id)
    return result


def copyable_message(text):
    """Inline copyable values preserve line layout and every displayed digit."""
    text=re.sub(r'<(b|strong)>([+-]?\d[\d,]*(?:\.\d+)?)</\1>(\s*(?:جنيه(?: مصري)?|ج\.م|USDT|USDC|ريال(?: سعودي)?|SAR|EGP|USD|\$))(?!\w)',
                lambda m:'<code>'+m[2]+'</code>'+m[3],text)
    money = re.compile(r'(?<![\w.])([+-]?\d[\d,]*(?:\.\d+)?)(\s*)(جنيه(?: مصري)?|ج\.م|USDT|USDC|ريال(?: سعودي)?|SAR|EGP|USD|\$)(?!\w)')
    inline = {'b','strong','i','em','u','ins','s','strike','del','tg-spoiler','span'}
    stack=[];protected=[];output=[];code_reopen=None
    for token in re.split(r'(<[^>]+>)',text):
        if token.startswith('<'):
            match=re.match(r'<(/?)([a-zA-Z-]+)\b',token)
            if match:
                closing,name=match[1],match[2].lower()
                if name=='code' and not closing and not protected:
                    output.append(''.join('</'+tag+'>' for tag,_ in reversed(stack))+'<code>')
                    code_reopen=''.join(tag for _,tag in stack);protected.append('code');continue
                if name=='code' and closing and code_reopen is not None and protected and protected[-1]=='code':
                    output.append('</code>'+code_reopen);code_reopen=None;protected.pop();continue
                if name in {'code','pre','a'}:
                    if closing:
                        if protected and protected[-1]==name:protected.pop()
                    else:protected.append(name)
                elif not protected and name in inline:
                    if closing:
                        if stack and stack[-1][0]==name:stack.pop()
                    else:stack.append((name,token))
            output.append(token)
        elif protected:output.append(token)
        else:
            close=''.join('</'+name+'>' for name,_ in reversed(stack))
            reopen=''.join(tag for _,tag in stack)
            output.append(money.sub(lambda m:close+'<code>'+m[1]+'</code>'+reopen+m[2]+m[3],token))
    return re.sub(r'<(b|strong|i|em|u|ins|s|strike|del)></\1>','', ''.join(output))


def url_button(text, url, style=None, icon_custom_emoji_id=None):
    result = {"text": text, "url": url}
    if style:
        result["style"] = style
    if icon_custom_emoji_id:
        result["icon_custom_emoji_id"] = str(icon_custom_emoji_id)
    return result


def payment_invoice_panel(
    sheet_name: str,
    rows_count: int,
    amount_usdt: float,
    pay_url: str,
    invoice_id: int,
    token: str,
    live_rate: float = 0.0,
    wallet_balance: float = 0.0,
    account_id: int | str = 0,
    operation_id: str = "",
    amount_egp: float = 30.0,
    show_crypto: bool | None = None,
):
    if show_crypto is None:
        try:
            from .config import show_crypto_payments
            show_crypto = show_crypto_payments()
        except Exception:
            show_crypto = False

    id_info = f"\n🆔 رقم المستخدم (User ID): <code>{account_id}</code>" if account_id else ""
    op_info = f"\n🧾 رقم العملية: <code>#{operation_id}</code>" if operation_id else ""

    if show_crypto:
        rate_info = f"\n📈 سعر الصرف الحي: <b>{live_rate:.2f} جنيه / دولار</b>" if live_rate > 0 else ""
        wallet_usdt = round(wallet_balance / live_rate, 2) if (live_rate > 0 and wallet_balance > 0) else 0.0
        usdt_note = f" (~{wallet_usdt:.2f} USDT)" if live_rate > 0 and wallet_balance > 0 else ""
        crypto_equiv_line = f"🪙 المعادل بالعملة الرقمية: <b>{amount_usdt:.2f} USDT / USDC</b>{rate_info}\n"
    else:
        usdt_note = ""
        crypto_equiv_line = ""

    text = (
        f"<b>💳 اختر طريقة الدفع لبدء فحص الشيت</b>\n\n"
        f"📁 الملف: <b>{safe(sheet_name)}</b>\n"
        f"🔢 عدد السجلات: <b>{count(rows_count)}</b>{id_info}{op_info}\n"
        f"💵 الرسوم الأساسية: <b>{amount_egp:.2f} جنيه مصري</b>\n"
        f"{crypto_equiv_line}"
        f"💼 رصيدك في المحفظة: <b>{wallet_balance:.2f} جنيه مصري</b>{usdt_note}\n\n"
        f"<i>اختر إحدى طرق الدفع التالية:</i>"
    )
    rows = [
        [button("🎫 Promo Code", f"pay:promo:{token}")],
        [button(f"💰 الدفع من المحفظة ({wallet_balance:.2f} ج.م)", f"pay:wallet:{token}")],
    ]
    if show_crypto:
        rows.extend([
            [button("🟡 Pay with Binance", f"pay:binance:{token}")],
            [button("🟢 USDT / USDC (BSC - BEP20) ⚡", f"pay:bep20:{token}")],
            [button("🌐 شبكات أخرى (TON / SOL / ERC20)", f"pay:crypto_menu:{token}")],
        ])
        if pay_url:
            rows.append([url_button(f"💎 ادفع المعادل ({amount_usdt:.2f} USDT) في @CryptoBot", pay_url)])
            rows.append([button("🔄 تحقق من دفع CryptoBot وابدأ الفحص 🚀", f"paycheck:{invoice_id}:{token}", "success")])
    rows.append([button("‹ مراجعة الشيت", f"map:{token}:review"), button("‹ الرئيسية", "home")])
    return Panel(text, rows)



def promo_input_panel(sheet_name: str, token: str):
    text = (
        f"<b>🎫 إدخال كود الخصم (Promo Code)</b>\n\n"
        f"📁 الملف: <b>{safe(sheet_name)}</b>\n\n"
        f"أرسل كود الخصم أو الكوبون الترويجي في رسالة نصية الآن.\n"
        f"<i>إذا كان الكود مجانياً سيتم فحص الشيت فوراً، أو سيتم تطبيق نسبة الخصم على الرسوم بالجنيه المصري.</i>\n\n"
        f"💡 <i>الأكواد المتاحة: WELCOME, VIP2026, FREECHECK</i>"
    )
    return Panel(text, [[button("‹ إلغاء ورجوع لطرق الدفع", f"map:{token}:pay")]])


def binance_pay_panel(sheet_name: str, amount_usdt: float, token: str, pay_link: str = "", amount_egp: float = 30.0, live_rate: float = 0.0, binance_pay_id: str = "852195905"):
    rate_str = f" (سعر الصرف: <b>{live_rate:.2f} ج.م/$</b>)" if live_rate > 0 else ""
    id_line = f"🆔 معرف بايننس باي (Pay ID):\n<code>{safe(binance_pay_id)}</code>\n<i>(اضغط على الرقم لنسخه مباشرة في تطبيق Binance)</i>\n\n" if binance_pay_id else ""
    text = (
        f"<b>🟡 الدفع التلقائي عبر Binance Pay</b>\n\n"
        f"📁 الملف: <b>{safe(sheet_name)}</b>\n"
        f"💵 الرسوم الأساسية: <b>{amount_egp:.2f} جنيه مصري</b>\n"
        f"🪙 المعادل المطلوب للدفع: <b>{amount_usdt:.2f} USDT</b>{rate_str}\n\n"
        f"{id_line}"
        f"<b>خطوات الدفع:</b>\n"
        f"1. اضغط الزر أدناه لفتح تطبيق Binance أو حوّل إلى Pay ID أعلاه.\n"
        f"2. حوّل المبلغ المعادل (<b>{amount_usdt:.2f} USDT</b>).\n"
        f"3. بعد إتمام التحويل، اضغط زر '📝 إدخال رقم العملية' وأرسل رقم العملية (Order ID) أو (Transaction ID) ليتم <b>التحقق التلقائي والفوري ⚡</b> وبدء الفحص مباشرة."
    )
    rows = []
    if pay_link:
        rows.append([url_button("📲 فتح تطبيق Binance والدفع فوراً ⚡", pay_link)])
    rows.append([button("📝 إدخال رقم العملية (تحقق فوري ⚡)", f"pay:tx_binance:{token}", "success")])
    rows.append([button("‹ رجوع لطرق الدفع", f"map:{token}:pay")])
    return Panel(text, rows)


def bep20_pay_panel(sheet_name: str, amount_usdt: float, bep20_address: str, token: str, currency: str = "USDT", contract_info: str = "***d580d", amount_egp: float = 30.0, live_rate: float = 0.0):
    rate_str = f"\n📈 سعر الصرف المعتمد: <b>{live_rate:.2f} جنيه / دولار</b>" if live_rate > 0 else ""
    text = (
        f"<b>🟢 الدفع المباشر عبر شبكة BNB Smart Chain (BEP20)</b>\n\n"
        f"📁 الملف: <b>{safe(sheet_name)}</b>\n"
        f"💵 الرسوم الأساسية: <b>{amount_egp:.2f} جنيه مصري</b>\n"
        f"🪙 المعادل المطلوب تحويله: <b>{amount_usdt:.2f} {safe(currency)}</b>{rate_str}\n"
        f"🌐 الشبكة: <b>BSC — BNB Smart Chain (BEP20)</b>\n\n"
        f"📍 عنوان الإيداع:\n"
        f"<code>{safe(bep20_address)}</code>\n"
        f"<i>(اضغط على العنوان لنسخه مباشرة)</i>\n\n"
        f"⚠️ <b>مهم جداً:</b> عند التحويل لهذا العنوان استخدم شبكة <b>BEP20 (BSC)</b> فقط حتى لا تضيع الأموال.\n\n"
        f"بعد إتمام التحويل، اضغط الزر أدناه لإرسال هاش المعاملة (TxID) ليتم <b>التحقق التلقائي والفوري ⚡</b> وبدء فحص الشيت مباشرة."
    )
    return Panel(text, [
        [button("📝 إدخال هاش المعاملة (تحقق فوري ⚡)", f"pay:tx_bep20:{token}", "success")],
        [button("🌐 شبكات دفع أخرى (TON / SOL / ETH)", f"pay:crypto_menu:{token}")],
        [button("‹ رجوع لطرق الدفع", f"map:{token}:pay")]
    ])


def crypto_menu_panel(sheet_name: str, token: str, amount_usdt: float, amount_egp: float = 30.0, live_rate: float = 0.0):
    rate_str = f" (سعر الصرف: <b>{live_rate:.2f} ج.م/$</b>)" if live_rate > 0 else ""
    text = (
        f"<b>🌐 الدفع بالعملات الرقمية (Crypto Networks)</b>\n\n"
        f"📁 الملف: <b>{safe(sheet_name)}</b>\n"
        f"💵 الرسوم الأساسية: <b>{amount_egp:.2f} جنيه مصري</b>\n"
        f"🪙 المعادل المطلوب: <b>{amount_usdt:.2f} USDT / USDC</b>{rate_str}\n\n"
        f"<i>اختر الشبكة والعملة التي ترغب بالتحويل من خلالها:</i>"
    )
    rows = [
        [button("🟢 USDT (BSC — BEP20) ⚡ فوري", f"pay:net:bsc_usdt:{token}")],
        [button("🟢 USDC (BSC — BEP20) ⚡ فوري", f"pay:net:bsc_usdc:{token}")],
        [button("💎 USDT (TON Network)", f"pay:net:ton_usdt:{token}")],
        [button("🟣 USDC (Solana — SOL)", f"pay:net:sol_usdc:{token}")],
        [button("🔷 USDT (Ethereum — ERC20)", f"pay:net:erc20_usdt:{token}")],
        [button("‹ رجوع لطرق الدفع", f"map:{token}:pay")],
    ]
    return Panel(text, rows)


def crypto_network_detail_panel(
    sheet_name: str,
    net_key: str,
    net_info: dict,
    token: str,
    amount_usdt: float,
    amount_egp: float = 30.0,
    live_rate: float = 0.0,
):
    curr = net_info.get("currency", "USDT")
    network_title = net_info.get("network", "Crypto")
    addr = net_info.get("address", "")
    is_case_sensitive = net_info.get("case_sensitive", False)
    auto_verify = net_info.get("auto_verify", False)

    rate_str = f"\n📈 سعر الصرف المعتمد: <b>{live_rate:.2f} جنيه / دولار</b>" if live_rate > 0 else ""

    cs_warning = (
        "\n⚠️ <b>تنبيه هام جداً:</b> عنوان هذه المحفظة <b>حساس لحالة الأحرف (Case-Sensitive)</b> بدقة! "
        "يرجى النقر على العنوان أدناه لنسخه بالكامل كما هو تماماً دون أي تعديل.\n"
        if is_case_sensitive else ""
    )

    auto_note = (
        "⚡ <b>ميزة التحقق التلقائي:</b> عند إرسال هاش العملية (TxID) سيتم <b>التحقق التلقائي والفوري في ثوانٍ</b> وبدء الفحص مباشرة دون انتظار المشرف!\n\n"
        if auto_verify else
        "⏳ سيتم استلام إثبات المعاملة ومراجعته وتأكيد الشيت فوراً.\n\n"
    )

    badge = "💎" if "ton" in net_key else ("🟣" if "sol" in net_key else ("🔷" if "erc20" in net_key else "🟢"))

    text = (
        f"<b>{badge} الدفع المباشر عبر {safe(network_title)}</b>\n\n"
        f"📁 الملف: <b>{safe(sheet_name)}</b>\n"
        f"💵 الرسوم الأساسية: <b>{amount_egp:.2f} جنيه مصري</b>\n"
        f"🪙 المعادل المطلوب: <b>{amount_usdt:.2f} {safe(curr)}</b>{rate_str}\n"
        f"🌐 الشبكة: <b>{safe(network_title)}</b>\n"
        f"🪙 العملة: <b>{safe(curr)}</b>\n\n"
        f"📍 عنوان الإيداع الخاص بنا:\n"
        f"<code>{safe(addr)}</code>\n"
        f"<i>(اضغط على العنوان لنسخه بنقرة واحدة)</i>\n"
        f"{cs_warning}\n"
        f"{auto_note}"
        f"بعد إتمام التحويل، اضغط الزر أدناه لإرسال إثبات المعاملة (TxID أو Signature):"
    )

    action_label = "📝 إدخال هاش المعاملة (تحقق فوري ⚡)" if auto_verify else "📝 إدخال رقم / هاش العملية (TxID)"
    rows = [
        [button(action_label, f"pay:tx_{net_key}:{token}", "success")],
        [button("‹ رجوع لقائمة شبكات الكريبتو", f"pay:crypto_menu:{token}")],
    ]
    return Panel(text, rows)


def tx_prompt_panel(sheet_name: str, method_name: str, token: str):
    text = (
        f"<b>📝 إرسال رقم العملية ({safe(method_name)})</b>\n\n"
        f"📁 الملف: <b>{safe(sheet_name)}</b>\n\n"
        f"أرسل الآن رقم العملية (TxID أو Order ID) في رسالة نصية هنا في الشات.\n"
        f"ستبقى الدفعة بانتظار مراجعة المشرف. تُضاف الورقة إلى الطابور بعد تأكيد الاستلام."
    )
    return Panel(text, [[button("‹ إلغاء ورجوع", f"map:{token}:pay")]])


def wallet_panel(user_id: int | str, balance: float, account_id: int | str = 0, live_rate: float = 0.0, sheet_price_egp: float = 30.0, show_crypto: bool | None = None):
    if show_crypto is None:
        try:
            from .config import show_crypto_payments
            show_crypto = show_crypto_payments()
        except Exception:
            show_crypto = False

    id_val = account_id or user_id
    if show_crypto:
        usdt_equiv = f" (~{balance / live_rate:.2f} USDT)" if (live_rate > 0 and balance > 0) else ""
        sheet_usdt = f" (~{sheet_price_egp / live_rate:.2f} USDT)" if live_rate > 0 else ""
        rate_info = f"\n📈 سعر الصرف الحي: <b>{live_rate:.2f} جنيه / دولار</b>" if live_rate > 0 else ""
    else:
        usdt_equiv = ""
        sheet_usdt = ""
        rate_info = ""

    text = (
        f"<b>💼 محفظتي</b>\n\n"
        f"🆔 رقم المستخدم: <code>{safe(id_val)}</code>\n"
        f"<i>(اضغط على الرقم لنسخه مباشرة)</i>\n\n"
        f"💰 رصيدك الحالي: <b>{balance:.2f} جنيه مصري</b>{usdt_equiv}{rate_info}\n\n"
        f"يمكنك استخدام رصيد محفظتك لفحص أي شيت بضغطة زر واحدة دون الحاجة للدفع في كل مرة.\n"
        f"رسوم فحص الورقة الحالية: <b>{sheet_price_egp:.2f} جنيه مصري</b>{sheet_usdt}."
    )
    if show_crypto:
        rows = [
            [button("شحن المحفظة", "wallet:topup_menu", "success"), button("كود رصيد أو عرض", "wallet:redeem")],
            [button('سجل المحفظة','oh:ledger:0'),button('طلباتي ومدفوعاتي','oh:list:0')],
            [button("🏠 الرئيسية (Home)", "home")]
        ]
    else:
        rows = [
            [button("🎁 إدخال كود رصيد أو شحن", "wallet:redeem", "success")],
            [button('سجل المحفظة','oh:ledger:0'),button('طلباتي ومدفوعاتي','oh:list:0')],
            [button("🏠 الرئيسية (Home)", "home")]
        ]
    return Panel(text, rows)


def wallet_topup_menu_panel(balance: float, account_id: int | str = 0, live_rate: float = 0.0, show_crypto: bool | None = None):
    if show_crypto is None:
        try:
            from .config import show_crypto_payments
            show_crypto = show_crypto_payments()
        except Exception:
            show_crypto = False

    if not show_crypto:
        text = (
            f"<b>💳 شحن رصيد المحفظة</b>\n\n"
            f"🆔 رقم المستخدم: <code>{safe(account_id)}</code>\n"
            f"💰 رصيدك الحالي: <b>{balance:.2f} جنيه مصري</b>\n\n"
            f"لشحن رصيد محفظتك، يرجى إدخال <b>كود رصيد (Redeem Code)</b> أو التواصل مع الإدارة للشحن المباشر.\n"
        )
        rows = [
            [button("🎁 إدخال كود رصيد أو شحن", "wallet:redeem", "primary")],
            [button("‹ رجوع للمحفظة", "wallet")],
        ]
        return Panel(text, rows)

    rate_str = f"\n📈 سعر الصرف الحي: <b>{live_rate:.2f} جنيه / دولار</b>" if live_rate > 0 else ""
    text = (
        f"<b>⚡ شحن رصيد المحفظة بالعملات الرقمية</b>\n\n"
        f"🆔 رقم المستخدم: <code>{safe(account_id)}</code>\n"
        f"💰 رصيدك الحالي: <b>{balance:.2f} جنيه مصري</b>{rate_str}\n\n"

        f"<i>يمكنك شحن المحفظة بأي مبلغ ترغب به (مثلاً 5$ أو 10$ أو 20$ أو أكثر).</i>\n"
        f"<i>يتم تحويل المبلغ تلقائياً بسعر الصرف وإضافته فوراً إلى رصيد محفظتك 🚀</i>\n\n"
        f"اختر وسيلة الدفع المناسبة لك:"
    )
    rows = [
        [button("🟡 Binance Pay (شحن فوري ⚡)", "wallet:topup_net:binance", "primary")],
        [button("🟢 USDT / USDC (BSC - BEP20) ⚡ فوري", "wallet:topup_net:bsc", "primary")],
        [button("💎 USDT (TON Network)", "wallet:topup_net:ton_usdt")],
        [button("🟣 USDC (Solana — SOL)", "wallet:topup_net:sol_usdc")],
        [button("🔷 USDT (Ethereum — ERC20)", "wallet:topup_net:erc20_usdt")],
        [button("‹ رجوع للمحفظة", "wallet")],
    ]
    return Panel(text, rows)



def wallet_topup_detail_panel(
    method_key: str,
    net_info: dict,
    account_id: int | str = 0,
    live_rate: float = 0.0,
    binance_pay_id: str = "852195905",
    binance_pay_link: str = "",
):
    if method_key == "binance":
        id_line = f"🆔 معرف بايننس باي (Pay ID):\n<code>{safe(binance_pay_id)}</code>\n<i>(اضغط على الرقم لنسخه مباشرة في تطبيق Binance)</i>\n\n" if binance_pay_id else ""
        text = (
            f"<b>🟡 شحن المحفظة عبر Binance Pay</b>\n\n"
            f"🆔 رقم حسابك: <code>{safe(account_id)}</code>\n"
            f"📈 سعر الصرف المعتمد: <b>{live_rate:.2f} جنيه مصري لكل 1 USDT</b>\n\n"
            f"{id_line}"
            f"<b>خطوات الشحن:</b>\n"
            f"1. حوّل أي مبلغ ترغب به إلى Pay ID أعلاه أو عبر الزر أدناه.\n"
            f"2. سيتم احتساب المبلغ بالجنيه المصري تلقائياً فور التحقق وإضافته لمحفظتك.\n"
            f"3. بعد التحويل، اضغط زر '📝 إدخال رقم العملية' وأرسل رقم العملية (Order ID أو TxID) ليتم <b>الشحن التلقائي والفوري ⚡</b>."
        )
        rows = []
        if binance_pay_link:
            rows.append([url_button("📲 فتح تطبيق Binance والدفع فوراً ⚡", binance_pay_link)])
        rows.append([button("📝 إدخال رقم العملية (شحن فوري ⚡)", "wallet:topup_tx:binance", "success")])
        rows.append([button("‹ رجوع لقائمة الشحن", "wallet:topup_menu")])
        return Panel(text, rows)

    elif method_key == "bsc":
        addr = net_info.get("address", "0xa5f435e52a8752816569a11f0c8660aa877faa72")
        text = (
            f"<b>🟢 شحن المحفظة عبر BNB Smart Chain (BEP20)</b>\n\n"
            f"🆔 رقم حسابك: <code>{safe(account_id)}</code>\n"
            f"🌐 الشبكة: <b>BSC — BNB Smart Chain (BEP20)</b>\n"
            f"🪙 العملات المقبولة: <b>USDT أو USDC</b>\n"
            f"📈 سعر الصرف: <b>{live_rate:.2f} جنيه لكل 1 دولار</b>\n\n"
            f"📍 عنوان الإيداع الخاص بنا:\n"
            f"<code>{safe(addr)}</code>\n"
            f"<i>(اضغط على العنوان لنسخه بنقرة واحدة)</i>\n\n"
            f"⚠️ <b>مهم:</b> يرجى إرسال التحويل عبر شبكة <b>BEP20 (BSC)</b> فقط.\n"
            f"⚡ <b>ميزة الشحن الفوري:</b> بعد التحويل، اضغط الزر أدناه وأرسل هاش المعاملة (TxID) ليتم <b>التحقق التلقائي على البلوكشين وإيداع الرصيد فوراً ⚡</b>."
        )
        return Panel(text, [
            [button("📝 إدخال هاش المعاملة (شحن فوري ⚡)", "wallet:topup_tx:bsc", "success")],
            [button("‹ رجوع لقائمة الشحن", "wallet:topup_menu")]
        ])

    else:
        network_title = net_info.get("network", "Crypto")
        curr = net_info.get("currency", "USDT")
        addr = net_info.get("address", "")
        is_case_sensitive = net_info.get("case_sensitive", False)
        badge = "💎" if "ton" in method_key else ("🟣" if "sol" in method_key else "🔷")
        cs_warning = (
            "\n⚠️ <b>تنبيه هام جداً:</b> عنوان هذه المحفظة <b>حساس لحالة الأحرف (Case-Sensitive)</b> بدقة! "
            "يرجى النقر على العنوان لنسخه كاملاً دون أي تعديل.\n"
            if is_case_sensitive else ""
        )
        text = (
            f"<b>{badge} شحن المحفظة عبر {safe(network_title)}</b>\n\n"
            f"🆔 رقم حسابك: <code>{safe(account_id)}</code>\n"
            f"🌐 الشبكة: <b>{safe(network_title)}</b>\n"
            f"🪙 العملة: <b>{safe(curr)}</b>\n"
            f"📈 سعر الصرف: <b>{live_rate:.2f} ج.م / $</b>\n\n"
            f"📍 عنوان الإيداع الخاص بنا:\n"
            f"<code>{safe(addr)}</code>\n"
            f"<i>(اضغط على العنوان لنسخه بنقرة واحدة)</i>\n"
            f"{cs_warning}\n"
            f"بعد إتمام التحويل، اضغط الزر أدناه وأرسل هاش المعاملة (TxID / Signature) ليتم مراجعته وإضافة الرصيد إلى محفظتك فوراً."
        )
        return Panel(text, [
            [button("📝 إدخال إثبات المعاملة (TxID)", f"wallet:topup_tx:{method_key}", "success")],
            [button("‹ رجوع لقائمة الشحن", "wallet:topup_menu")]
        ])


def wallet_topup_tx_prompt_panel(method_name: str, method_key: str):
    auto_text = (
        "⚡ <b>تحقق فوري:</b> يتم فحص العملية وإضافة الرصيد تلقائياً في ثوانٍ معدودة!"
        if method_key in ("binance", "bsc", "bsc_usdt", "bsc_usdc")
        else "⏳ سيتم استلام الإثبات ومراجعته واعتماد الرصيد فوراً."
    )
    text = (
        f"<b>📝 إرسال إثبات شحن المحفظة ({safe(method_name)})</b>\n\n"
        f"أرسل الآن رقم العملية أو هاش المعاملة (TxID أو Order ID) في رسالة نصية هنا.\n\n"
        f"{auto_text}"
    )
    return Panel(text, [[button("‹ إلغاء ورجوع", "wallet:topup_menu")]])


def wallet_topup_success_panel(
    amount_usdt: float,
    amount_egp: float,
    new_balance: float,
    tx_id: str,
    method: str,
    live_rate: float = 0.0,
    account_id: int | str = 0,
):
    rate_info = f"\n📈 سعر الصرف المعتمد: <b>{live_rate:.2f} ج.م/$</b>" if live_rate > 0 else ""
    id_info = f"\n🆔 رقم الحساب: <code>{account_id}</code>\n" if account_id else ""
    text = (
        f"<b>🎉 تم شحن رصيد محفظتك بنجاح ⚡</b>\n\n"
        f"{id_info}"
        f"💳 طريقة الدفع: <b>{safe(method)}</b>\n"
        f"🪙 المبلغ المستلم: <b>{amount_usdt:.2f} USDT/USDC</b>{rate_info}\n"
        f"💵 الرصيد المضاف: <b>+{amount_egp:.2f} جنيه مصري</b>\n"
        f"💼 رصيدك الإجمالي الجديد: <b>{new_balance:.2f} جنيه مصري</b>\n"
        f"🔗 رقم العملية: <code>{safe(tx_id)}</code>\n\n"
        f"✅ يمكنك الآن فحص الشيتات بضغطة زر واحدة من رصيد المحفظة 🚀"
    )
    return Panel(text, [
        [button("📎 فحص شيت جديد", "upload", "primary")],
        [button("💼 عرض المحفظة", "wallet")],
        [button("‹ الرئيسية", "home")],
    ])


def admin_topup_approval_panel(
    user_id: str,
    username: str,
    method: str,
    tx_id: str,
    amount_usdt: float,
    amount_egp: float,
    topup_id: str,
    account_id: int | str = 0,
):
    user_str = f"ID: <code>{user_id}</code>"
    if account_id:
        user_str = f"Account ID: <code>{account_id}</code> (Telegram: <code>{user_id}</code>)"
    text = (
        f"<b>🔔 طلب شحن محفظة جديد بانتظار التأكيد!</b>\n\n"
        f"👤 العميل: {user_str} (@{safe(username)})\n"
        f"💳 وسيلة الشحن: <b>{safe(method)}</b>\n"
        f"🪙 المبلغ المحول: <b>{amount_usdt:.2f} USDT/USDC</b> (~{amount_egp:.2f} ج.م)\n"
        f"🧾 رقم المعاملة (TxID): <code>{safe(tx_id, 256)}</code>\n\n"
        f"تحقق من استلام المبلغ في محفظتك، ثم اضغط زر الاعتماد لشحن محفظة العميل فوراً."
    )
    return Panel(text, [
        [button("✓ اعتماد وشحن المحفظة", f"admpay:approve_topup:{topup_id}", "success"),
         button("❌ رفض الشحن", f"admpay:reject_topup:{topup_id}", "danger")]
    ])


def redeem_input_panel():
    text = (
        f"<b>🎁 شحن المحفظة عبر كود (Redeem Code)</b>\n\n"
        f"أرسل كود الشحن في رسالة نصية الآن.\n"
        f"سيتم إضافة الرصيد بالجنيه المصري إلى محفظتك فوراً بمجرد إدخال كود صالح ⚡"
    )
    return Panel(text, [
        [button("‹ إلغاء ورجوع للمحفظة", "wallet")]
    ])


def redeem_success_panel(added_amount: float, new_balance: float, account_id: int | str = 0, live_rate: float = 0.0):
    id_info = f"🆔 رقم المستخدم (User ID): <code>{account_id}</code>\n" if account_id else ""
    added_usdt = f" (~{added_amount / live_rate:.2f} USDT)" if (live_rate > 0 and added_amount > 0) else ""
    new_usdt = f" (~{new_balance / live_rate:.2f} USDT)" if (live_rate > 0 and new_balance > 0) else ""
    text = (
        f"<b>🎉 تم شحن المحفظة بنجاح!</b>\n\n"
        f"{id_info}"
        f"➕ المبلغ المضاف: <b>+{added_amount:.2f} جنيه مصري</b>{added_usdt}\n"
        f"💼 رصيدك الإجمالي الآن: <b>{new_balance:.2f} جنيه مصري</b>{new_usdt}\n\n"
        f"يمكنك الآن استخدام رصيدك لفحص أي ملف بضغطة زر 🚀"
    )
    return Panel(text, [
        [button("📎 رفع ملف جديد", "upload"), button("💼 محفظتي", "wallet")],
        [button("‹ الرئيسية", "home")]
    ])


def admin_approval_panel(user_id: str, username: str, sheet_name: str, rows_count: int, method: str, tx_id: str, amount_usdt: float, token: str, account_id: int | str = 0, operation_id: str = "", amount_egp: float = 30.0):
    user_str = f"<code>{safe(user_id)}</code>"
    if account_id:
        user_str += f" (رقم المستخدم: <code>{safe(account_id)}</code>)"
    op_line = f"🧾 رقم العملية: <code>#{safe(operation_id)}</code>\n" if operation_id else ""
    text = (
        f"<b>🔔 إشعار دفعة جديدة بانتظار التأكيد!</b>\n\n"
        f"{op_line}"
        f"👤 العميل: {user_str} (@{safe(username)})\n"
        f"📁 الملف: <b>{safe(sheet_name)}</b> ({count(rows_count)} سجل)\n"
        f"💳 طريقة الدفع: <b>{safe(method)}</b>\n"
        f"💵 الرسوم الأساسية: <b>{amount_egp:.2f} جنيه مصري</b>\n"
        f"🪙 المعادل المحوّل: <b>{amount_usdt:.2f} USDT/USDC</b>\n"
        f"🧾 رقم المعاملة (TxID): <code>{safe(tx_id, 256)}</code>\n\n"
        f"تحقق من استلام المبلغ ورقم المعاملة، ثم أكد الدفعة لإضافة الورقة إلى الطابور. بدء الجولة إجراء مستقل."
    )
    return Panel(text, [
        [button("✓ تأكيد الدفعة وإضافة للطابور", f"admpay:approve:{token}", "success"),
         button("❌ رفض الدفعة", f"admpay:reject:{token}", "danger")]
    ])



def nav():
    return [button("‹ الرئيسية", "home"), button("📊 الحالة", "status")]


def state_label(state):
    if state.get('engine_online') is False:return '⚠ محرك الفحص غير متصل؛ الحالة غير مؤكدة'
    if state.get('stale') is True:return '◷ جارٍ تحديث الحالة؛ آخر بيانات محفوظة'
    if state.get("verifications"):
        return "🟠 بانتظار تحقق زين"
    if state.get("running"):
        return "⏸ متوقف مؤقتًا" if state.get("paused") else "🟢 الفحص جارٍ"
    if state.get("finalizing"):
        return "◷ جارٍ حفظ النتائج"
    kpis = state.get("kpis", {})
    total = number(kpis.get("total"))
    if total:
        return "✓ انتهت المعالجة" if number(kpis.get("completed")) >= total else "⚪ الجلسة غير نشطة"
    return "⚪ جاهز لفحصك"


def controls(state):
    if state.get('engine_online') is False or state.get('stale') is True:return []
    rows = []
    if state.get("running") and not state.get("verifications"):
        rows.append([button("▶ استئناف الفحص", "resume", "success") if state.get("paused")
                     else button("⏸ إيقاف مؤقت", "pause")])
    kpis = state.get("kpis", {})
    if (not state.get("running") and not state.get("round_pending") and not state.get("finalizing")
            and state.get("result_ready") is not False and number(kpis.get("total")) > 0
            and number(kpis.get("completed")) >= number(kpis.get("total"))):
        rows.append([button("📥 استلام نتائج الجولة", "results", "primary")])
    if number(state.get("kpis", {}).get("errors")) + number(state.get("kpis", {}).get("needs_review")):
        rows.append([button("🛠 إصلاح الأخطاء / إعادة الفحص", "repair")])
    return rows


def admin_menu(section):
    sections={
        'money':('💳 الطلبات والمال','متابعة طلبات العملاء، والتحصيل، والأرباح.',[
            [button('🧾 طلبات العملاء','oh:admin:0'),button('💳 مراجعة الدفع','payment_reviews')],
            [button('📊 الأرباح','pf:admin:0'),button('💸 السحوبات','withdrawals')],
            [button('👥 تقارير العملاء','menu:customers'),support_button()],
            [button('📚 سجل الدعم السابق','support_inbox')]]),
        'customers':('👥 تقارير العملاء','شحن الأكواد والاستفادة الفعلية من الفحص المجاني؛ دون الشركاء والمديرين.',[
            [button('💳 العملاء الذين شحنوا بالأكواد','cr:list:codes:0')],
            [button('🎁 العملاء المستفيدون من المجاني','cr:list:free:0')],
            [button('‹ الطلبات والمال','menu:money')]]),
        'partners':('🤝 الشركاء','الشراكات الحالية والسابقة ومراجعة ملفات الشركاء.',[
            [button('🤝 الشركاء الحاليون','cp:list:0'),button('🕘 الشركاء السابقون','pl:ended:0')],
            [button('📋 موافقات الشيت','ps:list:0')]]),
        'offers':('🎁 العروض والأكواد','إدارة العروض وحدودها وأكواد الرصيد المدفوع.',[
            [button('🎁 العروض','campaigns'),button('🎟 أكواد الرصيد','cash_codes')],
            [button('⚖️ حدود العروض','oa:limits:0')]]),
        'settings':('⚙️ الإعدادات','الأسعار وصلاحيات الإدارة.',[
            [button('💰 أسعار العملاء','prices'),button('📄 سعر شيت الشريك','partner_sheet_settings')],
            [button('🛡 المديرون والصلاحيات','admin_roles')]])}
    title,description,rows=sections[section]
    return Panel('<b>'+title+'</b>\n\n'+description,rows+[[button('‹ الرئيسية','home')]])


def operations(state, jobs):
    state = dict(state)
    state['round_pending'] = any(job.get('status') in {'pending','active'} for job in jobs)
    rows = [[button('📊 الحالة','status'),button('📁 إدارة الطابور','queue:0')]]
    rows.extend(controls(state))
    if (state.get('engine_online') is not False and not state.get('stale') and not state.get('running')
            and not state.get('finalizing') and not any(job.get('status') == 'active' for job in jobs)
            and any(job.get('status') == 'pending' for job in jobs)):
        rows.append([button('▶ بدء الملفات المنتظرة','start_queue','success')])
    rows.extend([[button('📋 ملفات المراجعة','drafts:0'),button('📎 رفع شيت','upload')],
                 [button('🗂 الأرشيف','archive'),button('❔ المساعدة','help')],
                 [button('↻ تحديث','menu:operations'),button('‹ الرئيسية','home')]])
    return Panel('<b>⚙️ إدارة التشغيل</b>\n\n'+state_label(state)+
                 '\nاختر الحالة أو ملفًا من الطابور لمراجعته. إجراءات التشغيل تعرض تأكيدًا قبل التنفيذ.',rows)


def partner_home_rows():
    return [button('💰 أرباحي','pf:home:0'),button('🎁 حملاتي','po:list:0'),button('📄 فحص الشركاء','pc:home:0')]


def home(state, jobs, account_id: int | str = 0, is_admin: bool = False, mode: str = "customer", wallet_balance: float = 0.0):
    id_line = f"🆔 رقم المستخدم: <code>{account_id}</code>\n" if account_id else ""
    bal_line = f"💼 رصيدك في المحفظة: <b>{wallet_balance:.2f} جنيه مصري</b>\n\n" if wallet_balance >= 0 else "\n"


    if mode == "admin" and is_admin:
        pending = sum(job.get("status") == "pending" for job in jobs)
        text = (
            f"<b>🛡 لوحة الإدارة</b>\n\n"
            f"{id_line}"
            f"{state_label(state)}\nالملف النشط: <b>{safe(state.get('workbook') or 'لا يوجد ملف حالي')}</b>\n"
            f"ملفات في الانتظار: {count(pending)}"
        )
        if state.get('engine_online') is False:
            text = '<b>🛡 لوحة الإدارة</b>\n\nمحرك الفحص غير متصل مؤقتًا.\nلا نستطيع تأكيد حالة التنفيذ أو عدد الملفات الآن. الطلبات محفوظة؛ لا تُعد الدفع.'
        quick=[item for row in controls(state) for item in row]
        labels={'results':'📥 نتائج الجولة','repair':'🛠 إعادة الفحص'}
        quick=[{**item,'text':labels.get(item['callback_data'][3:],item['text'])} for item in quick]
        return Panel(text, [
            [button('📎 رفع شيت','upload','success'),button('📊 المتابعة','status'),button('📁 الطابور','queue:0')],
            *[quick[i:i+2] for i in range(0,len(quick),2)],
            [button('💳 الطلبات والمال','menu:money'),button('🤝 الشركاء','menu:partners')],
            [button('🎁 العروض والأكواد','menu:offers'),button('⚙️ الإعدادات','menu:settings')],
            [button('⚙️ إدارة التشغيل','menu:operations'),button('🗂 الأرشيف','archive')],
            [button('💼 محفظتي','wallet'),button('👤 وضع الزبون','switch_mode:customer')],
            [support_button()],
        ])

    # Default / Customer Mode:
    text = (
        f"<b>تشيك.</b>\n<i>فحص وتدقيق شيتات زين بدقة وسرعة فائقة 🚀</i>\n\n"
        f"{id_line}"
        f"{bal_line}"
        'ارفع ملفك، راجع الرسوم، وتابع النتيجة من طلباتك.'
    )
    rows = [
        [button('📎 رفع ملف للفحص','upload','success')],
        [button('📋 ملفاتي للمراجعة','drafts:0'),button('🧾 طلباتي ومدفوعاتي','oh:list:0')],
        [button('💼 محفظتي','wallet'),button('📦 نتائجي السابقة','archive')],
        [button('📊 حالة فحوصاتي','status'),button('❔ المساعدة','help')],
        [support_button()],
    ]
    if is_admin:
        rows.append([button('🛡 العودة للإدارة','switch_mode:admin')])

    return Panel(text, rows)


def status(state):
    k = state.get("kpis", {})
    total, done = number(k.get("total")), number(k.get("completed"))
    percent = min(100, max(0, int(done / total * 100))) if total else 0
    filled = percent // 10
    bar = "▰" * filled + "▱" * (10 - filled)
    text = (f"<b>📊 متابعة الفحص</b>\n{state_label(state)}\n\n"
            f"<b>{safe(state.get('workbook') or 'لا يوجد ملف حالي')}</b>\n"
            f"<code>{bar}  {percent}%</code>\n"
            f"تمت معالجتها: {count(done)} من {count(total)}\n"
            f"متبقية: {count(k.get('remaining'))}\n\n"
            f"أرصدة مؤكدة: {count(k.get('verified'))}\n"
            f"مطابقات تامة: {count(k.get('matches'))}\n"
            f"فروقات الرصيد: {count(k.get('mismatches'))}\n"
            f"إجمالي الفروقات (الموجبة): {number(k.get('mismatch_total')):,.2f} ر.س\n"
            f"تحتاج مراجعة / غير موجودة: {count(number(k.get('needs_review')) + number(k.get('not_found')))}\n"
            f"مهلات وأخطاء: {count(k.get('errors'))}\n"
            f"مؤجلة: {count(k.get('deferred'))}\n\n"
            "<i>نسبة المعالجة مستقلة عن عدد الأرصدة المؤكدة.</i>")
    timing = state.get("session_timing") or {}
    started, server_time = number(timing.get("started_at")), number(timing.get("server_time"))
    if started > 0 and server_time >= started:
        elapsed = int(server_time - started)
        hours, rest = divmod(elapsed, 3600)
        minutes, seconds = divmod(rest, 60)
        try:
            began = datetime.fromtimestamp(started, timezone(timedelta(hours=3))).strftime("%H:%M:%S")
            text += f"\n\n⏱ المنقضي منذ البدء: <b>{hours:02}:{minutes:02}:{seconds:02}</b>\nبدأت: {began} · توقيت بغداد"
        except (ValueError, OverflowError, OSError):
            pass
    if state.get("verifications"):
        text += "\n\nأكمل تحقق زين في Chrome أو لوحة التحكم، ثم حدّث الحالة."
    return Panel(text, [[button("↻ تحديث الحالة", "status", "primary")], *controls(state),
                        [button("📁 الطابور", "queue:0"), button("‹ الرئيسية", "home")]])


def queue(jobs, page=0):
    size = 5
    pages = max(1, math.ceil(len(jobs) / size))
    page = min(max(0, page), pages - 1)
    labels = {"pending":"◷ في الانتظار", "active":"🟢 قيد المعالجة", "completed":"✓ انتهت المعالجة",
              "failed":"⚠ يحتاج متابعة", "interrupted":"⏸ متوقف"}
    text = f"<b>📁 طابور الملفات</b>\n{count(len(jobs))} ملف · صفحة {page+1} من {pages}\n"
    if not jobs:
        text += "\nالطابور فارغ. أرسل ملف XLSX لإضافته."
    for index, job in enumerate(jobs[page*size:(page+1)*size], page*size+1):
        text += (f"\n<b>{index}. {safe(job.get('filename'))}</b>\n"
                 f"{safe(job.get('sheet_name'), 65)} · {labels.get(job.get('status'), 'حالة غير معروفة')}\n"
                 f"معالجة: {count(job.get('completed'))} / {count(job.get('total_records'))}\n")
    rows = []
    for index, job in enumerate(jobs[page*size:(page+1)*size], page*size+1):
        if job.get("id"):
            rows.append([button(f"إدارة الشيت {index}", f"job:{job['id']}")])
    pagination = []
    if page > 0:
        pagination.append(button("‹ السابق", f"queue:{page-1}"))
    if page+1 < pages:
        pagination.append(button("التالي ›", f"queue:{page+1}"))
    if pagination:
        rows.append(pagination)
    if any(job.get("status") == "pending" for job in jobs):
        rows.append([button("▶ بدء الجولة", "start_queue", "success")])
    rows += [[button("↻ تحديث الطابور", f"queue:{page}"), button("📎 إضافة ملف", "upload")], nav()]
    return Panel(text, rows)


def customer_home(account_id=0):
    return Panel("<b>تشيك · فحص ملفات زين</b>\n\n"
                 f"رقم حسابك: <code>{safe(account_id)}</code>\n\n"
                 "① أرسل ملف XLSX\n② راجع الأعمدة والبيانات\n③ اختر الدفع ثم انتظر تأكيد المعالجة\n\n"
                 "ملفات المراجعة ومحفظتك تخص هذه المحادثة.",
                 [[button("📎 إرسال ملف XLSX", "upload", "primary")],
                  [button("📋 ملفاتي للمراجعة", "drafts:0"), button("💼 محفظتي", "wallet")],
                  [button('استرجاع نتيجة سابقة','archive')],
                  [button("💡 خطوات الاستخدام", "help")]])


def customer_rows(rows):
    """Keep customer navigation usable when a shared notice contains admin links."""
    allowed = {"home", "upload", "help", "wallet", "archive", "status", "subscription:check"}
    prefixes = ("wallet:", "drafts:", "map:", "pay:", "paycheck:", "switch_mode:","co:","cw:","bn:","pc:","pf:","po:","of:","cc:","ar:result:","sp:history:","sp:compose:","oh:list:","oh:open:","oh:ledger:","oh:archive:","oh:repeat:","oh:stop:","oh:stopconfirm:","oh:consent:","oh:clist:","oh:copen:","oh:cstop:","oh:cconsent:")
    prefixes = prefixes + ('pl:view:','pl:ack:','vc:start:','vc:list:','vc:open:','vc:phone:','vc:complaint:','panel:')
    result = []
    for row in rows:
        kept = []
        for item in row:
            action = str(item.get("callback_data", ""))
            if item.get("url") or (action.startswith("zc:") and
                                   not action[3:].startswith(('pf:admin:','pf:partner:')) and
                                   (action[3:] in allowed or action[3:].startswith(prefixes))):
                kept.append(item)
        if kept:
            result.append(kept)
    if not result:
        result.append([button("‹ الرئيسية", "home")])
    return result


def upload(admin=True):
    if not admin:
        return Panel("<b>📎 إضافة ملف · ① الإرسال</b>\n\n"
                     "أرسل ملف العملاء بصيغة XLSX كمرفق في هذه المحادثة.\n\n"
                     "سنفتح مراجعة الأعمدة أولًا. اختر الرقم والمبلغ، وصحح أي صف غير صالح، ثم افتح الرسوم وطرق الدفع.\n"
                     "لا يبدأ الفحص عند رفع الملف؛ تُضاف الورقة بعد تأكيد الدفع.",
                     [[button("📋 ملفاتي للمراجعة", "drafts:0"), button("‹ الرئيسية", "home")]])
    return Panel("<b>📎 أضف ملف العملاء</b>\n\n"
                 "<b>① أرسل الملف</b>\nاضغط المرفق واختر ملف الإكسل بصيغة XLSX.\n\n"
                 "<b>② نقرأ الأعمدة</b>\nنتحقق من رقم الحساب أو الخدمة والمبلغ المطلوب مراجعته. اسم العميل اختياري.\n\n"
                 "<b>③ تحقق ثم ابدأ</b>\nإذا لم نعرف البيانات، اختر عمود الرقم وعمود المبلغ. أضف الشيت إلى الطابور، وكرر ذلك للملفات الأخرى، ثم اضغط بدء الجولة. النتائج بعد اكتمال الفحص.",
                 [[button("ملفات تحتاج مراجعة", "drafts:0"), button("📁 عرض الطابور", "queue:0")], nav()])


def help_panel(admin=True):
    if not admin:
        return Panel("<b>💡 خطوات فحص ملفك</b>\n\n"
                     "① <b>إرسال الملف</b> · أرسل XLSX كمرفق.\n"
                     "② <b>مراجعة الأعمدة</b> · اختر عمود الرقم وعمود المبلغ؛ الصفر مبلغ صالح والخانة الفارغة تحتاج تصحيحًا.\n"
                     "③ <b>الدفع</b> · افتح الرسوم وطرق الدفع بعد نجاح التحقق.\n\n"
                     "يمكنك الرجوع إلى ملفاتك للمراجعة أو محفظتك من الرئيسية.",
                     [[button("📎 إرسال ملف", "upload", "primary"), button("‹ الرئيسية", "home")],[support_button()]])
    return Panel("<b>💡 استخدام تشيك</b>\n\n"
                 "<b>متابعة الفحص</b>\nالتقدم، الأرصدة المؤكدة، الفروقات، وحالات المراجعة في بطاقة واحدة. اضغط تحديث لقراءة الحالة الحالية.\n\n"
                 "<b>التحكم في الجلسة</b>\nأزرار الإيقاف والاستئناف تظهر عند وجود جلسة. إذا طلب زين تحققًا، أكمله في المتصفح أو اللوحة.\n\n"
                 "<b>النتائج</b>\nاضغط استلام النتائج، ثم أكد إرسال ملف الجلسة إلى هذه المحادثة.\n\n"
                 "<b>اختصارات</b>\n/start · /status · /queue · /results · /help",
                 [[button("📎 إضافة ملف", "upload")],[support_button()], nav()])


def notice(title, text, html=False):
    has_html_tags = any(tag in text for tag in ("<b>", "<code>", "<i>", "<s>", "<u>", "<pre>", "</a>", "</b>", "</code>"))
    if html or has_html_tags:
        body = text[:2000]
    else:
        body = safe(text, 1400)
    return Panel(f"<b>{safe(title)}</b>\n\n{body}", [nav()])


def confirmation(action, workbook, token):
    title, body, accept = {
        "pause": ("⏸ إيقاف مؤقت", "سنوقف استلام مهام جديدة؛ قد يكمل العامل الفحص الجاري.", "إيقاف مؤقت"),
        "resume": ("▶ استئناف الفحص", "سنكمل المهام المتبقية مع الاحتفاظ بالتقدم السابق.", "استئناف"),
        "results": ("📥 استلام نتائج الجولة", "سنرسل نتائج الجولة المكتملة إلى هذه المحادثة الخاصة. تتضمن الأرقام والأرصدة التي فُحصت.", "إرسال النتائج"),
        "repair": ("🛠 إعادة فحص الأخطاء", "سنُعيد فحص الأخطاء والحالات التي تحتاج مراجعة في هذه الجلسة. قد يستأنف ذلك الفحص المتوقف ويتصل بزين؛ لا يغيّر الخلايا الناقصة في ملفك.", "إعادة فحص الأخطاء"),
        "remove": ("إخراج الشيت من الطابور", "سيُزال إدخال الشيت من الطابور. ملف الإكسل ونتائجه يبقيان محفوظين.", "إخراج من الطابور"),
        "start_queue": ("▶ بدء الجولة", "سنفحص الملفات المنتظرة بالتتابع. يمكنك إيقاف الفحص مؤقتًا واستئنافه من الحالة؛ النتائج بعد اكتمال الجولة.", "بدء الفحص"),
    }[action]
    return Panel(f"<b>{title}</b>\n\nالملف: <b>{safe(workbook)}</b>\n\n{body}",
                 [[button(accept, f"confirm:{token}", "danger" if action == "remove" else ("success" if action in {"resume", "start_queue"} else "primary")),
                   button("رجوع", "queue:0" if action in {"remove", "start_queue"} else "status")]])
