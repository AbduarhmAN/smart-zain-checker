"""Domain Financial Engine for Smart Zain Checker.
Handles exact integer Halalas math, string parsing, and tolerance comparisons.
Eliminates all floating-point rounding errors.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Optional, Tuple

TOLERANCE_HALALAS = 20  # 0.20 SAR acceptable tolerance threshold


def sanitize_amount_string(raw: Any) -> str:
    """Normalizes an amount string by stripping currencies, commas, and Arabic digits."""
    if raw is None:
        return ""
    text = str(raw).strip()
    if not text:
        return ""

    # Translate Arabic-Indic numerals (٠١٢٣٤٥٦٧٨٩) to standard ASCII
    arabic_to_ascii = str.maketrans("٠١٢٣٤٥٦٧٨٩٫", "0123456789.")
    text = text.translate(arabic_to_ascii)

    # Remove currency words
    for noise in ("ر.س", "ر.س.", "ريال", "SAR", "sar", "SR", "sr", "%"):
        text = text.replace(noise, "")

    # Remove thousands separators and extra whitespace
    text = text.replace(",", "").replace(" ", "").strip()

    # Handle accounting parentheses: (123.45) -> -123.45
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1].strip()

    return text


def parse_money_to_halalas(raw: Any) -> Optional[int]:
    """Parses any input value into integer Halalas (1 SAR = 100 Halalas).
    Returns None if the value cannot be parsed as a valid numeric amount.
    """
    if raw is None:
        return None

    if isinstance(raw, (int, float)):
        try:
            dec = Decimal(str(raw))
            return int((dec * Decimal(100)).to_integral_value())
        except (InvalidOperation, ValueError, OverflowError):
            return None

    cleaned = sanitize_amount_string(raw)
    if not cleaned:
        return None

    try:
        dec = Decimal(cleaned)
        return int((dec * Decimal(100)).to_integral_value())
    except (InvalidOperation, ValueError, OverflowError):
        return None


def halalas_to_sar_float(halalas: Optional[int]) -> float:
    """Converts integer Halalas to a float SAR representation."""
    if halalas is None:
        return 0.0
    return halalas / 100.0


def format_sar(halalas: Optional[int]) -> str:
    """Formats Halalas into a display string in SAR with 2 decimal places."""
    if halalas is None:
        return "0.00 ر.س"
    sar = halalas / 100.0
    return f"{sar:,.2f} ر.س"


def is_amount_match(
    live_halalas: int,
    expected_primary: int,
    expected_secondary: Optional[int] = None,
    tolerance: int = TOLERANCE_HALALAS,
) -> Tuple[bool, int, int]:
    """Compares the live Zain portal amount (live_halalas) against primary (and optional secondary) expected amounts.
    Returns:
        (is_match: bool, effective_expected: int, diff_halalas: int)
    """
    diff_prim = abs(live_halalas - expected_primary)
    
    if expected_secondary is not None:
        diff_sec = abs(live_halalas - expected_secondary)
        if diff_sec <= tolerance:
            # Matches secondary (e.g. مبلغ العقد) perfectly!
            return True, expected_secondary, (live_halalas - expected_secondary)
        if diff_prim <= tolerance:
            # Matches primary (e.g. متبقي سداد) perfectly!
            return True, expected_primary, (live_halalas - expected_primary)
        
        # Mismatch against both: select the closest expected amount for reporting
        effective = expected_primary if diff_prim <= diff_sec else expected_secondary
        return False, effective, (live_halalas - effective)

    # Single expected amount comparison
    is_match = diff_prim <= tolerance
    return is_match, expected_primary, (live_halalas - expected_primary)
