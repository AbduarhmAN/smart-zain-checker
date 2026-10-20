"""Public check messages; raw provider diagnostics stay in journals and logs.

These are application support codes, independent of an upstream HTTP status.
"""
from __future__ import annotations

import re


def check_error_code(status: str, details: str = "") -> str:
    status = str(status or "").lower()
    text = " ".join(str(details or "").lower().split())
    public = re.fullmatch(r"خطأ نظام (40|403|404|408|500|503)", text)
    if public:
        return public.group(1)
    if status == "session_expired" or any(part in text for part in (
        "جلسة الويب", "session_expired", "session expired",
    )):
        return "40"
    if status == "not_found" or any(part in text for part in (
        "ezb5029", "رقم العقد لا يطابق", "not found", "غير مسجل في نظام زين",
    )):
        return "404"
    if status in {"blocked", "verification_required"} or any(part in text for part in (
        "رفض الطلب", "rejected the request", "http 403", "http 401",
        "cloudflare", "waf", "تحققًا بشريًا", "تحققا بشريا",
    )):
        return "403"
    if status == "timeout" or any(part in text for part in ("timeout", "timed out", "انتهت المهلة")):
        return "408"
    if status == "network_error" or any(part in text for part in (
        "connection", "network", "تعذر الاتصال", "المتصفح", "targetclosed",
    )):
        return "503"
    return "500"


def public_check_details(status: str, details: str = "") -> str:
    """Mask failed checks without changing success or customer-stop messages."""
    if status in {"match", "mismatch", "cancelled"}:
        return details
    if status == "not_found":
        return "رقم العقد لا يطابق أي سجل" if "عقد" in str(details) or "EZB5029" in str(details) else "الرقم غير موجود"
    return f"خطأ نظام {check_error_code(status, details)}"


def recoverable_check_failure(status: str, details: str = "") -> bool:
    """Recognize legacy transient failures that must be retried on resume."""
    return status in {'error', 'needs_review'} and check_error_code(status, details) in {'40', '403', '408', '503'}
